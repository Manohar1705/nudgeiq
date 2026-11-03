"""Scheduled sends (8d-2). Everything here runs without Redis. The `repo` tests run on memory
and, with NUDGEIQ_TEST_DSN set, on PostgreSQL too."""

import os
import threading
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import app.worker.tasks  # noqa: F401  (registers the tasks on the shared Celery app)
from app.agents.message import RenderedMessage
from app.api.main import create_app
from app.models import Channel, Journey, JourneyStatus, ScheduledSend
from app.orchestrator import LogDispatcher, Orchestrator, ScheduledDispatcher
from app.repository import InMemoryRepository, PostgresRepository
from app.worker.celery_app import (
    DEFAULT_DISPATCH_INTERVAL,
    DISPATCH_TASK_NAME,
    celery_app,
    create_celery_app,
    dispatch_interval_from_env,
)
from app.worker.sends import max_late_from_env, run_due_sends, skip_reason

DSN = os.environ.get("NUDGEIQ_TEST_DSN")


def make_send(now, decision_id="d1", customer_id="c1", due_in=0, **kw):
    base = dict(
        decision_id=decision_id,
        customer_id=customer_id,
        send_at=now + timedelta(minutes=due_in),
        channel=Channel.PUSH,
        text="You left something in your cart",
        variant_id="v1",
        template_id="t1",
        created_at=now,
    )
    base.update(kw)
    return ScheduledSend(**base)


class Recorder:
    def __init__(self, fail_for=()):
        self.delivered: list[str] = []
        self.fail_for = set(fail_for)

    def __call__(self, send):
        if send.decision_id in self.fail_for:
            raise RuntimeError("provider down")
        self.delivered.append(send.decision_id)


# ----------------------------------------------------------------- the dispatcher
def test_dispatcher_stores_the_send_and_ignores_a_repeat(repo, now):
    msg = RenderedMessage(variant_id="v9", template_id="t9", channel=Channel.SMS, text="hi STOP")
    disp = ScheduledDispatcher(repo)
    when = now + timedelta(hours=3)
    disp.send("d1", "c1", msg, when)
    disp.send("d1", "c1", msg, when + timedelta(hours=1))  # same decision again: ignored
    stored = repo.get_scheduled_send("d1")
    assert stored is not None
    assert (stored.status, stored.send_at, stored.channel) == ("scheduled", when, Channel.SMS)
    assert (stored.text, stored.variant_id, stored.template_id) == ("hi STOP", "v9", "t9")
    assert len(repo.list_scheduled_sends()) == 1


def test_orchestrator_with_scheduled_dispatcher_only_records_the_send(
    repo, config, library, make_state, now
):
    orch = Orchestrator(repo, config, library, seed=7, dispatcher=ScheduledDispatcher(repo))
    repo.save_customer(make_state())
    d = orch.decide("c1", now)
    assert not d.chosen.is_wait
    send = repo.get_scheduled_send(d.decision_id)
    assert send is not None and send.status == "scheduled"
    assert send.send_at == d.scheduled_for and send.text == d.message
    assert send.customer_id == "c1" and send.variant_id == d.variant_id


def test_a_wait_decision_schedules_nothing(repo, config, library, make_state, now):
    orch = Orchestrator(repo, config, library, seed=7, dispatcher=ScheduledDispatcher(repo))
    repo.save_customer(make_state(contactable=False))
    d = orch.decide("c1", now)
    assert d.chosen.is_wait
    assert repo.list_scheduled_sends() == []


# ----------------------------------------------------------------- repository behaviour
def test_list_and_filter(repo, now):
    repo.save_scheduled_send(make_send(now, "a", due_in=10))
    repo.save_scheduled_send(make_send(now, "b", due_in=20))
    repo.claim_due_sends(now + timedelta(minutes=15))  # claims "a"
    assert [s.decision_id for s in repo.list_scheduled_sends()] == ["b", "a"]  # latest first
    assert [s.decision_id for s in repo.list_scheduled_sends("sending")] == ["a"]
    assert [s.decision_id for s in repo.list_scheduled_sends("scheduled")] == ["b"]
    assert len(repo.list_scheduled_sends(limit=1)) == 1
    assert repo.get_scheduled_send("nope") is None


def test_claim_takes_only_due_sends_once(repo, now):
    repo.save_scheduled_send(make_send(now, "due", due_in=-5))
    repo.save_scheduled_send(make_send(now, "later", due_in=60))
    first = repo.claim_due_sends(now)
    assert [s.decision_id for s in first] == ["due"]
    assert first[0].status == "sending" and first[0].claimed_at == now
    assert repo.claim_due_sends(now) == []  # already taken
    assert repo.get_scheduled_send("later").status == "scheduled"  # type: ignore[union-attr]


def test_claim_respects_the_batch_limit_and_order(repo, now):
    for i, due in enumerate([-30, -10, -20]):
        repo.save_scheduled_send(make_send(now, f"d{i}", due_in=due))
    got = repo.claim_due_sends(now, limit=2)
    assert [s.decision_id for s in got] == ["d0", "d2"]  # oldest send_at first
    assert [s.decision_id for s in repo.claim_due_sends(now)] == ["d1"]


def test_many_threads_never_get_the_same_send(repo, now):
    for i in range(60):
        repo.save_scheduled_send(make_send(now, f"d{i:02d}", due_in=-1))
    taken: list[str] = []
    guard = threading.Lock()

    def grab() -> None:
        while True:
            got = repo.claim_due_sends(now, limit=7)
            if not got:
                return
            with guard:
                taken.extend(s.decision_id for s in got)

    threads = [threading.Thread(target=grab) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(taken) == [f"d{i:02d}" for i in range(60)]  # each exactly once


@pytest.mark.skipif(not DSN, reason="set NUDGEIQ_TEST_DSN to run the PostgreSQL tests")
def test_two_database_connections_never_get_the_same_send(now):
    """Two workers = two connections. Only the database can keep them apart."""
    assert DSN is not None
    a, b = PostgresRepository(DSN), PostgresRepository(DSN)
    try:
        a.reset()
        for i in range(80):
            a.save_scheduled_send(make_send(now, f"d{i:02d}", due_in=-1))
        taken: list[str] = []
        guard = threading.Lock()

        def grab(r: PostgresRepository) -> None:
            while True:
                got = r.claim_due_sends(now, limit=5)
                if not got:
                    return
                with guard:
                    taken.extend(s.decision_id for s in got)

        threads = [threading.Thread(target=grab, args=(r,)) for r in (a, b, a, b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(taken) == [f"d{i:02d}" for i in range(80)]
    finally:
        a.close()
        b.close()


# ----------------------------------------------------------------- delivering what is due
def test_due_sends_go_out_once_and_future_ones_wait(repo, make_state, now):
    repo.save_customer(make_state())
    repo.save_scheduled_send(make_send(now, "due", due_in=-1))
    repo.save_scheduled_send(make_send(now, "later", due_in=90))
    rec = Recorder()
    assert run_due_sends(repo, now, deliver=rec) == {
        "sent": 1,
        "skipped": 0,
        "failed": 0,
        "recovered": 0,
    }
    assert rec.delivered == ["due"]
    done = repo.get_scheduled_send("due")
    assert done is not None and done.status == "sent" and done.finished_at == now
    # a second pass (a repeated task) sends nothing more
    assert run_due_sends(repo, now, deliver=rec)["sent"] == 0
    assert rec.delivered == ["due"]
    # later, the second one comes due
    assert run_due_sends(repo, now + timedelta(minutes=91), deliver=rec)["sent"] == 1
    assert rec.delivered == ["due", "later"]


@pytest.mark.parametrize(
    ("customer_kw", "channel", "journey", "expected"),
    [
        ({"contactable": False}, Channel.PUSH, None, "no longer contactable"),
        ({"sms_opt_in": False}, Channel.SMS, None, "not opted in to SMS"),
        ({"email_opt_in": False}, Channel.EMAIL, None, "not opted in to email"),
        ({"has_app": False}, Channel.PUSH, None, "no app for push"),
        ({}, Channel.PUSH, JourneyStatus.CONVERTED, "journey is converted"),
        ({}, Channel.PUSH, JourneyStatus.EXITED, "journey is exited"),
    ],
)
def test_a_send_is_skipped_when_the_customer_changed(
    repo, make_state, now, customer_kw, channel, journey, expected
):
    repo.save_customer(make_state(**customer_kw))
    if journey:
        repo.save_journey(
            Journey(customer_id="c1", status=journey, started_at=now, base_abandoned_minutes=30)
        )
    repo.save_scheduled_send(make_send(now, due_in=-1, channel=channel))
    rec = Recorder()
    assert run_due_sends(repo, now, deliver=rec)["skipped"] == 1
    assert rec.delivered == []
    sent = repo.get_scheduled_send("d1")
    assert sent is not None and sent.status == "skipped" and expected in sent.reason


def test_unknown_customer_is_skipped(repo, now):
    repo.save_scheduled_send(make_send(now, due_in=-1))
    assert skip_reason(repo, make_send(now)) == "customer not found"


def test_a_send_that_is_far_too_late_is_skipped(repo, make_state, now):
    repo.save_customer(make_state())
    repo.save_scheduled_send(make_send(now, "late", due_in=-300))  # due 5 hours ago
    repo.save_scheduled_send(make_send(now, "ok", due_in=-60))
    rec = Recorder()
    counts = run_due_sends(repo, now, deliver=rec, max_late=timedelta(minutes=120))
    assert (counts["sent"], counts["skipped"]) == (1, 1)
    assert rec.delivered == ["ok"]
    late = repo.get_scheduled_send("late")
    assert late is not None and late.status == "skipped" and "too late" in late.reason


def test_one_failing_send_does_not_stop_the_rest_and_is_not_retried(repo, make_state, now):
    repo.save_customer(make_state())
    for i, name in enumerate(["a", "bad", "c"]):
        repo.save_scheduled_send(make_send(now, name, due_in=-10 + i))
    rec = Recorder(fail_for={"bad"})
    counts = run_due_sends(repo, now, deliver=rec)
    assert (counts["sent"], counts["failed"]) == (2, 1)
    assert rec.delivered == ["a", "c"]
    bad = repo.get_scheduled_send("bad")
    assert bad is not None and bad.status == "failed" and "provider down" in bad.reason
    assert run_due_sends(repo, now, deliver=rec)["failed"] == 0  # no retry (at most once)
    assert rec.delivered == ["a", "c"]


def test_a_send_stuck_in_sending_is_failed_not_sent_again(repo, make_state, now):
    repo.save_customer(make_state())
    repo.save_scheduled_send(make_send(now, "stuck", due_in=-1))
    assert [s.decision_id for s in repo.claim_due_sends(now)] == ["stuck"]  # worker "dies" here
    rec = Recorder()
    early = run_due_sends(repo, now + timedelta(minutes=5), deliver=rec)
    assert early["recovered"] == 0  # might still be running
    late = run_due_sends(repo, now + timedelta(minutes=30), deliver=rec)
    assert late["recovered"] == 1 and rec.delivered == []
    stuck = repo.get_scheduled_send("stuck")
    assert stuck is not None and stuck.status == "failed" and "double send" in stuck.reason


def test_batch_size_limits_one_pass(repo, make_state, now):
    repo.save_customer(make_state())
    for i in range(5):
        repo.save_scheduled_send(make_send(now, f"d{i}", due_in=-10 + i))
    rec = Recorder()
    assert run_due_sends(repo, now, deliver=rec, batch=2)["sent"] == 2
    assert run_due_sends(repo, now, deliver=rec, batch=10)["sent"] == 3
    assert rec.delivered == ["d0", "d1", "d2", "d3", "d4"]


# ----------------------------------------------------------------- settings
def test_max_late_setting():
    assert max_late_from_env({}) == timedelta(minutes=120)
    assert max_late_from_env({"NUDGEIQ_SEND_MAX_LATE_MINUTES": "45"}) == timedelta(minutes=45)
    for bad in ("0", "-3", "soon"):
        with pytest.raises(ValueError, match="NUDGEIQ_SEND_MAX_LATE_MINUTES"):
            max_late_from_env({"NUDGEIQ_SEND_MAX_LATE_MINUTES": bad})


def test_dispatch_interval_setting():
    assert dispatch_interval_from_env({}) == DEFAULT_DISPATCH_INTERVAL == 30
    assert dispatch_interval_from_env({"NUDGEIQ_DISPATCH_INTERVAL_SECONDS": "10"}) == 10
    for bad in ("4", "0", "fast"):
        with pytest.raises(ValueError, match="NUDGEIQ_DISPATCH_INTERVAL_SECONDS"):
            dispatch_interval_from_env({"NUDGEIQ_DISPATCH_INTERVAL_SECONDS": bad})


def test_beat_runs_the_dispatch_task_on_the_interval():
    app = create_celery_app("redis://example:6379/5", dispatch_interval=12)
    entry = app.conf.beat_schedule["dispatch-due-sends"]
    assert entry["task"] == DISPATCH_TASK_NAME == "nudgeiq.dispatch_due"
    assert entry["schedule"] == 12.0
    assert entry["options"]["expires"] == 24.0  # a stale pass is dropped, the next one covers it
    assert DISPATCH_TASK_NAME in celery_app.tasks


def test_dispatch_task_runs_a_pass_on_the_worker_repo(monkeypatch, make_state, now):
    from app.worker import tasks

    repo = InMemoryRepository()
    repo.save_customer(make_state())
    repo.save_scheduled_send(make_send(now - timedelta(minutes=1), due_in=0))
    monkeypatch.setattr(tasks, "worker_repo", lambda: repo)
    out = tasks.dispatch_due.apply().get()
    assert out["sent"] + out["skipped"] == 1  # real clock: far in the past, so it is skipped late


def test_worker_without_a_database_url_says_so(monkeypatch):
    from app.worker import tasks

    monkeypatch.setattr(tasks, "_repo", None)
    monkeypatch.delenv("NUDGEIQ_DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="NUDGEIQ_DATABASE_URL"):
        tasks.worker_repo()


# ----------------------------------------------------------------- the API switch
def test_switch_off_keeps_the_log_dispatcher(monkeypatch):
    monkeypatch.delenv("NUDGEIQ_SCHEDULED_SENDS", raising=False)
    app = create_app(InMemoryRepository())
    assert isinstance(app.state.runtime.dispatcher, LogDispatcher)


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_switch_on_without_postgres_fails_at_start_up(monkeypatch, value):
    monkeypatch.setenv("NUDGEIQ_SCHEDULED_SENDS", value)
    with pytest.raises(ValueError, match="NUDGEIQ_SCHEDULED_SENDS"):
        create_app(InMemoryRepository())


def test_switch_off_values_do_not_enable_it(monkeypatch):
    monkeypatch.setenv("NUDGEIQ_SCHEDULED_SENDS", "0")
    assert isinstance(create_app(InMemoryRepository()).state.runtime.dispatcher, LogDispatcher)


def test_endpoints_are_empty_and_validated_without_the_switch(repo):
    client = TestClient(create_app(repo))
    assert client.get("/scheduled-sends").json() == []
    assert client.get("/scheduled-sends?status=sent").json() == []
    assert client.get("/scheduled-sends?status=bogus").status_code == 422
    assert client.get("/scheduled-sends/nope").status_code == 404


@pytest.mark.skipif(not DSN, reason="set NUDGEIQ_TEST_DSN to run the PostgreSQL tests")
def test_decide_over_http_schedules_and_the_worker_pass_delivers(monkeypatch, now):
    assert DSN is not None
    monkeypatch.setenv("NUDGEIQ_SCHEDULED_SENDS", "1")
    pg = PostgresRepository(DSN)
    pg.reset()
    try:
        client = TestClient(create_app(pg))
        assert (
            client.post(
                "/customers",
                json={
                    "customer_id": "api_2",
                    "cohort_id": "cart_high_aov",
                    "cart_value": 120.0,
                    "abandoned_minutes": 35,
                    "preferred_channel": "push",
                    "signals": {"loyalty_tier": "circle", "card_holder": True},
                },
            ).status_code
            == 200
        )
        body = client.post(
            "/decide", json={"customer_id": "api_2", "as_of": now.isoformat()}
        ).json()
        assert body["message"] and body["scheduled_for"]
        row = client.get(f"/scheduled-sends/{body['decision_id']}").json()
        assert row["status"] == "scheduled" and row["text"] == body["message"]
        assert [
            r["decision_id"] for r in client.get("/scheduled-sends?status=scheduled").json()
        ] == [body["decision_id"]]

        rec = Recorder()
        before = run_due_sends(pg, now - timedelta(days=1), deliver=rec)
        assert before["sent"] == 0 and rec.delivered == []  # not due yet
        due = max_send = pg.get_scheduled_send(body["decision_id"]).send_at  # type: ignore[union-attr]
        assert run_due_sends(pg, max_send + timedelta(minutes=1), deliver=rec)["sent"] == 1
        assert rec.delivered == [body["decision_id"]] and due
        assert client.get(f"/scheduled-sends/{body['decision_id']}").json()["status"] == "sent"
    finally:
        pg.close()
