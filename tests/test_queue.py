import uuid
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
    queue.complete(job, {"estado": "investigado"})
    row = db.fetch_one("select status, outcome, finished_at from research_jobs")
    assert row["status"] == "hecho"
    assert row["outcome"] == {"estado": "investigado"}
    assert row["finished_at"] is not None


def test_failures_retry_with_backoff_then_give_up(conn):
    queue.enqueue("U1", "mensaje")
    for attempt in range(1, queue.MAX_ATTEMPTS + 1):
        db.execute("update research_jobs set not_before = null")
        job = queue.claim_next()
        status = queue.fail(job, f"fallo {attempt}")
        assert status == ("fallido" if attempt == queue.MAX_ATTEMPTS else "pendiente")
    row = db.fetch_one("select attempts, last_error from research_jobs")
    assert row["attempts"] == queue.MAX_ATTEMPTS
    assert row["last_error"] == f"fallo {queue.MAX_ATTEMPTS}"


def test_a_retry_waits_before_it_can_be_claimed_again(conn):
    queue.enqueue("U1", "mensaje")
    queue.fail(queue.claim_next(), "timeout")
    assert queue.claim_next() is None


def test_release_hands_the_job_back_without_burning_an_attempt(conn):
    queue.enqueue("U1", "mensaje")
    job = queue.claim_next()
    queue.release(job)
    again = queue.claim_next()
    assert again["id"] == job["id"]
    assert again["attempts"] == 1


def test_give_up_fails_at_once(conn):
    queue.enqueue("U1", "mensaje")
    queue.give_up(queue.claim_next(), "sin nombre ni empresa")
    assert queue.status_counts() == {"fallido": 1}


def test_finished_last_hour_counts_done_and_failed_jobs(conn):
    for user in ("U1", "U2", "U3"):
        queue.enqueue(user, "mensaje")
    queue.complete(queue.claim_next(), {})
    queue.give_up(queue.claim_next(), "x")
    assert queue.finished_last_hour() == 2


def test_the_hourly_limit_comes_from_config(conn):
    assert queue.hourly_limit() == 20


def test_recent_failures_lists_the_newest_first(conn):
    queue.enqueue("U1", "mensaje")
    queue.give_up(queue.claim_next(), "primero")
    queue.enqueue("U2", "mensaje")
    queue.give_up(queue.claim_next(), "segundo")
    assert [f["last_error"] for f in queue.recent_failures()] == ["segundo", "primero"]


# -- Transiciones protegidas: solo una tarea en_curso puede cambiar de estado --


def test_fail_on_a_finished_job_is_a_no_op(conn):
    queue.enqueue("U1", "mensaje")
    job = queue.claim_next()
    queue.complete(job, {"estado": "investigado"})
    assert queue.fail(job, "tarde") is None
    row = db.fetch_one(
        "select status, outcome, finished_at from research_jobs where id = %s", (job["id"],)
    )
    assert row["status"] == "hecho"
    assert row["outcome"] == {"estado": "investigado"}
    assert row["finished_at"] is not None


def test_fail_on_a_given_up_job_is_a_no_op(conn):
    queue.enqueue("U1", "mensaje")
    job = queue.claim_next()
    queue.give_up(job, "sin nombre ni empresa")
    assert queue.fail(job, "tarde") is None
    row = db.fetch_one("select status from research_jobs where id = %s", (job["id"],))
    assert row["status"] == "fallido"


def test_fail_on_an_unknown_job_returns_none(conn):
    assert queue.fail({"id": uuid.uuid4(), "started_at": None}, "x") is None


def test_complete_give_up_release_are_no_ops_on_a_job_that_is_not_en_curso(conn):
    queue.enqueue("U1", "mensaje")
    job = db.fetch_one("select id, started_at from research_jobs")
    assert queue.complete(job, {"x": 1}) is False
    assert queue.give_up(job, "y") is False
    assert queue.release(job) is False
    row = db.fetch_one("select status, attempts, outcome, last_error from research_jobs")
    assert row["status"] == "pendiente"
    assert row["attempts"] == 0
    assert row["outcome"] is None
    assert row["last_error"] is None


def test_a_second_release_of_the_same_job_is_a_no_op(conn):
    queue.enqueue("U1", "mensaje")
    job = queue.claim_next()
    assert queue.release(job) is True
    assert queue.release(job) is False
    row = db.fetch_one("select status, attempts from research_jobs")
    assert row["status"] == "pendiente"
    assert row["attempts"] == 0


# -- Un worker no puede tocar una tarea que reclaim_stale ya dio a otro --


def test_complete_after_a_stale_reclaim_leaves_the_new_claim_untouched(conn):
    queue.enqueue("U1", "mensaje")
    first = queue.claim_next()
    db.execute(
        "update research_jobs set started_at = now() - interval '45 minutes' where id = %s",
        (first["id"],),
    )
    assert queue.reclaim_stale() == 1
    second = queue.claim_next()
    assert second["id"] == first["id"]

    assert queue.complete(first, {"estado": "investigado"}) is False

    row = db.fetch_one(
        "select status, outcome, started_at from research_jobs where id = %s", (first["id"],)
    )
    assert row["status"] == "en_curso"
    assert row["outcome"] is None
    assert row["started_at"] == second["started_at"]


def test_fail_after_a_stale_reclaim_leaves_the_new_claim_untouched(conn):
    queue.enqueue("U1", "mensaje")
    first = queue.claim_next()
    db.execute(
        "update research_jobs set started_at = now() - interval '45 minutes' where id = %s",
        (first["id"],),
    )
    assert queue.reclaim_stale() == 1
    second = queue.claim_next()
    assert second["id"] == first["id"]

    assert queue.fail(first, "tarde, ya no es mía") is None

    row = db.fetch_one(
        "select status, attempts, last_error from research_jobs where id = %s", (first["id"],)
    )
    assert row["status"] == "en_curso"
    assert row["attempts"] == second["attempts"]
    assert row["last_error"] is None


def test_give_up_after_a_stale_reclaim_leaves_the_new_claim_untouched(conn):
    queue.enqueue("U1", "mensaje")
    first = queue.claim_next()
    db.execute(
        "update research_jobs set started_at = now() - interval '45 minutes' where id = %s",
        (first["id"],),
    )
    assert queue.reclaim_stale() == 1
    second = queue.claim_next()
    assert second["id"] == first["id"]

    assert queue.give_up(first, "sin nombre ni empresa") is False

    row = db.fetch_one("select status from research_jobs where id = %s", (first["id"],))
    assert row["status"] == "en_curso"


def test_release_after_a_stale_reclaim_leaves_the_new_claim_untouched(conn):
    queue.enqueue("U1", "mensaje")
    first = queue.claim_next()
    db.execute(
        "update research_jobs set started_at = now() - interval '45 minutes' where id = %s",
        (first["id"],),
    )
    assert queue.reclaim_stale() == 1
    second = queue.claim_next()
    assert second["id"] == first["id"]

    assert queue.release(first) is False

    row = db.fetch_one(
        "select status, started_at, attempts from research_jobs where id = %s", (first["id"],)
    )
    assert row["status"] == "en_curso"
    assert row["started_at"] == second["started_at"]
    assert row["attempts"] == second["attempts"]


# -- Backoff real: comprobar los minutos, no solo el estado --


def test_backoff_after_the_first_failure_is_about_fifteen_minutes(conn):
    queue.enqueue("U1", "mensaje")
    job = queue.claim_next()
    queue.fail(job, "timeout")
    row = db.fetch_one("select not_before, now() as db_now from research_jobs")
    delta = (row["not_before"] - row["db_now"]).total_seconds()
    assert abs(delta - 15 * 60) < 5


def test_backoff_after_the_second_failure_is_about_thirty_minutes(conn):
    queue.enqueue("U1", "mensaje")
    db.execute("update research_jobs set not_before = null")
    queue.fail(queue.claim_next(), "timeout")
    db.execute("update research_jobs set not_before = null")
    queue.fail(queue.claim_next(), "timeout otra vez")
    row = db.fetch_one("select not_before, now() as db_now from research_jobs")
    delta = (row["not_before"] - row["db_now"]).total_seconds()
    assert abs(delta - 30 * 60) < 5


# -- Reclamar tareas abandonadas por un worker caído --


def test_reclaim_stale_returns_an_abandoned_job_to_pending(conn):
    queue.enqueue("U1", "mensaje")
    job = queue.claim_next()
    db.execute(
        "update research_jobs set started_at = now() - interval '45 minutes' where id = %s",
        (job["id"],),
    )
    assert queue.reclaim_stale() == 1
    row = db.fetch_one(
        "select status, attempts, started_at, not_before from research_jobs where id = %s",
        (job["id"],),
    )
    assert row["status"] == "pendiente"
    assert row["attempts"] == 1
    assert row["started_at"] is None
    assert row["not_before"] is None
    again = queue.claim_next()
    assert again["id"] == job["id"]


def test_reclaim_stale_leaves_recent_jobs_alone(conn):
    queue.enqueue("U1", "mensaje")
    job = queue.claim_next()
    db.execute(
        "update research_jobs set started_at = now() - interval '5 minutes' where id = %s",
        (job["id"],),
    )
    assert queue.reclaim_stale() == 0
    row = db.fetch_one("select status from research_jobs where id = %s", (job["id"],))
    assert row["status"] == "en_curso"


def test_reclaim_stale_gives_up_a_job_that_exhausted_its_attempts(conn):
    queue.enqueue("U1", "mensaje")
    job = queue.claim_next()
    db.execute(
        "update research_jobs set started_at = now() - interval '45 minutes', attempts = %s "
        "where id = %s",
        (queue.MAX_ATTEMPTS, job["id"]),
    )
    assert queue.reclaim_stale() == 1
    row = db.fetch_one(
        "select status, finished_at, last_error from research_jobs where id = %s", (job["id"],)
    )
    assert row["status"] == "fallido"
    assert row["finished_at"] is not None
    assert row["last_error"] == "abandonada: el worker no terminó"


# -- hourly_limit no debe confiar ciegamente en lo que hay en config --


def test_hourly_limit_falls_back_to_default_for_bad_config_values(conn):
    bad_values = ("true", "20.7", '"veinte"', "null", "-1")
    try:
        for bad_value in bad_values:
            db.execute(
                "update config set value = %s::jsonb where key = 'research_por_hora'",
                (bad_value,),
            )
            assert queue.hourly_limit() == queue.DEFAULT_HOURLY_LIMIT
    finally:
        db.execute("update config set value = '20'::jsonb where key = 'research_por_hora'")


def test_the_radar_can_enqueue_research(conn):
    assert queue.enqueue("li:ACoAA1", "radar") is True
    assert db.fetch_one("select reason from research_jobs")["reason"] == "radar"
