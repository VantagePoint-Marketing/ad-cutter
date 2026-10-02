You are studying a short video that is performing well, as a reference for a small team that makes short vertical video ads
(15 to 60 seconds, 9:16, usually one person talking to the camera). Their editor is an automated program that can cut and
reorder footage, add word-by-word captions in any style, headlines and designed cards (stat cards, quotes, lower-thirds, charts,
callouts, comparison panels), motion, zooms, transitions, colour treatment and sound clean-up. It cannot film new footage.

What the team wants to learn from this reference: {focus}

{scope}

Describe how this video is made and why it works, as facts about what is on screen and in the sound: do not guess its view
counts or results. Be specific about timing (seconds) and about style (position, size, colour, animation).

Reply with one JSON object and nothing else:

{{
  "watched": {{
    "seconds": <the length of what you watched, in seconds>,
    "first_words": "<the first 8 words spoken in what you watched>",
    "last_words": "<the last 8 words spoken in what you watched>"
  }},
  "summary": "<two sentences: what the video is and who it is for>",
  "format": "<talking head | screen recording | b-roll montage | animation | mixed | other>",
  "hook": {{"at_s": <when the hook lands>, "what": "<what is said or shown in the first 3 seconds>", "why_it_works": "<one sentence>"}},
  "pacing": {{"cuts_per_10s": <an estimate>, "average_shot_seconds": <an estimate>, "feel": "<calm | steady | fast | frantic>"}},
  "captions": {{"present": <true or false>, "style": "<position, size, colour, font feel, animation, highlight method>"}},
  "graphics": ["<each overlay, card, chart or effect, with when it appears and what it does>"],
  "sound": {{"music": "<none | quiet bed | prominent>", "effects": "<none | few | many>", "voice": "<clean | room noise | enhanced>"}},
  "cta": "<how it ends and what it asks the viewer to do>",
  "palette": ["<up to 4 plain colour names>"],
  "works_because": ["<up to 3 short reasons, from what you saw>"],
  "borrow": [{{"name": "<two to five words>", "what": "<one sentence>", "tags": ["<one to three of: hook, story, pacing, cut, captions, typography, motion, transition, graphics, overlay, cta, color, sound, music, broll, framing, layout, emphasis, proof, format, social>"]}}],
  "do_not_copy": ["<anything risky for a financial advertiser: guaranteed results, unrealistic claims, fake testimonials, misleading charts>"]
}}

At most 6 items in "borrow".
