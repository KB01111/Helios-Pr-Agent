"""Multi-pass structured PR review pipeline (Pass A Walkthrough, Pass B Findings, Pass C Verifier).

Reuses the existing LLM routing, OpenRouter fallback, and glob helpers from
`backend/sandboxes/providers_review.py` with zero additional pip dependencies.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from typing import Any, Awaitable, Callable, Sequence

import httpx

try:  # cwd=backend / PYTHONPATH=backend
    import datamodel as dm
    from sandboxes.providers_review import (
        DEFAULT_LLM_MODEL,
        DEFAULT_SYSTEM_PROMPT,
        LLM_PROVIDERS,
        OPENROUTER_FALLBACK_MODEL,
        _OPENROUTER_RETRY_STATUSES,
        _extract_choice_content,
        _glob_matches,
        _sanitize_error_snippet,
        llm_chat_request,
        llm_key_for,
        resolve_llm_provider,
    )
except ImportError:  # pragma: no cover - `uvicorn backend.main:app` from repo root
    from backend import datamodel as dm  # type: ignore[no-redef]
    from backend.sandboxes.providers_review import (  # type: ignore[no-redef]
        DEFAULT_LLM_MODEL,
        DEFAULT_SYSTEM_PROMPT,
        LLM_PROVIDERS,
        OPENROUTER_FALLBACK_MODEL,
        _OPENROUTER_RETRY_STATUSES,
        _extract_choice_content,
        _glob_matches,
        _sanitize_error_snippet,
        llm_chat_request,
        llm_key_for,
        resolve_llm_provider,
    )

from .diff import DiffFile, ParsedDiff

log = logging.getLogger("pr-agent.review.pipeline")

PROFILE_THRESHOLDS: dict[str, dict[str, Any]] = {
    "chill": {"min_confidence": 0.70, "max_nits": 2},
    "assertive": {"min_confidence": 0.50, "max_nits": 6},
}

_SEVERITY_ORDER: dict[str, int] = {
    "critical": 0,
    "major": 1,
    "minor": 2,
    "nit": 3,
}

_SEVERITY_ALIASES: dict[str, str] = {
    "critical": "critical",
    "blocker": "critical",
    "high": "critical",
    "severe": "critical",
    "major": "major",
    "medium": "major",
    "warning": "major",
    "warn": "major",
    "minor": "minor",
    "low": "minor",
    "info": "minor",
    "suggestion": "minor",
    "nit": "nit",
    "nitpick": "nit",
    "trivial": "nit",
}

_CATEGORY_ALIASES: dict[str, str] = {
    "bug": "bug",
    "correctness": "bug",
    "logic": "bug",
    "error": "bug",
    "security": "security",
    "sec": "security",
    "vulnerability": "security",
    "vuln": "security",
    "perf": "perf",
    "performance": "perf",
    "maintainability": "maintainability",
    "refactor": "maintainability",
    "readability": "maintainability",
    "quality": "maintainability",
    "architecture": "maintainability",
    "style": "style",
    "formatting": "style",
    "lint": "style",
    "test": "test",
    "tests": "test",
    "testing": "test",
    "docs": "docs",
    "doc": "docs",
    "documentation": "docs",
}

_STOP_WORDS = frozenset({
    "the", "and", "for", "with", "that", "this", "from", "into", "when", "where",
    "should", "would", "could", "have", "been", "being", "about", "there", "their",
    "ignore", "dismiss", "dismissed", "skip", "wontfix", "dont", "never", "flag",
    "report", "pattern", "finding", "issue", "code", "file", "line", "lines",
})

LLMCallable = Callable[[list[dict[str, str]], bool], Awaitable[str]]


class LLMCallError(RuntimeError):
    """Raised when the upstream LLM request fails (HTTP or transport error)."""


# ---------------------------------------------------------------------------
# Tolerant JSON extraction & suggestion fence cleanup
# ---------------------------------------------------------------------------


def _strip_trailing_commas(raw: str) -> str:
    """Remove trailing commas before `}` or `]` outside JSON string literals."""
    out: list[str] = []
    in_str = False
    escaped = False
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if in_str:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == ",":
            j = i + 1
            while j < n and raw[j] in " \t\r\n":
                j += 1
            if j < n and raw[j] in "}]":
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _try_loads(candidate: str) -> Any | None:
    s = candidate.strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        pass
    cleaned = _strip_trailing_commas(s)
    if cleaned != s:
        try:
            return json.loads(cleaned)
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def _scan_balanced_json(text: str) -> list[str]:
    """Find balanced `{...}` or `[...]` substrings while respecting JSON string quoting."""
    results: list[str] = []
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if ch not in "{[":
            i += 1
            continue
        open_ch = ch
        close_ch = "}" if ch == "{" else "]"
        depth = 0
        in_str = False
        escaped = False
        j = i
        while j < n:
            c = text[j]
            if in_str:
                if escaped:
                    escaped = False
                elif c == "\\":
                    escaped = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == open_ch:
                    depth += 1
                elif c == close_ch:
                    depth -= 1
                    if depth == 0:
                        results.append(text[i : j + 1])
                        i = j
                        break
            j += 1
        i += 1
    return results


def extract_json(text: str) -> Any | None:
    """Tolerantly extract a JSON object or array from an LLM response."""
    if not text or not isinstance(text, str):
        return None
    stripped = text.strip()
    direct = _try_loads(stripped)
    if isinstance(direct, (dict, list)):
        return direct

    # 1. Markdown fenced code blocks (prefer ```json blocks first)
    fenced = re.findall(r"```(?:json|JSON)?\s*\n(.*?)```", stripped, flags=re.DOTALL)
    for block in fenced:
        parsed = _try_loads(block)
        if isinstance(parsed, (dict, list)):
            return parsed

    # 2. Balanced brace/bracket extraction from surrounding prose
    for candidate in _scan_balanced_json(stripped):
        parsed = _try_loads(candidate)
        if isinstance(parsed, (dict, list)):
            return parsed

    return None


def strip_code_fences(patch: str | None) -> str | None:
    """Strip outer markdown code fences (e.g. ```suggestion / ```python) while
    preserving internal code indentation."""
    if patch is None or not isinstance(patch, str):
        return None
    s = patch.replace("\r\n", "\n").replace("\r", "\n")
    if not s.strip():
        return None
    lines = s.split("\n")
    # Drop leading/trailing empty lines outside fences
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) >= 2 and re.match(r"^\s*`{3,}[a-zA-Z0-9_+-]*\s*$", lines[0]) and re.match(r"^\s*`{3,}\s*$", lines[-1]):
        lines = lines[1:-1]
    result = "\n".join(lines).rstrip("\n")
    return result if result.strip() else None


def _strip_mermaid_fences(diagram: Any) -> str | None:
    if not isinstance(diagram, str):
        return None
    cleaned = strip_code_fences(diagram)
    if not cleaned:
        return None
    low = cleaned.strip().lower()
    if low in ("null", "none", "n/a", "false"):
        return None
    return cleaned.strip()


# ---------------------------------------------------------------------------
# LLM transport helper
# ---------------------------------------------------------------------------


def resolve_llm_runtime(
    *,
    provider: str | None = None,
    model: str | None = None,
) -> tuple[str, str, str, str]:
    """Return `(provider, model, api_key, api_key_env)` using existing backend env conventions."""
    llm_provider = resolve_llm_provider(provider if provider is not None else os.environ.get("LLM_PROVIDER"))
    llm_key, llm_key_env = llm_key_for(llm_provider)
    configured_model = (model if model is not None else os.environ.get("LLM_MODEL", "")).strip()
    if not configured_model or (
        llm_provider != "openrouter"
        and (configured_model == DEFAULT_LLM_MODEL or configured_model.startswith("openrouter/"))
    ):
        configured_model = LLM_PROVIDERS[llm_provider]["default_model"]
    return llm_provider, configured_model, llm_key, llm_key_env


async def call_llm(
    messages: list[dict[str, str]],
    *,
    repo: str,
    pr_number: int,
    json_mode: bool = True,
    provider: str | None = None,
    model: str | None = None,
    client: Any = None,
    llm_call: LLMCallable | None = None,
    logs: list[str] | None = None,
) -> str:
    """Invoke the configured LLM provider with JSON-mode fallback and OpenRouter retry."""
    if llm_call is not None:
        return await llm_call(messages, json_mode)

    llm_provider, llm_model, llm_key, llm_key_env = resolve_llm_runtime(provider=provider, model=model)
    if not llm_key:
        raise LLMCallError(f"{llm_key_env} missing — cannot call LLM")

    llm_url, extra_headers, send_model = llm_chat_request(llm_provider, llm_model, repo, int(pr_number or 0))
    req_headers = {
        "Authorization": f"Bearer {llm_key}",
        "Content-Type": "application/json",
        **extra_headers,
    }

    async def _post(http_client: Any, target_model: str, use_json_mode: bool) -> Any:
        payload: dict[str, Any] = {"model": target_model, "messages": messages}
        if use_json_mode:
            payload["response_format"] = {"type": "json_object"}
        return await http_client.post(llm_url, headers=req_headers, json=payload)

    own = client is None
    http_client = client or httpx.AsyncClient(timeout=120.0)
    try:
        resp = await _post(http_client, send_model, json_mode)
        # Some free models/endpoints reject response_format; retry once without it.
        if json_mode and resp.status_code in (400, 422):
            if logs is not None:
                logs.append(f"review: model '{send_model}' rejected json_object mode (HTTP {resp.status_code}); retrying plain prompt.")
            resp = await _post(http_client, send_model, False)

        content = _extract_choice_content(resp.json()) if resp.status_code == 200 else ""

        if (
            llm_provider == "openrouter"
            and send_model != OPENROUTER_FALLBACK_MODEL
            and (resp.status_code in _OPENROUTER_RETRY_STATUSES or (resp.status_code == 200 and not content))
        ):
            reason = f"HTTP {resp.status_code}" if resp.status_code != 200 else "empty response"
            if logs is not None:
                logs.append(
                    f"review: primary model '{send_model}' returned {reason}; retrying with {OPENROUTER_FALLBACK_MODEL}."
                )
            resp = await _post(http_client, OPENROUTER_FALLBACK_MODEL, False)
            if resp.status_code == 200:
                content = _extract_choice_content(resp.json())

        if resp.status_code != 200:
            snippet = _sanitize_error_snippet(resp, llm_key)
            raise LLMCallError(
                f"LLM call failed HTTP {resp.status_code}" + (f" ({snippet})" if snippet else "")
            )
        return content
    finally:
        if own and hasattr(http_client, "aclose"):
            await http_client.aclose()


# ---------------------------------------------------------------------------
# Pass A: Structured Walkthrough
# ---------------------------------------------------------------------------


def _heuristic_effort(parsed_diff: ParsedDiff) -> int:
    churn = parsed_diff.total_additions + parsed_diff.total_deletions
    n_files = len(parsed_diff.files)
    if churn <= 30 and n_files <= 2:
        return 1
    if churn <= 150 and n_files <= 5:
        return 2
    if churn <= 400 and n_files <= 12:
        return 3
    if churn <= 1000 and n_files <= 25:
        return 4
    return 5


def _default_file_table(parsed_diff: ParsedDiff) -> list[dict[str, str]]:
    return [
        {
            "file": df.path,
            "change_summary": f"{df.status.capitalize()} (+{df.additions}/-{df.deletions})",
        }
        for df in parsed_diff.files[:40]
    ]


def _normalize_walkthrough(data: dict[str, Any], parsed_diff: ParsedDiff) -> dict[str, Any] | None:
    summary = str(data.get("summary") or "").strip()
    if not summary:
        return None
    raw_effort = data.get("effort")
    try:
        effort = max(1, min(5, int(round(float(raw_effort))))) if raw_effort is not None else _heuristic_effort(parsed_diff)
    except (TypeError, ValueError):
        effort = _heuristic_effort(parsed_diff)

    file_table: list[dict[str, str]] = []
    raw_table = data.get("file_table")
    if isinstance(raw_table, list):
        for row in raw_table:
            if not isinstance(row, dict):
                continue
            fpath = str(row.get("file") or row.get("path") or "").strip()
            csum = str(row.get("change_summary") or row.get("summary") or "").strip()
            if fpath and csum:
                file_table.append({"file": fpath, "change_summary": csum[:400]})
    if not file_table:
        file_table = _default_file_table(parsed_diff)

    diagram = _strip_mermaid_fences(data.get("sequence_diagram"))
    return {
        "summary": summary[:4000],
        "effort": effort,
        "file_table": file_table,
        "sequence_diagram": diagram,
    }


async def generate_walkthrough(
    parsed_diff: ParsedDiff,
    pr_meta: dict[str, Any],
    *,
    budgeted_diff: str = "",
    cut_notes: Sequence[str] | None = None,
    config: dict[str, Any] | None = None,
    client: Any = None,
    llm_call: LLMCallable | None = None,
    logs: list[str] | None = None,
) -> dict[str, Any]:
    """Pass A: Generate structured PR walkthrough JSON (with tolerant extractor, 1 retry, and markdown fallback)."""
    repo = str(pr_meta.get("repo") or "owner/repo")
    pr_number = int(pr_meta.get("pr_number") or 0)
    title = str(pr_meta.get("title") or "").strip()
    description = str(pr_meta.get("body") or pr_meta.get("description") or "").strip()[:1500]
    diff_text = budgeted_diff or parsed_diff.raw
    notes_block = (
        "\nOmitted/truncated parts (do not speculate about them):\n- " + "\n- ".join(cut_notes[:10])
        if cut_notes
        else ""
    )

    system_msg = (
        "You are a senior software architect writing a concise PR walkthrough. "
        "Respond ONLY with valid JSON matching this exact schema:\n"
        '{"summary": string, "effort": integer 1..5, '
        '"file_table": [{"file": string, "change_summary": string}], '
        '"sequence_diagram": string | null}\n'
        "Rules:\n"
        "- `summary`: 2-4 crisp sentences summarizing intent, behavior changes, and risk.\n"
        "- `effort`: review complexity from 1 (trivial) to 5 (complex/high-risk).\n"
        "- `file_table`: one entry per changed file.\n"
        "- `sequence_diagram`: valid Mermaid `sequenceDiagram` ONLY when control flow across components changed meaningfully; otherwise null."
    )
    user_msg = (
        f"PR: {repo} #{pr_number} — {title}\n"
        + (f"Description:\n{description}\n\n" if description else "\n")
        + f"Changed files ({len(parsed_diff.files)}, +{parsed_diff.total_additions}/-{parsed_diff.total_deletions}): "
        + ", ".join(parsed_diff.paths[:30])
        + notes_block
        + f"\n\nDiff:\n{diff_text}"
    )

    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_msg},
    ]

    last_raw = ""
    for attempt in range(2):
        raw = await call_llm(
            messages,
            repo=repo,
            pr_number=pr_number,
            json_mode=True,
            client=client,
            llm_call=llm_call,
            logs=logs,
        )
        last_raw = raw or last_raw
        extracted = extract_json(raw)
        if isinstance(extracted, dict):
            normalized = _normalize_walkthrough(extracted, parsed_diff)
            if normalized is not None:
                return normalized
        if attempt == 0:
            if logs is not None:
                logs.append("review: Pass A returned non-JSON walkthrough; retrying once with schema repair.")
            messages = [
                *messages,
                {"role": "assistant", "content": (raw or "")[:2000]},
                {
                    "role": "user",
                    "content": (
                        "Your previous reply could not be parsed as valid JSON. "
                        'Return ONLY a single JSON object: {"summary": "...", "effort": 2, '
                        '"file_table": [{"file": "...", "change_summary": "..."}], "sequence_diagram": null}.'
                    ),
                },
            ]

    # Markdown fallback after retry fails
    if logs is not None:
        logs.append("review: Pass A JSON retry failed; falling back to markdown walkthrough.")
    fallback_summary = last_raw.strip()[:4000] if last_raw.strip() else (
        f"Updated {len(parsed_diff.files)} file(s) (+{parsed_diff.total_additions}/-{parsed_diff.total_deletions} lines)."
    )
    return {
        "summary": fallback_summary,
        "effort": _heuristic_effort(parsed_diff),
        "file_table": _default_file_table(parsed_diff),
        "sequence_diagram": None,
    }


# ---------------------------------------------------------------------------
# Pass B: Structured Inline Findings
# ---------------------------------------------------------------------------


def _matching_path_instructions(
    config: dict[str, Any] | None,
    changed_paths: Sequence[str],
) -> list[tuple[str, str]]:
    if not config:
        return []
    raw_rules = config.get("path_instructions") or []
    matched: list[tuple[str, str]] = []
    for item in raw_rules:
        if not isinstance(item, dict):
            continue
        pat = str(item.get("path") or "").strip()
        instr = str(item.get("instructions") or "").strip()
        if not pat or not instr:
            continue
        if not changed_paths or any(_glob_matches(p, pat) for p in changed_paths):
            matched.append((pat, instr))
    return matched


def _format_memory_rules(memories: dict[str, Any] | None) -> str:
    if not isinstance(memories, dict):
        return ""
    rules = memories.get("rules") or []
    lines: list[str] = []
    for item in rules:
        if isinstance(item, dict):
            mid = str(item.get("id") or "").strip()
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            tag = f"[#{mid}]" if mid else "[rule]"
            blocking = " [BLOCKING]" if item.get("blocking") else ""
            lines.append(f"- {tag}{blocking} {text}")
        elif isinstance(item, str) and item.strip():
            lines.append(f"- {item.strip()}")
    if not lines:
        return ""
    return (
        "Repository memory rules (enforce these strictly and cite their ID in `learning_ids` when triggered):\n"
        + "\n".join(lines)
    )


def _format_dismissed_hints(memories: dict[str, Any] | None) -> str:
    if not isinstance(memories, dict):
        return ""
    dismissed = memories.get("dismissed") or []
    lines: list[str] = []
    for item in dismissed[:25]:
        if isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            raw_scopes = item.get("scope_globs")
            scopes = [str(g).strip() for g in raw_scopes if str(g).strip()] if isinstance(raw_scopes, list) else []
            if text:
                if scopes and scopes != ["**"]:
                    scope_str = ", ".join(f"`{g}`" for g in scopes[:6])
                    lines.append(f"- [ONLY for paths matching {scope_str}] {text[:240]}")
                else:
                    lines.append(f"- {text[:240]}")
        else:
            text = str(item or "").strip()
            if text:
                lines.append(f"- {text[:240]}")
    if not lines:
        return ""
    return (
        "Previously dismissed false-positive patterns (DO NOT report these within their listed path scope; "
        "scoped dismissals apply ONLY to matching paths):\n"
        + "\n".join(lines)
    )


def _coerce_finding(raw: Any) -> dm.Finding | None:
    if isinstance(raw, dm.Finding):
        return raw
    if not isinstance(raw, dict):
        return None
    file_path = str(raw.get("file") or raw.get("path") or "").strip()
    title = str(raw.get("title") or "").strip()
    if not file_path or not title:
        return None

    # Accept `line` shorthand as fallback for start_line/end_line
    raw_start = raw.get("start_line")
    raw_end = raw.get("end_line")
    raw_line = raw.get("line")
    if raw_start is None and raw_line is not None:
        raw_start = raw_line
    if raw_end is None:
        raw_end = raw_start if raw_start is not None else raw_line
    if raw_start is None:
        raw_start = raw_end
    try:
        start_line = int(raw_start)
        end_line = int(raw_end)
    except (TypeError, ValueError):
        return None
    if start_line < 1 or end_line < 1:
        return None
    if start_line > end_line:
        start_line, end_line = end_line, start_line

    side = str(raw.get("side") or "RIGHT").strip().upper()
    if side not in ("LEFT", "RIGHT"):
        side = "RIGHT"

    sev_raw = str(raw.get("severity") or "minor").strip().lower()
    severity = _SEVERITY_ALIASES.get(sev_raw, "minor")

    cat_raw = str(raw.get("category") or "maintainability").strip().lower()
    category = _CATEGORY_ALIASES.get(cat_raw, "maintainability")

    body = str(raw.get("body") or raw.get("message") or "").strip()[:8000]
    raw_patch = raw.get("suggestion_patch")
    if raw_patch is None:
        raw_patch = raw.get("suggestion")
    suggestion_patch = str(raw_patch)[:8000] if isinstance(raw_patch, str) and raw_patch.strip() else None

    raw_conf = raw.get("confidence", 0.75)
    try:
        parsed_conf = float(raw_conf)
    except (TypeError, ValueError):
        parsed_conf = 0.75
    if not math.isfinite(parsed_conf):
        return None
    confidence = max(0.0, min(1.0, parsed_conf))

    source_raw = str(raw.get("source") or "llm").strip().lower()
    source = source_raw if source_raw in ("llm", "tool", "memory") else "llm"

    raw_lids = raw.get("learning_ids") or []
    learning_ids: list[str] = []
    if isinstance(raw_lids, (str, int)):
        raw_lids = [raw_lids]
    if isinstance(raw_lids, list):
        for lid in raw_lids:
            cleaned = str(lid or "").strip().lstrip("#")
            if cleaned and cleaned not in learning_ids:
                learning_ids.append(cleaned)

    try:
        return dm.Finding(
            file=file_path[:500],
            start_line=start_line,
            end_line=end_line,
            side=side,  # type: ignore[arg-type]
            severity=severity,  # type: ignore[arg-type]
            category=category,  # type: ignore[arg-type]
            title=title[:200],
            body=body,
            suggestion_patch=suggestion_patch,
            confidence=confidence,
            source=source,  # type: ignore[arg-type]
            state="open",
            learning_ids=learning_ids,
        )
    except Exception:
        return None


def _extract_findings_list(extracted: Any) -> list[dm.Finding] | None:
    if isinstance(extracted, list):
        items = extracted
    elif isinstance(extracted, dict):
        raw_items = extracted.get("findings")
        if raw_items is None and extracted.get("issues") is not None:
            raw_items = extracted.get("issues")
        if not isinstance(raw_items, list):
            return None
        items = raw_items
    else:
        return None

    out: list[dm.Finding] = []
    for item in items:
        coerced = _coerce_finding(item)
        if coerced is not None:
            out.append(coerced)
    return out


async def generate_findings(
    parsed_diff: ParsedDiff,
    pr_meta: dict[str, Any],
    *,
    budgeted_diff: str = "",
    cut_notes: Sequence[str] | None = None,
    config: dict[str, Any] | None = None,
    memories: dict[str, Any] | None = None,
    client: Any = None,
    llm_call: LLMCallable | None = None,
    logs: list[str] | None = None,
) -> list[dm.Finding]:
    """Pass B: Emit strict-schema `Finding[]` grounded in the line-numbered diff,
    path_instructions, review profile, and repository memory."""
    cfg = config or {}
    repo = str(pr_meta.get("repo") or "owner/repo")
    pr_number = int(pr_meta.get("pr_number") or 0)
    title = str(pr_meta.get("title") or "").strip()
    profile = str(cfg.get("profile") or "chill").strip().lower()
    if profile not in PROFILE_THRESHOLDS:
        profile = "chill"

    base_prompt = str(cfg.get("system_prompt") or "").strip() or DEFAULT_SYSTEM_PROMPT
    profile_guidance = (
        "Review profile (assertive): catch bugs, security issues, performance regressions, "
        "maintainability problems, missing edge-case tests, and concrete polish improvements."
        if profile == "assertive"
        else "Review profile (chill): prioritize high-confidence bugs, security vulnerabilities, "
        "data-loss/crash risks, and major design flaws. Avoid minor style nitpicks."
    )

    sections: list[str] = [
        base_prompt,
        profile_guidance,
        (
            "Respond ONLY with valid JSON matching this strict schema:\n"
            '{"findings": [{"file": "path/to/file.py", "start_line": 10, "end_line": 12, '
            '"side": "RIGHT", "severity": "critical|major|minor|nit", '
            '"category": "bug|security|perf|maintainability|style|test|docs", '
            '"title": "Short specific title", "body": "Concrete impact & explanation", '
            '"suggestion_patch": "exact replacement code for start_line..end_line or null (NO markdown fences)", '
            '"confidence": 0.85, "learning_ids": []}]}\n'
            "Strict accuracy rules:\n"
            "1. Only report lines that appear in the diff below. Use the explicit `[R<line>]` numbers with `side: \"RIGHT\"` "
            "(or `[L<line>]` for deleted lines with `side: \"LEFT\"`).\n"
            "2. Keep `start_line` and `end_line` inside the SAME hunk.\n"
            "3. When providing `suggestion_patch`, supply exact drop-in replacement lines for `start_line..end_line` with NO backticks.\n"
            "4. If no real issues exist, return `{\"findings\": []}`."
        ),
    ]

    matched_instr = _matching_path_instructions(cfg, parsed_diff.paths)
    if matched_instr:
        sections.append(
            "Path-specific repository instructions:\n"
            + "\n".join(f"- `{pat}`: {instr}" for pat, instr in matched_instr)
        )

    mem_rules_block = _format_memory_rules(memories)
    if mem_rules_block:
        sections.append(mem_rules_block)

    dismissed_block = _format_dismissed_hints(memories)
    if dismissed_block:
        sections.append(dismissed_block)

    diff_for_prompt = budgeted_diff or parsed_diff.render_numbered()
    notes_block = (
        "\nOmitted/truncated parts (do not speculate about them):\n- " + "\n- ".join(cut_notes[:10])
        if cut_notes
        else ""
    )

    user_msg = (
        f"Review pull request '{title}' in {repo} #{pr_number}.\n"
        f"sha={pr_meta.get('sha', '')} action={pr_meta.get('action', '')}"
        + notes_block
        + f"\n\nLine-numbered diff:\n{diff_for_prompt}"
    )

    messages = [
        {"role": "system", "content": "\n\n".join(sections)},
        {"role": "user", "content": user_msg},
    ]

    for attempt in range(2):
        raw = await call_llm(
            messages,
            repo=repo,
            pr_number=pr_number,
            json_mode=True,
            client=client,
            llm_call=llm_call,
            logs=logs,
        )
        extracted = extract_json(raw)
        findings = _extract_findings_list(extracted)
        if findings is not None:
            return findings
        if attempt == 0:
            if logs is not None:
                logs.append("review: Pass B returned non-JSON findings; retrying once with schema repair.")
            messages = [
                *messages,
                {"role": "assistant", "content": (raw or "")[:2000]},
                {
                    "role": "user",
                    "content": (
                        "Your response was not valid JSON. Return ONLY `{\"findings\": [...]}` "
                        "matching the schema above (or `{\"findings\": []}` if no issues)."
                    ),
                },
            ]

    if logs is not None:
        logs.append("review: Pass B JSON retry failed; aborting review generation.")
    raise LLMCallError("Pass B failed to return valid structured findings after retry")


# ---------------------------------------------------------------------------
# Pass C: Deterministic Verifier & Filter
# ---------------------------------------------------------------------------


def _normalize_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for word in re.findall(r"[a-z0-9_]+", (text or "").lower()):
        if len(word) < 3 or word in _STOP_WORDS:
            continue
        # Light plural normalization so "docstrings" matches "docstring"
        if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        tokens.add(word)
    return tokens


def _matches_dismissed_pattern(
    finding: dict[str, Any],
    code_span: str,
    fingerprint: str,
    dismissed_entries: Sequence[Any],
) -> bool:
    if not dismissed_entries:
        return False
    fpath = str(finding.get("file") or "")
    title = str(finding.get("title") or "")
    category = str(finding.get("category") or "")
    body = str(finding.get("body") or "")
    finding_stem = dm._title_stem(title)
    finding_text = f"{title} {category} {body} {code_span}".lower()
    finding_tokens = _normalize_tokens(f"{title} {category} {body} {code_span}")

    for entry in dismissed_entries:
        if isinstance(entry, dict):
            scope_globs = entry.get("scope_globs")
            if isinstance(scope_globs, list) and scope_globs:
                if not any(_glob_matches(fpath, str(g)) for g in scope_globs if g):
                    continue
            if entry.get("fingerprint") and str(entry["fingerprint"]) == fingerprint:
                return True
            pattern_text = str(entry.get("text") or "").strip()
        else:
            pattern_text = str(entry or "").strip()

        if not pattern_text:
            continue

        pattern_low = pattern_text.lower()
        # 1. Fingerprint or full title stem match
        if fingerprint and fingerprint in pattern_low:
            return True
        if finding_stem and len(finding_stem.split()) >= 2 and finding_stem in pattern_low:
            return True

        pattern_stem = dm._title_stem(pattern_text)
        if pattern_stem and len(pattern_stem.split()) >= 2 and pattern_stem in finding_text:
            return True

        # 2. Meaningful token overlap check
        pattern_tokens = _normalize_tokens(pattern_text)
        if not pattern_tokens:
            continue
        shared = pattern_tokens & finding_tokens
        if len(pattern_tokens) == 1:
            only = next(iter(pattern_tokens))
            if len(only) >= 4 and only in _normalize_tokens(f"{title} {code_span}"):
                return True
        else:
            overlap = len(shared) / len(pattern_tokens)
            if len(shared) >= 2 and overlap >= 0.6:
                return True
    return False


def _validate_and_align_location(
    df: DiffFile,
    start_line: int,
    end_line: int,
    side: str,
    suggestion_patch: str | None,
) -> tuple[int, int, str, str | None] | None:
    """Validate `(start_line, end_line, side)` against `df` so GitHub Review API won't 422."""
    orig_start, orig_end, orig_side = start_line, end_line, side
    valid = df.valid_lines(side)

    if end_line not in valid:
        # Common LLM mistake: flagging a deleted line with side="RIGHT"
        if side == "RIGHT" and end_line in df.deleted_lines:
            side = "LEFT"
            valid = df.valid_lines(side)
        elif suggestion_patch is None:
            nearest = df.nearest_valid_line(end_line, side, max_distance=2)
            if nearest is None:
                return None
            end_line = nearest
            start_line = nearest
        else:
            # Never silently snap a committable suggestion patch to another line
            return None

    hunk = df.hunk_for_line(end_line, side)
    if hunk is None:
        return None

    hunk_valid = hunk.valid_lines(side)
    if start_line > end_line:
        start_line = end_line

    if start_line != end_line and start_line not in hunk_valid:
        # GitHub requires start_line and end_line to be in the SAME hunk.
        in_range = sorted(ln for ln in hunk_valid if start_line <= ln <= end_line)
        if in_range:
            start_line = in_range[0]
        else:
            start_line = end_line

    # Clear any suggestion patch whenever verification altered start, end, or side,
    # even for single-line patches (a 1-line patch could have been meant to replace a range).
    if (start_line, end_line, side) != (orig_start, orig_end, orig_side):
        suggestion_patch = None

    # Drop no-op suggestion patches identical to current code
    if suggestion_patch is not None and side == "RIGHT":
        existing_code = df.code_span(start_line, end_line, side)
        if existing_code and suggestion_patch == existing_code:
            suggestion_patch = None

    return start_line, end_line, side, suggestion_patch


def verify_and_filter_findings(
    raw_findings: Sequence[dm.Finding | dict[str, Any]],
    parsed_diff: ParsedDiff,
    *,
    anchor_diff: ParsedDiff | None = None,
    profile: str = "chill",
    memories: dict[str, Any] | None = None,
    existing_findings: Sequence[dict[str, Any]] | None = None,
    existing_fingerprints: set[str] | None = None,
    stats: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Pass C: Deterministic verifier and noise filter.

    - Validates `(file, end_line, side)` exists in `ParsedDiff` (adjusting minor non-suggestion
      offsets or dropping out-of-diff hallucinations so GitHub Review API won't HTTP 422).
    - Strips code fences from `suggestion_patch`.
    - Enforces profile thresholds (`chill`: confidence >= 0.70, max 2 nits;
      `assertive`: confidence >= 0.50, max 6 nits).
    - Suppresses findings matching `memories["dismissed"]` patterns.
    - Computes `datamodel.finding_fingerprint` and dedupes against previously stored
      open/dismissed findings for the PR (and within the batch).
    """
    prof = (profile or "chill").strip().lower()
    limits = PROFILE_THRESHOLDS.get(prof, PROFILE_THRESHOLDS["chill"])
    min_conf: float = limits["min_confidence"]
    max_nits: int = limits["max_nits"]

    counters = stats if stats is not None else {}
    for k in (
        "input",
        "dropped_invalid",
        "dropped_out_of_diff",
        "dropped_low_confidence",
        "dropped_dismissed",
        "dropped_duplicate",
        "dropped_nit_cap",
        "kept",
    ):
        counters.setdefault(k, 0)

    # Collect previously stored open or dismissed fingerprints (skip undelivered ones so retries can publish them)
    prior_fps: set[str] = set(existing_fingerprints or ())
    for item in existing_findings or ():
        if not isinstance(item, dict):
            continue
        if item.get("delivered") is False:
            continue
        state = str(item.get("state") or "open").lower()
        if state not in ("open", "dismissed"):
            continue
        fp = str(item.get("fingerprint") or "").strip()
        if fp:
            prior_fps.add(fp)
            continue
        # Fallback if a legacy record omitted stored fingerprint
        fpath = str(item.get("file") or "")
        df_prev = parsed_diff.get_file(fpath)
        span = ""
        if df_prev is not None:
            try:
                span = df_prev.code_span(
                    int(item.get("start_line") or 1),
                    int(item.get("end_line") or item.get("start_line") or 1),
                    str(item.get("side") or "RIGHT"),
                )
            except (TypeError, ValueError):
                span = ""
        if fpath and item.get("category") and item.get("title"):
            prior_fps.add(
                dm.finding_fingerprint(fpath, span, str(item["category"]), str(item["title"]))
            )

    dismissed_entries = (
        list(memories.get("dismissed") or []) if isinstance(memories, dict) else []
    )

    candidates: list[dict[str, Any]] = []
    for raw in raw_findings:
        counters["input"] += 1
        coerced = _coerce_finding(raw)
        if coerced is None:
            counters["dropped_invalid"] += 1
            continue

        df = parsed_diff.get_file(coerced.file)
        if df is None or df.is_binary:
            counters["dropped_out_of_diff"] += 1
            continue

        clean_patch = strip_code_fences(coerced.suggestion_patch)
        aligned = _validate_and_align_location(
            df,
            coerced.start_line,
            coerced.end_line,
            coerced.side,
            clean_patch,
        )
        if aligned is None:
            counters["dropped_out_of_diff"] += 1
            continue

        start_line, end_line, side, final_patch = aligned

        # When reviewing an incremental diff, validate the final anchor against the
        # full PR diff (`anchor_diff`) because GitHub interprets review comment
        # positions against the PR diff (not the incremental compare).
        if anchor_diff is not None:
            adf = anchor_diff.get_file(df.path)
            if adf is None or adf.is_binary:
                counters["dropped_out_of_diff"] += 1
                continue
            pr_hunk = adf.hunk_for_line(end_line, side)
            if pr_hunk is None or start_line not in pr_hunk.valid_lines(side):
                counters["dropped_out_of_diff"] += 1
                continue
            # For LEFT anchors, also ensure the deleted/old line text matches the PR merge base
            # (a line added in push 1 and deleted in push 2 never existed in the PR merge base).
            if side == "LEFT":
                inc_code = df.code_span(start_line, end_line, "LEFT")
                pr_code = adf.code_span(start_line, end_line, "LEFT")
                if not pr_code or (inc_code and inc_code != pr_code):
                    counters["dropped_out_of_diff"] += 1
                    continue

        if coerced.confidence < min_conf:
            counters["dropped_low_confidence"] += 1
            continue

        code_span = df.code_span(start_line, end_line, side)
        fp = dm.finding_fingerprint(df.path, code_span, coerced.category, coerced.title)

        finding_dict: dict[str, Any] = {
            "file": df.path,
            "start_line": start_line,
            "end_line": end_line,
            "side": side,
            "severity": coerced.severity,
            "category": coerced.category,
            "title": coerced.title,
            "body": coerced.body,
            "suggestion_patch": final_patch,
            "confidence": round(float(coerced.confidence), 3),
            "source": coerced.source,
            "state": "open",
            "learning_ids": list(coerced.learning_ids),
            "fingerprint": fp,
        }

        if _matches_dismissed_pattern(finding_dict, code_span, fp, dismissed_entries):
            counters["dropped_dismissed"] += 1
            continue

        if fp in prior_fps:
            counters["dropped_duplicate"] += 1
            continue

        candidates.append(finding_dict)

    # Highest severity and highest confidence first before batch dedupe & nit cap
    candidates.sort(
        key=lambda f: (
            _SEVERITY_ORDER.get(f["severity"], 9),
            -float(f["confidence"]),
            f["file"],
            int(f["start_line"]),
        )
    )

    verified: list[dict[str, Any]] = []
    seen_batch_fps: set[str] = set()
    nit_count = 0

    for item in candidates:
        fp = item["fingerprint"]
        if fp in seen_batch_fps:
            counters["dropped_duplicate"] += 1
            continue
        if item["severity"] == "nit":
            if nit_count >= max_nits:
                counters["dropped_nit_cap"] += 1
                continue
            nit_count += 1
        seen_batch_fps.add(fp)
        verified.append(item)

    counters["kept"] = len(verified)
    return verified
