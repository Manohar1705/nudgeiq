"""Choose the language-model client from environment variables.

  NUDGEIQ_LLM             unset or "mock": the offline mock (the default).
                          "groq" | "gemini" | "openrouter": that one provider.
                          "chain": try Groq, then Gemini, then OpenRouter (whichever are set up),
                          and finish with the mock, so "Generate variants" never breaks.
  GROQ_API_KEY            free key from console.groq.com
  NUDGEIQ_GROQ_MODEL      main Groq model name (you fill this in; names change often)
  NUDGEIQ_GROQ_MODEL_2    optional smaller Groq model, tried if the first one fails
  GEMINI_API_KEY          free key from aistudio.google.com
  NUDGEIQ_GEMINI_MODEL    Gemini model name
  OPENROUTER_API_KEY      key from openrouter.ai
  NUDGEIQ_OPENROUTER_MODEL  model name (ones ending in :free cost nothing)
  NUDGEIQ_LLM_TIMEOUT     seconds to wait for each answer (default 60)

A wrong setting raises LLMConfigError when the app starts. A provider that is set up but fails at
run time (rate limit, network blocked) is skipped in "chain" mode, and the variant's
`generated_by` shows who really wrote it.
"""

import os
from collections.abc import Mapping

from app.llm.base import LLMClient
from app.llm.fallback import FallbackClient
from app.llm.mock import MockLLMClient
from app.llm.openai_compat import DEFAULT_TIMEOUT, OpenAICompatClient

BASE_URLS = {
    "groq": "https://api.groq.com/openai/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "openrouter": "https://openrouter.ai/api/v1",
}
_KEY_VAR = {
    "groq": "GROQ_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}
_MODEL_VARS = {
    "groq": ("NUDGEIQ_GROQ_MODEL", "NUDGEIQ_GROQ_MODEL_2"),
    "gemini": ("NUDGEIQ_GEMINI_MODEL",),
    "openrouter": ("NUDGEIQ_OPENROUTER_MODEL",),
}
CHAIN_ORDER = ("groq", "gemini", "openrouter")
PROVIDERS = ("mock", *CHAIN_ORDER, "chain")


class LLMConfigError(ValueError):
    """The environment asks for a model client that cannot be built."""


def _get(env: Mapping[str, str], name: str) -> str:
    return (env.get(name) or "").strip()


def _timeout(env: Mapping[str, str]) -> float:
    raw = _get(env, "NUDGEIQ_LLM_TIMEOUT")
    try:
        timeout = float(raw) if raw else DEFAULT_TIMEOUT
    except ValueError:
        raise LLMConfigError(f"NUDGEIQ_LLM_TIMEOUT is {raw!r}; use a number") from None
    if timeout <= 0:
        raise LLMConfigError("NUDGEIQ_LLM_TIMEOUT must be above zero")
    return timeout


def _clients_for(provider: str, env: Mapping[str, str], timeout: float) -> list[LLMClient]:
    """Clients for one provider; empty when its key or model is not set."""
    key = _get(env, _KEY_VAR[provider])
    models = [m for m in (_get(env, v) for v in _MODEL_VARS[provider]) if m]
    if not key or not models:
        return []
    return [OpenAICompatClient(key, m, BASE_URLS[provider], timeout=timeout) for m in models]


def llm_from_env(env: Mapping[str, str] | None = None) -> LLMClient:
    env = os.environ if env is None else env
    provider = _get(env, "NUDGEIQ_LLM").lower() or "mock"
    if provider == "mock":
        return MockLLMClient()
    if provider not in PROVIDERS:
        raise LLMConfigError(f"NUDGEIQ_LLM is {provider!r}; choose one of {list(PROVIDERS)}")
    timeout = _timeout(env)

    if provider == "chain":
        clients: list[LLMClient] = []
        for p in CHAIN_ORDER:
            clients += _clients_for(p, env, timeout)
        if not clients:
            raise LLMConfigError(
                "NUDGEIQ_LLM=chain needs at least one provider set up: a key plus a model, "
                "e.g. GROQ_API_KEY and NUDGEIQ_GROQ_MODEL"
            )
        return FallbackClient([*clients, MockLLMClient()])

    if not _get(env, _KEY_VAR[provider]):
        raise LLMConfigError(f"NUDGEIQ_LLM={provider} needs {_KEY_VAR[provider]} to be set")
    clients = _clients_for(provider, env, timeout)
    if not clients:
        raise LLMConfigError(f"NUDGEIQ_LLM={provider} needs {_MODEL_VARS[provider][0]} to be set")
    return clients[0] if len(clients) == 1 else FallbackClient(clients)
