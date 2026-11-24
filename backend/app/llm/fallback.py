"""Try several clients in order; the first one that answers wins.

`name` is the model that wrote the last reply, so `generated_by` on each variant tells the truth
about which model (or the mock) produced it.
"""

from collections.abc import Sequence

from app.llm.base import LLMClient, LLMError, LLMRequest


class FallbackClient:
    def __init__(self, clients: Sequence[LLMClient]) -> None:
        if not clients:
            raise ValueError("at least one client is required")
        self._clients = list(clients)
        self.name = self._clients[0].name

    def __repr__(self) -> str:
        return f"FallbackClient({[c.name for c in self._clients]!r})"

    def complete(self, request: LLMRequest) -> str:
        failures: list[str] = []
        for client in self._clients:
            try:
                text = client.complete(request)
            except LLMError as exc:
                failures.append(f"{client.name}: {exc}")
                continue
            self.name = client.name
            return text
        raise LLMError("every model failed: " + "; ".join(failures))
