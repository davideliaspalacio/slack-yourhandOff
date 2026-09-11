from concurrent.futures import ThreadPoolExecutor

import pytest

from handoff_agent import db
from handoff_agent.ingest import queue


def test_enqueue_creates_one_pending_job(conn):
    assert queue.enqueue("U1", "mensaje") is True
    assert queue.status_counts() == {"pendiente": 1}


def test_only_one_open_job_per_person_even_under_concurrency(conn):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: queue.enqueue("U1", "mensaje"), range(8)))
    assert results.count(True) == 1
    assert queue.status_counts() == {"pendiente": 1}


def test_unknown_reasons_are_rejected(conn):
    with pytest.raises(ValueError):
        queue.enqueue("U1", "porque sí")


def test_claim_takes_the_oldest_pending_job(conn):
    queue.enqueue("U1", "mensaje")
    queue.enqueue("U2", "miembro_nuevo")
    job = queue.claim_next()
    assert job["slack_user_id"] == "U1"
    assert job["status"] == "en_curso"
    assert job["attempts"] == 1


def test_claim_returns_none_when_nothing_is_pending(conn):
    assert queue.claim_next() is None


def test_claim_skips_jobs_scheduled_for_later(conn):
    queue.enqueue("U1", "mensaje")
    db.execute("update research_jobs set not_before = now() + interval '1 hour'")
    assert queue.claim_next() is None


def test_concurrent_workers_never_claim_the_same_job(conn):
    queue.enqueue("U1", "mensaje")
    queue.enqueue("U2", "mensaje")
    with ThreadPoolExecutor(max_workers=4) as pool:
        claimed = [job for job in pool.map(lambda _: queue.claim_next(), range(4)) if job]
    assert sorted(job["slack_user_id"] for job in claimed) == ["U1", "U2"]


def test_complete_stores_the_outcome(conn):
    queue.enqueue("U1", "mensaje")
    job = queue.claim_next()
    queue.complete(job["id"], {"estado": "investigado"})
    row = db.fetch_one("select status, outcome, finished_at from research_jobs")
    assert row["status"] == "hecho"
    assert row["outcome"] == {"estado": "investigado"}
    assert row["finished_at"] is not None


def test_failures_retry_with_backoff_then_give_up(conn):
    queue.enqueue("U1", "mensaje")
    for attempt in range(1, queue.MAX_ATTEMPTS + 1):
        db.execute("update research_jobs set not_before = null")
        job = queue.claim_next()
        status = queue.fail(job["id"], f"fallo {attempt}")
        assert status == ("fallido" if attempt == queue.MAX_ATTEMPTS else "pendiente")
    row = db.fetch_one("select attempts, last_error from research_jobs")
    assert row["attempts"] == queue.MAX_ATTEMPTS
    assert row["last_error"] == f"fallo {queue.MAX_ATTEMPTS}"


def test_a_retry_waits_before_it_can_be_claimed_again(conn):
    queue.enqueue("U1", "mensaje")
    queue.fail(queue.claim_next()["id"], "timeout")
    assert queue.claim_next() is None


def test_release_hands_the_job_back_without_burning_an_attempt(conn):
    queue.enqueue("U1", "mensaje")
    job = queue.claim_next()
    queue.release(job["id"])
    again = queue.claim_next()
    assert again["id"] == job["id"]
    assert again["attempts"] == 1


def test_give_up_fails_at_once(conn):
    queue.enqueue("U1", "mensaje")
    queue.give_up(queue.claim_next()["id"], "sin nombre ni empresa")
    assert queue.status_counts() == {"fallido": 1}


def test_finished_last_hour_counts_done_and_failed_jobs(conn):
    for user in ("U1", "U2", "U3"):
        queue.enqueue(user, "mensaje")
    queue.complete(queue.claim_next()["id"], {})
    queue.give_up(queue.claim_next()["id"], "x")
    assert queue.finished_last_hour() == 2


def test_the_hourly_limit_comes_from_config(conn):
    assert queue.hourly_limit() == 20


def test_recent_failures_lists_the_newest_first(conn):
    queue.enqueue("U1", "mensaje")
    queue.give_up(queue.claim_next()["id"], "primero")
    queue.enqueue("U2", "mensaje")
    queue.give_up(queue.claim_next()["id"], "segundo")
    assert [f["last_error"] for f in queue.recent_failures()] == ["segundo", "primero"]
