"""The orchestrator: wires the agents into the two runtime flows.

decide(customer)        state -> guardrail -> propose -> filter -> Master -> message -> dispatch
record_outcome(outcome) reward -> policy update -> exit check -> retry hint

Agents never call each other; this class is the only place that knows the order.
Every step appends a note to the decision record (the 'why').
"""

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from app.agents.candidates import generate_candidates
from app.agents.guardrail import GuardrailAgent
from app.agents.master import MasterAgent, bucket_for
from app.agents.message import MessageAgent, MessageError
from app.agents.state_agent import CustomerNotFoundError, StateAgent
from app.agents.variant_selector import EpsilonGreedySelector, VariantSelector
from app.graph.engine import check_engine
from app.graph.trace import TraceRecorder
from app.models import (
    Action,
    CustomerState,
    Decision,
    Journey,
    JourneyStatus,
    Outcome,
    OutcomeResult,
    PolicyError,
    SavedOutcome,
    TemplateLibrary,
    TriggerEvent,
    UseCaseConfig,
)
from app.orchestrator.dispatch import Dispatcher, LogDispatcher
from app.orchestrator.outcomes import OutcomeProcessor, journey_for, minutes_since_abandonment
from app.orchestrator.variant_stats import CampaignVariantStats
from app.repository.base import Repository

log = logging.getLogger("nudgeiq")
graph_log = logging.getLogger("nudgeiq.graph")  # the logger app/graph/trace.py writes to

TOP_K_CANDIDATES = 10  # scored candidates kept on the decision record (the rest are counted)
DEFAULT_TRACED_DECISIONS = 2000  # graph traces kept in memory; the oldest are dropped first
EVENT_FIELDS = ("cart_value", "abandoned_minutes")  # payload fields a trigger may update


class DecisionNotFoundError(LookupError):
    pass


class OutcomeMismatchError(ValueError):
    pass


class InvalidEventError(ValueError):
    pass


class Orchestrator:
    def __init__(
        self,
        repo: Repository,
        config: UseCaseConfig,
        library: TemplateLibrary,
        *,
        master: MasterAgent | None = None,
        dispatcher: Dispatcher | None = None,
        seed: int | None = None,
        id_factory: Callable[[], str] | None = None,
        campaign_version: int | None = None,
        engine: str = "classic",
        trace: TraceRecorder | None = None,
    ) -> None:
        self.engine = check_engine(engine)
        # Per-node trace of graph decisions, keyed by decision id. Pass one shared recorder to
        # keep traces across orchestrators (the runtime does); it stays empty for "classic".
        self.trace = trace or TraceRecorder(max_runs=DEFAULT_TRACED_DECISIONS)
        self.repo = repo
        self.config = config
        self.campaign_version = campaign_version  # stamped on every decision this makes
        self.guardrail = GuardrailAgent(config.guardrails)
        self.state_agent = StateAgent(repo, config)
        self.message_agent = MessageAgent(
            library,
            seed=seed,
            selector=self._variant_selector(repo, config, campaign_version, seed),
        )
        self.master = master or MasterAgent(epsilon=config.learning.epsilon, seed=seed)
        self.dispatcher: Dispatcher = dispatcher or LogDispatcher()
        # Reward, learning and the journey after an outcome (8d-3b step 2). The same class runs in
        # the background worker; here it also hands each credited policy back to our master.
        self.outcomes = OutcomeProcessor(repo, config, on_policy=self._adopt_policy)
        self._new_id = id_factory or (lambda: f"d_{uuid.uuid4().hex[:12]}")
        # Policy safety (8d-3a): other processes may learn into the same stored policy. We
        # remember which version we hold and reload before deciding when it has moved on.
        self._policy_version = 0
        self._bad_policy_version: int | None = None  # a version we already logged as unusable
        stored = repo.read_policy(config.use_case_id)
        if stored is not None:
            self.master.load_state(stored.policy)  # PolicyError here stops the start-up, loudly
            self._policy_version = stored.version
        elif self.master.cells_learned:
            # A master that already learned something, given to an empty repository: store it, so
            # the first credit adds to it instead of replacing it.
            self.repo.save_policy(config.use_case_id, self.master.export_state())
            self._policy_version = self.repo.policy_version(config.use_case_id)
        # The graph is built now, so a missing langgraph package stops the start-up, not a request.
        self._graph: Any = None
        if self.engine == "graph":
            from app.graph.builder import build_decision_graph

            self._graph = build_decision_graph(self, self.trace)
            log.info("decision engine: graph (LangGraph) for campaign %s", config.use_case_id)

    # ---------------------------------------------------------------- journeys
    def _journey_for(self, state: CustomerState, now: datetime) -> Journey:
        return journey_for(self.repo, state, now)

    @staticmethod
    def _minutes_since_abandonment(journey: Journey, now: datetime) -> int:
        return minutes_since_abandonment(journey, now)

    # ------------------------------------------------------------------ events
    def handle_event(self, event: TriggerEvent, now: datetime | None = None) -> Decision:
        """A trigger (e.g. cart_abandoned) starts a fresh journey and runs the decision flow."""
        if event.event_type != self.config.trigger_event:
            raise InvalidEventError(
                f"unknown event type '{event.event_type}' (expected '{self.config.trigger_event}')"
            )
        customer = self.repo.get_customer(event.customer_id)
        if customer is None:
            raise CustomerNotFoundError(event.customer_id)
        updates = {k: event.payload[k] for k in EVENT_FIELDS if k in event.payload}
        try:
            updated = CustomerState.model_validate({**customer.model_dump(), **updates})
        except ValidationError as exc:
            raise InvalidEventError(f"invalid event payload: {exc.errors()[0]['msg']}") from exc
        self.repo.save_customer(updated)
        self.repo.save_journey(
            Journey(
                customer_id=updated.customer_id,
                started_at=event.occurred_at,
                base_abandoned_minutes=updated.abandoned_minutes,
            )
        )
        return self.decide(event.customer_id, now or event.occurred_at)

    # ------------------------------------------------------------------ decide
    def _refresh_policy(self) -> None:
        """Pick up what other processes have learned. One cheap version check per decision.

        A stored policy that has gone bad is logged once per version and ignored: decisions go
        on with the policy already in memory rather than failing every request."""
        use_case_id = self.config.use_case_id
        current = self.repo.policy_version(use_case_id)
        if current == self._policy_version or current == self._bad_policy_version:
            return
        stored = self.repo.read_policy(use_case_id)
        if stored is None:
            return
        try:
            self.master.load_state(stored.policy)
        except PolicyError as exc:
            self._bad_policy_version = stored.version
            log.error(
                "stored policy for %s (version %s) is unusable, keeping the policy in memory: %s",
                use_case_id,
                stored.version,
                exc,
            )
            return
        self._policy_version = stored.version
        self._bad_policy_version = None

    def decide(self, customer_id: str, now: datetime | None = None) -> Decision:
        now = now or datetime.now(UTC)
        self._refresh_policy()
        if self._graph is not None:
            return self._decide_with_graph(customer_id, now)
        return self._decide_classic(customer_id, now)

    def _decide_with_graph(self, customer_id: str, now: datetime) -> Decision:
        # The run id is separate from the decision id on purpose: the id factory must be called
        # exactly as often as in the classic engine, and the decision id only exists once
        # `load_state` has run. When the run ends, its trace is filed under the decision id.
        run_id = f"r_{uuid.uuid4().hex[:12]}"
        try:
            result = self._graph.invoke(
                {"run_id": run_id, "customer_id": customer_id, "now": now, "steps": []}
            )
        except Exception:
            # No Decision exists to attach it to. The log keeps every attempt under this run id,
            # and the error itself is raised unchanged, exactly as the classic engine would.
            self.trace.forget(run_id)
            raise
        decision: Decision = result["decision"]
        self.trace.rename(run_id, decision.decision_id)
        graph_log.info("run=%s is decision %s", run_id, decision.decision_id)
        return decision

    def _decide_classic(self, customer_id: str, now: datetime) -> Decision:
        state = self.state_agent.build_state(customer_id, now)  # raises CustomerNotFoundError
        journey = self._journey_for(state, now)
        update: dict[str, Any] = {
            "abandoned_minutes": self._minutes_since_abandonment(journey, now)
        }
        if journey.nudges_sent > 0:  # retry round: timings count from now, not from abandonment
            update["timing_anchor_minutes"] = 0
        state = state.model_copy(update=update)
        decision = Decision(
            decision_id=self._new_id(),
            customer_id=customer_id,
            created_at=now,
            state=state,
            is_holdout=state.is_holdout,
            bucket=bucket_for(state),
        )

        if journey.status != JourneyStatus.ACTIVE:
            decision.log("orchestrator", f"journey is {journey.status.value}: no nudge")
            return self._save(decision)

        result = generate_candidates(
            state, self.config, self.guardrail, now, can_send=self.message_agent.can_send
        )
        decision.steps.extend(result.steps)
        decision.blocked = list(result.blocked)
        if not result.candidates:
            decision.log("master", "no legal candidates: waiting")
            return self._save(decision)

        picked = self.master.decide(decision.decision_id, state, result.candidates)
        decision.candidates = picked.candidates[:TOP_K_CANDIDATES]
        decision.chosen = picked.chosen
        decision.explored = picked.explored
        decision.steps.extend(picked.steps)
        if len(picked.candidates) > TOP_K_CANDIDATES:
            decision.log(
                "orchestrator",
                f"scored {len(picked.candidates)} candidates; top {TOP_K_CANDIDATES} stored",
            )
        if decision.chosen.is_wait:
            return self._save(decision)

        if self._compose_and_send(decision, state, now):
            self.repo.save_journey(
                journey.model_copy(update={"nudges_sent": journey.nudges_sent + 1})
            )
        return self._save(decision)

    @staticmethod
    def _variant_selector(
        repo: Repository, config: UseCaseConfig, version: int | None, seed: int | None
    ) -> VariantSelector | None:
        """None means the Message agent's default (uniform among approved)."""
        learning = config.learning
        if learning.variant_selection != "epsilon_greedy":
            return None
        return EpsilonGreedySelector(
            CampaignVariantStats(repo, config.use_case_id, version),
            epsilon=learning.variant_epsilon,
            min_resolved=learning.variant_min_resolved,
            seed=seed,
        )

    def _compose_and_send(self, decision: Decision, state: CustomerState, now: datetime) -> bool:
        chosen = decision.chosen
        try:
            rendered = self.message_agent.render(
                chosen, state, self.state_agent.personalization(state.customer_id)
            )
        except MessageError as exc:
            decision.chosen = Action.wait()
            decision.log("message", f"could not build a message ({exc}): waiting")
            return False
        send_at = self.guardrail.send_time(state, chosen, now)
        decision.variant_id = rendered.variant_id
        decision.template_id = rendered.template_id
        decision.message = rendered.text
        decision.scheduled_for = send_at
        decision.log(
            "message", f"used approved variant {rendered.variant_id} ({rendered.selection})"
        )
        self.dispatcher.send(decision.decision_id, state.customer_id, rendered, send_at)
        self.repo.record_contact(state.customer_id, send_at)
        channel = chosen.channel.value if chosen.channel else ""
        decision.log("dispatch", f"{channel} send at {send_at:%Y-%m-%d %H:%M} UTC")
        return True

    def _save(self, decision: Decision) -> Decision:
        # Record which campaign (and version) made this, so a later outcome is applied with
        # that campaign's rewards and learner even if another campaign has since gone live.
        decision.use_case_id = self.config.use_case_id
        decision.campaign_version = self.campaign_version
        self.repo.save_decision(decision)
        return decision

    # ---------------------------------------------------------------- outcomes
    def _check_outcome(self, outcome: Outcome) -> Decision:
        """The decision this outcome is about. Raises if it does not exist or belongs to
        another customer, so a bad outcome is refused up front, before it is stored."""
        decision = self.repo.get_decision(outcome.decision_id)
        if decision is None:
            raise DecisionNotFoundError(outcome.decision_id)
        if decision.customer_id != outcome.customer_id:
            raise OutcomeMismatchError(
                f"decision {decision.decision_id} belongs to {decision.customer_id}, "
                f"not {outcome.customer_id}"
            )
        return decision

    def accept_outcome(self, outcome: Outcome, idempotency_key: str | None = None) -> SavedOutcome:
        """Asynchronous path, step 1 of 8d-3b: check the outcome and store it as 'pending', and
        stop. No reward, no learning, no journey change: a background task does that (step 2),
        so an outcome is never lost while the worker is down. Raises DecisionNotFoundError or
        OutcomeMismatchError exactly as record_outcome does, and OutcomeKeyConflict if the key
        was used for a different outcome. A repeated key returns the stored one (created=False).
        """
        self._check_outcome(outcome)
        return self.repo.save_outcome(outcome, status="pending", idempotency_key=idempotency_key)

    def record_outcome(self, outcome: Outcome, now: datetime | None = None) -> OutcomeResult:
        """The synchronous path: check, store, reward, learn, update the journey, all now.

        The outcome is stored as 'pending' first and marked 'processed' only when the work is
        done. A crash half way leaves it pending, for the sweep to finish, instead of claiming
        it was processed. (Before step 2 it was stored as 'processed' straight away.)"""
        now = now or outcome.occurred_at
        decision = self._check_outcome(outcome)
        saved = self.repo.save_outcome(outcome, status="pending")
        result = self.outcomes.apply(decision, outcome, now)
        self.repo.mark_outcome(saved.record.outcome_id, "processed", datetime.now(UTC))
        return result

    def _adopt_policy(self, policy: dict[str, Any], version: int) -> None:
        self.master.load_state(policy)
        self._policy_version = version
        self._bad_policy_version = None

    # ----------------------------------------------------------------- helpers
    def policy_summary(self) -> dict[str, Any]:
        return {"use_case_id": self.config.use_case_id, "cells_learned": self.master.cells_learned}
