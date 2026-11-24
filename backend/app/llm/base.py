"""What the variant generator needs from a language model, and nothing more.

A client turns one request into the model's raw text reply. Parsing, cleaning and validating
that reply is the generator's job, because a model's output is never trusted.
"""

from dataclasses import dataclass, field
from typing import Protocol

from app.models import Channel


class LLMError(Exception):
    """The model could not be reached, refused, or returned nothing usable."""


class LLMReplyError(LLMError):
    """The model answered, but not in the shape the generator asked for."""


@dataclass(frozen=True)
class DraftBrief:
    """The structured part of a request: what is being drafted and what to steer clear of."""

    template_id: str
    channel: Channel
    body: str  # the template's body, the message the variants are alternatives to
    count: int
    principles: list[str] = field(default_factory=list)  # persuasion principles to use
    tones: list[str] = field(default_factory=list)
    avoid: list[str] = field(default_factory=list)  # texts that already exist for this template


@dataclass(frozen=True)
class LLMRequest:
    """A real model reads `system` and `prompt`. The mock reads `brief` and ignores the text."""

    system: str
    prompt: str
    brief: DraftBrief


class LLMClient(Protocol):
    name: str  # recorded as `generated_by` on every variant it writes

    def complete(self, request: LLMRequest) -> str:
        """Return the model's raw reply. Raise LLMError if there is none."""
        ...
