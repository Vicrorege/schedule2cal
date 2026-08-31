import re
from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from services.llm.endpoints import LLMEndpoint, parse_endpoint_pool


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bot_token: str
    allowed_users: str = ""
    proxy: str = Field(
        default="",
        validation_alias=AliasChoices("PROXY", "TELEGRAM_PROXY"),
    )

    llm_provider: Literal["gemini", "openai"] = "gemini"
    # Нативные Gemini-ключи (обычный Google endpoint)
    llm_api_key: str = ""
    llm_api_keys: str = Field(
        default="",
        validation_alias=AliasChoices("LLM_API_KEYS", "GEMINI_API_KEYS"),
    )
    llm_base_url: str = Field(
        default="",
        validation_alias=AliasChoices("LLM_BASE_URL", "OPENAI_BASE_URL"),
    )
    llm_model: str = ""
    # Кастомные слоты: key|base_url|model
    llm_pool: str = Field(
        default="",
        validation_alias=AliasChoices("LLM_POOL", "LLM_ENDPOINTS"),
    )

    database_path: str = "data/schedule2cal.db"

    @model_validator(mode="after")
    def _require_endpoints(self) -> "Settings":
        if not self.gemini_native_keys and not self.custom_endpoints:
            raise ValueError(
                "Задай LLM_API_KEY (нативный Gemini) и/или LLM_POOL (key|url|model)"
            )
        return self

    @staticmethod
    def _split_keys(*chunks: str) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for chunk in chunks:
            if not chunk.strip():
                continue
            for part in re.split(r"[,;\n]+", chunk):
                key = part.strip()
                if key and key not in seen:
                    seen.add(key)
                    out.append(key)
        return out

    @property
    def gemini_native_keys(self) -> list[str]:
        return self._split_keys(self.llm_api_key, self.llm_api_keys)

    @property
    def custom_endpoints(self) -> list[LLMEndpoint]:
        default_base = self.llm_base_url.strip().rstrip("/") or None
        default_model = self.llm_model.strip() or None
        pooled = parse_endpoint_pool(
            self.llm_pool,
            default_base_url=default_base,
            default_model=default_model,
        )
        custom = [ep for ep in pooled if ep.base_url]
        if custom:
            return custom
        # openai-only режим: ключи + общий base_url
        if self.llm_provider == "openai" and default_base and self.gemini_native_keys:
            return [
                LLMEndpoint(api_key=key, base_url=default_base, model=default_model)
                for key in self.gemini_native_keys
            ]
        return []

    @property
    def llm_endpoints(self) -> list[LLMEndpoint]:
        native = [
            LLMEndpoint(api_key=key, model=self.llm_model or None)
            for key in self.gemini_native_keys
        ]
        return self.custom_endpoints + native

    @property
    def openai_endpoints(self) -> list[LLMEndpoint]:
        return self.custom_endpoints

    @property
    def primary_api_key(self) -> str:
        if self.gemini_native_keys:
            return self.gemini_native_keys[0]
        return self.custom_endpoints[0].api_key

    @property
    def allowed_user_ids(self) -> set[int]:
        if not self.allowed_users.strip():
            return set()
        return {int(uid.strip()) for uid in self.allowed_users.split(",") if uid.strip()}

    @property
    def gemini_model(self) -> str:
        return self.llm_model or "gemini-3.6-flash"

    @property
    def openai_model(self) -> str:
        return self.llm_model or "gpt-4o-mini"


@lru_cache
def get_settings() -> Settings:
    return Settings()
