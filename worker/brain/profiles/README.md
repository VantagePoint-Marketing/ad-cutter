# Style profiles

The planner reads every `*.json` file directly in this folder (`styleprofile.load_all`), so keep it short and curated.

- `talking_head_ads.json`: the consensus of the 12 reference analyses, cut down to what a single vertical talking-head
  clip can do. Written by hand from `raw/` on 2026-10-02.
- `raw/`: one analysis per reference video, as `tools/analyze_reference.py` saved them (free `gemini-3.8-flash`, $0). They
  describe how each reference is edited (mostly long-form editing essays with lots of B-roll and screen recordings), so
  they are kept as evidence and are NOT read by the planner. Add `--active` to the tool to save straight into this folder.
