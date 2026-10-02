"""Study one reference video with Gemini Pro (deep reasoning) and save a style profile in worker/brain/profiles/.

    python tools/analyze_reference.py <youtube link | video file> --name hillier_pacing --minutes 12 --estimate
    python tools/analyze_reference.py <youtube link | video file> --name hillier_pacing --minutes 12
    python tools/analyze_reference.py --list library/references.txt --estimate     # a whole list, priced first
    python tools/analyze_reference.py --list library/references.txt --yes          # ...then run it

A list has one `link [minutes] [name]` per line. Existing profiles are skipped unless `--force`. Share-link tracking
ids (`?si=...`) are dropped before a link is sent. `--estimate` prints the worst-case cost and spends nothing. Without it the analysis runs (one billed Pro call, no
retries) and the profile is saved only after validation. Needs OPENROUTER_VIDEO_AGENT_KEY in the environment. Public
YouTube links go over the one OpenRouter route that accepts them (not zero-retention: public videos only); local
files go zero-retention. Do not point it at private footage you would not send to Google.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ad_cutter  # noqa: E402
import llm  # noqa: E402
import styleprofile  # noqa: E402
from budget import LocalLedger  # noqa: E402


def run_list(args, cfg: dict, model: str, reasoning: str) -> int:
    items = styleprofile.parse_list(args.list.read_text(encoding="utf-8"))
    todo = [(u, m, n) for u, m, n in items if args.force or not (styleprofile.PROFILE_DIR / f"{n}.json").exists()]
    total = 0.0
    for url, minutes, name in todo:
        est = styleprofile.estimate(cfg, name, minutes)
        total += est
        print(f"  {name:22} {minutes:5.1f} min  up to ${est:.2f}  {url}")
    print(f"{model} ({reasoning} reasoning): {len(todo)} of {len(items)} to do, worst case ${total:.2f} in all "
          f"(real cost is usually lower)")
    if args.estimate or not todo:
        return 0
    if not args.yes:
        print("Nothing was run. Add --yes to spend it.")
        return 2
    client = llm.OpenRouter(key_env=cfg["openrouter_key_env"],
                            ledger=LocalLedger(Path(cfg["work_dir"]) / "spend-ledger.jsonl", cfg["monthly_budget_usd"]))
    failed = 0
    for url, minutes, name in todo:
        try:
            profile, notes, usage = styleprofile.analyze(cfg, client, url, name, minutes)
            print(f"saved {styleprofile.save(profile)} (cost ${usage.get('cost')})")
            for n in notes:
                print("  note:", n)
        except Exception as err:      # noqa: BLE001 - one failed video must not cost the rest of the list
            failed += 1
            print(f"FAILED {name}: {err}")
    return 1 if failed else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", nargs="?", help="a youtube.com/watch?v=... link, or a small local video file")
    ap.add_argument("--name", help="profile name, e.g. hillier_pacing")
    ap.add_argument("--list", type=Path, help="a file of `link [minutes] [name]` lines to analyse in one go")
    ap.add_argument("--yes", action="store_true", help="with --list: really run it (it spends up to the total shown)")
    ap.add_argument("--force", action="store_true", help="with --list: redo profiles that already exist")
    ap.add_argument("--minutes", type=float, default=15.0, help="video length, for the cost estimate (default 15)")
    ap.add_argument("--config", type=Path, default=ad_cutter.HERE / "config.json")
    ap.add_argument("--estimate", action="store_true", help="print the worst-case cost and stop")
    args = ap.parse_args(argv)
    cfg = ad_cutter.load_config(args.config)
    model, reasoning = llm.model_for(cfg, "reference")
    if args.list:
        return run_list(args, cfg, model, reasoning)
    if not args.source or not args.name:
        ap.error("give a source and --name, or --list FILE")
    est = styleprofile.estimate(cfg, args.name, args.minutes)
    print(f"{model} ({reasoning} reasoning), about {args.minutes:g} min of video: worst case ${est:.2f}")
    if args.estimate:
        return 0
    client = llm.OpenRouter(key_env=cfg["openrouter_key_env"],
                            ledger=LocalLedger(Path(cfg["work_dir"]) / "spend-ledger.jsonl", cfg["monthly_budget_usd"]))
    profile, notes, usage = styleprofile.analyze(cfg, client, args.source, args.name, args.minutes)
    path = styleprofile.save(profile)
    print(f"saved {path} (cost ${usage.get('cost')})")
    for n in notes:
        print("note:", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
