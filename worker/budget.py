"""Monthly Gemini spending guard.

Before a call we reserve its worst-case cost; afterwards we settle it with the real cost OpenRouter reports. A call is
refused if spent + reserved + estimate would pass the monthly cap. OpenRouter's own per-key limit is the hard stop;
this ledger stops us *before* reaching it and keeps a record of what each job cost.

LocalLedger: a JSON-lines file, for single-user runs on a PC.
The shared Postgres version (safe for several workers at once) lives with the job queue.
"""
from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass
from pathlib import Path


class BudgetExceeded(RuntimeError):
    pass


def month_key(now: dt.datetime | None = None) -> str:
    return (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m")


def check_room(spent: float, reserved: float, estimate: float, cap: float) -> None:
    """The one rule both ledgers apply."""
    if estimate < 0:
        raise ValueError("estimate must be >= 0")
    if spent + reserved + estimate > cap:
        raise BudgetExceeded(f"this call could cost up to ${estimate:.2f}, but only ${max(0.0, cap - spent - reserved):.2f} "
                             f"of this month's ${cap:.0f} Gemini budget is left")


@dataclass
class Reservation:
    id: str
    month: str
    estimate: float
    label: str


class LocalLedger:
    def __init__(self, path: Path, monthly_cap: float):
        self.path, self.cap = Path(path), float(monthly_cap)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _entries(self, month: str) -> list[dict]:
        if not self.path.exists():
            return []
        rows = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [r for r in rows if r.get("month") == month]

    def spent(self, month: str | None = None) -> float:
        return round(sum(r.get("cost") or 0.0 for r in self._entries(month or month_key())), 6)

    def reserve(self, estimate: float, label: str = "") -> Reservation:
        month = month_key()
        check_room(self.spent(month), 0.0, estimate, self.cap)     # single process: nothing else is in flight
        return Reservation(uuid.uuid4().hex, month, float(estimate), label)

    def settle(self, res: Reservation, actual: float | None, model: str = "", note: str = "") -> float:
        cost = float(actual if actual is not None else res.estimate)   # unknown cost counts as the worst case
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"id": res.id, "month": res.month, "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                                "label": res.label, "model": model, "estimate": res.estimate, "cost": cost,
                                "note": note}) + "\n")
        return cost
