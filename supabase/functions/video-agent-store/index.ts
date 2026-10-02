// The video agent's storage gate (Supabase Edge Function).
//
// The worker never holds a Supabase service key. It holds one shared token, and this function (which Supabase
// gives the service key at run time) lets that token do exactly four things, only inside the private bucket
// "video-agent" and only under a fixed set of folders:
//   GET  ?op=list&prefix=memory/          list objects under a prefix
//   POST ?op=sign-put  {path, size}       a one-time upload URL (the file goes straight to Storage, not through here)
//   POST ?op=sign-get  {path}             a download URL valid for 10 minutes
//   POST ?op=delete    {path}             delete one object
// Small text can also move through the function: PUT/GET ?op=text&path=...  (up to 1 MB).
//
// The token is checked by its SHA-256 hash (the hash below is not a secret; the token is 43 random characters).
// To rotate: generate a new token, put its hash here, redeploy, paste the new token into Railway.
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.45.4";

const BUCKET = "video-agent";
const TOKEN_SHA256 = "5973111e237453c8a339e7c84d77bbbe220ce43d41eb36c1b89a4e92270f8730";
const FOLDERS = ["skills/", "memory/", "references/", "assets/", "exports/"];
const MAX_PATH = 300;
const MAX_TEXT_BYTES = 1_000_000;
const MAX_FILE_BYTES = 50 * 1024 * 1024;
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

/** A path inside one of the allowed folders, or null. No '..', no leading slash, no odd characters. */
function cleanPath(p: unknown): string | null {
  if (typeof p !== "string" || p.length === 0 || p.length > MAX_PATH || !PATH_OK.test(p)) return null;
  if (p.split("/").some((part) => part === "" || part === "." || part === "..")) return null;
  return FOLDERS.some((f) => p.startsWith(f)) ? p : null;
}

function cleanPrefix(p: unknown): string | null {
  const text = typeof p === "string" ? p : "";
  if (text === "") return "";
  const trimmed = text.endsWith("/") ? text.slice(0, -1) : text;
  const path = cleanPath(trimmed + "/x");
  return path ? trimmed + "/" : null;
}

Deno.serve(async (req: Request) => {
  const token = req.headers.get("x-agent-token") ?? "";
  if (!sameText(await sha256Hex(token), TOKEN_SHA256)) return json({ error: "not allowed" }, 401);
  const url = new URL(req.url);
  const op = url.searchParams.get("op") ?? "";
  try {
    if (op === "list" && req.method === "GET") {
      const prefix = cleanPrefix(url.searchParams.get("prefix"));
      if (prefix === null) return json({ error: "bad prefix" }, 400);
      const folders = prefix === "" ? FOLDERS.map((f) => f.slice(0, -1)) : [prefix.slice(0, -1)];
      const out: { path: string; size: number | null; updated: string | null }[] = [];
      const walk = async (dir: string, depth: number) => {
        const { data, error } = await admin.storage.from(BUCKET).list(dir, { limit: 1000, sortBy: { column: "name", order: "asc" } });
        if (error) throw error;
        for (const item of data ?? []) {
          const path = dir ? `${dir}/${item.name}` : item.name;
          if (item.id === null && depth < 4) await walk(path, depth + 1);        // a folder
          else if (item.id !== null) out.push({ path, size: item.metadata?.size ?? null, updated: item.updated_at ?? null });
          if (out.length >= 2000) return;
        }
      };
      for (const f of folders) await walk(f, 0);
      return json({ objects: out });
    }
    if (op === "sign-put" && req.method === "POST") {
      const body = await req.json();
      const path = cleanPath(body?.path);
      if (!path) return json({ error: "bad path" }, 400);
      if (typeof body?.size === "number" && body.size > MAX_FILE_BYTES) return json({ error: "file too large" }, 413);
      const { data, error } = await admin.storage.from(BUCKET).createSignedUploadUrl(path, { upsert: true });
      if (error) throw error;
      return json({ path, url: data.signedUrl, token: data.token });
    }
    if (op === "sign-get" && req.method === "POST") {
      const path = cleanPath((await req.json())?.path);
      if (!path) return json({ error: "bad path" }, 400);
      const { data, error } = await admin.storage.from(BUCKET).createSignedUrl(path, 600);
      if (error) return json({ error: "not found" }, 404);
      return json({ path, url: data.signedUrl, expires_in: 600 });
    }
    if (op === "delete" && req.method === "POST") {
      const path = cleanPath((await req.json())?.path);
      if (!path) return json({ error: "bad path" }, 400);
      const { error } = await admin.storage.from(BUCKET).remove([path]);
      if (error) throw error;
      return json({ deleted: path });
    }
    if (op === "text") {
      const path = cleanPath(url.searchParams.get("path"));
      if (!path) return json({ error: "bad path" }, 400);
      if (req.method === "PUT") {
        const bytes = new Uint8Array(await req.arrayBuffer());
        if (bytes.byteLength > MAX_TEXT_BYTES) return json({ error: "too large for text; use sign-put" }, 413);
        const { error } = await admin.storage.from(BUCKET).upload(path, bytes, {
          upsert: true, contentType: req.headers.get("content-type") ?? "text/plain",
        });
        if (error) throw error;
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
