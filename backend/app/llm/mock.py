"""An offline stand-in for a language model. Deterministic, needs no key and no network.

It writes each variant by adding one short closing line to the template body, so the output
always uses only the template's own variables and keeps its SMS opt-out. It skips wordings
that already exist, so asking again gives new ones until the pool runs out.
"""

import json
import re

from app.llm.base import LLMRequest

# principle -> (tone it is usually written in, closing lines)
_POOL: dict[str, tuple[str, list[str]]] = {
    "social_proof": (
        "friendly",
        [
            "Plenty of shoppers picked this one.",
            "It has been a popular choice this week.",
            "Others who looked at it came back for it too.",
        ],
    ),
    "urgency": (
        "urgent",
        [
            "Do not wait too long.",
            "Stock can change quickly.",
            "Finish today so you do not miss out.",
        ],
    ),
    "commitment": (
        "warm",
        [
            "You already did the hard part.",
            "You were so close to finishing.",
            "Pick up right where you left off.",
        ],
    ),
    "reciprocity": (
        "generous",
        [
            "We saved it for you.",
            "We kept your cart safe, just for you.",
            "A small thank you for stopping by.",
        ],
    ),
    "authority": (
        "authoritative",
        [
            "Our team recommends it.",
            "Rated highly by our customers.",
            "A pick our staff stand behind.",
        ],
    ),
}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


class MockLLMClient:
    name = "mock"

    def complete(self, request: LLMRequest) -> str:
        brief = request.brief
        principles = [p for p in brief.principles if p in _POOL] or list(_POOL)
        taken = {_norm(t) for t in brief.avoid}
        rows = max(len(_POOL[p][1]) for p in principles)

        drafts: list[dict[str, str]] = []
        for row in range(rows):  # interleave principles so a small batch is varied
            for principle in principles:
                default_tone, closers = _POOL[principle]
                if row >= len(closers):
                    continue
                text = f"{brief.body.rstrip()}\n{closers[row]}"
                if _norm(text) in taken or len(drafts) >= brief.count:
                    continue
                tone = brief.tones[len(drafts) % len(brief.tones)] if brief.tones else default_tone
                drafts.append({"text": text, "principle": principle, "tone": tone})
                taken.add(_norm(text))
        return json.dumps(drafts)
