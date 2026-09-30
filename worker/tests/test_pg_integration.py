"""Real-Postgres checks for the job queue and the shared spending ledger.

Skipped unless TEST_DATABASE_URL points at a disposable database (Railway *staging*, never production).
They create their own rows with a unique marker and delete them afterwards.
"""
import os
import sys
import threading
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set (runs on Railway staging)")

if URL:
    import psycopg

    import budget
    import jobs
    import migrate
    from pg_budget import PostgresLedger, release_stale


def connect():
    return psycopg.connect(URL, autocommit=True)


@pytest.fixture(scope="module", autouse=True)
def schema():
    os.environ["DATABASE_URL"] = URL
    migrate.main()
    migrate.main()                              # second run is a no-op


@pytest.fixture
def key_name():
    name = f"test-{uuid.uuid4().hex[:8]}"
    yield name
    with connect() as c:
        c.execute("delete from spend_ledger where key_name = %s", (name,))
        c.execute("delete from budget_months where key_name = %s", (name,))


def test_parallel_reservations_never_exceed_the_cap(key_name):
    led = PostgresLedger(connect, key_name, monthly_cap=1.0)
    ok, refused = [], []

    def worker():
        try:
            ok.append(led.reserve(0.3, "parallel"))
        except budget.BudgetExceeded:
            refused.append(1)
    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(ok) == 3 and len(refused) == 5                  # 3 x 0.30 fits in 1.00, a 4th would not
    for r in ok:
        led.settle(r, 0.1)
    assert led.spent() == pytest.approx(0.3)
    with connect() as c:
        assert float(c.execute("select reserved from budget_months where key_name = %s", (key_name,)).fetchone()[0]) == 0


def test_settle_twice_counts_once(key_name):
    led = PostgresLedger(connect, key_name, monthly_cap=5.0)
    r = led.reserve(1.0)
    led.settle(r, 0.4)
    led.settle(r, 0.4)
    assert led.spent() == pytest.approx(0.4)


def test_stale_reservations_are_released_at_worst_case(key_name):
    led = PostgresLedger(connect, key_name, monthly_cap=5.0)
    r = led.reserve(0.7)
    with connect() as c:
        c.execute("update spend_ledger set created_at = now() - interval '3 hours' where id = %s", (r.id,))
    assert release_stale(connect, 90) >= 1
    assert led.spent() == pytest.approx(0.7)


def test_two_workers_never_claim_the_same_job():
    marker = f"test-{uuid.uuid4().hex[:8]}"
    with connect() as c:
        for i in range(5):
            c.execute("insert into jobs (created_by, source_key, source_name) values (%s, %s, %s)",
                      (marker, f"uploads/{marker}/{i}", f"{i}.mov"))
    claimed = []
    try:
        def grab():
            with connect() as c:
                while True:
                    row = c.execute(jobs.CLAIM_SQL + "", {"worker": threading.current_thread().name}).fetchone()
                    if not row or not row[3].startswith(f"uploads/{marker}"):
                        return
                    claimed.append(row[0])
        ts = [threading.Thread(target=grab, name=f"w{i}") for i in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert len(claimed) == len(set(claimed)) == 5
    finally:
        with connect() as c:
            c.execute("delete from jobs where created_by = %s", (marker,))


def test_stale_working_jobs_are_requeued_then_failed():
    marker = f"test-{uuid.uuid4().hex[:8]}"
    with connect() as c:
        c.execute("insert into jobs (created_by, source_key, source_name, status, attempts, heartbeat_at) values "
                  "(%s, 'k1', 'a', 'working', 1, now() - interval '1 hour'), "
                  "(%s, 'k2', 'b', 'working', 3, now() - interval '1 hour')", (marker, marker))
        c.execute(jobs.REQUEUE_STALE_SQL, (600,))
        rows = dict(c.execute("select source_key, status from jobs where created_by = %s", (marker,)).fetchall())
        c.execute("delete from jobs where created_by = %s", (marker,))
    assert rows == {"k1": "queued", "k2": "failed"}
