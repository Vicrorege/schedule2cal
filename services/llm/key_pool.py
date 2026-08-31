from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import TypeVar

from google import genai
from google.genai import errors as genai_errors
from openai import AsyncOpenAI

from services.http_proxy import gemini_http_options, openai_http_client
from services.llm.endpoints import LLMEndpoint

logger = logging.getLogger(__name__)

T = TypeVar("T")

LLM_REQUEST_TIMEOUT_SEC = 30
LLM_TIMEOUT_COOLDOWN_SEC = 30

_RETRY_IN_RE = re.compile(r"retry in\s+([\d.]+)\s*s", re.IGNORECASE)
_DAILY_MARKERS = (
    "perday",
    "per day",
    "free_tier",
    "freetier",
    "generaterequestsperday",
    "daily",
)
_MINUTE_MARKERS = (
    "perminute",
    "per minute",
    "requestsperminute",
    "rpm",
)


def _mask_key(key: str) -> str:
    if len(key) <= 8:
        return "***"
    return f"{key[:4]}…{key[-4:]}"


def _seconds_until_utc_midnight() -> float:
    now = datetime.now(timezone.utc)
    tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if now.hour or now.minute or now.second or now.microsecond:
        from datetime import timedelta

        tomorrow = tomorrow + timedelta(days=1)
    return max((tomorrow - now).total_seconds(), 60.0)


def cooldown_seconds_from_error(exc: BaseException) -> float:
    """Сколько секунд ключ считается убитым после quota/rate-limit ошибки."""
    text = str(exc)
    lower = text.casefold()
    normalized = lower.replace("-", "").replace("_", "").replace(" ", "")

    # Дневная квота → до полуночи UTC
    if any(m.replace(" ", "") in normalized for m in _DAILY_MARKERS):
        return _seconds_until_utc_midnight()

    if "free tier" in lower and ("day" in lower or "daily" in lower):
        return _seconds_until_utc_midnight()

    if "quota exceeded" in lower and "perday" in normalized:
        return _seconds_until_utc_midnight()

    # Минутная квота
    if any(m.replace(" ", "") in normalized for m in _MINUTE_MARKERS):
        match = _RETRY_IN_RE.search(text)
        if match:
            return max(float(match.group(1)), 30.0)
        return 60.0

    match = _RETRY_IN_RE.search(text)
    if match:
        return max(float(match.group(1)), 5.0)

    response = getattr(exc, "response", None)
    if response is not None:
        hdrs = getattr(response, "headers", None) or {}
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
            quota_id = str(item.get("quotaId", "")).casefold()
            if "perday" in quota_id or "daily" in quota_id:
                return _seconds_until_utc_midnight()

    # 429 без деталей — безопасно отложить на минуту
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if code == 429:
        return 60.0

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
    return (
        "RESOURCE_EXHAUSTED" in text
        or ("QUOTA" in text and "EXCEED" in text)
        or "RATE_LIMIT" in text
        or "TOO MANY REQUESTS" in text
    )


def is_failover_error(exc: BaseException) -> bool:
    """Ошибки, при которых стоит переключиться на другой слот."""
    if is_quota_error(exc):
        return True
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if code in {502, 503, 504}:
        return True
    text = str(exc).upper()
    return "502 BAD GATEWAY" in text or "503 SERVICE" in text or "504 GATEWAY" in text


class KeyCooldownRegistry:
    """Глобальный реестр убитых ключей (по api_key, не по слоту)."""

    def __init__(self) -> None:
        self._until: dict[str, float] = {}
        self._lock = asyncio.Lock()

    def is_alive(self, key: str) -> bool:
        return self._until.get(key, 0.0) <= time.time()

    def seconds_left(self, key: str) -> float:
        return max(self._until.get(key, 0.0) - time.time(), 0.0)

    async def mark_dead(self, key: str, seconds: float, *, reason: str = "") -> None:
        until = time.time() + seconds
        async with self._lock:
            prev = self._until.get(key, 0.0)
            self._until[key] = max(prev, until)
            final_until = self._until[key]
        left = final_until - time.time()
        when = datetime.fromtimestamp(final_until, tz=timezone.utc).strftime("%H:%M UTC")
        extra = f" ({reason})" if reason else ""
        logger.warning(
            "Ключ %s убит до %s (%.0f с)%s",
            _mask_key(key),
            when,
            left,
            extra,
        )

    async def alive_count(self, keys: list[str]) -> int:
        now = time.time()
        return sum(1 for k in keys if self._until.get(k, 0.0) <= now)


class _EndpointPool:
    def __init__(
        self,
        labels: list[str],
        key_ids: list[str],
        *,
        registry: KeyCooldownRegistry | None = None,
    ):
        if not labels:
            raise ValueError("Пул пуст")
        if len(labels) != len(key_ids):
            raise ValueError("labels и key_ids должны совпадать по длине")
        self._labels = labels
        self._key_ids = key_ids
        self._registry = registry or KeyCooldownRegistry()
        self._rr = 0
        self._lock = asyncio.Lock()

    @property
    def registry(self) -> KeyCooldownRegistry:
        return self._registry

    @property
    def size(self) -> int:
        return len(self._labels)

    def _slot_alive(self, idx: int) -> bool:
        return self._registry.is_alive(self._key_ids[idx])

    async def _pick(self, tried: set[int]) -> int | None:
        async with self._lock:
            n = len(self._labels)
            for offset in range(n):
                idx = (self._rr + offset) % n
                if idx in tried:
                    continue
                if not self._slot_alive(idx):
                    continue
                self._rr = (idx + 1) % n
                return idx
        return None

    async def _mark_dead(self, idx: int, seconds: float, *, reason: str = "") -> None:
        key = self._key_ids[idx]
        label = self._labels[idx]
        await self._registry.mark_dead(key, seconds, reason=f"{label}: {reason}".strip(": "))

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
            endpoint = self._labels[idx]
            logger.info("LLM запрос → %s", endpoint)
            try:
                return await asyncio.wait_for(call(idx), timeout=LLM_REQUEST_TIMEOUT_SEC)
            except TimeoutError as exc:
                last_exc = exc
                await self._mark_dead(idx, LLM_TIMEOUT_COOLDOWN_SEC, reason=f"timeout @ {endpoint}")
                logger.warning(
                    "Таймаут %s с на API [%s] — переключаюсь (живых ключей: %s)",
                    LLM_REQUEST_TIMEOUT_SEC,
                    endpoint,
                    await self._registry.alive_count(list(dict.fromkeys(self._key_ids))),
                )
                continue
            except Exception as exc:
                last_exc = exc
                if is_failover_error(exc):
                    cooldown = cooldown_seconds_from_error(exc)
                    if not is_quota_error(exc):
                        cooldown = min(cooldown, 120.0)
                    await self._mark_dead(idx, cooldown, reason=f"{type(exc).__name__} @ {endpoint}")
                    logger.info(
                        "Ошибка на API [%s] — переключаюсь (живых ключей: %s)",
                        endpoint,
                        await self._registry.alive_count(list(dict.fromkeys(self._key_ids))),
                    )
                    continue
                raise

        unique_keys = list(dict.fromkeys(self._key_ids))
        alive = await self._registry.alive_count(unique_keys)
        msg = exhausted_message
        if alive == 0 and unique_keys:
            msg += f" Следующий сброс ближайшего ключа через ~{max(self._registry.seconds_left(k) for k in unique_keys):.0f} с."
        raise RuntimeError(msg) from last_exc


class GeminiKeyPool(_EndpointPool):
    def __init__(
        self,
        api_keys: list[str],
        *,
        proxy: str = "",
        registry: KeyCooldownRegistry | None = None,
        model: str = "gemini",
    ):
        if not api_keys:
            raise ValueError("Нужен хотя бы один Gemini API key")
        labels = [
            f"generativelanguage.googleapis.com | {model} | {_mask_key(k)}" for k in api_keys
        ]
        super().__init__(labels, api_keys, registry=registry)
        self._clients: list[genai.Client] = []
        for key in api_keys:
            kwargs: dict = {"api_key": key}
            if proxy:
                kwargs["http_options"] = gemini_http_options(proxy)
            self._clients.append(genai.Client(**kwargs))
        logger.info("Gemini key pool: %d [%s]", len(api_keys), ", ".join(labels))

    async def call_at(self, idx: int, call: Callable[[genai.Client], Awaitable[T]]) -> T:
        if not self._slot_alive(idx):
            raise RuntimeError(f"Ключ {self._labels[idx]} в cooldown")
        return await call(self._clients[idx])

    async def run(self, call: Callable[[genai.Client], Awaitable[T]]) -> T:
        async def _inner(idx: int) -> T:
            return await call(self._clients[idx])

        return await self.run_with(
            _inner,
            exhausted_message=(
                f"Все Gemini API keys исчерпали квоту ({len(dict.fromkeys(self._key_ids))} уник.). "
                "Добавь ключи / LLM_POOL или подожди сброса."
            ),
        )


class OpenAIEndpointPool(_EndpointPool):
    """Пул OpenAI-compatible эндпоинтов (свой base_url/model на каждый слот)."""

    def __init__(
        self,
        endpoints: list[LLMEndpoint],
        *,
        proxy: str = "",
        default_model: str,
        registry: KeyCooldownRegistry | None = None,
    ):
        if not endpoints:
            raise ValueError("Нужен хотя бы один OpenAI-compatible endpoint")
        self._endpoints = endpoints
        self._default_model = default_model
        labels = [ep.label() for ep in endpoints]
        key_ids = [ep.api_key for ep in endpoints]
        super().__init__(labels, key_ids, registry=registry)
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

    async def call_at(
        self, idx: int, call: Callable[[AsyncOpenAI, str], Awaitable[T]]
    ) -> T:
        if not self._slot_alive(idx):
            raise RuntimeError(f"Слот {self._labels[idx]} в cooldown")
        return await call(self._clients[idx], self.model_for(idx))

    async def run(self, call: Callable[[AsyncOpenAI, str], Awaitable[T]]) -> T:
        async def _inner(idx: int) -> T:
            return await call(self._clients[idx], self.model_for(idx))

        return await self.run_with(
            _inner,
            exhausted_message=(
                f"Все LLM endpoints исчерпали квоту ({len(dict.fromkeys(self._key_ids))} уник. ключей). "
                "Добавь слоты в LLM_POOL или подожди."
            ),
        )
