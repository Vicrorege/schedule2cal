from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from google import genai
from google.genai import errors as genai_errors
from openai import AsyncOpenAI

from services.http_proxy import gemini_http_options, openai_http_client
from services.llm.endpoints import LLMEndpoint

logger = logging.getLogger(__name__)

T = TypeVar("T")
ClientT = TypeVar("ClientT")

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
    text = str(exc)
    lower = text.casefold()
    normalized = lower.replace("-", "").replace("_", "").replace(" ", "")

    if any(m in normalized for m in _DAILY_MARKERS) or (
        "free tier" in lower and "per day" in lower
    ):
        now = time.time()
        day = 24 * 3600
        until_midnight = day - (now % day)
        return max(until_midnight, 3600.0)

    match = _RETRY_IN_RE.search(text)
    if match:
        return max(float(match.group(1)), 5.0)

    headers = getattr(exc, "response", None)
    if headers is not None:
        hdrs = getattr(headers, "headers", None) or {}
        retry_after = hdrs.get("retry-after") or hdrs.get("Retry-After")
        if retry_after:
            try:
                return max(float(retry_after), 5.0)
            except ValueError:
                pass

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
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if code == 429:
        return True
    name = type(exc).__name__
    if name in {"RateLimitError", "ClientError", "ServerError"} and (
        "429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc).upper() or "rate" in str(exc).lower()
    ):
        return True
    if isinstance(exc, genai_errors.APIError) and getattr(exc, "code", None) == 429:
        return True
    text = str(exc).upper()
    return "RESOURCE_EXHAUSTED" in text or ("QUOTA" in text and "EXCEED" in text)


class _EndpointPool:
    def __init__(self, labels: list[str]):
        if not labels:
            raise ValueError("Пул пуст")
        self._labels = labels
        self._cooldown_until = [0.0] * len(labels)
        self._rr = 0
        self._lock = asyncio.Lock()

    @property
    def size(self) -> int:
        return len(self._labels)

    async def _pick(self, tried: set[int]) -> int | None:
        now = time.time()
        async with self._lock:
            n = len(self._labels)
            for offset in range(n):
                idx = (self._rr + offset) % n
                if idx in tried:
                    continue
                if self._cooldown_until[idx] > now:
                    continue
                self._rr = (idx + 1) % n
                return idx
            candidates = [i for i in range(n) if i not in tried]
            if not candidates:
                return None
            return min(candidates, key=lambda i: self._cooldown_until[i])

    async def _mark_cooldown(self, idx: int, seconds: float) -> None:
        until = time.time() + seconds
        async with self._lock:
            self._cooldown_until[idx] = max(self._cooldown_until[idx], until)
        logger.warning("LLM endpoint #%d (%s) cooldown %.0fs", idx + 1, self._labels[idx], seconds)

    async def run_with(
        self,
        call: Callable[[int], Awaitable[T]],
        *,
        exhausted_message: str,
    ) -> T:
        tried: set[int] = set()
        last_exc: BaseException | None = None
        while len(tried) < self.size:
            idx = await self._pick(tried)
            if idx is None:
                break
            tried.add(idx)
            try:
                return await call(idx)
            except Exception as exc:
                last_exc = exc
                if is_quota_error(exc):
                    await self._mark_cooldown(idx, cooldown_seconds_from_error(exc))
                    continue
                raise
        raise RuntimeError(exhausted_message) from last_exc


class GeminiKeyPool(_EndpointPool):
    def __init__(self, api_keys: list[str], *, proxy: str = ""):
        if not api_keys:
            raise ValueError("Нужен хотя бы один Gemini API key")
        labels = [_mask_key(k) for k in api_keys]
        super().__init__(labels)
        self._clients: list[genai.Client] = []
        for key in api_keys:
            kwargs: dict = {"api_key": key}
            if proxy:
                kwargs["http_options"] = gemini_http_options(proxy)
            self._clients.append(genai.Client(**kwargs))
        logger.info("Gemini key pool: %d [%s]", len(api_keys), ", ".join(labels))

    async def run(self, call: Callable[[genai.Client], Awaitable[T]]) -> T:
        async def _inner(idx: int) -> T:
            return await call(self._clients[idx])

        return await self.run_with(
            _inner,
            exhausted_message=(
                f"Все Gemini API keys исчерпали квоту ({self.size} шт.). "
                "Добавь ключи / LLM_POOL или подожди сброса."
            ),
        )


class OpenAIEndpointPool(_EndpointPool):
    """Пул OpenAI-compatible эндпоинтов (свой base_url/model на каждый слот)."""

    def __init__(self, endpoints: list[LLMEndpoint], *, proxy: str = "", default_model: str):
        if not endpoints:
            raise ValueError("Нужен хотя бы один OpenAI-compatible endpoint")
        self._endpoints = endpoints
        self._default_model = default_model
        labels = [ep.label() for ep in endpoints]
        super().__init__(labels)
        self._clients: list[AsyncOpenAI] = []
        for ep in endpoints:
            kwargs: dict = {"api_key": ep.api_key}
            if ep.base_url:
                kwargs["base_url"] = ep.base_url
            if proxy:
                kwargs["http_client"] = openai_http_client(proxy)
            self._clients.append(AsyncOpenAI(**kwargs))
        logger.info("OpenAI endpoint pool: %d [%s]", len(endpoints), "; ".join(labels))

    def model_for(self, idx: int) -> str:
        return self._endpoints[idx].model or self._default_model

    async def run(self, call: Callable[[AsyncOpenAI, str], Awaitable[T]]) -> T:
        async def _inner(idx: int) -> T:
            return await call(self._clients[idx], self.model_for(idx))

        return await self.run_with(
            _inner,
            exhausted_message=(
                f"Все LLM endpoints исчерпали квоту ({self.size} шт.). "
                "Добавь слоты в LLM_POOL или подожди."
            ),
        )
