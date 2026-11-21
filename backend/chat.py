"""PR chat & feedback loop: ``@pr-agent`` / ``@<app-slug>`` command handling.

Processes PR issue comments and inline review comments mentioning the agent:
- Strict authorization gate: only ``OWNER``, ``MEMBER``, and ``COLLABORATOR``
  human commenters may trigger actions; bots and untrusted external accounts
  are rejected without side effects or replies.
- Commands: ``remember`` (``learn``), ``dismiss`` (``wontfix``), ``resolve``,
  ``review`` (``re-review``), ``readiness`` (``score``), ``help``, and ``ask``.
- Replies inline on review threads (``POST /pulls/{n}/comments/{id}/replies``)
  with automatic fallback to PR issue comments (``POST /issues/{n}/comments``).
"""
from __future__ import annotations

import contextlib
import inspect
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

try:  # cwd=backend
    import datamodel as dm
    import events
    import github_app
    import store
    from memory import service as memory_service
    from sandboxes.providers_review import (
        DEFAULT_LLM_MODEL,
        LLM_PROVIDERS,
        OPENROUTER_FALLBACK_MODEL,
        _OPENROUTER_RETRY_STATUSES,
        _extract_choice_content,
        _sanitize_error_snippet,
        _split_files,
        llm_chat_request,
        llm_key_for,
        prepare_diff,
        resolve_llm_provider,
    )
except ImportError:  # pragma: no cover - `uvicorn backend.main:app` from repo root
    from backend import datamodel as dm  # type: ignore[no-redef]
    from backend import events  # type: ignore[no-redef]
    from backend import github_app  # type: ignore[no-redef]
    from backend import store  # type: ignore[no-redef]
    from backend.memory import service as memory_service  # type: ignore[no-redef]
    from backend.sandboxes.providers_review import (  # type: ignore[no-redef]
        DEFAULT_LLM_MODEL,
        LLM_PROVIDERS,
        OPENROUTER_FALLBACK_MODEL,
        _OPENROUTER_RETRY_STATUSES,
        _extract_choice_content,
        _sanitize_error_snippet,
        _split_files,
        llm_chat_request,
        llm_key_for,
        prepare_diff,
        resolve_llm_provider,
    )

log = logging.getLogger("pr-agent.chat")

GITHUB_API = "https://api.github.com"

ALLOWED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

# Invisible HTML marker appended to every reply we post so PAT-mode webhooks
# (which arrive with sender.type=="User" and author_association=="OWNER") can
# never trigger a self-reply loop.
SELF_MARKER_PREFIX = "<!-- pr-agent-control:"
CHAT_REPLY_MARKER = "<!-- pr-agent-control:chat-reply -->"

_FENCED_CODE_RE = re.compile(r"(?ms)^[ \t]*(```|~~~).*?(?:^[ \t]*\1[ \t]*$|\Z)")
_BLOCKQUOTE_LINE_RE = re.compile(r"(?m)^[ \t]*>.*$")
_INLINE_CODE_RE = re.compile(r"`+[^`\n]+`+")

_COMMAND_ALIASES: dict[str, str] = {
    "remember": "remember",
    "learn": "remember",
    "dismiss": "dismiss",
    "wontfix": "dismiss",
    "won'tfix": "dismiss",
    "false-positive": "dismiss",
    "ignore": "dismiss",
    "resolve": "resolve",
    "review": "review",
    "re-review": "review",
    "rereview": "review",
    "readiness": "readiness",
    "score": "readiness",
    "merge-readiness": "readiness",
    "help": "help",
    "commands": "help",
    "ask": "ask",
}

_SEVERITY_RANK = {"critical": 4, "major": 3, "minor": 2, "nit": 1}

_ASK_SYSTEM_PROMPT = (
    "You are PR Agent, a concise and accurate code review assistant replying inside a GitHub pull request thread. "
    "Ground your answer strictly in the provided PR title, diff snippet, and repository memories. "
    "Do not speculate about code not shown. Keep replies clear, direct, and under 250 words."
)

_HELP_TEXT = """**PR Agent commands**

- `@pr-agent remember <rule>` *(or `learn`)* — Save a repo-specific convention (scoped to the current file on inline threads)
- `@pr-agent dismiss [reason]` *(or `wontfix`)* — Dismiss the finding on this thread and teach the agent to suppress similar false positives
- `@pr-agent resolve` — Mark the finding on this thread as resolved
- `@pr-agent review` *(or `re-review`)* — Trigger a fresh review of this PR
- `@pr-agent readiness` *(or `score`)* — Recompute and report the merge-readiness score
- `@pr-agent ask <question>` *(or any question after the mention)* — Ask about this PR grounded in the diff and repo memory
- `@pr-agent help` — Show this command list"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mention_regex() -> re.Pattern[str]:
    names = {"pr-agent"}
    slug = github_app.app_slug()
    if slug:
        names.update({slug, f"{slug}[bot]"})
    alts = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    return re.compile(rf"(?<![\w@])@(?:{alts})(?![\w-])", re.IGNORECASE)


def _mask_non_command_markdown(text: str) -> str:
    """Replace fenced code blocks, blockquote lines, and inline code spans with
    equal-length spaces so mention scanning ignores quoted/code examples while
    preserving exact character offsets in the original text."""
    def _spaces(match: re.Match[str]) -> str:
        return " " * (match.end() - match.start())

    masked = _FENCED_CODE_RE.sub(_spaces, text)
    masked = _BLOCKQUOTE_LINE_RE.sub(_spaces, masked)
    masked = _INLINE_CODE_RE.sub(_spaces, masked)
    return masked


def parse_agent_command(body: str) -> tuple[str, str] | None:
    """Parse ``@pr-agent`` / ``@<app-slug>`` command mentions into ``(cmd, arg)``.

    Returns ``None`` if ``body`` carries a PR-Agent self-reply marker or does
    not contain an active mention outside code blocks, inline backticks, and
    quoted lines. ``cmd`` is always one of: ``remember``, ``dismiss``,
    ``resolve``, ``review``, ``readiness``, ``help``, ``ask``.
    """
    text = str(body or "")
    if SELF_MARKER_PREFIX in text:
        return None
    masked = _mask_non_command_markdown(text)
    if not events.mentions_agent(masked):
        return None
    match = _mention_regex().search(masked)
    if match is None:
        return None

    remainder = text[match.end():].lstrip(" \t:,;-")
    remainder = remainder.strip()
    if not remainder:
        return ("help", "")

    # Two-word "won't fix" / "wont fix" alias for dismiss.
    wont_fix = re.match(r"(?i)^won'?t\s+fix\b[\s:,-]*(.*)$", remainder, flags=re.DOTALL)
    if wont_fix:
        return ("dismiss", wont_fix.group(1).strip())

    parts = remainder.split(None, 1)
    first_raw = parts[0].lstrip("/").rstrip(":,;-").lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    if first_raw in _COMMAND_ALIASES:
        cmd = _COMMAND_ALIASES[first_raw]
        return (cmd, rest)

    # Free-form question / prompt after the mention -> route to `ask`.
    return ("ask", remainder)


def _norm_rel_path(path: str) -> str:
    s = str(path or "").strip().replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return s.lstrip("/")


def _load_pr_findings(repo: str, pr_number: int) -> list[tuple[str, dict[str, Any]]]:
    prefix = f"finding:{repo.lower()}:{int(pr_number)}:"
    keys = store.rkeys(f"{prefix}*")
    if not keys:
        return []
    out: list[tuple[str, dict[str, Any]]] = []
    for key, raw in zip(keys, store.rget_json_many(keys)):
        if not isinstance(raw, dict):
            continue
        fp = str(raw.get("fingerprint") or key[len(prefix):]).strip()
        if fp:
            out.append((fp, dict(raw)))
    return out


def _finding_comment_ids(finding: dict[str, Any]) -> set[int]:
    ids: set[int] = set()
    for field in ("comment_id", "github_comment_id", "review_comment_id", "gh_comment_id"):
        val = finding.get(field)
        try:
            num = int(val)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if num > 0:
            ids.add(num)
    return ids


def _resolve_finding_target(
    repo: str,
    pr_number: int,
    *,
    comment_id: int = 0,
    in_reply_to: int | None = None,
    path: str = "",
    query: str = "",
) -> tuple[tuple[str, dict[str, Any]] | None, list[tuple[str, dict[str, Any]]]]:
    """Resolve a target finding for `dismiss` / `resolve`.

    Returns ``(matched, ambiguous_candidates)``:
    - ``matched`` is ``(fingerprint, finding)`` when uniquely identified.
    - ``ambiguous_candidates`` is non-empty when multiple open findings could
      match and the caller should request clarification rather than guess.
    """
    findings = _load_pr_findings(repo, pr_number)
    if not findings:
        return None, []

    supplied_ids = {int(t) for t in (in_reply_to, comment_id) if t and int(t) > 0}

    # 1. Exact match by thread root (`in_reply_to`) or comment_id.
    for target_id in (in_reply_to, comment_id):
        if target_id and int(target_id) > 0:
            tid = int(target_id)
            for fp, item in findings:
                if tid in _finding_comment_ids(item):
                    return (fp, item), []

    # 2. Exact match by explicit fingerprint in query (takes precedence over file fallback).
    clean_query = str(query or "").strip().lower()
    if clean_query:
        for fp, item in findings:
            if fp.lower() in clean_query:
                return (fp, item), []

    # 3. Match by normalized file path (only when unambiguous — never guess by severity alone).
    norm_path = _norm_rel_path(path)
    query_tokens = set(re.findall(r"[a-z0-9]{3,}", clean_query))
    if norm_path:
        path_matches = [
            (fp, item)
            for fp, item in findings
            if _norm_rel_path(str(item.get("file") or item.get("path") or "")) == norm_path
        ]
        if path_matches:
            open_matches = [pair for pair in path_matches if pair[1].get("state", "open") == "open"]
            candidates = open_matches or path_matches

            if query_tokens:
                scored_path: list[tuple[int, str, dict[str, Any]]] = []
                for fp, item in candidates:
                    title_tokens = set(re.findall(r"[a-z0-9]{3,}", str(item.get("title") or "").lower()))
                    overlap = len(query_tokens & title_tokens)
                    if overlap > 0:
                        scored_path.append((overlap, fp, item))
                if scored_path:
                    scored_path.sort(key=lambda t: t[0], reverse=True)
                    top_overlap = scored_path[0][0]
                    top_tied = [(fp, item) for ov, fp, item in scored_path if ov == top_overlap]
                    if len(top_tied) == 1:
                        return top_tied[0], []
                    return None, top_tied

            # If a comment/thread ID was supplied and didn't match step 1, do not fall back
            # blindly to findings that already belong to a different GitHub comment thread.
            if supplied_ids:
                unlinked = [pair for pair in candidates if not _finding_comment_ids(pair[1])]
                if len(unlinked) == 1 and len(candidates) == 1:
                    return unlinked[0], []
                if len(candidates) > 1:
                    return None, candidates
                return None, []

            if len(candidates) == 1:
                return candidates[0], []
            return None, candidates

    # 4. Match by title tokens in query (for PR-level comments without a file path).
    if query_tokens:
        scored: list[tuple[int, str, dict[str, Any]]] = []
        for fp, item in findings:
            if item.get("state", "open") != "open":
                continue
            title_tokens = set(re.findall(r"[a-z0-9]{3,}", str(item.get("title") or "").lower()))
            overlap = len(query_tokens & title_tokens)
            if overlap > 0:
                scored.append((overlap, fp, item))
        if scored:
            scored.sort(key=lambda t: t[0], reverse=True)
            top_overlap = scored[0][0]
            top_tied = [(fp, item) for ov, fp, item in scored if ov == top_overlap]
            if len(top_tied) == 1:
                return top_tied[0], []
            return None, top_tied

    return None, []


def _find_matching_finding(
    repo: str,
    pr_number: int,
    *,
    comment_id: int = 0,
    in_reply_to: int | None = None,
    path: str = "",
    query: str = "",
) -> tuple[str, dict[str, Any]] | None:
    matched, _ = _resolve_finding_target(
        repo,
        pr_number,
        comment_id=comment_id,
        in_reply_to=in_reply_to,
        path=path,
        query=query,
    )
    return matched


async def _invoke_callback(fn: Callable[..., Any], repo: str, pr_number: int) -> Any:
    try:
        sig = inspect.signature(fn)
        params = [
            p for p in sig.parameters.values()
            if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values())
        if has_varargs or len(params) >= 2:
            call_args: tuple[Any, ...] = (repo, pr_number)
        elif len(params) == 1:
            call_args = (pr_number,)
        else:
            call_args = ()
    except (TypeError, ValueError):
        call_args = (repo, pr_number)
    res = fn(*call_args)
    if inspect.isawaitable(res):
        return await res
    return res


async def _fetch_pr_context(
    http: httpx.AsyncClient,
    repo: str,
    pr_number: int,
    token: str,
) -> tuple[str, str]:
    """Best-effort fetch of PR title and budgeted diff snippet for `ask`."""
    if not token:
        return "", ""
    base_headers = {
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    title = ""
    diff_snippet = ""
    try:
        meta_resp = await http.get(
            f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}",
            headers={**base_headers, "Accept": "application/vnd.github+json"},
        )
        if meta_resp.status_code == 200:
            meta = meta_resp.json()
            if isinstance(meta, dict):
                title = str(meta.get("title") or "").strip()
    except Exception as exc:
        log.warning("chat.ask: PR metadata fetch failed (%s)", type(exc).__name__)

    try:
        diff_resp = await http.get(
            f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}",
            headers={**base_headers, "Accept": "application/vnd.github.diff"},
        )
        if diff_resp.status_code == 200 and diff_resp.text:
            diff_snippet, _ = prepare_diff(diff_resp.text, budget=12000)
    except Exception as exc:
        log.warning("chat.ask: PR diff fetch failed (%s)", type(exc).__name__)

    return title, diff_snippet


async def _answer_question(
    repo: str,
    pr_number: int,
    question: str,
    *,
    path: str = "",
    token: str = "",
    client: httpx.AsyncClient | None = None,
) -> tuple[str, dict[str, Any]]:
    """Answer a PR question via the configured LLM provider grounded in PR diff + repo memory."""
    llm_provider = resolve_llm_provider(os.environ.get("LLM_PROVIDER"))
    llm_key, llm_key_env = llm_key_for(llm_provider)
    if not llm_key:
        return (
            f"I can't answer right now because `{llm_key_env}` is not configured on the backend.",
            {"ok": False, "reason": "missing_llm_key", "provider": llm_provider},
        )

    configured_model = os.environ.get("LLM_MODEL", "").strip()
    if not configured_model or (
        llm_provider != "openrouter"
        and (configured_model == DEFAULT_LLM_MODEL or configured_model.startswith("openrouter/"))
    ):
        configured_model = LLM_PROVIDERS[llm_provider]["default_model"]

    client_cm = contextlib.nullcontext(client) if client is not None else httpx.AsyncClient(timeout=60.0)
    async with client_cm as http:
        pr_title, diff_snippet = await _fetch_pr_context(http, repo, pr_number, token)

        changed_paths: list[str] = []
        if path.strip():
            changed_paths.append(_norm_rel_path(path))
        if diff_snippet:
            for p, _ in _split_files(diff_snippet):
                np = _norm_rel_path(p)
                if np and np not in changed_paths:
                    changed_paths.append(np)

        mem_lines: list[str] = []
        used_mem_ids: list[str] = []
        if changed_paths:
            retrieved = memory_service.retrieve_for_diff(repo, changed_paths, diff_snippet, max_chars=2500)
            for m in retrieved["rules"]:
                mem_lines.append(f"- [{m['id']}] {m['text']}")
            used_mem_ids = list(retrieved["used_ids"])
        else:
            for m in memory_service.list_memories(repo, status="active")[:8]:
                if m.get("kind") != "dismissed_pattern":
                    mem_lines.append(f"- [{m['id']}] {m['text']}")
                    used_mem_ids.append(str(m["id"]))

        prompt_parts = [f"Repository: {repo}", f"Pull Request: #{pr_number}"]
        if pr_title:
            prompt_parts.append(f"Title: {pr_title}")
        if path.strip():
            prompt_parts.append(f"File under discussion: {path.strip()}")
        if mem_lines:
            prompt_parts.append("Repository memories & conventions:\n" + "\n".join(mem_lines))
        if diff_snippet:
            prompt_parts.append("PR diff snippet:\n" + diff_snippet)
        prompt_parts.append(f"Question:\n{question.strip()}")
        user_prompt = "\n\n".join(prompt_parts)

        llm_url, extra_headers, send_model = llm_chat_request(llm_provider, configured_model, repo, pr_number)
        req_headers = {
            "Authorization": f"Bearer {llm_key}",
            "Content-Type": "application/json",
            **extra_headers,
        }
        messages = [
            {"role": "system", "content": _ASK_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        answer = ""
        used_model = send_model
        try:
            resp = await http.post(
                llm_url,
                headers=req_headers,
                json={"model": send_model, "messages": messages},
            )
            if resp.status_code == 200:
                answer = _extract_choice_content(resp.json())
            if (
                llm_provider == "openrouter"
                and send_model != OPENROUTER_FALLBACK_MODEL
                and (resp.status_code in _OPENROUTER_RETRY_STATUSES or (resp.status_code == 200 and not answer))
            ):
                used_model = OPENROUTER_FALLBACK_MODEL
                resp = await http.post(
                    llm_url,
                    headers=req_headers,
                    json={"model": OPENROUTER_FALLBACK_MODEL, "messages": messages},
                )
                if resp.status_code == 200:
                    answer = _extract_choice_content(resp.json())
            if resp.status_code != 200 or not answer:
                snippet = _sanitize_error_snippet(resp, llm_key)
                detail = f" ({snippet})" if snippet else ""
                return (
                    f"Sorry, I couldn't generate an answer right now (LLM HTTP {resp.status_code}{detail}).",
                    {"ok": False, "provider": llm_provider, "model": used_model, "status_code": resp.status_code},
                )
        except Exception as exc:
            log.warning("chat.ask: LLM call error (%s)", type(exc).__name__)
            return (
                f"Sorry, I couldn't generate an answer right now ({type(exc).__name__}).",
                {"ok": False, "provider": llm_provider, "model": used_model, "error": type(exc).__name__},
            )

        return (
            answer,
            {"ok": True, "provider": llm_provider, "model": used_model, "memory_ids": used_mem_ids},
        )


async def _post_github_reply(
    repo: str,
    pr_number: int,
    body: str,
    *,
    token: str,
    comment_id: int = 0,
    in_reply_to: int | None = None,
    is_review_comment: bool = False,
    client: httpx.AsyncClient | None = None,
) -> tuple[bool, str | None, int | None]:
    """Post reply on review thread when applicable, falling back to PR issue comment."""
    if not token:
        return False, None, None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    tagged_body = body if SELF_MARKER_PREFIX in body else f"{body}\n\n{CHAT_REPLY_MARKER}"
    client_cm = contextlib.nullcontext(client) if client is not None else httpx.AsyncClient(timeout=30.0)
    async with client_cm as http:
        reply_target = in_reply_to or comment_id
        if is_review_comment and reply_target:
            try:
                resp = await http.post(
                    f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/comments/{int(reply_target)}/replies",
                    headers=headers,
                    json={"body": tagged_body},
                )
                if resp.status_code in (200, 201):
                    return True, "review_comment_reply", resp.status_code
                log.warning(
                    "chat: review comment reply HTTP %s for %s #%d; falling back to issue comment",
                    resp.status_code, repo, pr_number,
                )
            except Exception as exc:
                log.warning("chat: review comment reply error (%s); falling back to issue comment", type(exc).__name__)

        try:
            resp = await http.post(
                f"{GITHUB_API}/repos/{repo}/issues/{pr_number}/comments",
                headers=headers,
                json={"body": tagged_body},
            )
            if resp.status_code in (200, 201):
                return True, "issue_comment", resp.status_code
            return False, "issue_comment", resp.status_code
        except Exception as exc:
            log.warning("chat: issue comment post failed (%s)", type(exc).__name__)
            return False, None, None


async def handle_pr_mention(
    repo: str,
    pr_number: int,
    *,
    body: str,
    author: str,
    association: str,
    comment_id: int = 0,
    path: str = "",
    in_reply_to: int | None = None,
    is_review_comment: bool = False,
    sender_is_bot: bool = False,
    trigger_review_fn: Callable[..., Any] | None = None,
    recompute_readiness_fn: Callable[..., Any] | None = None,
    github_token: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Handle one ``@pr-agent`` / ``@<app-slug>`` mention on a PR thread."""
    if not dm.valid_repo(repo):
        return {"handled": False, "authorized": False, "reason": "invalid_repo"}
    try:
        pr_num = int(pr_number)
        if pr_num <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return {"handled": False, "authorized": False, "reason": "invalid_pr_number"}

    if SELF_MARKER_PREFIX in str(body or ""):
        return {"handled": False, "authorized": False, "reason": "self_reply"}

    parsed = parse_agent_command(body)
    if parsed is None:
        return {"handled": False, "reason": "no_mention"}

    clean_author = str(author or "").strip()
    if sender_is_bot or clean_author.lower().endswith("[bot]"):
        return {
            "handled": False,
            "authorized": False,
            "reason": "bot_author",
            "author": clean_author,
        }

    assoc = str(association or "").strip().upper()
    if assoc not in ALLOWED_ASSOCIATIONS:
        return {
            "handled": False,
            "authorized": False,
            "reason": "unauthorized_association",
            "author": clean_author,
            "association": assoc,
        }

    cmd, arg = parsed
    if github_token is None:
        token, _ = await github_app.token_for_repo(repo)
    else:
        token = github_token

    result: dict[str, Any] = {
        "handled": True,
        "authorized": True,
        "command": cmd,
        "arg": arg,
        "repo": repo,
        "pr_number": pr_num,
        "author": clean_author,
    }
    reply_text = ""

    if cmd == "remember":
        if not arg.strip():
            reply_text = (
                "Please include the convention to remember, e.g. "
                "`@pr-agent remember Always validate webhook signatures.`"
            )
            result["created"] = False
        else:
            norm_path = _norm_rel_path(path)
            scope_globs = [norm_path] if norm_path else ["**"]
            mem = memory_service.add_memory(
                repo,
                {
                    "kind": "learning",
                    "text": arg.strip()[:dm.MEMORY_TEXT_MAX],
                    "scope_globs": scope_globs,
                    "source": "chat",
                    "author": clean_author[:80] or None,
                    "status": "active",
                    "confidence": 0.9,
                },
            )
            scope_display = ", ".join(f"`{g}`" for g in mem["scope_globs"])
            reply_text = (
                f"Saved repo learning `{mem['id']}` (scope: {scope_display}).\n\n"
                f"> {mem['text']}"
            )
            result["created"] = True
            result["memory"] = mem

    elif cmd == "dismiss":
        matched, ambiguous = _resolve_finding_target(
            repo,
            pr_num,
            comment_id=comment_id,
            in_reply_to=in_reply_to,
            path=path,
            query=arg,
        )
        if matched is not None:
            fp, finding = matched
            dismissal = memory_service.record_finding_dismissal(
                repo,
                pr_num,
                fp,
                finding=finding,
                reason=arg,
                author=clean_author,
            )
            result["dismissal"] = dismissal
            title = str(finding.get("title") or fp)
            mem = dismissal.get("memory")
            if isinstance(mem, dict):
                reply_text = (
                    f"Dismissed finding **{title}** (`{fp[:12]}`) and updated suppression pattern "
                    f"`{mem['id']}` (scope: `{', '.join(mem.get('scope_globs') or ['**'])}`). "
                    "Future reviews will avoid repeating this pattern."
                )
            else:
                reply_text = (
                    f"Dismissed finding **{title}** (`{fp[:12]}`). "
                    f"Recorded dismissal #{dismissal['dismissal_count']} — add a reason or dismiss once more "
                    "to suppress this pattern repo-wide."
                )
        elif ambiguous:
            result["dismissal"] = None
            result["ambiguous"] = True
            opts = "\n".join(
                f"- `{fp[:16]}` — {item.get('title') or '(untitled)'}"
                for fp, item in ambiguous[:5]
            )
            reply_text = (
                "Multiple findings match here; please reply directly on the finding's thread or "
                "include its fingerprint:\n\n" + opts
            )
        elif arg.strip() or path.strip():
            norm_path = _norm_rel_path(path)
            synth_title = arg.strip()[:120] or f"finding on {norm_path}"
            synth_fp = dm.finding_fingerprint(norm_path, "", "maintainability", synth_title)
            dismissal = memory_service.record_finding_dismissal(
                repo,
                pr_num,
                synth_fp,
                finding={"file": norm_path, "category": "maintainability", "title": synth_title},
                reason=arg or f"Dismissed by {clean_author}",
                author=clean_author,
            )
            result["dismissal"] = dismissal
            mem = dismissal.get("memory")
            mem_id = mem["id"] if isinstance(mem, dict) else synth_fp[:12]
            reply_text = (
                f"Recorded dismissal pattern `{mem_id}` — future reviews will suppress similar findings."
            )
        else:
            result["dismissal"] = None
            reply_text = (
                "I couldn't match an active finding on this thread. "
                "Reply directly on a finding comment or include a reason, e.g. `@pr-agent dismiss intentional fallback`."
            )

    elif cmd == "resolve":
        matched, ambiguous = _resolve_finding_target(
            repo,
            pr_num,
            comment_id=comment_id,
            in_reply_to=in_reply_to,
            path=path,
            query=arg,
        )
        if matched is not None:
            fp, finding = matched
            now_iso = _now_iso()
            finding["state"] = "resolved"
            finding["resolved_at"] = now_iso
            if clean_author:
                finding["resolved_by"] = clean_author
            if dm._FINGERPRINT_RE.fullmatch(fp):
                store.rset_json(dm.finding_key(repo, pr_num, fp), finding, ex=dm.FINDING_TTL_S)
            store.rset(dm.dirty_key(repo, pr_num), f"{now_iso}:{uuid.uuid4().hex[:8]}", ex=dm.DIRTY_TTL_S)
            result["resolved"] = True
            result["finding"] = finding
            title = str(finding.get("title") or fp)
            reply_text = f"Marked finding **{title}** (`{fp[:12]}`) as resolved."
        elif ambiguous:
            result["resolved"] = False
            result["ambiguous"] = True
            opts = "\n".join(
                f"- `{fp[:16]}` — {item.get('title') or '(untitled)'}"
                for fp, item in ambiguous[:5]
            )
            reply_text = (
                "Multiple findings match here; please reply directly on the finding's thread or "
                "include its fingerprint to resolve:\n\n" + opts
            )
        else:
            result["resolved"] = False
            reply_text = "No matching finding was found on this thread to resolve."

    elif cmd == "review":
        if trigger_review_fn is not None:
            try:
                rev = await _invoke_callback(trigger_review_fn, repo, pr_num)
                result["review"] = rev
                if isinstance(rev, dict):
                    status = str(rev.get("status") or "queued")
                    run_id = rev.get("run_id") or rev.get("id")
                    suffix = f" (run `{run_id}`)" if run_id else ""
                    reply_text = f"Review {status} for PR #{pr_num}{suffix}."
                else:
                    reply_text = f"Triggered a fresh review for PR #{pr_num}."
            except Exception as exc:
                log.warning("chat.review: trigger failed (%s)", type(exc).__name__)
                result["review_error"] = type(exc).__name__
                reply_text = f"Failed to trigger a review right now ({type(exc).__name__})."
        else:
            result["review"] = None
            reply_text = "Review triggering is not enabled in this handler."

    elif cmd == "readiness":
        report: dict[str, Any] | None = None
        if recompute_readiness_fn is not None:
            try:
                raw_report = await _invoke_callback(recompute_readiness_fn, repo, pr_num)
                if hasattr(raw_report, "model_dump"):
                    report = raw_report.model_dump()
                elif isinstance(raw_report, dict):
                    report = raw_report
            except Exception as exc:
                log.warning("chat.readiness: recompute failed (%s)", type(exc).__name__)
                result["readiness_error"] = type(exc).__name__
                reply_text = f"Failed to recompute merge readiness ({type(exc).__name__})."
        else:
            cached = store.rget_json(dm.merge_report_key(repo, pr_num))
            if isinstance(cached, dict):
                report = cached

        if report is not None:
            result["readiness"] = report
            score = report.get("score", "?")
            band = str(report.get("band") or "unknown").replace("_", " ")
            blockers = [
                it for it in (report.get("items") or [])
                if isinstance(it, dict) and it.get("kind") == "blocker" and not it.get("done")
            ]
            reply_lines = [f"Merge readiness for PR #{pr_num}: **{score}/100** · `{band}`"]
            if blockers:
                reply_lines.append("")
                reply_lines.append("**Blockers:**")
                for b in blockers[:5]:
                    reply_lines.append(f"- {b.get('text') or b.get('code')}")
            reply_text = "\n".join(reply_lines)
        elif not reply_text:
            result["readiness"] = None
            reply_text = f"No merge-readiness report is available for PR #{pr_num} yet."

    elif cmd == "help":
        reply_text = _HELP_TEXT

    elif cmd == "ask":
        if not arg.strip():
            result["ask"] = {"ok": False, "reason": "empty_question"}
            reply_text = (
                "Please include a question after `@pr-agent ask`, e.g. "
                "`@pr-agent ask What does this change affect?`"
            )
        else:
            answer, meta = await _answer_question(
                repo,
                pr_num,
                arg,
                path=path,
                token=token,
                client=client,
            )
            result["ask"] = meta
            reply_text = answer

    reply_posted, reply_kind, reply_status = await _post_github_reply(
        repo,
        pr_num,
        reply_text,
        token=token,
        comment_id=comment_id,
        in_reply_to=in_reply_to,
        is_review_comment=is_review_comment,
        client=client,
    )
    result["reply"] = reply_text
    result["reply_posted"] = reply_posted
    result["reply_kind"] = reply_kind
    if reply_status is not None:
        result["reply_status"] = reply_status
    return result
