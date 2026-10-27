import json
import socket
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .base import (
    LLMProvider,
    PermanentProviderError,
    ProviderRateLimited,
    ProviderResponse,
    ProviderTimeout,
    TransientProviderError,
)


class OpenAICompatibleProvider(LLMProvider):
    def __init__(self, *, api_key, base_url):
        if not api_key:
            raise PermanentProviderError("LLM_API_KEY is not configured")
        if not base_url:
            raise PermanentProviderError("LLM_BASE_URL is not configured")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def analyze_structured(
        self, *, model, system_prompt, user_prompt, schema, timeout, max_output_tokens
    ):
        if not model:
            raise PermanentProviderError("LLM_MODEL is not configured")
        payload = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_output_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema["title"], "strict": True, "schema": schema},
            },
        }).encode("utf-8")
        request = Request(
            f"{self.base_url}/chat/completions",
            data=payload,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                raw_body = response.read().decode("utf-8")
        except HTTPError as exc:
            if exc.code == 429:
                raise ProviderRateLimited("Provider rate limit") from exc
            if exc.code in {500, 502, 503, 504}:
                raise TransientProviderError(f"Provider HTTP {exc.code}") from exc
            raise PermanentProviderError(f"Provider HTTP {exc.code}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise ProviderTimeout("Provider request timed out") from exc
        except URLError as exc:
            raise TransientProviderError("Provider network error") from exc
        try:
            body = json.loads(raw_body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise PermanentProviderError("Provider response is not valid JSON") from exc
        try:
            content = body["choices"][0]["message"]["content"]
            usage = body.get("usage", {})
            return ProviderResponse(
                content=content,
                input_tokens=int(usage.get("prompt_tokens", 0)),
                output_tokens=int(usage.get("completion_tokens", 0)),
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise PermanentProviderError("Provider response envelope is invalid") from exc
