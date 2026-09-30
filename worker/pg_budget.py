"""The shared Gemini spending ledger in Postgres: safe when several workers run at once.

reserve() locks this key's row for the month, checks spent + reserved + estimate against the cap, and records the
reservation, all in one transaction. settle() moves the reservation into `spent` using the real cost.
release_stale() (nightly) settles reservations left behind by a crashed worker at their full reserved amount.
"""
from __future__ import annotations

import uuid

from budget import Reservation, check_room, month_key


class PostgresLedger:
    def __init__(self, connect, key_name: str, monthly_cap: float, job_id: str | None = None):
        """connect: a zero-argument function returning a new psycopg connection."""
        self.connect, self.key_name, self.cap, self.job_id = connect, key_name, float(monthly_cap), job_id

    def spent(self, month: str | None = None) -> float:
        with self.connect() as conn:
            row = conn.execute("select spent from budget_months where key_name = %s and month = %s",
                               (self.key_name, month or month_key())).fetchone()
        return float(row[0]) if row else 0.0

    def reserve(self, estimate: float, label: str = "") -> Reservation:
        month, rid = month_key(), uuid.uuid4().hex
        with self.connect() as conn, conn.transaction():
            conn.execute("insert into budget_months (key_name, month, cap_usd) values (%s, %s, %s) "
                         "on conflict (key_name, month) do nothing", (self.key_name, month, self.cap))
            spent, reserved, cap = conn.execute(
                "select spent, reserved, cap_usd from budget_months where key_name = %s and month = %s for update",
                (self.key_name, month)).fetchone()
            check_room(float(spent), float(reserved), estimate, min(float(cap), self.cap))
            conn.execute("update budget_months set reserved = reserved + %s where key_name = %s and month = %s",
                         (estimate, self.key_name, month))
            conn.execute("insert into spend_ledger (id, key_name, month, job_id, label, reserved) "
                         "values (%s, %s, %s, %s, %s, %s)", (rid, self.key_name, month, self.job_id, label, estimate))
        return Reservation(rid, month, float(estimate), label)

    def settle(self, res: Reservation, actual: float | None, model: str = "", note: str = "") -> float:
        cost = float(actual if actual is not None else res.estimate)
        with self.connect() as conn, conn.transaction():
            row = conn.execute("select reserved from spend_ledger where id = %s and status = 'reserved' for update",
                               (res.id,)).fetchone()
            if row is None:                       # already settled (e.g. by the nightly clean-up): don't count twice
                return cost
            conn.execute("update budget_months set reserved = greatest(reserved - %s, 0), spent = spent + %s "
                         "where key_name = %s and month = %s", (row[0], cost, self.key_name, res.month))
            conn.execute("update spend_ledger set status = 'settled', cost = %s, model = %s, note = %s, "
                         "settled_at = now() where id = %s", (cost, model, note[:300], res.id))
            if self.job_id:
                conn.execute("update jobs set cost_usd = cost_usd + %s where id = %s", (cost, self.job_id))
        return cost


def release_stale(connect, older_than_minutes: int = 90) -> int:
    """Settle reservations a crashed worker never settled, counting their full reserved amount."""
    with connect() as conn, conn.transaction():
        rows = conn.execute("select id, key_name, month, reserved from spend_ledger where status = 'reserved' "
                            "and created_at < now() - make_interval(mins => %s) for update skip locked",
                            (older_than_minutes,)).fetchall()
        for rid, key_name, month, reserved in rows:
            conn.execute("update budget_months set reserved = greatest(reserved - %s, 0), spent = spent + %s "
                         "where key_name = %s and month = %s", (reserved, reserved, key_name, month))
            conn.execute("update spend_ledger set status = 'settled', cost = reserved, note = 'released: worker "
                         "never settled (counted at worst case)', settled_at = now() where id = %s", (rid,))
    return len(rows)
