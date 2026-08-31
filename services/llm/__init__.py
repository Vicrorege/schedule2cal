from bot.config import Settings
from services.llm.base import LLMProvider
from services.llm.gemini import GeminiProvider
from services.llm.openai_provider import OpenAIProvider


def create_llm_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "openai":
        return OpenAIProvider(settings)
    return GeminiProvider(settings)
