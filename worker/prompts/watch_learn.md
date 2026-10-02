You are studying a tutorial or explainer about video editing, motion graphics, captions or short-form video craft, on behalf
of a small team that makes short vertical video ads (15 to 60 seconds, 9:16, usually one person talking to the camera). Their
editor is an automated program. It can: cut and reorder the spoken footage, trim pauses, add word-by-word captions in any
style, add headlines and designed cards (stat cards, quotes, lower-thirds, charts, callouts, comparison panels), animate text
and shapes with HTML, CSS and GSAP, punch-in zooms, transitions, colour treatment, loudness and noise clean-up with ffmpeg,
and end cards. It builds visuals as HTML pages rendered to video. It cannot film new footage.

What the team wants to learn from this video: {focus}

{scope}

Write down the techniques this video teaches and the lessons behind them. Only what the video actually teaches, in its own
terms; do not add advice of your own. Skip sponsor segments and self-promotion. For each technique give enough detail that a
programmer could build it: what it looks like, when to use it, and the steps in plain words (name the tool the video uses, and
what it would be in HTML, CSS, GSAP or ffmpeg if that is obvious).

Reply with one JSON object and nothing else:

{{
  "watched": {{
    "seconds": <the length of what you watched, in seconds>,
    "first_words": "<the first 8 words spoken in what you watched>",
    "last_words": "<the last 8 words spoken in what you watched>"
  }},
  "summary": "<two sentences: what this video (or this part of it) teaches>",
  "techniques": [
    {{
      "at_s": <seconds from the start of what you watched, where it is taught>,
      "name": "<two to five words, e.g. 'caption pop on key word'>",
      "what": "<one or two sentences: what it looks like>",
      "when_to_use": "<one sentence: when it helps an ad, or when it hurts>",
      "how": "<two to six short steps, separated by ' | '>",
      "tags": ["<one to four of: hook, story, pacing, cut, captions, typography, motion, transition, graphics, overlay, cta, color, sound, music, broll, framing, layout, emphasis, proof, format, tools, ffmpeg, hyperframes, gsap, social>"]
    }}
  ],
  "lessons": [
    {{
      "at_s": <seconds from the start of what you watched>,
      "topic": "<two or three words>",
      "principle": "<one sentence: the lesson as the video states it>",
      "tags": ["<one to three tags from the same list>"]
    }}
  ]
}}

At most 12 techniques and 20 lessons. An empty list is fine when the video teaches none.
