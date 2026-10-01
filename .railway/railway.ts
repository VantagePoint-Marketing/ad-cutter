// Railway Infrastructure as Code for the "App · Video Agent" project.
// One file describes an environment: `railway config plan` shows the changes, `railway config apply` makes them.
// Config as Code (railway.json) is deprecated on Railway and new services cannot use it, so this file is the
// single source of truth for service settings. Secrets are never written here: OPENROUTER_VIDEO_AGENT_KEY is
// pasted into Railway by a person and `preserve()` keeps whatever is set.
import { bucket, defineRailway, github, postgres, preserve, project, ref, service } from "railway/iac";

const REPO = "VantagePoint-Marketing/ad-cutter";
// Bucket region codes (sjc, iad, ams, sin) are not the same as deploy regions, and a bucket's region can't change
// later. Postgres and the worker are left on the workspace's default deploy region, which is Virginia today, the
// same place as this bucket. Check with `railway config plan` after the first apply: it should report no changes.
const BUCKET_REGION = "iad";

export default defineRailway((ctx) => {
  const production = ctx.environment === "production";

  // Private Postgres: no TCP proxy, reachable only on the private network (security design blocker B1).
  // Railway databases are private by default; after each apply, confirm no TCP proxy exists on it.
  const db = postgres("Postgres");

  // Raw uploads (uploads/<job id>/source) and finished ads (results/<job id>/...). See worker/storage.py.
  const media = bucket("media", { region: BUCKET_REGION });

  const worker = service("worker", {
    source: github(REPO, { branch: production ? "main" : "w0-worker-cloud" }),
    build: {
      builder: "DOCKERFILE",
      dockerfilePath: "worker/Dockerfile", // build context is the repo root
      watchPatterns: ["worker/**", "db/**"],
    },
    deploy: {
      preDeployCommand: ["python migrate.py"], // applies db/migrations/*.sql once, with the DB owner's connection
      startCommand: "python jobs.py",
      restartPolicyType: "ON_FAILURE",
      restartPolicyMaxRetries: 10,
    },
    env: {
      DATABASE_URL: db.env.DATABASE_URL, // the .railway.internal host
      BUCKET: ref(media, "BUCKET"),
      ENDPOINT: ref(media, "ENDPOINT"),
      REGION: ref(media, "REGION"),
      ACCESS_KEY_ID: ref(media, "ACCESS_KEY_ID"),
      SECRET_ACCESS_KEY: ref(media, "SECRET_ACCESS_KEY"),
      MONTHLY_BUDGET_USD: "90", // our own ledger cap; the OpenRouter key itself is limited to $100/month
      OPENROUTER_VIDEO_AGENT_KEY: preserve(), // set by hand in Railway, never in a file
    },
  });

  // The web page: one shared link (https://<domain>/<APP_LINK_TOKEN>/), no accounts. It uses the same Postgres and
  // bucket as the worker; the browser sends clips straight into the bucket through short-lived signed links, and the
  // page sets the bucket's CORS rule to its own address when it starts (RAILWAY_PUBLIC_DOMAIN).
  const web = service("web", {
    source: github(REPO, { branch: production ? "main" : "w0-worker-cloud" }),
    build: {
      builder: "DOCKERFILE",
      dockerfilePath: "web/Dockerfile", // build context is the repo root; the image also copies worker/storage.py
      watchPatterns: ["web/**", "worker/storage.py"],
    },
    deploy: {
      startCommand: "python app.py",
      healthcheckPath: "/healthz",
      restartPolicyType: "ON_FAILURE",
      restartPolicyMaxRetries: 10,
    },
    networking: {
      serviceDomains: { [production ? "video-agent.up.railway.app" : "video-agent-staging.up.railway.app"]: {} },
    },
    env: {
      DATABASE_URL: db.env.DATABASE_URL,
      BUCKET: ref(media, "BUCKET"),
      ENDPOINT: ref(media, "ENDPOINT"),
      REGION: ref(media, "REGION"),
      ACCESS_KEY_ID: ref(media, "ACCESS_KEY_ID"),
      SECRET_ACCESS_KEY: ref(media, "SECRET_ACCESS_KEY"),
      APP_LINK_TOKEN: preserve(), // the secret part of the link; set by hand in Railway, never in a file
    },
  });

  return project("App · Video Agent", {
    resources: [db, media, worker, web],
  });
});
