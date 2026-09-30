# Railway configuration for this repo

`railway.ts` describes everything in one Railway environment of the project **App · Video Agent**: the private
Postgres, the `media` bucket and the `worker` service (later also the web app). Railway reads this file only when
someone runs the CLI; it is not read during deploys.

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
`worker/` once the `w0-worker-cloud` branch is merged.

Secrets never go in this file. `OPENROUTER_VIDEO_AGENT_KEY` is pasted into the worker's variables in the Railway
dashboard (worker service → Variables), and `preserve()` in `railway.ts` keeps whatever is set there.

`npm run railway -- ...` runs the repo-local CLI (`scripts/railway.mjs`), which works around a Windows-only path
problem in the IaC SDK's version check. A globally installed `railway` works the same way on macOS and Linux.
