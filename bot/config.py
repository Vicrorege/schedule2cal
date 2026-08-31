from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field
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
    llm_api_key: str
    llm_model: str = ""

    semester_end_date: str = "2027-05-31"
    database_path: str = "data/schedule2cal.db"

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
