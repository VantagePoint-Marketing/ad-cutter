# Railway configuration for this repo

`railway.ts` describes everything in one Railway environment of the project **App · Video Agent**: the private
Postgres, the `media` bucket, the `worker` service and the `web` service (the page). Railway reads this file only
when someone runs the CLI; it is not read during deploys.

From the repo root, after `npm install` (once) and a signed-in CLI (`npm run railway -- login`, once per PC):

```bash
npm run railway -- link          # once per PC: pick "App · Video Agent" and the environment (staging or production)
npm run railway -- config plan   # read-only preview of what would change
npm run railway -- config apply  # shows the plan again, asks "yes", then creates or updates the resources
```

Removing a resource from `railway.ts` deletes it on the next apply, and so does leaving out a variable on a service
this file manages (only `preserve()` keeps a hand-set value), so read the plan lines marked destructive. Any extra
variable added by hand in the dashboard must also be declared here, or the next apply removes it.

Production is not ready for an apply yet: the file points production at branch `main`, which will only contain
`worker/` and `web/` once the `w0-worker-cloud` branch is merged.

Secrets never go in this file. Two values are pasted into the Railway dashboard by a person and kept by
`preserve()`:

- `OPENROUTER_VIDEO_AGENT_KEY` on the **worker** (worker service → Variables).
- `GEMINI_API_KEY` and `YOUTUBE_API_KEY` on the **worker**: the reference library (Google AI Studio key on a
  project without billing, so it stays on the free tier; YouTube Data API key). `LIBRARY_ENABLED` is set by this
  file ("1" on staging, "0" on production).
- `APP_LINK_TOKEN` on the **web** service: the secret part of the page's link, `https://<domain>/<token>/`.
  16 or more letters, digits, `-` or `_`. Until it is set, the service runs but every page is a 404 and
  `/healthz` says why.

The web service's public address is declared under `networking.serviceDomains` (Railway refuses the simpler
`domains:` form for `*.up.railway.app` names). The page reads its own address from `RAILWAY_PUBLIC_DOMAIN` and sets
the bucket's CORS rule to it at start-up, so browsers can upload clips straight into the bucket.

`npm run railway -- ...` runs the repo-local CLI (`scripts/railway.mjs`), which works around a Windows-only path
problem in the IaC SDK's version check. A globally installed `railway` works the same way on macOS and Linux.
