"""Study one reference video with Gemini Pro (deep reasoning) and save a style profile in worker/brain/profiles/.

    python tools/analyze_reference.py <youtube link | video file> --name hillier_pacing --minutes 12 --estimate
    python tools/analyze_reference.py <youtube link | video file> --name hillier_pacing --minutes 12

`--estimate` prints the worst-case cost and spends nothing. Without it the analysis runs (one billed Pro call, no
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="a youtube.com/watch?v=... link, or a small local video file")
    ap.add_argument("--name", required=True, help="profile name, e.g. hillier_pacing")
    ap.add_argument("--minutes", type=float, default=15.0, help="video length, for the cost estimate (default 15)")
    ap.add_argument("--config", type=Path, default=ad_cutter.HERE / "config.json")
    ap.add_argument("--estimate", action="store_true", help="print the worst-case cost and stop")
    args = ap.parse_args(argv)
    cfg = ad_cutter.load_config(args.config)
    model, reasoning = llm.model_for(cfg, "reference")
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
