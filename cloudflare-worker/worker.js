// Helios PR-Agent edge worker — free-tier cloud fallback for the desktop daemon.
//
// GitHub -> this worker -> FastAPI backend (/webhooks/github)
//
//  * Always on, even when the desktop is asleep and the FastAPI free tier is cold:
//    it verifies the webhook signature, answers GitHub in milliseconds, and
//    delivers to the backend in the background with retries.
//  * The backend decides who handles the PR: if the desktop daemon has a fresh
//    heartbeat it defers the event to it, otherwise the cloud decider acts
//    immediately. Idempotency on (repo, pr_number, head_sha) lives in the
//    backend, so retries and replays here are always safe.
//  * A cron trigger (every minute) wakes the backend and calls /decisions/sweep,
//    which decides events the desktop did not pick up within the grace period.
//    That is the "desktop went to sleep" fallback.
//  * Optional KV namespace `PENDING` parks deliveries the backend could not take
//    (down / cold-start timeout) and replays them from the cron. Without it, the
//    in-request retries are all you get.
//
// Config (wrangler.toml / `wrangler secret put`):
//   BACKEND_URL             https://… base URL of the FastAPI backend   (var)
//   GITHUB_WEBHOOK_SECRET   same secret as the repo webhook             (secret)
//   BACKEND_API_TOKEN       Bearer token if the backend sets one        (secret, optional)
//   PENDING                 KV binding for parked deliveries            (optional)

const PR_ACTIONS = new Set(["opened", "reopened", "synchronize", "labeled"]);
const DEFAULT_DELAYS_MS = [0, 1000, 3000];
const MAX_BODY_BYTES = 2 * 1024 * 1024;
const MAX_PARKED_ATTEMPTS = 60; // ~1 hour of per-minute cron replays
const KV_REPLAY_EVERY_MIN = 5; // KV free tier allows ~1000 list() calls/day
const KV_REPLAY_BATCH = 10; // stay well inside the 50-subrequest limit

const enc = new TextEncoder();
const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json" } });

function hexToBytes(hex) {
  if (typeof hex !== "string" || hex.length % 2 !== 0 || !/^[0-9a-f]+$/i.test(hex)) return null;
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  return out;
}

/** Constant-time check of X-Hub-Signature-256 (WebCrypto verify is constant-time). */
export async function verifySignature(secret, rawBody, header) {
  if (!secret || typeof header !== "string" || !header.startsWith("sha256=")) return false;
  const sig = hexToBytes(header.slice("sha256=".length));
  if (!sig) return false;
  const key = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["verify"]);
  return crypto.subtle.verify("HMAC", key, sig, rawBody);
}

function backendBase(env) {
  const raw = String(env.BACKEND_URL || "").trim().replace(/\/+$/, "");
  let url;
  try {
    url = new URL(raw);
  } catch {
    return null;
  }
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
  if (url.protocol !== "https:" && !(local && url.protocol === "http:")) return null;
  return raw;
}

function delaysFor(env) {
  // Test hook; production uses the defaults.
  if (!env.RETRY_DELAYS_MS) return DEFAULT_DELAYS_MS;
  return String(env.RETRY_DELAYS_MS).split(",").map((n) => Math.max(0, Number(n) || 0));
}

const sleep = (ms) => (ms > 0 ? new Promise((r) => setTimeout(r, ms)) : Promise.resolve());

/** One delivery attempt. Returns "ok" | "retry" | "drop". */
export async function forward(env, item) {
  const base = backendBase(env);
  if (!base) {
    console.error("BACKEND_URL missing or not https");
    return "drop";
  }
  try {
    const resp = await fetch(`${base}/webhooks/github`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "user-agent": "helios-edge-worker",
        "x-github-event": item.event,
        "x-github-delivery": item.delivery,
        // Forwarded verbatim: the backend re-verifies against the same secret.
        "x-hub-signature-256": item.signature,
      },
      body: item.body,
    });
    if (resp.ok) return "ok";
    // 409 = another handler holds the claim right now; 5xx = cold/failing backend.
    if (resp.status === 409 || resp.status >= 500 || resp.status === 429) return "retry";
    console.error(`backend rejected delivery ${item.delivery}: HTTP ${resp.status}`);
    return "drop";
  } catch (err) {
    console.error(`backend unreachable for ${item.delivery}: ${err && err.name}`);
    return "retry";
  }
}

/** Deliver with in-request retries; park in KV when the backend stays unavailable. */
export async function deliver(env, item) {
  for (const delay of delaysFor(env)) {
    await sleep(delay);
    const result = await forward(env, item);
    if (result === "ok") return "delivered";
    if (result === "drop") return "dropped";
  }
  if (env.PENDING) {
    await env.PENDING.put(`pending:${item.delivery}`, JSON.stringify({ ...item, attempts: 0 }), {
      expirationTtl: 24 * 3600,
    });
    return "parked";
  }
  console.error(`delivery ${item.delivery} not accepted and no PENDING KV bound`);
  return "lost";
}

export async function handleWebhook(request, env, ctx) {
  if (!env.GITHUB_WEBHOOK_SECRET || !backendBase(env)) {
    return json({ error: "worker not configured" }, 500);
  }
  const declared = Number(request.headers.get("content-length") || 0);
  if (declared > MAX_BODY_BYTES) return json({ error: "payload too large" }, 413);
  const raw = new Uint8Array(await request.arrayBuffer());
  if (raw.byteLength > MAX_BODY_BYTES) return json({ error: "payload too large" }, 413);

  const signature = request.headers.get("x-hub-signature-256") || "";
  if (!(await verifySignature(env.GITHUB_WEBHOOK_SECRET, raw, signature))) {
    return json({ error: "bad signature" }, 401);
  }
  const event = request.headers.get("x-github-event") || "";
  if (event === "ping") return json({ pong: true });
  if (event !== "pull_request") return json({ skipped: event || "no-event" }, 202);

  let payload;
  try {
    payload = JSON.parse(new TextDecoder().decode(raw));
  } catch {
    return json({ error: "invalid JSON" }, 400);
  }
  const pr = payload && payload.pull_request;
  const action = payload && payload.action;
  if (!pr || typeof pr !== "object" || !PR_ACTIONS.has(action)) return json({ skipped: action || "no-pull_request" }, 202);
  if (payload.sender && payload.sender.type === "Bot") return json({ skipped: "bot" }, 202);

  const delivery = request.headers.get("x-github-delivery") || crypto.randomUUID();
  const item = { body: new TextDecoder().decode(raw), event, delivery, signature };
  ctx.waitUntil(deliver(env, item));
  return json({ accepted: true, delivery }, 202);
}

/** Replay parked deliveries (throttled to protect the KV free tier). */
export async function replayParked(env) {
  if (!env.PENDING) return { replayed: 0 };
  const listed = await env.PENDING.list({ prefix: "pending:", limit: KV_REPLAY_BATCH });
  let replayed = 0;
  for (const { name } of listed.keys) {
    const stored = await env.PENDING.get(name, "json");
    if (!stored) continue;
    const result = await forward(env, stored);
    if (result === "retry" && (stored.attempts || 0) + 1 < MAX_PARKED_ATTEMPTS) {
      await env.PENDING.put(name, JSON.stringify({ ...stored, attempts: (stored.attempts || 0) + 1 }), {
        expirationTtl: 24 * 3600,
      });
      continue;
    }
    if (result === "retry") console.error(`giving up on parked delivery ${stored.delivery}`);
    await env.PENDING.delete(name);
    replayed += result === "ok" ? 1 : 0;
  }
  return { replayed };
}

/** Cron: wake the backend and let it decide events the desktop did not claim in time. */
export async function sweepBackend(env) {
  const base = backendBase(env);
  if (!base) return { swept: false };
  const headers = { "user-agent": "helios-edge-worker" };
  if (env.BACKEND_API_TOKEN) headers.authorization = `Bearer ${env.BACKEND_API_TOKEN}`;
  try {
    const resp = await fetch(`${base}/decisions/sweep`, { method: "POST", headers });
    if (!resp.ok) console.error(`sweep failed: HTTP ${resp.status}`);
    return { swept: resp.ok };
  } catch (err) {
    console.error(`sweep unreachable: ${err && err.name}`);
    return { swept: false };
  }
}

export async function runCron(env, scheduledTime = Date.now()) {
  const minute = Math.floor(scheduledTime / 60000);
  const parked = minute % KV_REPLAY_EVERY_MIN === 0 ? await replayParked(env) : { replayed: 0 };
  const sweep = await sweepBackend(env);
  return { ...parked, ...sweep };
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/health") return json({ ok: true });
    if (request.method === "POST" && url.pathname === "/webhooks/github") return handleWebhook(request, env, ctx);
    return json({ error: "not found" }, 404);
  },
  async scheduled(event, env, ctx) {
    ctx.waitUntil(runCron(env, event.scheduledTime));
  },
};
