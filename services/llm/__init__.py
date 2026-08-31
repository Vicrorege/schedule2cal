from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.config import Settings
    from services.llm.base import LLMProvider


def create_llm_provider(settings: "Settings") -> "LLMProvider":
    from services.llm.gemini import GeminiProvider
    from services.llm.hybrid_provider import HybridLLMProvider
    from services.llm.openai_provider import OpenAIProvider

    native = settings.gemini_native_keys
    custom = settings.custom_endpoints

    if native and custom:
        return HybridLLMProvider(settings)
    if custom or settings.llm_provider == "openai":
        return OpenAIProvider(settings)
    return GeminiProvider(settings)
