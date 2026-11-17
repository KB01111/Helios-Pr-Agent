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
