# PR Agent Control

Tauri desktop app (Windows) that wires up a **free-tier GitHub PR review agent** for your
repositories, plus the PR-Agent harness itself. Two review engines supported:

- **PR-Agent** ([The-PR-Agent/pr-agent](https://github.com/The-PR-Agent/pr-agent)) — best
  review quality, runs as a GitHub Action. Commands: `/review`, `/describe`, `/improve`, `/ask`.
- **OpenRabbit** ([aryanbrite/openrabbit](https://github.com/aryanbrite/openrabbit)) — simplest
  zero-host setup, fully automatic reviews on every PR via GitHub Actions.

Both run on **OpenRouter free models** (default
`thinkingmachines/inkling-2-small:free`). No paid API needed.

## What's in this repo

| Path | What it is |
|---|---|
| `src/` | Tauri frontend (tabbed UI: Review, Agents & repo sync, Runs, Quota, Providers, Integrations, Settings) |
| `src-tauri/` | Rust backend: config/workflow read-write, local review runner, Box provisioner bridge, GitHub device-flow OAuth (desktop token kept in memory/session only, never written to disk) |
| `upstash-box/` | Node provisioner + Python cron script that auto-reviews open PRs on a schedule (runs on Upstash Box free tier) |
| `.pr_agent.toml` | PR-Agent config for this repo (free OpenRouter model + `openrouter/free` fallback) |
| `.github/workflows/pr-agent.yml` | PR-Agent GitHub Action for this repo |

## Run the app

```powershell
npm install
npx tauri dev        # dev window
npx tauri build      # installer in src-tauri\target\release\bundle
```

Requirements: Node 18+, Rust stable, WebView2 (preinstalled on Windows 10/11).

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
   the Rust backend, then **Test** hits the live provider APIs.
7. **Settings** — backend URL/API token, GitHub OAuth (device flow or PAT), and desktop keys.

## Secrets & security

- Tokens saved from **Integrations** are written to `backend/.env` (git-ignored) by Rust and
  are never stored in `localStorage`.
- GitHub OAuth/PAT tokens used by the desktop app stay in memory/sessionStorage only.
- The backend loads `backend/.env` (then repo-root `.env`) at startup; real process env vars
  always win over file values.
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
| Upstash Redis | REST URL + token | write/read roundtrip (`PING`) |
| OpenRouter | `OPENROUTER_API_KEY` | `GET openrouter.ai/api/v1/auth/key` |

`GET /config/status` returns booleans/mode only — secret values are never echoed.
With `BACKEND_API_TOKEN` set, agent CRUD, run history/approval, quota,
`GET /config/status`, and `POST /config/test` require
`Authorization: Bearer <token>`. Only the SSE stream
also accepts `?token=` because `EventSource` cannot set headers; other endpoints
reject query-string credentials so they cannot leak through proxy logs.

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

## Deploy the backend (FastAPI)

```powershell
cd backend
copy .env.example .env   # then fill in real values (never commit .env)
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
# or: fastapi deploy  (entrypoint: backend.main:app, see pyproject.toml)
```

Health: `GET /health` · status: `GET /config/status` (auth when a token is set) · docs: `/docs`.

### Env vars

| Var | Required for | Default |
|---|---|---|
| `GITHUB_WEBHOOK_SECRET` | verifying `X-Hub-Signature-256` on `/webhooks/github` | empty = dev mode (accepts unsigned, logs warning) |
| `GITHUB_TOKEN` | fetching diffs + posting review comments | — |
| `OPENROUTER_API_KEY` / `LLM_MODEL` | reviews | model default `thinkingmachines/inkling-2-small:free` |
| `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN` | shared state (agents, runs, quota) | unset = in-memory fallback |
| `UPSTASH_BOX_API_KEY` | interactive (warm-box) runs | — |
| `VERCEL_TOKEN` (or `VERCEL_API_TOKEN`), `VERCEL_TEAM_ID` | ephemeral sandbox runs | — |
| `BACKEND_API_TOKEN` | Bearer auth on agent/run/quota/config-status + `/config/test` endpoints | empty = open (local dev only) |
| `ALLOWED_ORIGINS` | CORS allowlist, comma-separated. Desktop dev: `tauri://localhost,http://tauri.localhost,http://localhost:1420`. Browsers need their origin listed. Most app calls go through the Rust proxy, but the live SSE stream opens directly from the webview and needs its origin here | `[]` (no cross-origin; set `*` only for local dev) |
| `QUOTA_LIMIT` | vCPU-seconds per provider per month | `18000` |
| `PORT` / `LOG_LEVEL` | server | `8000` / `INFO` |

Production checklist: set `BACKEND_API_TOKEN` (long random), set `ALLOWED_ORIGINS`
to your exact origins, set `GITHUB_WEBHOOK_SECRET`, and never commit `.env`
(files are git-ignored; CI uses Actions secrets instead).
