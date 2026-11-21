# PR Agent feature parity plan (Macroscope + CodeRabbit)

Date: 2026-11-20 · Branch: `claude/pr-agent-feature-parity-8181e4`

## Goal

Deploy PR agents per repository that:

1. **Remember the repo**: conventions, accepted and rejected feedback, architecture notes, and recurring bugs, reused in every later review.
2. **Analyse each PR in depth**: walkthrough, inline findings with committable fixes, and cross-file context.
3. **Give a mergeability score (0–100)** plus a list of **what must be fixed before merge**, kept current as the PR changes.
4. Match the review features of CodeRabbit and Macroscope, while keeping the free-tier, zero-host default.

---

## 1. Where the app stands today

| Area | Current state | Where |
|---|---|---|
| Auto review via Actions | Generates `pr-agent.yml` (upstream PR-Agent action, OpenRouter free model), persistent review comment, `/review` `/describe` `/improve` commands, draft skip, per-PR queueing | `src-tauri/src/autoreview.rs`, `src/pages/auto-review.tsx` |
| Backend review engine | `ReviewOnlyProvider`: fetches the diff and makes **one free-text LLM call**. It posts **one sticky issue comment**, with no inline comments, no structure, and no severity | `backend/sandboxes/providers_review.py` |
| Webhook intake | Handles `pull_request` `opened/reopened/synchronize/labeled` only. No comments, reviews, checks, closed/merged, or push | `backend/main.py:503` |
| Triage | Deterministic calibrated decider (SKIP / FAST_REVIEW / SANDBOX_TEST / ESCALATE_HUMAN) with safety flags, mirrored in Rust | `backend/sandboxes/decider.py`, `src-tauri/src/decider.rs` |
| Idempotency | `(repo, pr, head_sha)` eval registry in Redis | `backend/store.py:564` |
| Sandboxes | Upstash Box and Vercel providers. Both are best effort and fall back to review-only | `backend/sandboxes/providers_*.py`, `router.py` |
| Agents | `AgentConfig{name, system_prompt, target_repos, trigger, runtime_mode}`. **No memory, no rules, no scoring** | `backend/main.py:188` |
| Desktop | Tauri + React: Auto review, Agents, Runs, Autonomy (daemon), Quota, Providers, Integrations, Settings | `src/pages/*` |
| Auth to GitHub | PAT / OAuth device-flow token. **No GitHub App**, so the agent cannot create Check Runs and comments appear as the user | `src-tauri/src/main.rs`, `src/lib/github.ts` |
| Storage | Redis (`REDIS_URL` or Upstash REST) with an in-memory dev fallback. Nothing persistent per repo | `backend/store.py` |

**Summary:** there is solid infrastructure for triage, idempotency, sandboxing, and deployment, but the review itself is a thin pass-through. There is no repo memory, no structured findings, no merge gate, and no conversation loop. Most of the work below goes into a new **native review engine** in the backend. The upstream PR-Agent Action stays as the zero-host "lite" mode.

---

## 2. Feature parity matrix

✅ = have · 🟡 = partial · ❌ = missing. The last column is the phase that closes the gap.

| Capability | CodeRabbit | Macroscope | Us today | Phase |
|---|---|---|---|---|
| PR summary / walkthrough + file-change table | ✅ | ✅ | 🟡 (PR-Agent `/describe` in Actions only) | P1 |
| Sequence / flow diagram of the change | ✅ | — | ❌ | P1 |
| Inline line comments with severity | ✅ | ✅ | 🟡 (Actions only, none in backend) | P1 |
| Committable suggestion blocks | ✅ | ✅ | ❌ | P1 |
| Incremental review (only new commits, no repeated findings) | ✅ | ✅ | ❌ | P1 |
| Auto title, labels, suggested reviewers, related PRs/issues | ✅ | 🟡 | ❌ | P1 / P4 |
| Repo config file and path-scoped instructions | ✅ (`.coderabbit.yaml`) | ✅ (rules) | 🟡 (one global `system_prompt`) | P1 |
| **Repo learnings / memory** (from chat and feedback) | ✅ | ✅ | ❌ | **P2** |
| Codebase graph / cross-file context (callers, defs, AST) | ✅ | ✅ (AST walkers, core strength) | ❌ | P3 |
| Linters / SAST / secret scanning folded into the review | ✅ (40+ tools) | 🟡 | 🟡 (decider safety regexes) | P3 |
| Linked-issue assessment (does the PR do what the issue asks) | ✅ | ✅ | ❌ | P3 |
| **Pre-merge checks / mergeability / approvability** | ✅ (pre-merge checks) | ✅ (Approvability) | ❌ | **P4** |
| Chat with the bot in PR threads (`@pr-agent …`) | ✅ | ✅ | 🟡 (fixed `/commands` in Actions) | P5 |
| Learn from reactions / resolutions / "won't fix" | ✅ | ✅ | ❌ | P2 + P5 |
| Fix-it-for-me (agent pushes fix commits or a stacked PR) | ✅ (finishing touches) | ✅ (Fix It For Me) | ❌ | P5 |
| Generate unit tests / docstrings | ✅ | — | ❌ | P5 |
| Commit / PR status digests and team reports | 🟡 | ✅ (status reports, Slack) | ❌ | P6 |
| Dashboard / analytics (findings accepted, time to merge) | ✅ | ✅ | 🟡 (runs, decisions stats) | P6 |
| Bot identity via a GitHub App and Check Runs | ✅ | ✅ | ❌ | **P0** |

---

## 3. Target architecture

```
GitHub ──webhook──> Cloudflare worker ──> FastAPI backend
                                          ├─ event router (PR, comments, reviews, threads, checks, push, closed)
                                          ├─ triage (existing decider)
                                          ├─ Review engine (NEW)
                                          │    context builder ─ repo memory ─ code graph ─ linters
                                          │    multi-pass LLM -> structured Findings (JSON schema)
                                          │    dedupe/fingerprint -> publisher (review + inline + suggestions)
                                          ├─ Merge-readiness scorer (NEW) -> Check Run + sticky checklist
                                          ├─ Memory service (NEW): learnings, repo profile, finding history
                                          └─ Chat/command handler (NEW): @pr-agent, resolve, fix, learn
Desktop app ── deploy agents per repo, edit memory, view scores/blockers, analytics
Actions "lite" mode ── upstream PR-Agent plus memory file plus a merge-readiness step (no backend needed)
```

**Key decisions**

- **D1: GitHub App as the agent identity.** Check Runs (needed for a real merge gate and branch protection) can only be created by GitHub Apps. A PAT is limited to commit statuses. Ship an App manifest flow in the desktop app ("Create my PR Agent App"). Keep PAT mode as a fallback that publishes a commit status instead of a Check Run.
- **D2: Build a native engine; don't fork PR-Agent.** Memory injection, structured findings, fingerprints, and scoring all need control over prompts and output, so the backend gets its own engine. The PR-Agent Action stays as the "lite" tier and still benefits from memory (see P2.6).
- **D3: Memory without vector infrastructure at first.** Use Redis plus path-glob and keyword scoping, which fits the free tier and the "no ML packages" rule. Embeddings via an OpenRouter embedding model are an optional upgrade behind a flag (P2.7).
- **D4: A deterministic score with LLM input, not an LLM-guessed number.** The score comes from explicit, explainable signals. The LLM contributes findings and severities, never the final number. That keeps the score stable and auditable, in the same spirit as the existing decider.

---

## 4. Phased plan

### P0: Foundations (prerequisite for everything else)

1. **GitHub App support**
   - App-manifest creation flow in the desktop app (Integrations → "Create PR Agent App"). It stores `APP_ID`, the private key, and the webhook secret in `backend/.env` through the existing Rust writer.
   - Permissions: contents R (W for P5 fixes), pull_requests RW, issues RW, checks RW, statuses RW, metadata R. Events: pull_request, pull_request_review, pull_request_review_comment, pull_request_review_thread, issue_comment, check_suite, check_run, status, push, installation.
   - Backend `github_client.py`: installation-token minting (JWT → `/app/installations/{id}/access_tokens`) with a Redis cache. A single client replaces the ad-hoc `httpx` calls in `providers_review.py`. PAT fallback is kept.
2. **Event router.** Split `github_webhook` (`backend/main.py:503`) into a dispatcher keyed on `X-GitHub-Event` + action. Existing PR handling moves unchanged into `events/pull_request.py`. Add handler stubs for every event listed above. The Cloudflare worker already forwards `x-github-event`; extend its allow-list.
3. **Data model (Redis, JSON).**
   - `repo:{repo}:profile`: languages, frameworks, test command, and build command (detected).
   - `repo:{repo}:config`: parsed `.pragent.yaml` (cached by blob sha).
   - `mem:{repo}:{id}`: learnings (P2).
   - `finding:{repo}:{pr}:{fingerprint}`: finding state (open / resolved / dismissed / outdated).
   - `merge:{repo}:{pr}`: latest readiness report.
   - Size budgets and TTLs, sized for the 30 MB free Redis. Add `store` helpers for sorted sets (`ZADD`/`ZRANGE`) for recency.
4. **Agent config v2.** Extend `AgentConfig`: `profile` (chill / assertive), `memory_enabled`, `merge_gate` (off / advisory / required), `score_threshold`, `path_instructions[]`, `tools[]` (linters), `chat_enabled`, `autofix` (off / suggest / push). Migrate existing agents with defaults.

**Done when:** an App-installed repo delivers all events to the backend, existing PR flows pass the current tests unchanged, and the token is minted per installation.

### P1: Review engine parity (structured, inline, incremental)

1. **Repo config file `.pragent.yaml`** (CodeRabbit-style), read from the base branch so a PR cannot change its own rules:
   `reviews.profile`, `path_filters`, `path_instructions[{path, instructions}]`, `auto_review{drafts, base_branches, ignore_title_keywords}`, `labels`, `pre_merge_checks` (P4), `tools` (P3), `chat`, `memory`. Validate it with a JSON Schema and publish the schema so editors autocomplete.
2. **Context builder:** diff hunks with surrounding file context (base and head blobs), PR metadata, commit messages, linked issues, repo profile, applicable path instructions, and (later) memory and graph. Token budgeting reuses `prepare_diff`, upgraded to rank files by risk (decider signals) instead of truncating.
3. **Multi-pass LLM pipeline:**
   - Pass A, *Summarize*: walkthrough, per-file change table, a Mermaid sequence diagram when control flow changes, and an effort estimate (1–5).
   - Pass B, *Review*, per file-group in parallel: emits `Finding[]` against a strict JSON schema: `{file, start_line, end_line, side, severity: critical|major|minor|nit, category: bug|security|perf|maintainability|style|test|docs, title, body, suggestion_patch?, confidence 0..1}`.
   - Pass C, *Verify*: a cheap self-check pass that drops findings with low confidence, findings that hallucinate lines outside the diff, and findings that duplicate memory "dismissed" patterns. This is the main noise-control lever and maps to Macroscope's precision focus.
   - Use `response_format` JSON where the provider supports it. Otherwise use a tolerant JSON extractor with one retry.
4. **Publisher**
   - One PR review (`POST /pulls/{n}/reviews`) with inline comments. Each `suggestion_patch` renders as a ```` ```suggestion ```` block so it can be committed in one click.
   - Sticky summary comment (reuse `review_marker`) holding the walkthrough, diagram, finding counts, and a link to the readiness report.
   - Optionally update the PR description and title, and apply labels from config.
5. **Incremental reviews and fingerprints.** Fingerprint = hash(file, normalized code span, category, title stem). On `synchronize`, review only `compare(last_reviewed_sha..head)`. Carry open findings forward. Mark findings *outdated* when their lines change and *resolved* when the review thread is resolved or the code is fixed (re-check). Never re-post an identical finding.
6. **Desktop:** an "Agent profile" editor (profile, path instructions, labels) that can commit `.pragent.yaml` to a repo, the same way Auto review commits workflows today.

**Done when:** a test PR gets a walkthrough, inline comments with committable suggestions, and no duplicate comments after two more pushes. Golden tests cover the JSON parsing and fingerprinting.

### P2: Repo-specific memory (core requirement)

1. **Memory types** (`mem:{repo}:{id}`):
   - `learning`: natural-language rule, such as "We use `Result<T, AppError>`, never `unwrap()` in `src-tauri`". Fields: `scope_globs`, `source` (chat / dismissal / manual / merged-outcome), `author`, `confidence`, `hits`, `last_used`, `created_at`.
   - `dismissed_pattern`: fingerprint stem plus the reason, from 👎 reactions, "won't fix" replies, or threads resolved without a code change.
   - `repo_fact`: architecture notes, owners, critical paths, and the test and build commands (auto-detected plus editable).
   - `incident`: recurring-bug signatures extracted from merged fix PRs (`fix:` commits, linked bug issues).
2. **Capture paths:**
   - Chat: `@pr-agent remember <text>` / `@pr-agent learn` replies (P5) create a learning scoped to the files under discussion.
   - Implicit: a finding dismissed twice with the same stem becomes a `dismissed_pattern`. An accepted suggestion raises the confidence of the related learning.
   - On `pull_request closed+merged`: extract up to N candidate learnings from the review conversation (LLM summarizer). They are queued as **pending** until a maintainer approves them in the app, or auto-approved when `memory.auto_approve: true`.
   - Bootstrap: on install, index `CONTRIBUTING.md`, `AGENTS.md`/`CLAUDE.md`, lint configs, and `.editorconfig` into `repo_fact`s.
3. **Retrieval at review time:** select memories whose `scope_globs` match the changed files, rank them by keyword overlap with the diff, confidence, and recency, then cap them to a token budget (for example 1.5k tokens). Inject them into Pass B as "Repository rules", and into Pass C as "Known false-positive patterns".
4. **Attribution:** every finding that applied a learning cites it ("Based on learning #42"), and the summary lists the learnings used. This mirrors CodeRabbit and builds trust.
5. **API and UI:** `GET/POST/PATCH/DELETE /repos/{repo}/memory`, `POST /repos/{repo}/memory/{id}/approve`, and import/export as JSON. A new desktop **Memory** page per repo: search, edit, approve pending entries, see hit counts, prune stale entries.
6. **Actions "lite" mode bridge:** export approved learnings to `.pr-agent/memory.md` in the repo (a commit from the app or the backend). The generated `pr-agent.yml` gains a step that reads the file into `PR_REVIEWER__EXTRA_INSTRUCTIONS`, so zero-host users also get repo memory. Bump the `autoreview.rs` template version so repos show *Update available*.
7. *(Optional, flagged)* Embeddings: an OpenRouter embedding model plus Redis vector search (Redis Stack) or a small in-process cosine search over the top 500 memories.

**Done when:** a dismissed finding does not reappear on a new PR touching the same pattern, a `remember` instruction changes the next review, and both behaviours are covered by integration tests with the in-memory store.

### P3: Deeper analysis (context the LLM can't infer from the diff)

1. **Code graph:** run tree-sitter parsing in the sandbox (Box) or in the Action step, not in the slim backend. For changed symbols, collect definitions, callers, and implementers, then attach the top-k snippets to the context. Cache the per-repo symbol index by commit sha in Box storage. This is the Macroscope-style "AST walker" capability.
2. **Tool runners** in the sandbox, chosen by repo profile: ruff, eslint, clippy, `tsc --noEmit`, semgrep (OSS rules), gitleaks, actionlint, hadolint, and markdownlint. Their findings are normalised into the same `Finding` schema with `source: tool`. The LLM dedupes them and explains them; it does not re-report them.
3. **Run the project's tests** when the decider says `SANDBOX_TEST`: this reuses existing routing, with the test command from the repo profile. Pass/fail and new failures feed the score.
4. **Linked-issue assessment:** parse `Fixes #n` / Jira / Linear keys and fetch the issue body. Pass A outputs `{requirement, addressed: yes|partial|no, evidence}` for each requirement.
5. **Security-sensitive diffs:** the existing `SAFETY_FLAGGED` decider signal becomes a dedicated security pass with stricter instructions and a forced `critical` floor for secrets.

### P4: Mergeability score and "what to fix before merge" (core requirement)

1. **Signals** (`backend/merge/signals.py`), recomputed on every relevant event: push, check_suite/check_run/status completed, review submitted, thread resolved, comment, labeled, and base-branch push (behind or conflict).

   | Signal | Source | Hard blocker? |
   |---|---|---|
   | Merge conflicts / `mergeable_state=dirty` | PR API | Yes |
   | Required CI checks failing / pending | Checks + statuses API | Failing = yes, pending = no |
   | Open findings by severity (unresolved, not outdated) | Finding store | Any `critical` = yes |
   | Unresolved human review threads | GraphQL `reviewThreads` | Configurable |
   | Required approvals missing / changes requested | Reviews + branch protection | Yes if protected |
   | Tests touched for changed source (or covered by the sandbox run) | Diff + P3.3 | No (penalty) |
   | Linked issue requirements addressed | P3.4 | `no` = configurable |
   | PR size / risk (decider class, safety flags) | Decider | Safety flag = yes unless a human approves |
   | Description / title quality, conventional title | Pass A + config | No |
   | Branch behind base by > N commits | Compare API | No (penalty) |
   | Custom pre-merge checks from `.pragent.yaml` (natural-language assertions the LLM evaluates, e.g. "public API changes update CHANGELOG") | LLM, per check | Per-check `mode: error|warning` |
   | Memory rules marked `blocking: true` violated | P2 | Yes |

2. **Score formula** (versioned like the decider, `MERGE_POLICY_VERSION`): start at 100 and subtract weighted penalties (for example critical 40, major 12, minor 3, nit 0, failing check 30, missing approval 20, conflict 50, untested source change 8, behind base 5, and so on). Clamp to 0–100. **Any hard blocker caps the score at 49**, so the label never reads "ready" while something blocks. Bands: **≥ 85 Ready**, 70–84 Ready with nits, 50–69 Needs work, < 50 Blocked. Weights live in `.pragent.yaml` with safe defaults. A weights fixture (`merge_cases.json`) pins the behaviour, as `decider_cases.json` does now.
3. **Outputs**
   - A **Check Run "PR Agent · Merge readiness"** (App mode) whose conclusion depends on `merge_gate`: `advisory` → `neutral`, `required` → `failure` below the threshold. Users can add it to branch protection. In PAT mode it is a commit status instead.
   - A **sticky "Before you merge" comment**: the score badge, the band, and a checkbox checklist of blockers, then recommended items, each with a deep link to the finding, thread, or check. Items tick themselves off when resolved. Example:
     ```
     PR Agent merge readiness: 62/100 · Needs work
     Blockers
     - [ ] CI: `test (ubuntu)` failing — tests/test_store.py::test_eval_lock
     - [ ] Critical: possible token leak in logs — backend/main.py:311
     Should fix
     - [ ] 2 major findings unresolved (perf, error handling)
     - [ ] No tests added for backend/merge/score.py
     Nice to have
     - [ ] 4 nits · PR title not conventional
     ```
   - `GET /repos/{repo}/pulls/{n}/readiness` returns JSON for the desktop app and other agents.
4. **Approvability (Macroscope-style):** when the score is ≥ the threshold, there are no safety flags, the decider class is `SKIP`/`FAST_REVIEW`, and the repo opted in, the agent submits an `APPROVE` review (App identity) for low-risk PRs such as docs or lockfile bumps. It is off by default and never applies to safety-flagged paths.
5. **Desktop:** a **PR inbox** page that lists open PRs across deployed repos with their score, band, and top blockers. Clicking a PR opens the checklist and findings, with re-run, dismiss, and "teach the agent" actions.
6. **Actions "lite" mode:** a small composite step after PR-Agent (Python script shipped in the template) that computes a reduced score from conflicts, checks, the PR-Agent review's severity labels, and memory rules. It posts the same sticky checklist plus a commit status, so the core user ask also works without the backend.

**Done when:** the fixture tests pin the score for each band, a real PR's checklist updates live as CI goes green and threads resolve, and the Check Run can be set as required in branch protection.

### P5: Conversation and autofix

1. **Chat:** `issue_comment` and `pull_request_review_comment` mentioning `@pr-agent` (or the App's slug) route to an agent loop with the PR context, the thread, and memory. It can answer, explain, `remember`, `resolve`, `dismiss`, `review` (re-run), `summary`, `readiness`, and `help`. Commands are limited to OWNER/MEMBER/COLLABORATOR, as the Actions workflow does today.
2. **Feedback capture:** 👍/👎 reactions on findings (poll on review events) and thread replies feed memory confidence (P2.2).
3. **Fix-it:** `@pr-agent fix <finding|all-critical>` runs in a sandbox (Box). It applies the suggestion patches, or generates a fix, runs the tests and linters, then pushes a commit to the PR branch (same-repo branches only, with `autofix: push`) or opens a stacked PR (`autofix: stacked`). It reports the result in the thread. It never runs on fork PRs.
4. **Finishing touches:** `generate tests`, `generate docstrings`, and `update changelog`, each as stacked-PR commits.

### P6: Desktop deployment UX, reporting, and quality

1. **Deploy wizard** (extends Auto review): pick repos → choose a tier (*Lite: Actions* / *Full: App + backend*) → profile and merge-gate settings → seed memory (auto-index plus import) → deploy. Per-repo status shows App installed, config committed, memory size, last score posted.
2. **Analytics:** findings posted vs accepted vs dismissed per category, precision proxy, average score at merge, time to readiness, top recurring learnings. Built from the stores above with no new infrastructure.
3. **Digests (optional):** daily or weekly repo summaries (merged PRs, risky changes, open blockers) to Slack or email, generated on the existing cron (Cloudflare or Box).
4. **Eval harness:** `backend/tests/eval/` with a set of PR fixtures (diff, expected findings, expected blockers). It measures precision and recall of findings and stability of the score per model and prompt version. It gates prompt and model changes in CI, so free-model swaps don't silently degrade quality.

---

## 5. Sequencing and rough size

| Phase | Depends on | Size | Ships the user ask? |
|---|---|---|---|
| P0 Foundations | — | M | — |
| P1 Engine | P0 | L | Inline analysis |
| P2 Memory | P1 (findings and fingerprints) | M | **Repo memory** ✅ |
| P4 Merge readiness | P1 (+P2 for blocking rules) | M | **Score + blockers** ✅ |
| P3 Deep analysis | P1, sandboxes | L | Better findings and test signal |
| P5 Chat / autofix | P0, P2 | L | Teach / fix loop |
| P6 UX / analytics / evals | all | M | Polish and quality gate |

Recommended order: **P0 → P1 → P2 → P4 → P3 → P5 → P6**. P4 is pulled ahead of P3 because the score is already useful with CI, conflict, review, and finding signals, and P3 only makes it sharper. The Actions-lite bridges (P2.6, P4.6) can ship in parallel right after P2 and P4, because they only touch `autoreview.rs` and its template.

## 6. Risks and mitigations

- **Free-model quality and JSON reliability:** use strict schema prompts, retry once, run the verify pass, and use the eval harness. Allow a per-pass model override (a cheap model for verify, a stronger one for review).
- **Redis free-tier size:** memory caps per repo (for example 500 learnings), LRU pruning by `last_used`, findings TTL 30 days after close.
- **Noise and trust:** confidence threshold, a nit cap per review, the `chill` profile by default, and dismissal learning.
- **Security:** read `.pragent.yaml` and memory only from the base branch, never run fork code with secrets, autofix only on same-repo branches, keep the existing `${{` injection guard and the safety decider, and verify App webhook signatures (already done).
- **Two review paths duplicating comments:** the deploy wizard enforces one tier per repo (the "Both" mode already warns about this today).

## 7. Open questions for you

1. Is creating a **GitHub App** acceptable (required for real Check Runs, merge gating, and bot identity), or must everything stay PAT-only (commit status instead)?
2. Should learnings from merged PRs be **auto-approved** or always need your approval in the app? The proposed default is "needs approval".
3. Merge gate default: **advisory** (`neutral` Check Run) or **required** below the threshold?
4. Should autofix be allowed to **push to PR branches**, or only open stacked PRs?

---

## 8. Status check — Shipped production capabilities

**P0, P1, P2, P4, P5 (chat + feedback loop), and desktop/edge production hardening are shipped.**

| Capability | Status | Where |
|---|---|---|
| **GitHub App + Cloud settings + Event router (P0)** | ✅ **Shipped** | `backend/github_app.py`, `backend/events.py`, `backend/runtime_settings.py`, `src-tauri/src/github_app.rs` |
| **Native Structured Review Engine: walkthrough, inline committable suggestions, incremental `compare` diff, fingerprinting & `.pragent.yaml` (P1)** | ✅ **Shipped** | `backend/review/{config,diff,pipeline,publisher}.py`, `backend/sandboxes/providers_review.py` |
| **Repo Memory, dismissal learning, doc bootstrap & `.pr-agent/memory.md` Actions-lite bridge (P2)** | ✅ **Shipped** | `backend/memory/service.py`, `src/pages/memory.tsx`, `src-tauri/src/autoreview.rs` (v7), `src-tauri/src/main.rs` (`github_read_repo_file`, `github_commit_repo_file`) |
| **Merge Readiness (0–100) + Check Run / PAT status + sticky checklist + PR Inbox (P4)** | ✅ **Shipped** | `backend/merge/{signals,score,publisher}.py`, `backend/tests/merge_cases.json`, `src/pages/pr-inbox.tsx` |
| **`@pr-agent` PR Chat & feedback loop (`remember`, `dismiss`, `resolve`, `review`, `readiness`, `ask`, `help`) (P5a)** | ✅ **Shipped** | `backend/chat.py` (with `OWNER`/`MEMBER`/`COLLABORATOR` gate, bot + self-reply loop guards) |
| **Cloud Setup Wizard, Agent v2 UI & `.pragent.yaml` commit (P6a)** | ✅ **Shipped** | `src/pages/cloud-setup.tsx`, `src/pages/agents.tsx`, `src/lib/cloud-agent.ts` |
| **Windows DPAPI Local Secret Vault & Desktop Packaging** | ✅ **Shipped** | `src-tauri/src/vault.rs` (`%APPDATA%\com.pragent.control\vault.bin`), `src/lib/session.ts`, `src-tauri/tauri.conf.json` |
| **Cloudflare Worker Dual-Secret Forwarding** | ✅ **Shipped** | `cloudflare-worker/worker.js` (timing-safe HMAC-SHA256 against `GITHUB_WEBHOOK_SECRET` + `GITHUB_APP_WEBHOOK_SECRET`; all 11 GitHub App events with `x-github-event` & `x-github-delivery` preserved) |
| Linked-issue check, sandbox linters (`ruff`, `eslint`, `gitleaks`, `actionlint`) (P3) | Planned | Future |
| Approvability auto-approve & AST code graph (P3/P4.4) | Planned | Future |
| Fix-it push commits / stacked PRs (P5b) | Planned | Future (kept off until `contents: write` autofix workflow ships) |

### Known operational notes

- Saving secrets to a deployed backend needs `BACKEND_API_TOKEN` set on the backend (which
  also fails closed on unsigned webhooks unless `ALLOW_UNSIGNED_WEBHOOKS=1`) and Redis
  connected so settings, memory, findings, and readiness reports survive restarts. On
  Windows, opting in to **Remember securely on this PC** stores `BACKEND_API_TOKEN` (and
  optional desktop tokens) encrypted with current-user Windows DPAPI in
  `%APPDATA%\com.pragent.control\vault.bin`.
- GitHub's API cannot toggle an App's webhook *active* flag after creation; the wizard
  therefore checks for a public HTTPS base URL before creating the App, and **Fix webhook
  URL** repoints the URL/secret on existing Apps.
- If using the Cloudflare Worker in front of a GitHub App, set `GITHUB_APP_WEBHOOK_SECRET`
  via `wrangler secret put` to match the App's webhook secret (alongside
  `GITHUB_WEBHOOK_SECRET` if legacy repo webhooks are also active).
