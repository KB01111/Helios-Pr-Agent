// Helios PR-Agent edge worker — free-tier cloud fallback for the desktop daemon.
//
// GitHub -> this worker -> FastAPI backend (/webhooks/github)
//
//  * Always on, even when the desktop is asleep and the FastAPI free tier is cold:
//    it verifies the webhook signature and makes one delivery attempt (GitHub gives
//    up after 10 s). A 2xx means delivered or safely parked; if the backend is down
//    and nothing could be parked, it answers 502 so GitHub shows a failed delivery.
//  * The backend decides who handles the PR: if the desktop daemon has a fresh
//    heartbeat it defers the event to it, otherwise the cloud decider acts
//    immediately. Idempotency on (repo, pr_number, head_sha) lives in the
//    backend, so retries and replays here are always safe.
//  * A cron trigger (every minute) wakes the backend and calls /decisions/sweep,
//    which decides events the desktop did not pick up within the grace period.
//    That is the "desktop went to sleep" fallback.
//  * Optional KV namespace `PENDING` parks deliveries the backend could not take
//    (down / cold-start timeout) *before* the worker acks, and the cron replays them.
//    Without it, an outage is a visible 502 and GitHub's "Redeliver" is the recovery.
//
// Config (wrangler.toml / `wrangler secret put`):
//   BACKEND_URL             https://… base URL of the FastAPI backend   (var)
//   GITHUB_WEBHOOK_SECRET   same secret as the repo webhook             (secret)
//   GITHUB_APP_WEBHOOK_SECRET  the GitHub App's webhook secret          (secret, optional)
//   BACKEND_API_TOKEN       Bearer token if the backend sets one        (secret, optional)
//   PENDING                 KV binding for parked deliveries            (optional)

// Events the backend acts on. pull_request commit actions drive reviews; the rest feed
// the per-PR activity log (reviews, threads, CI results) used for merge readiness, PR
// chat/memory learning, the GitHub App installation cache and config invalidation.
// Anything else is acked here.
export const FORWARDED_EVENTS = new Set([
  "pull_request",
  "issue_comment",
  "pull_request_review",
  "pull_request_review_comment",
  "pull_request_review_thread",
  "check_suite",
  "check_run",
  "status",
  "push",
  "installation",
  "installation_repositories",
]);
// Conversational events from bots are dropped (no reply loops); CI results are not,
// because check suites and statuses are usually reported by bots.
const BOT_SKIPPED_EVENTS = new Set(["issue_comment", "pull_request_review", "pull_request_review_comment"]);
const MAX_BODY_BYTES = 2 * 1024 * 1024;
// GitHub gives up on a webhook after 10 s, so the single inline attempt must finish well inside that.
const FORWARD_TIMEOUT_MS = 7000;
const KV_REPLAY_EVERY_MIN = 5; // KV free tier allows ~1000 list() calls/day
const KV_REPLAY_BATCH = 10; // stay well inside the 50-subrequest limit
// The free tier caps KV *writes* at 1000/day, so at most ~1000 deliveries can be parked per
// 24 h TTL: one list page normally holds them all. A second page is only read when the first
// is full, which keeps the 5-minute cron at ~288 list() calls/day (free budget: 1000).
const KV_REPLAY_MAX_PAGES = 2;
const KV_REPLAY_WINDOW = 1000; // keys listed per replay (one list() call, the KV maximum)

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
  const trimmed = typeof secret === "string" ? secret.trim() : "";
  if (!trimmed || typeof header !== "string" || !header.startsWith("sha256=")) return false;
  const sig = hexToBytes(header.slice("sha256=".length));
  if (!sig || sig.byteLength !== 32) return false;
  const key = await crypto.subtle.importKey("raw", enc.encode(trimmed), { name: "HMAC", hash: "SHA-256" }, false, ["verify"]);
  return crypto.subtle.verify("HMAC", key, sig, rawBody);
}

/**
 * Verify X-Hub-Signature-256 against both GITHUB_WEBHOOK_SECRET and
 * GITHUB_APP_WEBHOOK_SECRET without short-circuiting so timing does not leak
 * which secret matched.
 */
export async function verifyAnySignature(env, rawBody, header) {
  const repoSecret = typeof env.GITHUB_WEBHOOK_SECRET === "string" ? env.GITHUB_WEBHOOK_SECRET.trim() : "";
  const appSecret = typeof env.GITHUB_APP_WEBHOOK_SECRET === "string" ? env.GITHUB_APP_WEBHOOK_SECRET.trim() : "";
  const [repoOk, appOk] = await Promise.all([
    repoSecret ? verifySignature(repoSecret, rawBody, header) : Promise.resolve(false),
    appSecret ? verifySignature(appSecret, rawBody, header) : Promise.resolve(false),
  ]);
  return Boolean(repoOk | appOk);
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
      signal: AbortSignal.timeout(Number(env.FORWARD_TIMEOUT_MS) || FORWARD_TIMEOUT_MS),
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

/**
 * One delivery attempt. On a retryable failure the delivery is parked in KV *before* we
 * answer GitHub, so a successful ack always means "delivered or safely parked"; the cron
 * replays parked items. Without KV a failure is reported ("lost") so the caller can fail
 * the webhook visibly. Returns "delivered" | "dropped" | "parked" | "lost".
 */
export async function deliver(env, item) {
  const result = await forward(env, item);
  if (result === "ok") return "delivered";
  if (result === "drop") return "dropped";
  if (!env.PENDING) {
    console.error(`delivery ${item.delivery} not accepted and no PENDING KV bound`);
    return "lost";
  }
  try {
    await env.PENDING.put(`pending:${item.delivery}`, JSON.stringify(item), { expirationTtl: 24 * 3600 });
    return "parked";
  } catch (err) {
    console.error(`could not park ${item.delivery}: ${err && err.name}`);
    return "lost";
  }
}

/** Read at most `max` bytes, cancelling the stream as soon as the limit is exceeded. */
async function readBounded(request, max) {
  if (!request.body) return new Uint8Array(0);
  const reader = request.body.getReader();
  const chunks = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > max) {
      await reader.cancel();
      return null;
    }
    chunks.push(value);
  }
  const out = new Uint8Array(total);
  let offset = 0;
  for (const c of chunks) {
    out.set(c, offset);
    offset += c.byteLength;
  }
  return out;
}

/** Why an allow-listed event is not worth forwarding, or null to forward it. */
export function skipReason(event, payload) {
  if (!payload || typeof payload !== "object") return "no-payload";
  const action = typeof payload.action === "string" ? payload.action : "";
  if (BOT_SKIPPED_EVENTS.has(event) && payload.sender && payload.sender.type === "Bot") return "bot";
  switch (event) {
    case "pull_request":
      return payload.pull_request && typeof payload.pull_request === "object" ? null : "no-pull_request";
    case "issue_comment":
      return payload.issue && payload.issue.pull_request ? null : "not-a-pull-request";
    case "check_suite":
    case "check_run":
      return action === "completed" ? null : action || "no-action";
    case "push": {
      const branch = payload.repository && payload.repository.default_branch;
      return branch && payload.ref === `refs/heads/${branch}` ? null : "not-default-branch";
    }
    default:
      return null;
  }
}

export async function handleWebhook(request, env, _ctx) {
  const hasSecret = Boolean(
    (typeof env.GITHUB_WEBHOOK_SECRET === "string" && env.GITHUB_WEBHOOK_SECRET.trim()) ||
    (typeof env.GITHUB_APP_WEBHOOK_SECRET === "string" && env.GITHUB_APP_WEBHOOK_SECRET.trim())
  );
  if (!hasSecret || !backendBase(env)) {
    return json({ error: "worker not configured" }, 500);
  }
  const declared = Number(request.headers.get("content-length") || 0);
  if (declared > MAX_BODY_BYTES) return json({ error: "payload too large" }, 413);
  const raw = await readBounded(request, MAX_BODY_BYTES);
  if (raw === null) return json({ error: "payload too large" }, 413);

  const signature = request.headers.get("x-hub-signature-256") || "";
  // A GitHub App signs with GITHUB_APP_WEBHOOK_SECRET; repo webhooks with GITHUB_WEBHOOK_SECRET.
  if (!(await verifyAnySignature(env, raw, signature))) {
    return json({ error: "bad signature" }, 401);
  }
  const event = request.headers.get("x-github-event") || "";
  if (event === "ping") return json({ pong: true });
  if (!FORWARDED_EVENTS.has(event)) return json({ skipped: event || "no-event" }, 202);

  const bodyText = new TextDecoder().decode(raw);
  let payload;
  try {
    payload = JSON.parse(bodyText);
  } catch {
    return json({ error: "invalid JSON" }, 400);
  }
  const skip = skipReason(event, payload);
  if (skip) return json({ skipped: skip }, 202);

  const delivery = request.headers.get("x-github-delivery") || crypto.randomUUID();
  const item = { body: bodyText, event, delivery, signature };
  // Delivered or safely parked => 202. Otherwise fail the webhook so GitHub shows a failed
  // delivery that can be redelivered, instead of a silent 202 for work that was lost.
  if ((await deliver(env, item)) === "lost") return json({ error: "backend unavailable", delivery }, 502);
  return json({ accepted: true, delivery }, 202);
}

/**
 * Replay parked deliveries (throttled to protect the KV free tier). Each run takes the
 * next window of KV_REPLAY_BATCH keys (rotating with the clock), so a backlog larger than
 * one batch is worked through instead of retrying the same first keys until they expire.
 */
export async function replayParked(env, slot = 0) {
  if (!env.PENDING) return { replayed: 0 };
  // Follow the cursor so a backlog larger than one page is still reachable (bounded: each
  // extra page is another list() against the free-tier budget).
  const names = [];
  let cursor;
  for (let page = 0; page < KV_REPLAY_MAX_PAGES; page++) {
    const listed = await env.PENDING.list({ prefix: "pending:", limit: KV_REPLAY_WINDOW, cursor });
    names.push(...listed.keys.map((k) => k.name));
    if (listed.list_complete !== false || !listed.cursor) break;
    cursor = listed.cursor;
  }
  if (!names.length) return { replayed: 0 };
  const start = (slot * KV_REPLAY_BATCH) % names.length;
  let replayed = 0;
  for (let i = 0; i < Math.min(KV_REPLAY_BATCH, names.length); i++) {
    const name = names[(start + i) % names.length];
    const stored = await env.PENDING.get(name, "json");
    if (!stored) continue;
    const result = await forward(env, stored);
    if (result === "retry") continue; // stays parked until it succeeds or its 24h TTL ends
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
    const resp = await fetch(`${base}/decisions/sweep`, {
      method: "POST",
      headers,
      signal: AbortSignal.timeout(Number(env.SWEEP_TIMEOUT_MS) || 15000),
    });
    if (!resp.ok) console.error(`sweep failed: HTTP ${resp.status}`);
    return { swept: resp.ok };
  } catch (err) {
    console.error(`sweep unreachable: ${err && err.name}`);
    return { swept: false };
  }
}

export async function runCron(env, scheduledTime = Date.now()) {
  const minute = Math.floor(scheduledTime / 60000);
  const parked =
    minute % KV_REPLAY_EVERY_MIN === 0 ? await replayParked(env, minute / KV_REPLAY_EVERY_MIN) : { replayed: 0 };
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
