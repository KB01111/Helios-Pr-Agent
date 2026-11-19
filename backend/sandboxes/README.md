# Hybrid Cloud Sandbox Router

Routes each PR agent run to the cheapest provider with quota headroom.

## Routing table

| `runtime_mode` | Primary provider  | Notes                              |
|----------------|-------------------|------------------------------------|
| `interactive`  | `upstash_box`     | Warm box per repo, 1h idle free    |
| `ephemeral`    | `vercel_sandbox`  | One-shot sandbox, torn down after  |
| `review_only`  | `review_only`     | No sandbox, 0 CPU-sec, just a PR comment |

## Quota failover

- Key: `quota:{provider}:YYYY-MM` (UTC month), limit **18000** CPU-sec (5h) each.
- Threshold: **90%** (16200). When the requested provider is at/over threshold,
  the router fails over to the other provider.
- Both providers ≥ 90% → `review_only` (0 CPU).
- After each run the router increments the serving provider's quota key
  (via `store.incr_quota(provider, seconds)`, raw Redis `INCRBYFLOAT`, or
  in-memory counters when `store is None`).
- Any provider exception → fallback to `ReviewOnlyProvider` (unless already
  `review_only`, which returns `failed`).

## Return shape

`{provider, status, logs: list[str], diff: str, cpu_seconds: float, pr_comment_posted: bool}`

## Env vars

- `GITHUB_TOKEN` — diff fetch + comment post (review path).
- `LLM_PROVIDER` (`openrouter` default, `commandcode`, `opencode_go`) and
  `LLM_MODEL` — review generation. `LLM_MODEL` blank uses the provider default.
- `OPENROUTER_API_KEY`, or `COMMANDCODE_API_KEY` (`CMD_API_KEY`), or
  `OPENCODE_GO_API_KEY` (`OPENCODE_API_KEY`) — key for the selected provider.
- `UPSTASH_BOX_API_KEY` — warm-box exec (else graceful review fallback).
- `VERCEL_TOKEN` (`VERCEL_API_TOKEN` accepted), `VERCEL_TEAM_ID?`, `VERCEL_PROJECT_ID?`.
