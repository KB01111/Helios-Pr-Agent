# PR Agent Control

Tauri desktop app (Windows) that wires up a **free-tier GitHub PR review agent** for your
repositories, plus the PR-Agent harness itself. Two review engines supported:

- **PR-Agent** ([The-PR-Agent/pr-agent](https://github.com/The-PR-Agent/pr-agent)) — best
  review quality, runs as a GitHub Action. Commands: `/review`, `/describe`, `/improve`, `/ask`.
- **OpenRabbit** ([aryanbrite/openrabbit](https://github.com/aryanbrite/openrabbit)) — simplest
  zero-host setup, fully automatic reviews on every PR via GitHub Actions.

Both run on **OpenRouter free models** (default
`thinkingmachines/inkling-2-small:free`). No paid API needed.

## Download (Windows)

Grab the [latest release](https://github.com/KB01111/Helios-Pr-Agent/releases/latest).
The NSIS `...setup.exe` is the recommended installer; an MSI is also published.
The installers are unsigned, so Windows SmartScreen may ask for confirmation.

## What's in this repo

| Path | What it is |
|---|---|
| `src/` | Tauri frontend: React + TypeScript + Tailwind v4 + [shadcn/ui](https://ui.shadcn.com), built with Vite (sidebar UI: Auto review, Review, Agents & repo sync, Runs, Autonomy, Quota, Providers, Integrations, Settings). `src/components/ui/` holds the shadcn components; add more with `npx shadcn@latest add <name>` |
| `src-tauri/` | Rust backend: config/workflow read-write, local review runner, Box provisioner bridge, GitHub device-flow OAuth (desktop token kept in memory/session by default; opt-in save to `backend/.env`), the 1-click repo setup commands (`github_list_repos`, `github_repo_status`, `github_setup_repo`, `github_disable_repo`), the auto-review workflow generator (`src-tauri/src/autoreview.rs`) and PR commands (`github_list_pulls`, `github_pr_command`) |
| `backend/` | FastAPI webhook/event brain, agent/run API, quota, and Redis-backed shared state (`REDIS_URL` on FastAPI Cloud, or Upstash REST) |
| `cloudflare-worker/` | Free-tier edge receiver + cron for the cloud fallback (signature check, retrying forward, deferred-event sweep) |
| `upstash-box/` | Node provisioner + Python cron script that auto-reviews open PRs on a schedule (runs on Upstash Box free tier) |
| `.pr_agent.toml` | PR-Agent config for this repo (free OpenRouter model + `openrouter/free` fallback) |
| `.github/workflows/pr-agent.yml` | PR-Agent GitHub Action for this repo |

## Run the app

```powershell
npm install
npx tauri dev        # dev window (starts the Vite dev server on :1420 with hot reload)
npx tauri build      # type-checks + builds the UI into dist/, then the installer in src-tauri\target\release\bundle
npm run typecheck    # TypeScript check only
```

Requirements: Node 20.19+ (Vite 8), Rust stable, WebView2 (preinstalled on Windows 10/11), and
Python 3.12+ for local CLI reviews (or set `PR_AGENT_BASE_PYTHON` to a 3.12+ interpreter).

### Local CLI review (Review tab)

`Run locally` shells out to PR-Agent on this PC. It needs:

- **GitHub sign-in** in Settings → GitHub (device flow or PAT). The session token is
  passed to the CLI as `GITHUB__USER_TOKEN`; it is never written to disk.
- **PR-Agent CLI** installed once. Providers → Install CLI creates a dedicated venv at
  `%LOCALAPPDATA%\PR Agent Control\pr-agent-venv` and installs pinned
  `pr-agent[github]==0.50.0` there, so the backend's Python packages are not touched.
  Override the location with `PR_AGENT_VENV` (and the base interpreter with
  `PR_AGENT_BASE_PYTHON`).
- **OpenRouter key** in the Review card (session only).

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:757d17abfdee93ddec5ec42e030e15e212d2a80eb4f16492d75588171517d34d -->

Installed builds initialize `.pr_agent.toml` in the per-user application configuration
directory (`%APPDATA%\com.pragent.control` on Windows) from an embedded default.
Development builds use the repository configuration. Loading, saving, and local reviews
use this same file, passed as `--extra_config_url` even when the target repository has
no configuration. Legacy `custom_model_max_tokens` settings are migrated to
`max_model_tokens` before execution. Copy CLI generates a command for PowerShell on
Windows and a POSIX shell elsewhere, using the selected Python interpreter. For
private repos, the GitHub token needs `repo` scope.

<!-- /coderabbit-ai-generated:v1 -->

## Auto review: deploy PR Agent to your repos

The **Auto review** page (the app's start page) is the quickest way to get every pull
request reviewed. Reviews run in GitHub Actions, so nothing needs hosting. By default
GitHub withholds repository secrets from pull requests that come from forks (private repos
can opt in to sending them). Whenever `OPENROUTER_API_KEY` is unavailable to a run, the
workflow logs a warning and skips PR Agent, so such fork PRs are not reviewed.

1. **Connect GitHub** — device flow, or a PAT with the `repo` **and** `workflow` scopes
   (session only). `workflow` is required to create or update files in
   `.github/workflows`. Sign-ins made before this scope was added only have `repo`: use
   **Sign in again** (or paste a new PAT) once, otherwise Enable fails at the workflow step.
2. **OpenRouter key** — paste a free key ([openrouter.ai/settings/keys](https://openrouter.ai/settings/keys)).
   It is stored encrypted as each repo's `OPENROUTER_API_KEY` Actions secret. Updating a repo
   that already has the secret works without the key.
3. **Pick what the reviewer does** — review every PR, write the PR description, suggest
   improvements, re-review on every push, skip drafts, model, and free-text review
   instructions. **Preview workflow** shows the exact YAML that will be committed.
4. **Enable** a repo (or tick several and use **Enable / update**). The app commits
   `.github/workflows/pr-agent.yml` to the default branch and sets the secret.

Each repo shows its live state: *Auto review on*, *Update available* (older template or a
hand-edited file), *settings differ* (the repo runs other settings than the page),
*Missing API key*, *On · custom workflow* (a `pr-agent.yml` this app did not write),
*OpenRabbit* or *backend webhook*. **Update all** brings every outdated repo to the current
settings in one go. Per repo you can also:

- **Run on a pull request** — pick an open PR and post `/review`, `/describe` or `/improve`;
  the workflow replies on the PR.
- **View runs** — opens the workflow's Actions page.
- **Turn off** — deletes the workflow file with a commit (optionally also the webhook
  registered for the backend URL shown in the dialog, and the repo secrets).

The generated workflow writes its settings as JSON on its first line
(`# pr-agent-control: {...}`), which is how the app reads them back. Compared with the old
fixed template it also:

- re-reviews on `synchronize` (PR-Agent ignores that action unless it is listed in
  `github_action_config.pr_actions`), editing one persistent review comment instead of
  adding a new one per push; describe/improve run once, on the first eligible event: when
  the PR opens, or (with "Skip draft PRs") when a draft is marked ready for review. They do
  not run on pushes or reopens. Converting a PR to draft and back can run them again when
  drafts are skipped;
- queues runs per PR instead of cancelling them: one run at a time, and while one runs only
  the newest waiting run is kept. A description being written is never dropped, and an older
  review can never overwrite the review of a newer commit;
- skips draft PRs until they are ready for review (optional);
- only answers `/commands` on pull requests (not plain issues) from owners, members and
  collaborators, and skips cleanly with a warning when the secret is missing (e.g. fork PRs);
- rejects review instructions containing `${{` so they cannot inject Actions expressions.

The webhook/backend modes and the OpenRabbit reviewer remain under Integrations → GitHub
1-click repo setup (below).

## Use it

1. **Review** — writes the model into `.pr_agent.toml` (env pins win in CI) and can run a
   manual review from your PC.
2. **Agents & repo sync** — create agent configs (repo targets, trigger, runtime mode) and
   sync repositories from GitHub.
3. **Runs** — inspect background runs, view diffs, and approve pending runs.
4. **Quota** — Upstash Box / Vercel Sandbox vCPU-second usage.
5. **Providers** — write `.github/workflows/pr-agent.yml` or `openrabbit.yml` into any local
   repo, ready to commit.
6. **Integrations** — paste provider tokens; **Connect** writes them to `backend/.env` via
   the Rust backend, then **Test** hits the live provider APIs. The GitHub card also
   configures webhooks, secrets, and workflows for selected repos in one click (see below).
7. **Settings** — backend URL/API token, GitHub OAuth (device flow or PAT), and desktop keys.

## Secrets & security

- Tokens saved from **Integrations** are written to `backend/.env` (git-ignored) by Rust and
  are never stored in `localStorage`.
- GitHub OAuth/PAT tokens used by the desktop app stay in memory/sessionStorage only; the
  1-click GitHub setup can opt in to also saving the token to `backend/.env` (see below).
- The backend loads `backend/.env` (then repo-root `.env`) at startup; real process env vars
  always win over file values. On FastAPI Cloud, `REDIS_URL` is injected by the Redis Cloud
  integration and is never written to the repo.
- After cloning, add the Actions secret `OPENROUTER_API_KEY` (your OpenRouter key) to any
  repo using these workflows, otherwise the Actions fail on missing secrets.
- `.env` files are git-ignored — copy `.env.example` / `upstash-box/.env.example` to start.

## 1-click setup (Integrations tab)

The desktop app's **Integrations** tab connects all five providers without touching
a terminal. Status dots show what is configured; **Test** hits a live check;
**Connect** writes the token to `backend/.env` via the Tauri backend (git-ignored,
never `localStorage`) — restart/redeploy the backend afterwards so it picks up
the new values.

| Provider | What to paste | Live check (`POST /config/test`) |
|---|---|---|
| GitHub | PAT (`repo` scope) + webhook secret | `GET api.github.com/user` with the token |
| Vercel Sandbox | `VERCEL_TOKEN` (+ optional `VERCEL_TEAM_ID`) | `GET api.vercel.com/v2/user` |
| Upstash Box | `UPSTASH_BOX_API_KEY` | key format + non-destructive API reachability probe |
| Redis | `REDIS_URL` (FastAPI Cloud / Redis Cloud) or Upstash REST URL + token | `PING` (fallback write/read roundtrip) |
| OpenRouter | `OPENROUTER_API_KEY` | `GET openrouter.ai/api/v1/auth/key` |

`GET /config/status` returns booleans/mode only — secret values are never echoed.
With `BACKEND_API_TOKEN` set, agent CRUD, run history/approval, quota,
`GET /config/status`, and `POST /config/test` require
`Authorization: Bearer <token>`. Only the SSE stream
also accepts `?token=` because `EventSource` cannot set headers; other endpoints
reject query-string credentials so they cannot leak through proxy logs.

### GitHub repo setup in one click

The GitHub card in **Integrations** signs in, lists your repos, and configures the
selected ones end to end — no copy-pasting webhook URLs or Actions secrets.

**Review mode** (selector on the card) decides what is configured per repo:

| Mode | Configures | Needs a public backend? |
|---|---|---|
| **GitHub Actions only** (default) | Actions secret + workflow file | No |
| Backend only | Webhook (+ backend agent) | Yes, public HTTPS |
| Both | Everything | Yes. A PR gets two reviews (Action + backend comment) |

Pick one path per repo to avoid duplicate reviews. Repo status badges understand the
mode: steps a mode does not need show as "—" instead of failing.

Prerequisites:

- An OAuth App at `github.com/settings/developers` with **Device Flow enabled** (callback
  can be `http://localhost`). Enter its **Client ID** in Settings → GitHub or in the
  Integrations card; it is a non-secret value stored in `localStorage`.
- The `repo` scope, which the device flow requests. It covers webhooks, repository
  contents, and Actions secrets for private repos.
- Admin permission on each repo: only repos where the token reports
  `permissions.admin == true` are configurable; the rest are listed but disabled.
- A **public HTTPS** backend base URL, reachable as `<base URL>/webhooks/github`.
  `localhost` will never receive GitHub deliveries.

Flow:

1. **Sign in with GitHub** — the app shows the device code, opens
   `https://github.com/login/device`, and polls until you approve. The access token stays
   in memory/session only.
2. **Pick repos** — up to 500 repos are loaded via `GET /user/repos` (100 per page, sorted
   by last update) into a searchable checklist; private repos are marked.
3. **Configure selected** — runs per repo, with the following steps:
   - **Webhook** — `<base URL>/webhooks/github`, content type `application/json`, events
     `pull_request` only, active, using the supplied secret. An existing hook with the
     same URL is PATCHed instead of duplicated.
   - **Actions secrets** — `OPENROUTER_API_KEY`, plus `LLM_API_KEY` for the OpenRabbit
     workflow, encrypted with the repo public key (libsodium sealed box).
   - **Workflow file** — commits `.github/workflows/pr-agent.yml` or
     `.github/workflows/openrabbit.yml` from the app's built-in template to the default
     branch. If the file exists with different content it is updated (GET sha, then PUT);
     identical content is left unchanged.
   - **Agent** (optional) — creates a backend agent with `target_repos` set to the
     successful repos, or updates the same-named agent by merging those repos into its
     existing targets. `trigger` is `"on_commit"` and `runtime_mode` is `"review_only"`, so
     reviews start on the next PR push.

Each repo shows per-step status (webhook / secrets / workflow); a retry button re-runs
only the failed repos, and a failure in one repo or step does not block the others. Setup
is idempotent: re-running updates the existing webhook, upserts the secrets, and commits
only when the workflow content changed. The card pushes the app's **built-in** templates,
not this repo's own `.github/workflows/pr-agent.yml`, so every configured repo receives
the same workflow.

The token stays in memory/sessionStorage, is never logged or written to `localStorage`,
and only reaches `backend/.env` when you tick the opt-in. That checkbox saves
`GITHUB_TOKEN`, and another saves `GITHUB_WEBHOOK_SECRET`, via the Rust backend
(git-ignored); restart the backend afterwards so it picks up the values. The secret must
match the backend's `GITHUB_WEBHOOK_SECRET` — the same one used when creating the hooks.

The manual webhook and OAuth instructions below remain valid as a fallback.

### Redis (FastAPI Cloud / Redis Cloud)

For the deployed backend, connect the Redis Cloud integration in the FastAPI Cloud app
(**Integrations → Redis Cloud** → create or select the free database). FastAPI Cloud
injects the connection string as `REDIS_URL`; no Redis secret needs to be pasted into the
desktop app. Redeploy after connecting.

The backend accepts either:

- `REDIS_URL=rediss://default:...@host:port` — FastAPI Cloud, Redis Cloud, or local Redis.
- `UPSTASH_REDIS_REST_URL` + `UPSTASH_REDIS_REST_TOKEN` — the existing Upstash REST path.

`REDIS_URL` wins when both are present. The generic client uses one connection pool per
process, `SCAN` instead of `KEYS`, and `MGET` for list/replay reads, which keeps command
count and connection churn low on the free tier. Use one small database and avoid running
many backend replicas against the same free database.

Check it with `GET /health` (`"store": "redis"`) or Integrations → Redis → **Test**.
`GET /config/status` reports `source: "REDIS_URL"` without echoing the URL.

To configure a local or self-hosted backend from the app, paste the URL into
Integrations → Redis → **Redis URL** and click **Connect Redis URL**. That writes
`backend/.env` (git-ignored); restart the backend afterwards. For FastAPI Cloud, set
`REDIS_URL` in the app's environment variables instead — the desktop button only writes a
local `backend/.env`.

### GitHub webhook URL (copy-paste)

1. Deploy the backend (see below) to get a public base URL, e.g. `https://your-app.vercel.app`.
2. In the Integrations tab enter that base URL — the Payload URL preview becomes
   `https://your-app.vercel.app/webhooks/github`. Copy it.
3. Repo Settings → Webhooks → Add webhook: paste the URL, content type
   `application/json`, secret = the webhook secret saved above, events = pull requests only.

### GitHub OAuth in the app (device flow, no backend needed)

1. Create an OAuth App at `github.com/settings/developers` → enable **Device Flow**,
   callback can be `http://localhost`.
2. Paste the App's Client ID into Settings → GitHub → **Connect with GitHub**.
3. The browser opens GitHub — enter the shown code, approve, done. The token lives
   in memory/session only (or paste a classic PAT with `repo` scope instead).

## Calibrated decisions, always-on daemon & edge fallback

Before any sandbox is spent, every PR head commit is triaged by a small, deterministic,
calibrated policy (`backend/sandboxes/decider.py`, mirrored in `src-tauri/src/decider.rs`;
both are pinned to `backend/tests/decider_cases.json`). No ML packages are installed in
the backend, so free-tier cold starts stay fast.

```json
{ "decision": "SKIP | FAST_REVIEW | SANDBOX_TEST | ESCALATE_HUMAN", "confidence": 0.91,
  "provider": "none | review | vercel | box", "reason_code": "DOCS_ONLY | LOCKFILE_BUMP | STATIC_DIFF_OK | UI_PREVIEW_NEEDED | RUNTIME_TEST_NEEDED | LOW_CONFIDENCE_GRAY_ZONE | SAFETY_FLAGGED",
  "policy_version": "v1", "latency_ms": 8 }
```

| Signal | Outcome |
|---|---|
| docs only; small trusted lockfile bump | `SKIP` (`DOCS_ONLY`, `LOCKFILE_BUMP`) |
| small, static source diff | `FAST_REVIEW` via the zero-CPU `review` provider |
| UI files dominate | `SANDBOX_TEST` on `vercel` (`UI_PREVIEW_NEEDED`) |
| tests/config/larger source churn | `SANDBOX_TEST` on `box` (`RUNTIME_TEST_NEEDED`) |
| workflows, Dockerfiles, `.env`, keys, auth/secret-ish paths, secret-like or `curl \| sh` added lines | `ESCALATE_HUMAN` (`SAFETY_FLAGGED`) — always, at every autonomy level |
| best class below the confidence floor, truncated or oversized diff | `ESCALATE_HUMAN` (`LOW_CONFIDENCE_GRAY_ZONE`) |

Guardrails: SKIP is never produced by scoring; a decision can only *reduce* spend relative
to an agent's `runtime_mode` (a `review_only` agent never gets a sandbox); `labeled` events
bypass the gate (a human asked on purpose). Confidence is a temperature-scaled softmax;
`GET /decisions/stats` reports how often humans override each decision type so the
temperature/thresholds can be re-fit (bump `POLICY_VERSION` when you do).

**Strict idempotency.** Every path registers `(repo, pr_number, head_sha)` in
`backend/store.py` (`eval_register`, NX) before acting: cloud webhook, Cloudflare worker
replays, the desktop daemon, and the Box cron loop (`BACKEND_URL` + `/decisions/claim`).
Only one wins per head commit; a new push is a new evaluation. (The GitHub Action
workflow runs independently of the backend and is not covered.)

**Desktop daemon (Autonomy tab).** The Rust daemon heartbeats to the backend, pulls PR
facts the cloud deferred to it, decides locally and submits the decision. Closing the
window keeps it running in the tray ("Stop daemon and quit" ends it); it does **not**
start at OS login. An optional classifier sidecar on `127.0.0.1` (`POST /classify` ->
`{"probs": {"static":…, "ui":…, "runtime":…}}`) is blended into the scoring branch only —
it can never override a guardrail. The backend token is held in memory only.

**Cloud fallback.** If the desktop's heartbeat lapses (asleep/offline/stopped), the cloud
decides immediately; if it goes away mid-flight, `POST /decisions/sweep` decides deferred
events after `DECIDER_DEFER_GRACE_S` (default 120 s). Autonomy levels: `observe` (shadow —
record only, legacy behaviour), `assist` (default — acts alone on cheap confident calls,
asks otherwise), `auto` (acts at/above the floor). Escalations appear in the app's inbox.

### Cloudflare worker (free tier)

`cloudflare-worker/` is an always-on edge receiver: it verifies the GitHub signature, acks
quickly, makes one delivery attempt to the backend and, if that fails, parks the event in
KV (optional) for the cron to replay — without KV an outage answers 502 so GitHub shows a
failed delivery you can redeliver. A per-minute cron calls `/decisions/sweep` to wake the
backend and run the fallback.

```bash
cd cloudflare-worker
# set BACKEND_URL in wrangler.toml, then:
npx wrangler secret put GITHUB_WEBHOOK_SECRET
npx wrangler secret put BACKEND_API_TOKEN   # only if the backend sets one
npx wrangler deploy
```

Point the repo webhook (pull requests, `application/json`, same secret) at
`https://<worker>.workers.dev/webhooks/github`. Tests: `node --test cloudflare-worker/worker.test.js`.

## Deploy the backend (FastAPI)

```powershell
cd backend
copy .env.example .env   # then fill in real values (never commit .env)
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
# or: fastapi deploy  (entrypoint: backend.main:app, see pyproject.toml)
```

On FastAPI Cloud, add the Redis Cloud integration in the app's Integrations tab; the
platform injects `REDIS_URL` automatically and redeploys the backend. `redis>=5` is
declared in `pyproject.toml` and `backend/requirements.txt`.

Health: `GET /health` · status: `GET /config/status` (auth when a token is set) · docs: `/docs`.

### Env vars

| Var | Required for | Default |
|---|---|---|
| `GITHUB_WEBHOOK_SECRET` | verifying `X-Hub-Signature-256` on `/webhooks/github` | empty = dev mode (accepts unsigned, logs warning) |
| `GITHUB_TOKEN` | fetching diffs + posting review comments | — |
| `OPENROUTER_API_KEY` / `LLM_MODEL` | reviews | model default `thinkingmachines/inkling-2-small:free` |
| `REDIS_URL` **or** `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN` | shared state (agents, runs, quota). `REDIS_URL` wins when both are set; FastAPI Cloud injects it from the Redis Cloud integration | unset = in-memory fallback |
| `UPSTASH_BOX_API_KEY` | interactive (warm-box) runs | — |
| `VERCEL_TOKEN` (or `VERCEL_API_TOKEN`), `VERCEL_TEAM_ID` | ephemeral sandbox runs | — |
| `BACKEND_API_TOKEN` | Bearer auth on agent/run/quota/config-status + `/config/test` endpoints | empty = open (local dev only) |
| `ALLOWED_ORIGINS` | CORS allowlist, comma-separated. Desktop dev: `tauri://localhost,http://tauri.localhost,http://localhost:1420`. Browsers need their origin listed. Most app calls go through the Rust proxy, but the live SSE stream opens directly from the webview and needs its origin here | `[]` (no cross-origin; set `*` only for local dev) |
| `DECIDER_ENABLED` / `DECIDER_DEFER_GRACE_S` | calibrated decision layer on/off; seconds to wait for the desktop daemon before the cloud decides | `1` / `120` |
| `REVIEW_DIFF_MAX_CHARS` | diff budget for backend `review_only` reviews. Large files share it fairly; lockfiles/binaries are dropped; a new push updates the previous review comment instead of adding another | `60000` |
| `QUOTA_LIMIT` | vCPU-seconds per provider per month | `18000` |
| `PORT` / `LOG_LEVEL` | server | `8000` / `INFO` |

Production checklist: set `BACKEND_API_TOKEN` (long random), set `ALLOWED_ORIGINS`
to your exact origins, set `GITHUB_WEBHOOK_SECRET`, connect the Redis Cloud integration
(or set `REDIS_URL` / Upstash REST vars), and never commit `.env` (files are git-ignored;
CI uses Actions secrets instead).
