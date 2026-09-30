"""Apply db/migrations/*.sql in name order, once each. Railway runs this as the worker's pre-deploy step, using the
database owner's connection (DATABASE_URL). The web app never runs migrations.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

MIGRATIONS = Path(os.environ.get("MIGRATIONS_DIR", Path(__file__).resolve().parents[1] / "db" / "migrations"))
LOCK_ID = 7_412_001        # advisory lock: two deploys can't migrate at once


def pending(applied: set[str]) -> list[Path]:
    return [p for p in sorted(MIGRATIONS.glob("*.sql")) if p.name not in applied]


def main() -> int:
    import psycopg
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, connect_timeout=15) as conn:
        conn.execute("select pg_advisory_lock(%s)", (LOCK_ID,))
        try:
            conn.execute("create table if not exists schema_migrations (name text primary key, "
                         "applied_at timestamptz not null default now())")
            applied = {r[0] for r in conn.execute("select name from schema_migrations").fetchall()}
            todo = pending(applied)
            for path in todo:
                with conn.transaction():
                    conn.execute(path.read_text(encoding="utf-8"))
                    conn.execute("insert into schema_migrations (name) values (%s)", (path.name,))
                print(f"applied {path.name}")
            print(f"migrations up to date ({len(applied) + len(todo)} applied)")
        finally:
            conn.execute("select pg_advisory_unlock(%s)", (LOCK_ID,))
    return 0


if __name__ == "__main__":
    sys.exit(main())
