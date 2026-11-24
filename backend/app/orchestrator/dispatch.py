"""Dispatch: the last step of a decision.

LogDispatcher (default) pretends to send right away. ScheduledDispatcher (8d-2) writes the send
to the database; a background worker delivers it when its time comes.
"""

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.agents.message import RenderedMessage
from app.models import ScheduledSend
from app.repository.base import Repository


@dataclass(frozen=True)
class SentMessage:
    decision_id: str
    customer_id: str
    send_at: datetime
    message: RenderedMessage


class Dispatcher(Protocol):
    def send(
        self, decision_id: str, customer_id: str, message: RenderedMessage, send_at: datetime
    ) -> None: ...


class LogDispatcher:
    """Pretends to send. Keeps the most recent messages so tests and demos can look at them."""

    def __init__(self, keep: int = 1000) -> None:
        self.sent: deque[SentMessage] = deque(maxlen=keep)

    def send(
        self, decision_id: str, customer_id: str, message: RenderedMessage, send_at: datetime
    ) -> None:
        self.sent.append(SentMessage(decision_id, customer_id, send_at, message))


class ScheduledDispatcher:
    """Stores each send in the database for the worker to deliver at `send_at`.

    It never touches Redis, so the API keeps deciding even when Redis or the worker is down;
    the sends simply wait in the table. Safe to call twice for one decision (the second is ignored).
    """

    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    def send(
        self, decision_id: str, customer_id: str, message: RenderedMessage, send_at: datetime
    ) -> None:
        self.repo.save_scheduled_send(
            ScheduledSend(
                decision_id=decision_id,
                customer_id=customer_id,
                send_at=send_at,
                channel=message.channel,
                text=message.text,
                variant_id=message.variant_id,
                template_id=message.template_id,
            )
        )
