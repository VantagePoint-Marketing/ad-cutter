# ad-cutter

Turns a raw vertical talking-head video into ready-to-review Meta ad cuts.

**To use:** drag a video file onto `Cut ads.cmd`. The finished ads land in
`OneDrive\RevOps\Meta Ads\05 Ad Cutter Drafts\<video name> (<date>)\` with a `Review Notes.md`.

How it works:
1. **Planning (Gemini Pro, via OpenRouter):** Gemini watches the video and plans the ads: segments, headline, callouts, caption fixes and claims to review. It picks words by index, so it can't invent timestamps.
2. **Cutting and rendering (this PC):** Whisper transcribes locally. Every cut edge is snapped to the gap between words, pauses are trimmed, and audio is levelled to -14 LUFS. HyperFrames renders the captions, headline, callouts and CTA.
3. **Checking:** each finished ad is transcribed again and compared with its captions.

- Settings live in `config.json`: brand, CTA, model and output folder.
- Useful options: `--replan` (ask Gemini again, about $0.10), `--only 2` (rebuild one ad), `--no-render`.
- Tests: `python -m pytest -q`.

**Data:** only a small 640p proxy of the video goes to Gemini, through OpenRouter's zero-retention Google endpoints. The key is read from `call-intel\.env`. Nothing is published anywhere.
