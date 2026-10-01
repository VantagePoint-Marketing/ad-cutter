You are studying a video about the craft of video editing, on behalf of a small team that cuts short vertical ads:
15 to 60 seconds, 9:16, one speaker talking to the camera, with word-by-word captions, a headline at the top, a few
hand-drawn marker callouts and a closing call-to-action card. Their editor is an automated pipeline with exactly
these levers:

- hook: which sentence the ad opens on
- segment_order: the order of the spoken segments
- length: how long the ad runs
- cut_points: exactly where each cut lands (on a breath, mid-syllable, before or after a pause)
- pause_trim: how much silence is left between words and between cuts
- headline: the on-screen headline text
- callouts: the short marker notes and the moments they appear
- captions: the word-by-word captions and which words are highlighted
- cta: the closing card
- primary_text: the ad copy above the video
- angle: which idea the ad leads with

It has no music, no B-roll, no sound effects, no colour work and no second camera.

{scope}

Write down the lessons this video teaches about editing, each as a principle with the moment it is taught. For each
lesson say which lever it maps to, or "none" when the team cannot act on it (keep those too; they go to a wishlist).
Only what the video actually teaches, in its own terms; do not add advice of your own. Skip sponsor segments.

Reply with one JSON object and nothing else:

{{
  "watched": {{
    "seconds": <the length of what you watched, in seconds>,
    "first_words": "<the first 8 words spoken in what you watched>",
    "last_words": "<the last 8 words spoken in what you watched>"
  }},
  "summary": "<two sentences: what this video (or this part of it) is about>",
  "lessons": [
    {{
      "at_s": <seconds from the start of what you watched, where this is taught>,
      "topic": "<two or three words>",
      "principle": "<one sentence: the lesson as the video states it>",
      "lever": "<hook | segment_order | length | cut_points | pause_trim | headline | callouts | captions | cta | primary_text | angle | none>",
      "how_we_apply": "<one sentence: how the team's pipeline would apply it, or why it cannot>"
    }}
  ]
}}

At most 25 lessons.
