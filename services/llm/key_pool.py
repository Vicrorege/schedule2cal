from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from google import genai
from google.genai import errors as genai_errors

from services.http_proxy import gemini_http_options

logger = logging.getLogger(__name__)

T = TypeVar("T")

_RETRY_IN_RE = re.compile(r"retry in\s+([\d.]+)\s*s", re.IGNORECASE)
_DAILY_MARKERS = (
    "perday",
    "free_tier",
    "freetier",
    "generaterequestsperday",
)


def _mask_key(key: str) -> str:
    if len(key) <= 8:
        return "***"
    return f"{key[:4]}…{key[-4:]}"


def cooldown_seconds_from_error(exc: BaseException) -> float:
    """Секунды до следующей попытки с этим ключом."""
    text = str(exc)
    lower = text.casefold()

    # Дневная free-квота — не дёргаем ключ до следующего UTC-дня
    if any(m in lower.replace("-", "").replace("_", "") for m in _DAILY_MARKERS) or (
        "free tier" in lower and "per day" in lower
    ):
        now = time.time()
        # до полуночи UTC + небольшой запас
        day = 24 * 3600
        until_midnight = day - (now % day)
        return max(until_midnight, 3600.0)

    match = _RETRY_IN_RE.search(text)
    if match:
        return max(float(match.group(1)), 5.0)

    details = getattr(exc, "details", None) or getattr(exc, "response_json", None)
    if isinstance(details, dict):
        err = details.get("error") or details
        for item in err.get("details") or []:
            if not isinstance(item, dict):
                continue
            delay = item.get("retryDelay")
            if isinstance(delay, str) and delay.endswith("s"):
                try:
                    return max(float(delay[:-1]), 5.0)
                except ValueError:
                    pass

    return 60.0


def is_quota_error(exc: BaseException) -> bool:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code == 429:
        return True
    name = type(exc).__name__
    if name in {"ClientError", "ServerError"} and "RESOURCE_EXHAUSTED" in str(exc):
        return True
    if isinstance(exc, genai_errors.APIError) and getattr(exc, "code", None) == 429:
        return True
    text = str(exc).upper()
    return "RESOURCE_EXHAUSTED" in text or ("QUOTA" in text and "EXCEED" in text)


class GeminiKeyPool:
    """Round-robin по ключам Gemini; при 429 — cooldown и следующий ключ."""

    def __init__(self, api_keys: list[str], *, proxy: str = ""):
        if not api_keys:
            raise ValueError("Нужен хотя бы один Gemini API key")

        self._keys = list(api_keys)
        self._clients: list[genai.Client] = []
        self._cooldown_until: list[float] = [0.0] * len(self._keys)
        self._rr = 0
        self._lock = asyncio.Lock()

        for key in self._keys:
            kwargs: dict = {"api_key": key}
            if proxy:
                kwargs["http_options"] = gemini_http_options(proxy)
            self._clients.append(genai.Client(**kwargs))

        logger.info(
            "Gemini key pool: %d ключ(ей) [%s]",
            len(self._keys),
            ", ".join(_mask_key(k) for k in self._keys),
        )

    @property
    def size(self) -> int:
        return len(self._keys)

    async def _pick(self, tried: set[int]) -> int | None:
        now = time.time()
        async with self._lock:
            n = len(self._keys)
            for offset in range(n):
                idx = (self._rr + offset) % n
                if idx in tried:
                    continue
                if self._cooldown_until[idx] > now:
                    continue
                self._rr = (idx + 1) % n
                return idx
            # Все в cooldown / уже пробовали — взять с ближайшим окончанием cooldown
            candidates = [i for i in range(n) if i not in tried]
            if not candidates:
                return None
            return min(candidates, key=lambda i: self._cooldown_until[i])

    async def _mark_cooldown(self, idx: int, seconds: float) -> None:
        until = time.time() + seconds
        async with self._lock:
            self._cooldown_until[idx] = max(self._cooldown_until[idx], until)
        logger.warning(
            "Gemini key #%d (%s) на cooldown %.0f с",
            idx + 1,
            _mask_key(self._keys[idx]),
            seconds,
        )

    async def run(self, call: Callable[[genai.Client], Awaitable[T]]) -> T:
        tried: set[int] = set()
        last_exc: BaseException | None = None

        while len(tried) < len(self._keys):
            idx = await self._pick(tried)
            if idx is None:
                break
            tried.add(idx)
            try:
                return await call(self._clients[idx])
            except Exception as exc:
                last_exc = exc
                if is_quota_error(exc):
                    await self._mark_cooldown(idx, cooldown_seconds_from_error(exc))
                    continue
                raise

        raise RuntimeError(
            f"Все Gemini API keys исчерпали квоту ({len(self._keys)} шт.). "
            "Добавь ключи из других проектов в LLM_API_KEY / LLM_API_KEYS "
            "или подожди сброса free tier."
        ) from last_exc
