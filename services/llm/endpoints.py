from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LLMEndpoint:
    """Один слот в пуле: ключ + опционально OpenAI-compatible base_url и model."""

    api_key: str
    base_url: str | None = None
    model: str | None = None

    @property
    def is_openai_compatible(self) -> bool:
        return bool(self.base_url)

    def label(self) -> str:
        key = self.api_key
        masked = f"{key[:4]}…{key[-4:]}" if len(key) > 8 else "***"
        parts = [masked]
        if self.base_url:
            parts.append(self.base_url.rstrip("/"))
        if self.model:
            parts.append(self.model)
        return " | ".join(parts)


def parse_endpoint_line(
    line: str,
    *,
    default_base_url: str | None = None,
    default_model: str | None = None,
) -> LLMEndpoint | None:
    """
    Форматы строки:
      key
      key|base_url
      key|base_url|model
      key||model          (базовый URL из default)
    """
    raw = line.strip()
    if not raw or raw.startswith("#"):
        return None

    parts = [p.strip() for p in raw.split("|")]
    if not parts or not parts[0]:
        return None

    api_key = parts[0]
    base_url = default_base_url
    model = default_model

    if len(parts) >= 2 and parts[1]:
        base_url = parts[1].rstrip("/")
    if len(parts) >= 3 and parts[2]:
        model = parts[2]

    return LLMEndpoint(api_key=api_key, base_url=base_url or None, model=model or None)


def parse_endpoint_pool(
    raw: str,
    *,
    default_base_url: str | None = None,
    default_model: str | None = None,
) -> list[LLMEndpoint]:
    if not raw.strip():
        return []
    out: list[LLMEndpoint] = []
    for chunk in raw.replace("\r\n", "\n").replace(";", "\n").split("\n"):
        ep = parse_endpoint_line(
            chunk,
            default_base_url=default_base_url,
            default_model=default_model,
        )
        if ep:
            out.append(ep)
    return out
