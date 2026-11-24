"""Real language-model client for any OpenAI-style `/chat/completions` API.

Groq, Google AI Studio (Gemini) and OpenRouter all speak this format, so one small client covers
them. It uses httpx (already in requirements.txt). One blocking request per call. The key travels
only in the Authorization header and is never put in an error message, a repr or a log line.
Every failure is turned into an LLMError, so the caller never sees an httpx exception.
"""

from typing import Any

import httpx

from app.llm.base import LLMError, LLMReplyError, LLMRequest

DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_TOKENS = 2000
_DETAIL_CHARS = 300


class OpenAICompatClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        transport: httpx.BaseTransport | None = None,  # tests pass a fake one
    ) -> None:
        if not api_key.strip():
            raise ValueError("an API key is required")
        if not model.strip():
            raise ValueError("a model name is required")
        self._api_key = api_key.strip()
        self.name = model.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_tokens = max_tokens
        self._transport = transport

    def __repr__(self) -> str:  # never show the key
        return f"OpenAICompatClient(model={self.name!r}, base_url={self.base_url!r})"

    def complete(self, request: LLMRequest) -> str:
        body = {
            "model": self.name,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.prompt},
            ],
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            with httpx.Client(transport=self._transport, timeout=self.timeout) as http:
                response = http.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
        except httpx.TimeoutException:
            raise LLMError(f"the model did not answer within {self.timeout:g} seconds") from None
        except httpx.HTTPError as exc:
            raise LLMError(f"could not reach the model ({type(exc).__name__})") from None

        if response.status_code != 200:
            raise LLMError(f"the API answered {response.status_code}: {_error_detail(response)}")
        try:
            data = response.json()
        except ValueError:
            raise LLMError("the API's answer was not JSON") from None
        return _text_of(data)


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
        error = payload.get("error") if isinstance(payload, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
    except (ValueError, AttributeError):
        message = None
    text = message if isinstance(message, str) and message else response.text.strip()
    return (text or "no details")[:_DETAIL_CHARS]


def _text_of(data: Any) -> str:
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise LLMReplyError("the API's answer had no choices")
    choice = choices[0]
    message = choice.get("message")
    text = message.get("content") if isinstance(message, dict) else None
    if choice.get("finish_reason") == "length":
        raise LLMReplyError("the model's reply was cut off before it finished")
    if not isinstance(text, str) or not text.strip():
        raise LLMReplyError("the model's reply had no text")
    return text
