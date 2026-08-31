import re
from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    # Один ключ или несколько через запятую / перевод строки
    llm_api_key: str = ""
    # Доп. пул (удобно не смешивать с одиночным LLM_API_KEY)
    llm_api_keys: str = Field(
        default="",
        validation_alias=AliasChoices("LLM_API_KEYS", "GEMINI_API_KEYS"),
    )
    llm_model: str = ""

    database_path: str = "data/schedule2cal.db"

    @model_validator(mode="after")
    def _require_api_keys(self) -> "Settings":
        if not self.api_keys:
            raise ValueError("Задай LLM_API_KEY и/или LLM_API_KEYS")
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
    def api_keys(self) -> list[str]:
        return self._split_keys(self.llm_api_key, self.llm_api_keys)

    @property
    def gemini_api_keys(self) -> list[str]:
        return self.api_keys

    @property
    def primary_api_key(self) -> str:
        return self.api_keys[0]

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
