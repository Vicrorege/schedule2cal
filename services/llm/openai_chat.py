from __future__ import annotations

import base64
import logging

from openai import APIStatusError, AsyncOpenAI

from services.image_processor import fit_for_llm

logger = logging.getLogger(__name__)


async def chat_with_image(
    client: AsyncOpenAI,
    model: str,
    *,
    prompt: str,
    image_bytes: bytes,
    response_format: dict,
    json_object_hint: bool = True,
) -> str:
    """OpenAI-compatible vision chat; при 413 сжимает картинку и повторяет."""
    payload = image_bytes
    full_prompt = prompt
    if json_object_hint:
        full_prompt += "\n\nОтветь ТОЛЬКО валидным JSON-объектом, без markdown и пояснений."

    last_exc: APIStatusError | None = None
    for attempt in range(3):
        b64 = base64.standard_b64encode(payload).decode()
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                            },
                            {"type": "text", "text": full_prompt},
                        ],
                    }
                ],
                response_format=response_format,
                temperature=0.1,
            )
            return response.choices[0].message.content or ""
        except APIStatusError as exc:
            last_exc = exc
            if exc.status_code == 413 and attempt < 2:
                prev = len(payload)
                payload = fit_for_llm(payload, max_bytes=max(180_000, len(payload) // 2))
                logger.warning(
                    "413 Request Entity Too Large на %s — сжимаю %d → %d байт (попытка %d)",
                    model,
                    prev,
                    len(payload),
                    attempt + 2,
                )
                continue
            raise

    raise last_exc  # type: ignore[misc]


async def chat_with_text(
    client: AsyncOpenAI,
    model: str,
    *,
    system_prompt: str,
    user_prompt: str,
    response_format: dict,
    json_object_hint: bool = True,
) -> str:
    """OpenAI-compatible text chat с отдельным system-промптом."""
    system = system_prompt
    if json_object_hint:
        system += "\n\nОтветь ТОЛЬКО валидным JSON-объектом, без markdown и пояснений."

    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_prompt},
        ],
        response_format=response_format,
        temperature=0.0,
    )
    return response.choices[0].message.content or ""
