"""Study reference videos and save style profiles in worker/brain/profiles/.

    python tools/analyze_reference.py <youtube link | video file> --name hillier_pacing --minutes 12
    python tools/analyze_reference.py --list library/references.txt --estimate     # what it would use, spends nothing
    python tools/analyze_reference.py --list library/references.txt                # run it

Order of preference for a public YouTube link: the free Gemini keys first (GEMINI_API_KEYS in the environment,
comma separated), rotated, on the best free Flash model, then down the free model ladder in config.json
(`reference.free_models`). Only when every free option is used up does it fall back to the paid Gemini Pro on
OpenRouter (OPENROUTER_VIDEO_AGENT_KEY), and then only up to `--max-paid` dollars in total (default
`reference.paid_allowance_usd`; 0 turns the paid route off). A local file is never sent to the free tier: it goes to
the paid zero-retention route, so it needs the OpenRouter key.

A list has one `link [minutes] [name]` per line. Existing profiles are skipped unless `--force`. Share-link tracking
ids (`?si=...`) are dropped before a link is sent. Free-tier content can be used by Google: public videos only.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ad_cutter  # noqa: E402
import freepool  # noqa: E402
import gemini_free  # noqa: E402
import llm  # noqa: E402
import styleprofile  # noqa: E402
from budget import LocalLedger  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", nargs="?", help="a youtube.com/watch?v=... link, or a small local video file")
    ap.add_argument("--name", help="profile name, e.g. hillier_pacing")
    ap.add_argument("--minutes", type=float, default=15.0, help="video length (default 15), for windows and costs")
    ap.add_argument("--list", type=Path, help="a file of `link [minutes] [name]` lines to analyse in one go")
    ap.add_argument("--force", action="store_true", help="with --list: redo profiles that already exist")
    ap.add_argument("--max-paid", type=float, help="most dollars the paid fallback may use in this run")
    ap.add_argument("--config", type=Path, default=ad_cutter.HERE / "config.json")
    ap.add_argument("--estimate", action="store_true", help="show the plan and the paid worst case, run nothing")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    cfg = ad_cutter.load_config(args.config)
    settings = cfg.get("reference") or {}
    paid_allowance = args.max_paid if args.max_paid is not None else float(settings.get("paid_allowance_usd", 5.0))

    if args.list:
        items = styleprofile.parse_list(args.list.read_text(encoding="utf-8"))
        items = [(u, m, n) for u, m, n in items if args.force or not (styleprofile.PROFILE_DIR / f"{n}.json").exists()]
    elif args.source and args.name:
        items = [(args.source, args.minutes, args.name)]
    else:
        ap.error("give a source and --name, or --list FILE")
    if not items:
        print("Nothing to do: every profile in the list already exists (use --force to redo them).")
        return 0

    keys = freepool.keys_from_env()
    ring = freepool.KeyRing(keys, settings.get("free_models")) if keys else None
    paid_key = bool(os.environ.get(cfg["openrouter_key_env"], "").strip())
    model, reasoning = llm.model_for(cfg, "reference")
    print(f"Free first: {len(keys)} Gemini key(s), models {', '.join(ring.models) if ring else 'none'}")
    print(f"Paid fallback: {model} ({reasoning} reasoning), up to ${paid_allowance:.2f} in total, "
          f"key {'set' if paid_key else 'NOT set'}")
    worst = 0.0
    for src, minutes, name in items:
        est = styleprofile.estimate(cfg, name, minutes)
        worst += est
        is_link = bool(styleprofile.normalize_youtube(src))
        windows = len(styleprofile.plan_windows(minutes, int(settings.get("window_seconds", 600)),
                                                int(settings.get("max_windows", 3)))) if is_link else 1
        print(f"  {name:26} {minutes:5.1f} min  {windows} free window(s); if paid: up to ${est:.2f}")
    print(f"{len(items)} video(s). The free route costs nothing; if everything fell back to paid the worst case would "
          f"be ${worst:.2f}, capped at ${paid_allowance:.2f}.")
    if args.estimate:
        return 0
    if not keys and not (paid_key and paid_allowance > 0):
        print("Nothing can run: no free keys (GEMINI_API_KEYS) and no paid key allowed. Set one and try again.")
        return 1

    client = (llm.OpenRouter(key_env=cfg["openrouter_key_env"],
                             ledger=LocalLedger(Path(cfg["work_dir"]) / "spend-ledger.jsonl", cfg["monthly_budget_usd"]))
              if paid_key else None)
    paid_left, spent, failed, free_n, paid_n = paid_allowance, 0.0, 0, 0, 0
    for n, (src, minutes, name) in enumerate(items, 1):
        print(f"[{n}/{len(items)}] {name}: watching ({minutes:g} min)...", flush=True)
        try:
            profile, notes, info = styleprofile.analyze_best(cfg, ring, client, src, name, minutes,
                                                             paid_left=paid_left, log=print)
        except gemini_free.GeminiFreeError as err:     # one video's problem, or a network that is down
            failed += 1
            print(f"FAILED {name}: {err}")
            if err.kind == "network":
                print("Stopping: the network looks down. Run again later.")
                break
            continue
        except (llm.LLMError, freepool.FreeExhausted, OSError) as err:
            failed += 1
            print(f"FAILED {name}: {err}")
            if "paid allowance" in str(err) or "no paid" in str(err) or isinstance(err, freepool.FreeExhausted):
                print("Stopping: nothing is left to run the remaining videos on. Run again later (free limits "
                      "reset daily) or raise --max-paid.")
                break
            continue
        path = styleprofile.save(profile)
        if info["route"] == "paid":
            paid_n += 1
            spent += info["cost"]
            paid_left -= max(info["cost"], 0.0)
        else:
            free_n += 1
        print(f"saved {path.name}: {info['route']} ({', '.join(info['models'])}, {info['windows']} window(s), "
              f"${info['cost']:.2f})")
        for n in notes:
            print("  note:", n)
    print(f"Done: {free_n} free, {paid_n} paid (${spent:.2f}), {failed} failed."
          + (f" Free route: {ring.status()}; calls per key: {dict(sorted(ring.calls.items()))}" if ring else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
