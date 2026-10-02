// The video agent's storage gate (Supabase Edge Function).
//
// The worker never holds a Supabase key. Each Railway environment holds its OWN shared token, and this function (which Supabase
// gives the service key at run time) lets that token do a few things, only inside the private bucket "video-agent":
//   GET  ?op=list&prefix=memory/          list objects (any environment's, read-only for the others)
//   POST ?op=sign-put  {path, size}       a one-time upload URL (the file goes straight to Storage, not through here)
//   POST ?op=sign-get  {path}             a download URL valid for 2 minutes
//   POST ?op=delete    {path}             delete one object (only under exports/ or a _selftest folder)
//   PUT/GET ?op=text&path=...             small text through the function (up to 1 MB)
// Paths look like <folder>/<environment>/...  with folder one of skills, memory, references, assets, exports.
// Rules:
//   * a token may WRITE only under <folder>/<its own environment>/ (staging cannot touch production's files);
//   * writes never replace an existing object, except under exports/ and _selftest/ folders (history is add-only);
//   * deletes are allowed only under exports/ and _selftest/ folders;
//   * nothing is stored once the bucket holds MAX_BUCKET_BYTES.
// Tokens are checked by SHA-256 hash (the hashes below are not secrets; each token is 43 random characters). To rotate a token:
// add its new hash next to the old one, redeploy, paste the new token into Railway, then remove the old hash and redeploy.
import { createClient } from "npm:@supabase/supabase-js@2.45.4";

const BUCKET = "video-agent";
const TOKENS: Record<string, string> = {
  "e0ac939e377d2757a53c2ed30650472d0d611e411301b424deb8d8e5f73da4dd": "staging",
  "fe8895160516a7a703923459cddd462ff6b3f4ded669e17cb5005bb42a60cdea": "production",
};
const FOLDERS = ["skills/", "memory/", "references/", "assets/", "exports/"];
const MAX_PATH = 300;
const MAX_SEGMENTS = 6;
const MAX_TEXT_BYTES = 1_000_000;
const MAX_FILE_BYTES = 50 * 1024 * 1024;
const MAX_BUCKET_BYTES = 10 * 1024 * 1024 * 1024;
const MAX_LIST_CALLS = 200;
const MAX_LIST_RESULTS = 2000;
const PATH_OK = /^[A-Za-z0-9][A-Za-z0-9._\-\/ ]*$/;

const admin = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!, {
  auth: { persistSession: false, autoRefreshToken: false },
});

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", "cache-control": "no-store" } });

async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest)).map((b) => b.toString(16).padStart(2, "0")).join("");
}

function sameText(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

/** Which environment this token belongs to, or null. Compares against every hash without stopping at the first. */
async function environmentOf(token: string): Promise<string | null> {
  const hash = await sha256Hex(token);
  let found: string | null = null;
  for (const [known, env] of Object.entries(TOKENS)) if (sameText(hash, known)) found = env;
  return found;
}

/** A path inside one of the allowed folders, or null. No '..', no leading slash, no odd characters, at most six parts. */
function cleanPath(p: unknown): string | null {
  if (typeof p !== "string" || p.length === 0 || p.length > MAX_PATH || !PATH_OK.test(p)) return null;
  const parts = p.split("/");
  if (parts.length > MAX_SEGMENTS || parts.some((part) => part === "" || part === "." || part === "..")) return null;
  return FOLDERS.some((f) => p.startsWith(f)) ? p : null;
}

function cleanPrefix(p: unknown): string | null {
  const text = typeof p === "string" ? p : "";
  if (text === "") return "";
  const trimmed = text.endsWith("/") ? text.slice(0, -1) : text;
  const path = cleanPath(trimmed + "/x");
  return path ? trimmed + "/" : null;
}

const isSelftest = (path: string) => path.split("/")[2] === "_selftest";
/** Writing is allowed only under <folder>/<own environment>/<something>. */
const mayWrite = (env: string, path: string) => path.split("/").length >= 3 && path.split("/")[1] === env;
/** Replacing an existing object is allowed only for exports (derived, rebuilt) and selftest files. */
const mayReplace = (path: string) => path.startsWith("exports/") || isSelftest(path);
const mayDelete = mayReplace;

async function bucketBytes(): Promise<number> {
  const { data, error } = await admin.rpc("video_agent_bucket_bytes");
  if (error) throw error;
  return Number(data ?? 0);
}

async function readJson(req: Request): Promise<Record<string, unknown> | null> {
  try {
    const body = await req.json();
    return body && typeof body === "object" ? body : null;
  } catch {
    return null;
  }
}

Deno.serve(async (req: Request) => {
  const env = await environmentOf(req.headers.get("x-agent-token") ?? "");
  if (!env) {
    console.warn("video-agent-store: rejected call");
    return json({ error: "not allowed" }, 401);
  }
  const url = new URL(req.url);
  const op = url.searchParams.get("op") ?? "";
  try {
    if (op === "list" && req.method === "GET") {
      const prefix = cleanPrefix(url.searchParams.get("prefix"));
      if (prefix === null) return json({ error: "bad prefix" }, 400);
      const roots = prefix === "" ? FOLDERS.map((f) => f.slice(0, -1)) : [prefix.slice(0, -1)];
      const out: { path: string; size: number | null; updated: string | null }[] = [];
      let calls = 0;
      let truncated = false;
      const walk = async (dir: string, depth: number) => {
        for (let offset = 0; ; offset += 100) {
          if (calls++ >= MAX_LIST_CALLS || out.length >= MAX_LIST_RESULTS) { truncated = true; return; }
          const { data, error } = await admin.storage.from(BUCKET).list(dir, { limit: 100, offset, sortBy: { column: "name", order: "asc" } });
          if (error) throw error;
          for (const item of data ?? []) {
            const path = `${dir}/${item.name}`;
            if (item.id === null) { if (depth < MAX_SEGMENTS - 1) await walk(path, depth + 1); }
            else out.push({ path, size: item.metadata?.size ?? null, updated: item.updated_at ?? null });
          }
          if ((data ?? []).length < 100) return;
        }
      };
      for (const root of roots) await walk(root, 0);
      return json({ objects: out, truncated });
    }
    if (op === "sign-put" && req.method === "POST") {
      const body = await readJson(req);
      if (!body) return json({ error: "bad request" }, 400);
      const path = cleanPath(body.path);
      if (!path) return json({ error: "bad path" }, 400);
      if (!mayWrite(env, path)) return json({ error: "this token may only write under <folder>/" + env + "/" }, 403);
      const size = typeof body.size === "number" ? body.size : MAX_FILE_BYTES;
      if (size > MAX_FILE_BYTES) return json({ error: "file too large" }, 413);
      if ((await bucketBytes()) + size > MAX_BUCKET_BYTES) return json({ error: "storage is full" }, 507);
      const { data, error } = await admin.storage.from(BUCKET).createSignedUploadUrl(path, { upsert: mayReplace(path) });
      if (error) return json({ error: "already exists" }, 409);
      return json({ path, url: data.signedUrl, token: data.token });
    }
    if (op === "sign-get" && req.method === "POST") {
      const body = await readJson(req);
      const path = cleanPath(body?.path);
      if (!path) return json({ error: "bad path" }, 400);
      const { data, error } = await admin.storage.from(BUCKET).createSignedUrl(path, 120);
      if (error) return json({ error: "not found" }, 404);
      return json({ path, url: data.signedUrl, expires_in: 120 });
    }
    if (op === "delete" && req.method === "POST") {
      const body = await readJson(req);
      const path = cleanPath(body?.path);
      if (!path) return json({ error: "bad path" }, 400);
      if (!mayWrite(env, path) || !mayDelete(path)) return json({ error: "deleting is limited to your own exports/ and _selftest/ files" }, 403);
      const { error } = await admin.storage.from(BUCKET).remove([path]);
      if (error) throw error;
      return json({ deleted: path });
    }
    if (op === "text") {
      const path = cleanPath(url.searchParams.get("path"));
      if (!path) return json({ error: "bad path" }, 400);
      if (req.method === "PUT") {
        if (!mayWrite(env, path)) return json({ error: "this token may only write under <folder>/" + env + "/" }, 403);
        const bytes = new Uint8Array(await req.arrayBuffer());
        if (bytes.byteLength > MAX_TEXT_BYTES) return json({ error: "too large for text; use sign-put" }, 413);
        if ((await bucketBytes()) + bytes.byteLength > MAX_BUCKET_BYTES) return json({ error: "storage is full" }, 507);
        const { error } = await admin.storage.from(BUCKET).upload(path, bytes, {
          upsert: mayReplace(path), contentType: req.headers.get("content-type") ?? "text/plain",
        });
        if (error) return json({ error: "already exists" }, 409);
        return json({ saved: path, bytes: bytes.byteLength });
      }
      if (req.method === "GET") {
        const { data, error } = await admin.storage.from(BUCKET).download(path);
        if (error || !data) return json({ error: "not found" }, 404);
        return new Response(data, { headers: { "content-type": data.type || "application/octet-stream", "cache-control": "no-store" } });
      }
    }
    return json({ error: "unknown operation" }, 400);
  } catch (err) {
    console.error("video-agent-store", op, String(err).slice(0, 300));
    return json({ error: "storage error" }, 502);
  }
});
