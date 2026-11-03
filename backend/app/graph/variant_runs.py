"""Start, read and resume variant-generation runs (step 8e-3).

A run is one pass of the variant graph with its own id. `start` runs it until it either finishes
or stops at `await_review`. `review` resumes a stopped run with a person's decisions.

Where a run lives: in memory (langgraph's InMemorySaver), so a server restart forgets the run.
That loses nothing important: the new variants were already saved as PENDING, and the ordinary
approve and reject endpoints still work on them. Only the "resume this run" handle is gone.
A durable checkpointer (PostgreSQL) is a later step, if it is ever wanted.

Needs the langgraph package. It is imported when a service is built, not when this module is
imported, so the API starts without it.
"""

import contextlib
import logging
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.campaigns import (
    DEFAULT_COUNT,
    CampaignError,
    CampaignService,
    CampaignStateError,
    SkippedDraft,
    ValidationIssue,
)
from app.graph.trace import TraceEntry, TraceError, TraceRecorder
from app.graph.variant_state import ACTIONS, REVIEW_NODE
from app.llm import LLMClient, LLMError
from app.models import MessageVariant, VariantStatus

RunStatus = Literal["awaiting_review", "completed", "error"]
DEFAULT_MAX_RUNS = 200
DEFAULT_LLM_ATTEMPTS = 2  # the model call is tried this many times before the run fails
DEFAULT_RETRY_DELAY = 0.5  # seconds, times the attempt number

log = logging.getLogger("nudgeiq.graph")

# Errors that mean "the request or the data is wrong, or the model said no". They are raised
# unchanged so the API turns them into its usual 404 / 409 / 422 / 502. Anything else is a bug
# or an outage, and becomes a RunFailedError with a plain message.
EXPECTED_ERRORS: tuple[type[Exception], ...] = (CampaignError, LLMError, ValueError, LookupError)


class RunNotFoundError(CampaignError, LookupError):
    """No such run (never started, forgotten after a restart, or for another campaign)."""


class RunStateError(CampaignStateError):
    """The run is not waiting for a review (it already finished, or it failed)."""


class RunFailedError(Exception):
    """A node failed for a reason the request cannot fix. Carries where, never a traceback.

    The details are in the trace and in the log (with the traceback), under the run id.
    """

    def __init__(self, run_id: str, node: str | None, error: TraceError | None) -> None:
        self.run_id = run_id
        self.node = node
        self.error = error
        where = f"at '{node}'" if node else "inside the graph"
        kind = f" ({error.type})" if error else ""
        super().__init__(f"variant run '{run_id}' failed {where}{kind}; see the server log")


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    variant_id: str = Field(min_length=1)
    action: Literal["approve", "reject"]
    note: str | None = Field(default=None, max_length=500)


class RunStep(BaseModel):
    agent: str
    note: str


class ReviewOutcome(BaseModel):
    variant_id: str
    action: str
    ok: bool
    status: str | None = None  # the variant's status after the decision
    error: str | None = None
    code: str | None = None
    warnings: list[ValidationIssue] = Field(default_factory=list)


class RunView(BaseModel):
    run_id: str
    campaign_id: str
    template_id: str
    status: RunStatus
    # the variants this run saved, as stored right now (so their status is up to date)
    variants: list[MessageVariant]
    # advice for the reviewer, by variant id (from the approval checks)
    warnings: dict[str, list[ValidationIssue]]
    skipped: list[SkippedDraft]
    reviewed: list[ReviewOutcome]
    pending: list[str]  # variant ids from this run that are still PENDING
    steps: list[RunStep]
    # One entry per attempt of each node: timing, outcome, keys written. See app/graph/trace.py.
    trace: list[TraceEntry] = Field(default_factory=list)
    # Set when the latest attempt failed (status "error"); None otherwise.
    error: TraceEntry | None = None


@dataclass
class _Run:
    campaign_id: str
    template_id: str
    lock: threading.Lock


class VariantRunService:
    def __init__(
        self,
        service: CampaignService,
        client: LLMClient,
        *,
        max_runs: int = DEFAULT_MAX_RUNS,
        llm_attempts: int = DEFAULT_LLM_ATTEMPTS,
        retry_delay: float = DEFAULT_RETRY_DELAY,
        recorder: TraceRecorder | None = None,
    ) -> None:
        from langgraph.checkpoint.memory import InMemorySaver

        from app.graph.variant_builder import build_variant_graph

        self.service = service
        self._saver = InMemorySaver()
        self.trace = recorder or TraceRecorder()
        self._graph = build_variant_graph(
            service,
            client,
            self._saver,
            self.trace,
            llm_attempts=llm_attempts,
            retry_delay=retry_delay,
        )
        self._max_runs = max_runs
        self._runs: OrderedDict[str, _Run] = OrderedDict()
        self._mu = threading.Lock()

    # ------------------------------------------------------------------ public
    def start(
        self,
        campaign_id: str,
        template_id: str,
        *,
        count: int = DEFAULT_COUNT,
        principles: list[str] | None = None,
        tones: list[str] | None = None,
    ) -> RunView:
        """Run until the graph finishes or waits for a person. Errors are the plain endpoint's."""
        run_id = uuid.uuid4().hex
        initial: dict[str, Any] = {
            "run_id": run_id,
            "campaign_id": campaign_id,
            "template_id": template_id,
            "count": count,
            "principles": list(principles or []),
            "tones": list(tones or []),
            "steps": [],
        }
        try:
            self._graph.invoke(initial, self._config(run_id))
        except Exception as exc:
            failure = self._failure(run_id, exc)
            self._forget(run_id)  # a run that failed to start leaves nothing behind
            if failure is None:
                raise
            raise failure from exc
        run = _Run(campaign_id, template_id, threading.Lock())
        with self._mu:
            self._runs[run_id] = run
            self._prune()
        return self._view(run_id, run)

    def get(self, run_id: str, campaign_id: str | None = None) -> RunView:
        run = self._find(run_id, campaign_id)
        return self._view(run_id, run)

    def review(
        self,
        run_id: str,
        decisions: list[ReviewDecision],
        campaign_id: str | None = None,
    ) -> RunView:
        """Resume a waiting run with a person's decisions.

        A request that names a variant not in this run, or the same variant twice, is refused
        (ValueError) before anything is resumed, so the run stays waiting and can be retried.
        An empty list ends the run and leaves every variant pending.
        """
        from langgraph.types import Command

        run = self._find(run_id, campaign_id)
        with run.lock:  # two reviewers at once cannot both resume the same pause
            snapshot = self._graph.get_state(self._config(run_id))
            if self._status(snapshot) != "awaiting_review":
                raise RunStateError(f"run '{run_id}' is not waiting for a review")
            in_run = set(snapshot.values.get("created_ids", []))
            seen: set[str] = set()
            for d in decisions:
                if d.action not in ACTIONS:  # the model already limits this; belt and braces
                    raise ValueError(f"unknown action {d.action!r}")
                if d.variant_id not in in_run:
                    raise ValueError(f"variant '{d.variant_id}' is not part of this run")
                if d.variant_id in seen:
                    raise ValueError(f"variant '{d.variant_id}' appears twice")
                seen.add(d.variant_id)
            payload = {"decisions": [d.model_dump() for d in decisions]}
            try:
                self._graph.invoke(Command(resume=payload), self._config(run_id))
            except Exception as exc:
                # The run stays where its last checkpoint left it, and shows the failure.
                failure = self._failure(run_id, exc)
                if failure is None:
                    raise
                raise failure from exc
        return self._view(run_id, run)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _config(run_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": run_id}}

    def _find(self, run_id: str, campaign_id: str | None) -> _Run:
        with self._mu:
            run = self._runs.get(run_id)
        if run is None or (campaign_id is not None and run.campaign_id != campaign_id):
            raise RunNotFoundError(run_id)
        return run

    @staticmethod
    def _status(snapshot: Any) -> RunStatus:
        waiting = tuple(snapshot.next)
        if not waiting:
            return "completed"
        if waiting == (REVIEW_NODE,):
            return "awaiting_review"
        return "error"  # stopped somewhere else: a node raised after the pause

    def _failure(self, run_id: str, exc: Exception) -> RunFailedError | None:
        """None for an expected error (raise it as it is); a RunFailedError for anything else."""
        if isinstance(exc, EXPECTED_ERRORS):
            return None
        last = self.trace.last_error(run_id)
        log.error("variant run %s failed", run_id, exc_info=exc)  # the traceback stays here
        return RunFailedError(run_id, last.node if last else None, last.error if last else None)

    def _view(self, run_id: str, run: _Run) -> RunView:
        snapshot = self._graph.get_state(self._config(run_id))
        values = snapshot.values
        created_ids: list[str] = list(values.get("created_ids", []))
        variants: list[MessageVariant] = []
        for variant_id in created_ids:
            stored = self.service.repo.get_variant(run.campaign_id, variant_id)
            if stored is not None:
                variants.append(stored)
        return RunView(
            run_id=run_id,
            campaign_id=run.campaign_id,
            template_id=run.template_id,
            status=self._status(snapshot),
            variants=variants,
            warnings={
                vid: [ValidationIssue.model_validate(i) for i in issues]
                for vid, issues in values.get("warnings", {}).items()
                if vid in created_ids
            },
            skipped=[SkippedDraft.model_validate(x) for x in values.get("skipped", [])],
            reviewed=[ReviewOutcome.model_validate(r) for r in values.get("reviewed", [])],
            pending=[v.variant_id for v in variants if v.status == VariantStatus.PENDING],
            steps=[RunStep.model_validate(s) for s in values.get("steps", [])],
            trace=self.trace.get(run_id),
            error=self.trace.last_error(run_id),
        )

    def _forget(self, run_id: str) -> None:
        self.trace.forget(run_id)
        delete = getattr(self._saver, "delete_thread", None)
        if delete is not None:
            with contextlib.suppress(Exception):  # cleanup only; never hide the real error
                delete(run_id)

    def _prune(self) -> None:
        """Keep memory bounded: drop the oldest finished runs. A waiting run is never dropped.

        Called with self._mu held.
        """
        for run_id in list(self._runs):
            if len(self._runs) <= self._max_runs:
                return
            snapshot = self._graph.get_state(self._config(run_id))
            if self._status(snapshot) == "completed":
                del self._runs[run_id]
                self._forget(run_id)
