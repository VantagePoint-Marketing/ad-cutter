You are a senior short-form video editor and motion designer reverse-engineering what makes a reference video work.
Watch the whole video, with sound. Your job is to write a **style profile**: an exact, reusable description of how it
is edited, expressed in the vocabulary of the renderer that will reproduce the style ("{name}").

Measure; do not guess. Count cuts over a stretch you can verify and divide. Where the video does not show something
(for example it has no music, or no captions), say so by leaving that list empty. Be specific about what you actually
see: how words are revealed, how emphasis is shown, what appears on screen and when, how the video ends.

## What to extract

1. **Cadence**: the average shot length in seconds; whether cuts land on the music's beat; the share of the video that
   is B-roll or cutaway footage (0 to 1); how often it punches in or zooms ({zoom_punch}); and the overall pace.
2. **Captions**: which of the caption styles below the video's captions are closest to (more than one if they change),
   whether they are upper case, sentence case or lower case, and where they sit. Say in the notes how words are
   highlighted or revealed.
3. **Headlines or title text**: which headline styles it is closest to, and how titles or labels are used.
4. **Callouts** (arrows, boxes, labels, stickers, underlines): the closest callout styles and entrance motions.
5. **Transitions** between shots, from: {transitions}. (The renderer can currently draw only: {rendered}; list what
   the video really uses anyway.)
6. **End screen / call to action**: the closest layouts, and the tone of the closing line (direct, playful, soft sell).
7. **Rules**: up to 12 short, concrete, testable rules a different editor could follow to get this feel (for example
   "cut every 1.2 to 1.8 seconds while the speaker is talking" or "never show more than two callouts at once").
8. **Avoid**: up to 8 things this style clearly does not do.

Choose names only from these menus; anything else is ignored:

{menu}

Pick the closest names even when the match is not exact, and say how it differs in the notes.

## Output

Reply with one JSON object and nothing else:

```json
{{
  "summary": "two sentences: what the video is and what makes its editing distinctive",
  "cadence": {{"average_shot_seconds": 1.4, "cut_on_beat": true, "b_roll_ratio": 0.4, "zoom_punch": "every_4_6_seconds", "pace": "tight"}},
  "captions": {{"styles": ["bold_outline"], "case": ["upper"], "position": ["middle"], "notes": "..."}},
  "headline": {{"styles": ["big_stack"], "notes": "..."}},
  "callouts": {{"styles": ["sticky_note"], "motions": ["pop"], "notes": "..."}},
  "transitions": {{"types": ["hard_cut", "zoom_punch"], "notes": "..."}},
  "end_screen": {{"layouts": ["big_question"], "tone": "..."}},
  "rules": ["..."],
  "avoid": ["..."]
}}
```
