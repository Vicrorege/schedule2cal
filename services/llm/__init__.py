from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.config import Settings
    from services.llm.base import LLMProvider


def create_llm_provider(settings: "Settings") -> "LLMProvider":
    from services.llm.gemini import GeminiProvider
    from services.llm.openai_provider import OpenAIProvider

    if settings.llm_provider == "openai" or any(ep.base_url for ep in settings.llm_endpoints):
        return OpenAIProvider(settings)
    return GeminiProvider(settings)
