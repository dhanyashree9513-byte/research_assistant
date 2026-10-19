"""remember(): the work for one key runs once per run; failures are not saved. Needs Redis (docker compose up -d)."""

import uuid

import pytest
import redis

import store

try:
    store.client().ping()
except redis.ConnectionError:
    pytest.skip("Redis is not running", allow_module_level=True)


def counter(result):
    calls = []
    def work():
        calls.append(1)
        return result
    return work, calls


def test_second_call_with_same_key_is_served_from_redis():
    run, (work, calls) = f"test-{uuid.uuid4()}", counter({"status": "ok", "n": 1})
    assert store.remember(run, "research:1", work) == ({"status": "ok", "n": 1}, False)
    assert store.remember(run, "research:1", work) == ({"status": "ok", "n": 1}, True)
    assert len(calls) == 1  # the LLM-call stand-in ran once


def test_failed_results_are_not_saved():
    run, (work, calls) = f"test-{uuid.uuid4()}", counter({"status": "timeout"})
    keep = lambda r: r["status"] == "ok"
    store.remember(run, "research:1", work, keep)
    store.remember(run, "research:1", work, keep)
    assert len(calls) == 2  # tried again, not served a remembered failure


def test_other_runs_do_not_share_results():
    work, calls = counter({"status": "ok"})
    store.remember(f"test-{uuid.uuid4()}", "research:1", work)
    store.remember(f"test-{uuid.uuid4()}", "research:1", work)
    assert len(calls) == 2
