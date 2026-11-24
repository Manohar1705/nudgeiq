"""A per-node trace for any graph (step 8e-4).

`traced(...)` wraps one node function. Every time the node runs it writes one `TraceEntry`: which
node, which attempt, how it ended, how long it took, which state keys it wrote and the notes it
added. A failed attempt is recorded too, which is the point: when a node raises, LangGraph drops
that node's update, so the state cannot carry the failure. The entries go to a `TraceRecorder`
next to the graph instead, keyed by run id.

What the trace does not hold: the node's input or output values. They can be large and can hold
customer data. An entry says what was written (key names) and the note, nothing more.

Retries live here too, because a retry is just another attempt in the trace. A node is retried
only for the error types it is given, and only up to `attempts`. Anything else is raised at once.

No langgraph import: the caller says which exception means "paused, not failed"
(`passthrough`), so this file works with any graph, or none.
"""

import functools
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

log = logging.getLogger("nudgeiq.graph")

MAX_ERROR_CHARS = 300
DEFAULT_MAX_ENTRIES = 100  # per run; a run that keeps replaying cannot grow without limit

TraceStatus = Literal["ok", "paused", "retry", "error"]


class TraceError(BaseModel):
    type: str  # the exception class name, e.g. "LLMError"
    message: str  # cut to MAX_ERROR_CHARS; never a traceback


class TraceEntry(BaseModel):
    seq: int  # 1, 2, 3... in the order the attempts happened
    node: str
    attempt: int = 1
    status: TraceStatus
    started_at: str  # UTC, ISO 8601
    duration_ms: float
    wrote: list[str] = Field(default_factory=list)  # state keys the node returned
    notes: list[str] = Field(default_factory=list)  # the `note` of each step it added
    error: TraceError | None = None


def describe_error(exc: BaseException) -> TraceError:
    text = str(exc) or type(exc).__name__
    if len(text) > MAX_ERROR_CHARS:
        text = text[: MAX_ERROR_CHARS - 3] + "..."
    return TraceError(type=type(exc).__name__, message=text)


class DecisionTrace(BaseModel):
    """What `GET /decisions/{id}/trace` returns: the graph's per-node trace for one decision."""

    decision_id: str
    engine: str  # "graph" records a trace; "classic" has no graph, so `entries` is always empty
    entries: list[TraceEntry] = Field(default_factory=list)


class TraceRecorder:
    """Keeps each run's entries in memory. Safe to share between threads.

    max_runs  keep at most this many runs, dropping the oldest first. None (default) keeps all;
              the variant service prunes by itself. The decision graph makes one run per
              decision and never stops, so it sets a limit.
    """

    def __init__(
        self, *, max_entries: int = DEFAULT_MAX_ENTRIES, max_runs: int | None = None
    ) -> None:
        if max_runs is not None and max_runs < 1:
            raise ValueError("max_runs must be at least 1")
        self._max = max_entries
        self._max_runs = max_runs
        self._runs: OrderedDict[str, list[TraceEntry]] = OrderedDict()
        self._mu = threading.Lock()

    def add(
        self,
        run_id: str,
        *,
        node: str,
        attempt: int,
        status: TraceStatus,
        started_at: str,
        duration_ms: float,
        wrote: list[str] | None = None,
        notes: list[str] | None = None,
        error: TraceError | None = None,
    ) -> TraceEntry:
        with self._mu:
            entries = self._runs.setdefault(run_id, [])
            if self._max_runs is not None:
                while len(self._runs) > self._max_runs:
                    self._runs.popitem(last=False)  # the oldest run; never the one just added
            seq = entries[-1].seq + 1 if entries else 1
            entry = TraceEntry(
                seq=seq,
                node=node,
                attempt=attempt,
                status=status,
                started_at=started_at,
                duration_ms=duration_ms,
                wrote=wrote or [],
                notes=notes or [],
                error=error,
            )
            if len(entries) < self._max:
                entries.append(entry)
            return entry

    def get(self, run_id: str) -> list[TraceEntry]:
        with self._mu:
            return list(self._runs.get(run_id, []))

    def last_error(self, run_id: str) -> TraceEntry | None:
        """The failure that ended the run's latest attempt, or None if it last succeeded.

        A "retry" entry is not a failure: another attempt followed it.
        """
        entries = self.get(run_id)
        if entries and entries[-1].status == "error":
            return entries[-1]
        return None

    def forget(self, run_id: str) -> None:
        with self._mu:
            self._runs.pop(run_id, None)

    def rename(self, old: str, new: str) -> None:
        """Move a run's entries to another key (the decision graph: run id -> decision id).

        Does nothing if `old` has no entries. Entries already under `new` are replaced.
        """
        with self._mu:
            entries = self._runs.pop(old, None)
            if entries is not None:
                self._runs.pop(new, None)
                self._runs[new] = entries


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _note_of(step: Any) -> str:
    """The note of one step. The variant graph keeps steps as dicts, the decision graph as
    `DecisionStep` models; both are read the same way."""
    if isinstance(step, dict):
        return str(step.get("note", ""))
    return str(getattr(step, "note", ""))


def traced[S](
    name: str,
    fn: Callable[[S], dict[str, Any]],
    recorder: TraceRecorder,
    *,
    attempts: int = 1,
    retry_on: tuple[type[BaseException], ...] = (),
    passthrough: tuple[type[BaseException], ...] = (),
    delay: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
) -> Callable[[S], dict[str, Any]]:
    """Wrap one node.

    attempts     total tries (1 = no retry). Only an error in `retry_on` is tried again.
    delay        seconds to wait before attempt n+1, multiplied by n (0.5, 1.0, ...)
    passthrough  exceptions that mean "stop here, nothing is wrong" (a LangGraph pause). They
                 are recorded as "paused" and raised again untouched.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")

    @functools.wraps(fn)
    def run(state: S) -> dict[str, Any]:
        getter = getattr(state, "get", None)
        run_id = str(getter("run_id", "-")) if callable(getter) else "-"
        for attempt in range(1, attempts + 1):
            started_at = _now()
            t0 = time.perf_counter()

            def record(
                status: TraceStatus,
                update: dict[str, Any] | None = None,
                error: TraceError | None = None,
                *,
                _attempt: int = attempt,
                _started: str = started_at,
                _t0: float = t0,
            ) -> None:
                ms = round((time.perf_counter() - _t0) * 1000, 2)
                notes = [_note_of(s) for s in (update or {}).get("steps", [])]
                recorder.add(
                    run_id,
                    node=name,
                    attempt=_attempt,
                    status=status,
                    started_at=_started,
                    duration_ms=ms,
                    wrote=sorted(update or {}),
                    notes=notes,
                    error=error,
                )
                if status == "ok" or status == "paused":
                    log.info(
                        "node=%s run=%s attempt=%d status=%s ms=%.1f",
                        name,
                        run_id,
                        _attempt,
                        status,
                        ms,
                    )
                else:
                    log.warning(
                        "node=%s run=%s attempt=%d status=%s ms=%.1f error=%s: %s",
                        name,
                        run_id,
                        _attempt,
                        status,
                        ms,
                        error.type if error else "-",
                        error.message if error else "-",
                    )

            try:
                update = fn(state)
            except passthrough:
                record("paused")
                raise
            except Exception as exc:
                will_retry = attempt < attempts and isinstance(exc, retry_on)
                record("retry" if will_retry else "error", error=describe_error(exc))
                if not will_retry:
                    raise
                sleep(delay * attempt)
                continue
            record("ok", update)
            return update
        raise AssertionError("unreachable: the last attempt returns or raises")

    return run
