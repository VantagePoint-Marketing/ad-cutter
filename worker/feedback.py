"""What the team said about earlier ads, turned into a short block of examples for the next plan.

Staff click "Good" or "Not right" (with an optional note) under each finished ad on the page. `team_notes(conn)` picks
the newest few and formats them for the planning prompt. They are taste hints, quoted as data: the prompt says they
never override the request or the rules, and every note is flattened to one short line first.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("ad-cutter")
MAX_ITEMS, MAX_BAD, MAX_GOOD = 8, 5, 3          # the plan: at most 8 items, at most 5 bad and 3 good, one per job
MAX_NOTE = 300
EMPTY = "(Nothing yet.)"

# the newest verdict for each ad of each job, with the ad's name and headline from the job's saved result
RECENT_SQL = """
with latest as (
    select distinct on (job_id, ad_k) id, job_id, ad_k, verdict, note, created_at
      from ad_feedback order by job_id, ad_k, created_at desc, id desc
)
select l.job_id::text, l.verdict, l.note, a->>'name', a->>'headline', a->>'angle'
  from latest l
  join jobs j on j.id = l.job_id
 cross join lateral jsonb_array_elements(case when jsonb_typeof(j.result->'ads') = 'array'
                                              then j.result->'ads' else '[]'::jsonb end) a
 where a->>'k' = l.ad_k::text
 order by l.created_at desc, l.id desc
 limit 60
"""


def one_line(text, limit: int) -> str:
    """Staff text for a prompt: one line, no control characters, no backticks or braces, cut to `limit`."""
    flat = re.sub(r"[\x00-\x1f\x7f`{}]+|\s+", " ", str(text or "")).strip()
    return flat[:limit].rstrip()


def pick(rows: list[tuple]) -> list[tuple]:
    """Newest first; one item per job, at most MAX_BAD "bad" and MAX_GOOD "good", MAX_ITEMS in all."""
    seen, bad, good, out = set(), 0, 0, []
    for row in rows:
        job_id, verdict = row[0], row[1]
        if job_id in seen or verdict not in ("good", "bad"):
            continue
        if verdict == "bad" and bad >= MAX_BAD or verdict == "good" and good >= MAX_GOOD:
            continue
        seen.add(job_id)
        bad, good = bad + (verdict == "bad"), good + (verdict == "good")
        out.append(row)
        if len(out) == MAX_ITEMS:
            break
    return out


def render(rows: list[tuple]) -> str:
    lines = []
    for _job, verdict, note, name, headline, angle in pick(rows):
        what = f'"{one_line(name, 60)}", headline "{one_line(headline, 90)}"'
        if angle:
            what += f", angle: {one_line(angle, 120)}"
        said = one_line(note, MAX_NOTE)
        lines.append(f"- {'Not right' if verdict == 'bad' else 'Good'} ({what}): " + (f'"{said}"' if said else "no note"))
    return "\n".join(lines) or EMPTY


def team_notes(conn) -> str:
    """The block for the planning prompt; EMPTY when there is no feedback yet or it cannot be read (never an error:
    a missing hint must not stop a job)."""
    try:
        return render(conn.execute(RECENT_SQL).fetchall())
    except Exception as err:   # noqa: BLE001
        log.warning("could not read the team's feedback: %s", err)
        return EMPTY
