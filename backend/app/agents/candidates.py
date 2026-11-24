"""Builds the legal candidate list: propose (Channel/Timing/Offer) then filter (Guardrail).

The work is split into three small steps (entry_check, propose_actions, filter_candidates) so the
LangGraph pipeline can run them as separate nodes. generate_candidates() calls the same three
steps in order, so the classic and graph engines share the same code and write the same trace.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from itertools import product

from app.agents.channel import propose_channels
from app.agents.guardrail import GuardrailAgent
from app.agents.offer import propose_levers, propose_offers
from app.agents.timing import propose_timings
from app.models import Action, Channel, CustomerState, DecisionStep, UseCaseConfig


@dataclass
class CandidateResult:
    candidates: list[Action] = field(default_factory=list)
    suppressed: bool = False  # failed the entry rule: do not contact at all
    forced_wait: bool = False  # eligible, but no legal action right now
    blocked: dict[str, str] = field(default_factory=dict)  # action key -> reason
    steps: list[DecisionStep] = field(default_factory=list)

    def note(self, agent: str, text: str) -> None:
        self.steps.append(DecisionStep(agent=agent, note=text))


@dataclass(frozen=True)
class FilterOutcome:
    candidates: list[Action]
    blocked: dict[str, str]  # action key -> reason
    forced_wait: bool
    steps: list[DecisionStep]


def _step(agent: str, text: str) -> DecisionStep:
    return DecisionStep(agent=agent, note=text)


def entry_check(state: CustomerState, guardrail: GuardrailAgent) -> tuple[bool, list[DecisionStep]]:
    """Step 1: the entry rule. Returns (allowed, trace notes)."""
    entry = guardrail.check_entry(state)
    if not entry.allowed:
        return False, [_step("guardrail", "suppressed at entry: " + "; ".join(entry.reasons))]
    return True, [_step("guardrail", "entry rule passed")]


def propose_actions(
    state: CustomerState,
    config: UseCaseConfig,
    can_send: Callable[[Channel, str | None], bool] | None = None,
) -> tuple[list[Action], list[DecisionStep]]:
    """Step 2: every channel x timing x offer x lever this customer is eligible for, minus
    actions that have no approved message variant. Returns (actions, trace notes)."""
    steps: list[DecisionStep] = []
    channels = propose_channels(state, config)
    timings = propose_timings(state, config)
    offers = propose_offers(state, config)
    levers = propose_levers(state, config)
    steps.append(_step("channel", f"eligible channels: {[c.value for c in channels]}"))
    steps.append(_step("timing", f"reachable timings: {timings}"))
    steps.append(_step("offer", f"eligible offers: {offers}; levers: {levers}"))

    proposed = [
        Action(channel=ch, timing=t, offer=o, lever=lv)
        for lv, ch, o, t in product(levers, channels, offers, timings)
    ]
    if can_send is not None:
        sendable = [a for a in proposed if a.channel is not None and can_send(a.channel, a.offer)]
        if len(sendable) < len(proposed):
            steps.append(
                _step(
                    "message",
                    f"dropped {len(proposed) - len(sendable)} actions with no approved variant",
                )
            )
        proposed = sendable
    return proposed, steps


def filter_candidates(
    state: CustomerState,
    proposed: list[Action],
    guardrail: GuardrailAgent,
    now: datetime,
) -> FilterOutcome:
    """Step 3: the guardrails remove what is not allowed right now."""
    steps: list[DecisionStep] = []
    filtered = guardrail.filter_actions(state, proposed, now)
    blocked = dict(filtered.blocked)
    if filtered.global_block:
        steps.append(_step("guardrail", f"all actions blocked: {filtered.global_block}"))
        return FilterOutcome([], blocked, True, steps)

    if filtered.blocked:
        reasons: dict[str, int] = {}
        for reason in filtered.blocked.values():
            reasons[reason] = reasons.get(reason, 0) + 1
        summary = "; ".join(f"{n} x {r}" for r, n in reasons.items())
        steps.append(
            _step("guardrail", f"blocked {len(filtered.blocked)} of {len(proposed)}: {summary}")
        )

    candidates = filtered.allowed
    if not candidates:
        steps.append(_step("guardrail", "no legal action left: wait"))
    else:
        steps.append(_step("guardrail", f"{len(candidates)} legal candidates"))
    return FilterOutcome(candidates, blocked, not candidates, steps)


def generate_candidates(
    state: CustomerState,
    config: UseCaseConfig,
    guardrail: GuardrailAgent,
    now: datetime,
    can_send: Callable[[Channel, str | None], bool] | None = None,
) -> CandidateResult:
    result = CandidateResult()

    allowed, steps = entry_check(state, guardrail)
    result.steps.extend(steps)
    if not allowed:
        result.suppressed = True
        return result

    proposed, steps = propose_actions(state, config, can_send)
    result.steps.extend(steps)

    outcome = filter_candidates(state, proposed, guardrail, now)
    result.blocked = outcome.blocked
    result.forced_wait = outcome.forced_wait
    result.candidates = outcome.candidates
    result.steps.extend(outcome.steps)
    return result
