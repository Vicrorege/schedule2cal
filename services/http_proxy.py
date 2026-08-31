import httpx
from google.genai import types


def gemini_http_options(proxy: str) -> types.HttpOptions:
    return types.HttpOptions(
        client_args={"transport": httpx.HTTPTransport(proxy=proxy)},
        async_client_args={"transport": httpx.AsyncHTTPTransport(proxy=proxy)},
    )


def openai_http_client(proxy: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(proxy=proxy, timeout=120.0)
