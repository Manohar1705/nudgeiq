"""Worker set-up (8d-1). Needs no Redis, except the one opt-in test at the bottom."""

import os

import pytest

from app.worker.celery_app import (
    DEFAULT_REDIS_URL,
    QUEUE_NAME,
    celery_app,
    create_celery_app,
    redis_url_from_env,
)
from app.worker.check import run_check
from app.worker.tasks import ping


# ---------------------------------------------------------------- settings
def test_redis_url_defaults_and_overrides():
    assert redis_url_from_env({}) == DEFAULT_REDIS_URL
    assert redis_url_from_env({"NUDGEIQ_REDIS_URL": "  "}) == DEFAULT_REDIS_URL
    assert (
        redis_url_from_env({"NUDGEIQ_REDIS_URL": "redis://redis:6379/2"}) == "redis://redis:6379/2"
    )
    assert redis_url_from_env({"NUDGEIQ_REDIS_URL": "rediss://h:6380/0"}) == "rediss://h:6380/0"


@pytest.mark.parametrize("bad", ["localhost:6379", "http://x", "amqp://guest@h//"])
def test_bad_redis_url_is_rejected_with_a_clear_message(bad):
    with pytest.raises(ValueError, match="NUDGEIQ_REDIS_URL"):
        redis_url_from_env({"NUDGEIQ_REDIS_URL": bad})


def test_app_is_configured_for_safe_task_handling():
    app = create_celery_app("redis://example:6379/5")
    assert app.conf.broker_url == "redis://example:6379/5"
    assert app.conf.result_backend == "redis://example:6379/5"
    assert app.conf.task_default_queue == QUEUE_NAME
    assert app.conf.accept_content == ["json"]  # never pickle
    assert app.conf.task_serializer == "json" and app.conf.result_serializer == "json"
    assert app.conf.task_acks_late is True  # a dead worker's task is not lost
    assert app.conf.worker_prefetch_multiplier == 1
    assert app.conf.result_expires == 3600


def test_the_ping_task_is_registered_on_the_shared_app():
    assert "nudgeiq.ping" in celery_app.tasks
    assert ping.name == "nudgeiq.ping"


# ---------------------------------------------------------------- the task itself
def test_ping_echoes_without_a_broker():
    # .apply() runs the task in this process: no Redis, no worker needed.
    out = ping.apply(args=["hi"]).get()
    assert out["echo"] == "hi"
    assert out["at"].endswith("+00:00")
    assert ping.apply().get()["echo"] == "ping"


# ---------------------------------------------------------------- the check command
def test_check_fails_fast_when_redis_is_down(monkeypatch):
    monkeypatch.setattr(celery_app.conf, "broker_url", "redis://127.0.0.1:1/0")
    ok, message = run_check(timeout=1)
    assert ok is False
    assert message.startswith("could not reach Redis at redis://127.0.0.1:1/0")


# ---------------------------------------------------------------- real Redis (opt-in)
REDIS = os.environ.get("NUDGEIQ_TEST_REDIS")


@pytest.mark.skipif(not REDIS, reason="set NUDGEIQ_TEST_REDIS=redis://localhost:6379/15 to run")
def test_ping_round_trip_through_real_redis():
    from celery.contrib.testing.worker import start_worker

    app = create_celery_app(REDIS)
    task = app.tasks["nudgeiq.ping"]
    with start_worker(app, queues=[QUEUE_NAME], pool="solo", perform_ping_check=False):
        out = task.delay("round trip").get(timeout=15)
    assert out["echo"] == "round trip"
    assert out["worker"]
