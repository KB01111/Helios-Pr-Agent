# PR Agent Control

Tauri desktop app (Windows) that wires up a **free-tier GitHub PR review agent** for your
repositories, plus the PR-Agent harness itself. Two providers supported:

- **PR-Agent** ([The-PR-Agent/pr-agent](https://github.com/The-PR-Agent/pr-agent)) — best
  review quality, runs as a GitHub Action. Commands: `/review`, `/describe`, `/improve`, `/ask`.
- **OpenRabbit** ([aryanbrite/openrabbit](https://github.com/aryanbrite/openrabbit)) — simplest
  zero-host setup, fully automatic reviews on every PR via GitHub Actions.

Both run on **OpenRouter free models** (default
`thinkingmachines/inkling-2-small:free`). No paid API needed.

## What's in this repo

| Path | What it is |
|---|---|
| `src/` | Tauri frontend (7 cards: model config, manual review run, Upstash Box auto-run, GitHub OAuth device flow, PR-Agent + OpenRabbit provider writers, raw TOML editor) |
| `src-tauri/` | Rust backend: config/workflow read-write, local review runner, Box provisioner bridge, GitHub device-flow OAuth (in-memory token, never written to disk) |
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

1. **Model** — card 1 writes the model into `.pr_agent.toml` (env pins win in CI).
2. **Manual review** — install `pr-agent[github]` (card 2 button or `pip install "pr-agent[github]"`),
   paste a GitHub token + OpenRouter key, review any PR from your PC.
3. **Upstash Box auto-run** — card 3 provisions a free Box that polls your repos on a cron
   schedule and posts reviews without your PC being on.
4. **GitHub login** — card 4: device-flow OAuth (needs a `GITHUB_OAUTH_CLIENT_ID` in `.env`;
   token stays in memory only) or paste a classic PAT with `repo` scope.
5. **Provider workflows** — cards 5/6 write `.github/workflows/pr-agent.yml` or
   `openrabbit.yml` into any local repo, ready to commit.

## Secrets & security

- API keys and tokens are **memory-only**: the app never writes them to files.
- After cloning, add the Actions secret `OPENROUTER_API_KEY` (your OpenRouter key) to any
  repo using these workflows, otherwise the Actions fail on missing secrets.
- `.env` files are git-ignored — copy `.env.example` / `upstash-box/.env.example` to start.

## 1-click setup (Integrations tab)

The desktop app's **Integrations** tab connects all three providers without touching
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
With `BACKEND_API_TOKEN` set, `/config/test` and all mutating endpoints
(`POST/PUT/DELETE /agents`, `POST /runs/{id}/approve`) require
`Authorization: Bearer <token>` (the SSE stream also accepts `?token=` since
`EventSource` cannot set headers).

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

Health: `GET /health` · status: `GET /config/status` · docs: `/docs`.

### Env vars

| Var | Required for | Default |
|---|---|---|
| `GITHUB_WEBHOOK_SECRET` | verifying `X-Hub-Signature-256` on `/webhooks/github` | empty = dev mode (accepts unsigned, logs warning) |
| `GITHUB_TOKEN` | fetching diffs + posting review comments | — |
| `OPENROUTER_API_KEY` / `LLM_MODEL` | reviews | model default `thinkingmachines/inkling-2-small:free` |
| `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN` | shared state (agents, runs, quota) | unset = in-memory fallback |
| `UPSTASH_BOX_API_KEY` | interactive (warm-box) runs | — |
| `VERCEL_TOKEN` (or `VERCEL_API_TOKEN`), `VERCEL_TEAM_ID` | ephemeral sandbox runs | — |
| `BACKEND_API_TOKEN` | Bearer auth on mutating + `/config/test` endpoints | empty = open (local dev only) |
| `ALLOWED_ORIGINS` | CORS allowlist, comma-separated. Desktop dev: `tauri://localhost,http://localhost:1420`. Browsers need their origin listed; the Tauri app talks to the backend via its Rust proxy so it works regardless | `[]` (no cross-origin; set `*` only for local dev) |
| `QUOTA_LIMIT` | vCPU-seconds per provider per month | `18000` |
| `PORT` / `LOG_LEVEL` | server | `8000` / `INFO` |

Production checklist: set `BACKEND_API_TOKEN` (long random), set `ALLOWED_ORIGINS`
to your exact origins, set `GITHUB_WEBHOOK_SECRET`, and never commit `.env`
(files are git-ignored; CI uses Actions secrets instead).
