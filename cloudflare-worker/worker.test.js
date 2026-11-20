// Run with: node --test cloudflare-worker/
import test from "node:test";
import assert from "node:assert/strict";
import worker, { runCron, verifySignature, deliver, replayParked } from "./worker.js";

const SECRET = "whsec_test";
const baseEnv = { GITHUB_WEBHOOK_SECRET: SECRET, BACKEND_URL: "https://backend.example.com", RETRY_DELAYS_MS: "0,0,0" };

async function sign(body, secret = SECRET) {
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const mac = new Uint8Array(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(body)));
  return "sha256=" + [...mac].map((b) => b.toString(16).padStart(2, "0")).join("");
}

const prBody = (over = {}) =>
  JSON.stringify({
    action: "opened",
    pull_request: { number: 7, head: { sha: "a".repeat(40), ref: "feat" } },
    repository: { full_name: "o/r" },
    sender: { type: "User" },
    ...over,
  });

async function post(body, { event = "pull_request", signature, env = baseEnv, delivery = "d-1" } = {}) {
  const pending = [];
  const request = new Request("https://edge.example.com/webhooks/github", {
    method: "POST",
    body,
    headers: {
      "x-github-event": event,
      "x-github-delivery": delivery,
      "x-hub-signature-256": signature ?? (await sign(body)),
    },
  });
  const response = await worker.fetch(request, env, { waitUntil: (p) => pending.push(p) });
  await Promise.all(pending);
  return response;
}

function stubFetch(handler) {
  const calls = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    return handler(String(url), init, calls.length);
  };
  return { calls, restore: () => (globalThis.fetch = original) };
}

function fakeKv() {
  const store = new Map();
  return {
    store,
    writes: 0,
    lists: 0,
    put: async function (k, v) { this.writes++; store.set(k, v); },
    get: async (k, type) => (store.has(k) ? (type === "json" ? JSON.parse(store.get(k)) : store.get(k)) : null),
    delete: async (k) => void store.delete(k),
    list: async function ({ prefix, limit, cursor }) {
      this.lists++;
      const all = [...store.keys()].filter((k) => k.startsWith(prefix)).sort();
      const from = Number(cursor || 0);
      const page = all.slice(from, from + limit);
      const complete = from + limit >= all.length;
      return { keys: page.map((name) => ({ name })), list_complete: complete, cursor: complete ? undefined : String(from + limit) };
    },
  };
}

test("rejects a bad or missing signature without forwarding", async () => {
  const f = stubFetch(() => new Response("{}", { status: 202 }));
  try {
    assert.equal((await post(prBody(), { signature: "sha256=" + "0".repeat(64) })).status, 401);
    assert.equal((await post(prBody(), { signature: "nonsense" })).status, 401);
    assert.equal((await post(prBody(), { signature: await sign(prBody(), "other-secret") })).status, 401);
    assert.equal(f.calls.length, 0);
  } finally {
    f.restore();
  }
});

test("verifySignature is false for malformed headers", async () => {
  const raw = new TextEncoder().encode("x");
  assert.equal(await verifySignature(SECRET, raw, ""), false);
  assert.equal(await verifySignature(SECRET, raw, "sha256=zz"), false);
  assert.equal(await verifySignature("", raw, await sign("x")), false);
  assert.equal(await verifySignature(SECRET, raw, await sign("x")), true);
});

test("ping, unknown events, uninteresting actions and bot comments are acknowledged without forwarding", async () => {
  const f = stubFetch(() => new Response("{}", { status: 202 }));
  try {
    assert.equal((await post("{}", { event: "ping" })).status, 200);
    assert.deepEqual(await (await post("{}", { event: "star" })).json(), { skipped: "star" });
    const push = JSON.stringify({ ref: "refs/heads/feat", repository: { full_name: "o/r", default_branch: "main" } });
    assert.deepEqual(await (await post(push, { event: "push" })).json(), { skipped: "not-default-branch" });
    const issue = JSON.stringify({ action: "created", issue: { number: 3 }, sender: { type: "User" } });
    assert.deepEqual(await (await post(issue, { event: "issue_comment" })).json(), { skipped: "not-a-pull-request" });
    const botComment = JSON.stringify({ action: "created", issue: { number: 3, pull_request: {} }, sender: { type: "Bot" } });
    assert.deepEqual(await (await post(botComment, { event: "issue_comment" })).json(), { skipped: "bot" });
    const suite = JSON.stringify({ action: "requested", check_suite: {}, repository: { full_name: "o/r" } });
    assert.deepEqual(await (await post(suite, { event: "check_suite" })).json(), { skipped: "requested" });
    assert.equal((await post("not json")).status, 400);
    assert.equal(f.calls.length, 0);
  } finally {
    f.restore();
  }
});

test("activity events (closed PRs, bot PRs, reviews, CI, default-branch pushes) are forwarded", async () => {
  const f = stubFetch(() => new Response("{}", { status: 202 }));
  try {
    assert.equal((await post(prBody({ action: "closed" }), { delivery: "c1" })).status, 202);
    assert.equal((await post(prBody({ sender: { type: "Bot" } }), { delivery: "c2" })).status, 202);
    const review = JSON.stringify({ action: "submitted", pull_request: { number: 7 }, sender: { type: "User" } });
    assert.equal((await post(review, { event: "pull_request_review", delivery: "c3" })).status, 202);
    const suite = JSON.stringify({ action: "completed", check_suite: { pull_requests: [] }, sender: { type: "Bot" } });
    assert.equal((await post(suite, { event: "check_suite", delivery: "c4" })).status, 202);
    const push = JSON.stringify({ ref: "refs/heads/main", repository: { full_name: "o/r", default_branch: "main" } });
    assert.equal((await post(push, { event: "push", delivery: "c5" })).status, 202);
    assert.deepEqual(f.calls.map((c) => c.init.headers["x-github-event"]), [
      "pull_request", "pull_request", "pull_request_review", "check_suite", "push",
    ]);
  } finally {
    f.restore();
  }
});

test("a valid pull_request is acked fast and forwarded verbatim with its signature", async () => {
  const f = stubFetch(() => new Response("{}", { status: 202 }));
  try {
    const body = prBody();
    const resp = await post(body);
    assert.equal(resp.status, 202);
    assert.equal(f.calls.length, 1);
    const { url, init } = f.calls[0];
    assert.equal(url, "https://backend.example.com/webhooks/github");
    assert.equal(init.body, body);
    assert.equal(init.headers["x-hub-signature-256"], await sign(body));
    assert.equal(init.headers["x-github-event"], "pull_request");
    assert.equal(init.headers["x-github-delivery"], "d-1");
  } finally {
    f.restore();
  }
});

test("a retryable failure is parked before acking; 4xx is dropped; without KV it is a visible 502", async () => {
  const kv = fakeKv();
  let f = stubFetch(() => new Response("{}", { status: 503 }));
  try {
    assert.equal((await post(prBody(), { env: { ...baseEnv, PENDING: kv }, delivery: "p1" })).status, 202);
    assert.equal(f.calls.length, 1); // one inline attempt only: GitHub times out at 10 s
    assert.deepEqual([...kv.store.keys()], ["pending:p1"]);
    assert.equal((await post(prBody(), { delivery: "p2" })).status, 502);
  } finally {
    f.restore();
  }
  f = stubFetch(() => new Response("{}", { status: 409 })); // another handler holds the claim
  try {
    assert.equal((await post(prBody(), { env: { ...baseEnv, PENDING: kv }, delivery: "p3" })).status, 202);
    assert.ok(kv.store.has("pending:p3"));
  } finally {
    f.restore();
  }
  f = stubFetch(() => new Response("{}", { status: 400 }));
  try {
    assert.equal((await post(prBody(), { env: { ...baseEnv, PENDING: kv }, delivery: "p4" })).status, 202);
    assert.equal(kv.store.has("pending:p4"), false);
  } finally {
    f.restore();
  }
});

test("if parking itself fails the webhook fails visibly", async () => {
  const kv = { ...fakeKv(), put: async () => { throw new Error("KV write limit"); } };
  const f = stubFetch(() => new Response("{}", { status: 503 }));
  try {
    assert.equal((await post(prBody(), { env: { ...baseEnv, PENDING: kv } })).status, 502);
  } finally {
    f.restore();
  }
});

test("parks deliveries in KV when the backend is down, then the cron replays them", async () => {
  const kv = fakeKv();
  const env = { ...baseEnv, PENDING: kv };
  let f = stubFetch(() => {
    throw new TypeError("offline");
  });
  try {
    await post(prBody(), { env, delivery: "parked-1" });
    assert.equal(f.calls.length, 1);
    assert.deepEqual([...kv.store.keys()], ["pending:parked-1"]);
  } finally {
    f.restore();
  }
  f = stubFetch(() => new Response("{}", { status: 202 }));
  try {
    const out = await runCron({ ...env, BACKEND_API_TOKEN: "tok" }, 5 * 60000);
    assert.deepEqual(out, { replayed: 1, swept: true });
    assert.equal(kv.store.size, 0);
    const sweep = f.calls.find((c) => c.url.endsWith("/decisions/sweep"));
    assert.equal(sweep.init.method, "POST");
    assert.equal(sweep.init.headers.authorization, "Bearer tok");
  } finally {
    f.restore();
  }
});

test("cron skips KV replay off-minutes but always sweeps", async () => {
  const kv = fakeKv();
  await kv.put("pending:x", JSON.stringify({ body: "{}", event: "pull_request", delivery: "x", signature: "s" }));
  const f = stubFetch(() => new Response("{}", { status: 200 }));
  try {
    const out = await runCron({ ...baseEnv, PENDING: kv }, 3 * 60000);
    assert.deepEqual(out, { replayed: 0, swept: true });
    assert.equal(kv.store.size, 1);
    assert.equal(f.calls.length, 1);
  } finally {
    f.restore();
  }
});

test("insecure or missing BACKEND_URL never forwards", async () => {
  const f = stubFetch(() => new Response("{}", { status: 202 }));
  try {
    assert.equal((await post(prBody(), { env: { ...baseEnv, BACKEND_URL: "http://backend.example.com" } })).status, 500);
    assert.equal((await post(prBody(), { env: { GITHUB_WEBHOOK_SECRET: SECRET } })).status, 500);
    assert.equal(await deliver({ BACKEND_URL: "ftp://x", RETRY_DELAYS_MS: "0" }, { event: "e", delivery: "d", signature: "s", body: "{}" }), "dropped");
    assert.equal(f.calls.length, 0);
  } finally {
    f.restore();
  }
});

test("health and unknown routes", async () => {
  assert.equal((await worker.fetch(new Request("https://e/health"), baseEnv, {})).status, 200);
  assert.equal((await worker.fetch(new Request("https://e/nope"), baseEnv, {})).status, 404);
});

test("replay rotates through a backlog bigger than one batch", async () => {
  const kv = fakeKv();
  for (let i = 0; i < 25; i++) {
    await kv.put(`pending:${String(i).padStart(2, "0")}`, JSON.stringify({ body: "{}", event: "pull_request", delivery: `d${i}`, signature: "s" }));
  }
  const tried = new Set();
  const f = stubFetch((url, init) => {
    tried.add(init.headers["x-github-delivery"]);
    return new Response("{}", { status: 503 }); // backend still down: nothing is removed
  });
  try {
    for (let slot = 0; slot < 3; slot++) await replayParked({ ...baseEnv, PENDING: kv }, slot);
    assert.equal(tried.size, 25); // every parked delivery got a turn, not just the first ten
    assert.equal(kv.store.size, 25);
  } finally {
    f.restore();
  }
});

test("a body without Content-Length is cut off at the size limit before any buffering or forwarding", async () => {
  const f = stubFetch(() => new Response("{}", { status: 202 }));
  try {
    const big = new Uint8Array(3 * 1024 * 1024);
    const stream = new ReadableStream({ start(c) { c.enqueue(big); c.close(); } });
    const request = new Request("https://edge.example.com/webhooks/github", {
      method: "POST", body: stream, duplex: "half",
      headers: { "x-github-event": "pull_request", "x-hub-signature-256": "sha256=" + "0".repeat(64) },
    });
    const resp = await worker.fetch(request, baseEnv, { waitUntil() {} });
    assert.equal(resp.status, 413);
    assert.equal(f.calls.length, 0);
  } finally {
    f.restore();
  }
});

test("each forward attempt is bounded by a timeout signal", async () => {
  const f = stubFetch(() => new Response("{}", { status: 202 }));
  try {
    await post(prBody());
    assert.ok(f.calls[0].init.signal instanceof AbortSignal);
  } finally {
    f.restore();
  }
});

test("a stalled backend is aborted by the timeout and the delivery is parked", async () => {
  const kv = fakeKv();
  const f = stubFetch((url, init) => new Promise((_resolve, reject) => {
    init.signal.addEventListener("abort", () => reject(init.signal.reason)); // never answers
  }));
  try {
    const started = Date.now();
    const resp = await post(prBody(), { env: { ...baseEnv, PENDING: kv, FORWARD_TIMEOUT_MS: "25" }, delivery: "stall-1" });
    assert.equal(resp.status, 202);
    assert.ok(Date.now() - started < 2000);
    assert.ok(kv.store.has("pending:stall-1"));
  } finally {
    f.restore();
  }
});

test("replay reaches a backlog larger than one KV page within the list-call budget", async () => {
  const kv = fakeKv();
  const total = 1500; // more than one 1000-key page
  for (let i = 0; i < total; i++) {
    await kv.put(`pending:${String(i).padStart(5, "0")}`, JSON.stringify({ body: "{}", event: "pull_request", delivery: `d${i}`, signature: "s" }));
  }
  const parkWrites = kv.writes;
  const tried = new Set();
  const f = stubFetch((url, init) => {
    tried.add(init.headers["x-github-delivery"]);
    return new Response("{}", { status: 503 });
  });
  try {
    const slots = total / 10;
    for (let slot = 0; slot < slots; slot++) await replayParked({ ...baseEnv, PENDING: kv }, slot);
    assert.equal(tried.size, total); // everything, including keys past the first page
    assert.equal(kv.writes, parkWrites); // no KV writes while the backend is down
    assert.ok(kv.lists <= 2 * slots); // at most two list() calls per run
  } finally {
    f.restore();
  }
});

test("a small backlog costs exactly one list() call per replay", async () => {
  const kv = fakeKv();
  await kv.put("pending:a", JSON.stringify({ body: "{}", event: "pull_request", delivery: "a", signature: "s" }));
  const f = stubFetch(() => new Response("{}", { status: 503 }));
  try {
    await replayParked({ ...baseEnv, PENDING: kv }, 1);
    assert.equal(kv.lists, 1);
  } finally {
    f.restore();
  }
});

test("deliveries signed with the GitHub App secret are accepted when configured", async () => {
  const f = stubFetch(() => new Response("{}", { status: 202 }));
  try {
    const body = prBody();
    const appSigned = await sign(body, "app-secret");
    assert.equal((await post(body, { signature: appSigned })).status, 401);
    const env = { ...baseEnv, GITHUB_APP_WEBHOOK_SECRET: "app-secret" };
    assert.equal((await post(body, { signature: appSigned, env })).status, 202);
    assert.equal((await post(body, { env })).status, 202); // repo secret still works
    const appOnlyEnv = { GITHUB_APP_WEBHOOK_SECRET: "app-secret", BACKEND_URL: baseEnv.BACKEND_URL };
    assert.equal((await post(body, { signature: appSigned, env: appOnlyEnv })).status, 202);
  } finally {
    f.restore();
  }
});
