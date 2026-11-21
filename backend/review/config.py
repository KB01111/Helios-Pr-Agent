"""Repository configuration (`.pragent.yaml` / `.pragent.yml`) loader, merger & exporter.

Security rule: repository config is ALWAYS fetched from the pull request's
**base branch** (never the head branch), so an untrusted PR cannot weaken
review profiles, disable merge gates, or inject prompt instructions.

Zero extra pip dependencies: uses PyYAML (`yaml.safe_load`) when available
and transparently falls back to a clean pure-Python YAML subset parser.
"""
from __future__ import annotations

import base64
import logging
import re
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

try:  # cwd=backend / PYTHONPATH=backend
    import datamodel as dm
    import store
    from sandboxes.providers_review import GITHUB_API, _glob_matches
    from sandboxes.safety import UnsafeSandboxInput, safe_git_ref, safe_repo
except ImportError:  # pragma: no cover - `uvicorn backend.main:app` from repo root
    from backend import datamodel as dm  # type: ignore[no-redef]
    from backend import store  # type: ignore[no-redef]
    from backend.sandboxes.providers_review import GITHUB_API, _glob_matches  # type: ignore[no-redef]
    from backend.sandboxes.safety import UnsafeSandboxInput, safe_git_ref, safe_repo  # type: ignore[no-redef]

log = logging.getLogger("pr-agent.review.config")

CONFIG_FILENAMES: tuple[str, ...] = (".pragent.yaml", ".pragent.yml")
REPO_CONFIG_TTL_S = 7 * 24 * 3600
_MAX_CONFIG_BYTES = 64 * 1024
_TOOL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


# ---------------------------------------------------------------------------
# Pydantic schema
# ---------------------------------------------------------------------------


class PathInstruction(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: str = Field(min_length=1, max_length=200)
    instructions: str = Field(min_length=1, max_length=2000)


class PathFilters(BaseModel):
    model_config = ConfigDict(extra="ignore")

    include: list[str] = Field(default_factory=list, max_length=100)
    exclude: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="before")
    @classmethod
    def _coerce_list_syntax(cls, data: Any) -> Any:
        """Accept CodeRabbit-style list (`["!dist/**", "src/**"]`) or `{include, exclude}`."""
        if data is None:
            return {}
        if isinstance(data, list):
            inc: list[str] = []
            exc: list[str] = []
            for raw in data:
                item = str(raw or "").strip()
                if not item:
                    continue
                if item.startswith("!"):
                    pat = item[1:].strip()
                    if pat:
                        exc.append(pat)
                else:
                    inc.append(item)
            return {"include": inc, "exclude": exc}
        return data


class AutoReviewConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    drafts: bool = False
    base_branches: list[str] = Field(default_factory=list, max_length=50)
    ignore_title_keywords: list[str] = Field(default_factory=list, max_length=50)


class MemoryConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    auto_approve: bool = False


class ChatConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True


class LabelsConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    auto_apply: bool = False


class PreMergeCheck(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=120)
    instructions: str = Field(default="", max_length=2000)
    mode: Literal["warning", "error"] = "warning"

    @model_validator(mode="before")
    @classmethod
    def _coerce_item(cls, data: Any) -> Any:
        if isinstance(data, str):
            text = data.strip()
            return {"name": text[:120], "instructions": text, "mode": "warning"}
        if isinstance(data, dict):
            out = dict(data)
            if not out.get("name") and out.get("title"):
                out["name"] = out["title"]
            if not out.get("instructions") and out.get("description"):
                out["instructions"] = out["description"]
            if not out.get("instructions") and out.get("name"):
                out["instructions"] = str(out["name"])
            return out
        return data


class RepoConfig(BaseModel):
    """Validated `.pragent.yaml` / `.pragent.yml` repository configuration."""

    model_config = ConfigDict(extra="ignore")

    profile: Literal["chill", "assertive"] = "chill"
    path_filters: PathFilters = Field(default_factory=PathFilters)
    path_instructions: list[PathInstruction] = Field(default_factory=list, max_length=50)
    auto_review: AutoReviewConfig = Field(default_factory=AutoReviewConfig)
    merge_gate: Literal["off", "advisory", "required"] = "advisory"
    score_threshold: int = Field(default=85, ge=0, le=100)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    chat: ChatConfig = Field(default_factory=ChatConfig)
    labels: LabelsConfig = Field(default_factory=LabelsConfig)
    pre_merge_checks: list[PreMergeCheck] = Field(default_factory=list, max_length=30)
    tools: list[str] = Field(default_factory=list, max_length=20)
    weights: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _hoist_nested_reviews(cls, data: Any) -> Any:
        """Accept CodeRabbit-style `reviews:` nesting alongside flat keys."""
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise ValueError("repo config must be a YAML mapping")
        out = dict(data)
        reviews = out.get("reviews")
        if isinstance(reviews, dict):
            for key in ("profile", "path_filters", "path_instructions", "auto_review"):
                if key not in out and key in reviews:
                    out[key] = reviews[key]
        # Clean invalid/empty tool names rather than failing the whole repo config
        if isinstance(out.get("tools"), list):
            out["tools"] = [
                str(t).strip().lower()
                for t in out["tools"]
                if isinstance(t, str) and _TOOL_RE.fullmatch(t.strip().lower())
            ]
        return out


# ---------------------------------------------------------------------------
# Pure-Python YAML subset parser & YAML exporter (zero external deps)
# ---------------------------------------------------------------------------


def _strip_yaml_comment(line: str) -> str:
    """Strip unquoted `# ...` comments from a single YAML line."""
    in_single = False
    in_double = False
    escaped = False
    for idx, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if in_double and ch == "\\":
            escaped = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if idx == 0 or line[idx - 1].isspace():
                return line[:idx].rstrip()
    return line.rstrip()


def _split_flow_items(inner: str) -> list[str]:
    """Split comma-separated flow sequence/mapping items respecting quotes and nesting."""
    items: list[str] = []
    cur: list[str] = []
    in_single = False
    in_double = False
    escaped = False
    depth = 0
    for ch in inner:
        if escaped:
            cur.append(ch)
            escaped = False
            continue
        if in_double and ch == "\\":
            cur.append(ch)
            escaped = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            cur.append(ch)
        elif ch == '"' and not in_single:
            in_double = not in_double
            cur.append(ch)
        elif not in_single and not in_double:
            if ch in "[{":
                depth += 1
                cur.append(ch)
            elif ch in "]}":
                depth = max(0, depth - 1)
                cur.append(ch)
            elif ch == "," and depth == 0:
                items.append("".join(cur).strip())
                cur = []
            else:
                cur.append(ch)
        else:
            cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        items.append(tail)
    return items


def _find_mapping_colon(text: str) -> int:
    """Index of the first unquoted `:` that separates a YAML key from its value, or -1."""
    in_single = False
    in_double = False
    escaped = False
    depth = 0
    for idx, ch in enumerate(text):
        if escaped:
            escaped = False
            continue
        if in_double and ch == "\\":
            escaped = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if ch in "[{":
                depth += 1
            elif ch in "]}":
                depth = max(0, depth - 1)
            elif ch == ":" and depth == 0:
                if idx + 1 == len(text) or text[idx + 1].isspace():
                    return idx
    return -1


def _parse_scalar(raw: str) -> Any:
    val = raw.strip()
    if not val:
        return None
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(item) for item in _split_flow_items(inner)]
    if val.startswith("{") and val.endswith("}"):
        inner = val[1:-1].strip()
        if not inner:
            return {}
        out: dict[str, Any] = {}
        for item in _split_flow_items(inner):
            cidx = _find_mapping_colon(item)
            if cidx == -1:
                raise ValueError(f"invalid flow mapping entry: {item!r}")
            k = str(_parse_scalar(item[:cidx]))
            v = _parse_scalar(item[cidx + 1:])
            out[k] = v
        return out
    if (val.startswith('"') and val.endswith('"') and len(val) >= 2):
        inner = val[1:-1]
        # Handle common double-quoted escapes
        return (
            inner.replace('\\"', '"')
            .replace("\\n", "\n")
            .replace("\\t", "\t")
            .replace("\\\\", "\\")
        )
    if (val.startswith("'") and val.endswith("'") and len(val) >= 2):
        return val[1:-1].replace("''", "'")

    low = val.lower()
    if low in ("null", "~"):
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    if re.fullmatch(r"[+-]?[0-9]+", val):
        try:
            return int(val)
        except ValueError:
            pass
    if re.fullmatch(r"[+-]?[0-9]+\.[0-9]+", val):
        try:
            return float(val)
        except ValueError:
            pass
    return val


def parse_yaml_subset(text: str) -> dict[str, Any]:
    """Pure-Python YAML subset parser supporting mappings, sequences, flow syntax,
    nested lists of mappings, and `|`/`>` block scalars."""
    if len(text.encode("utf-8", errors="ignore")) > _MAX_CONFIG_BYTES:
        raise ValueError("YAML config exceeds size limit")

    raw_lines = text.splitlines()
    # Keep (indent, stripped_content, raw_line) for non-blank/non-comment lines
    # while preserving original raw_lines for multiline block scalars.
    n = len(raw_lines)

    def _next_significant(idx: int) -> int:
        while idx < n:
            cleaned = _strip_yaml_comment(raw_lines[idx])
            stripped = cleaned.lstrip(" ")
            if stripped and stripped != "---" and stripped != "...":
                return idx
            idx += 1
        return n

    def _indent_of(line: str) -> int:
        if "\t" in line[: len(line) - len(line.lstrip())]:
            raise ValueError("tabs are not allowed for YAML indentation")
        return len(line) - len(line.lstrip(" "))

    def _parse_block_scalar(start_idx: int, parent_indent: int, style: str) -> tuple[str, int]:
        fold = style.startswith(">")
        chomp = style[1:] if len(style) > 1 else ""
        lines: list[str] = []
        block_indent: int | None = None
        i = start_idx
        while i < n:
            raw = raw_lines[i]
            if not raw.strip():
                lines.append("")
                i += 1
                continue
            ind = _indent_of(raw)
            if ind <= parent_indent:
                break
            if block_indent is None:
                block_indent = ind
            if ind < block_indent:
                break
            lines.append(raw[block_indent:])
            i += 1
        # Trim trailing empty lines according to chomp mode
        while lines and lines[-1] == "":
            lines.pop()
        if fold:
            joined = " ".join(part if part else "\n" for part in lines).replace(" \n ", "\n")
        else:
            joined = "\n".join(lines)
        if chomp != "-" and joined and not joined.endswith("\n"):
            joined += "\n"
        return joined, i

    def _parse_node(idx: int, min_indent: int) -> tuple[Any, int]:
        idx = _next_significant(idx)
        if idx >= n:
            return {}, idx
        line = _strip_yaml_comment(raw_lines[idx])
        indent = _indent_of(line)
        if indent < min_indent:
            return None, idx
        stripped = line[indent:]
        if stripped == "-" or stripped.startswith("- "):
            return _parse_list(idx, indent)
        return _parse_mapping(idx, indent)

    def _parse_list(idx: int, list_indent: int) -> tuple[list[Any], int]:
        out: list[Any] = []
        while True:
            idx = _next_significant(idx)
            if idx >= n:
                break
            line = _strip_yaml_comment(raw_lines[idx])
            indent = _indent_of(line)
            if indent < list_indent:
                break
            if indent > list_indent:
                raise ValueError(f"unexpected indentation in YAML list at line {idx + 1}")
            stripped = line[indent:]
            if not (stripped == "-" or stripped.startswith("- ")):
                break
            rest = stripped[1:].lstrip(" ")
            item_indent = indent + 2
            if not rest:
                val, idx = _parse_node(idx + 1, indent + 1)
                out.append(val)
                continue
            # Check if inline mapping on the `- key: val` line
            cidx = _find_mapping_colon(rest)
            if cidx != -1 and not rest.startswith(("{", "[", '"', "'")):
                item_dict: dict[str, Any] = {}
                key = str(_parse_scalar(rest[:cidx]))
                val_raw = rest[cidx + 1:].strip()
                if val_raw in ("|", "|-", ">", ">-"):
                    val, idx = _parse_block_scalar(idx + 1, indent, val_raw)
                elif not val_raw:
                    nxt = _next_significant(idx + 1)
                    if nxt < n and _indent_of(_strip_yaml_comment(raw_lines[nxt])) >= item_indent:
                        val, idx = _parse_node(nxt, item_indent)
                    else:
                        val = None
                        idx += 1
                else:
                    val = _parse_scalar(val_raw)
                    idx += 1
                item_dict[key] = val
                # Read remaining keys of this mapping at indent >= item_indent
                nxt = _next_significant(idx)
                if nxt < n:
                    nxt_line = _strip_yaml_comment(raw_lines[nxt])
                    nxt_ind = _indent_of(nxt_line)
                    nxt_stripped = nxt_line[nxt_ind:]
                    if nxt_ind >= item_indent and not (nxt_stripped == "-" or nxt_stripped.startswith("- ")):
                        extra, idx = _parse_mapping(nxt, nxt_ind)
                        item_dict.update(extra)
                out.append(item_dict)
            else:
                out.append(_parse_scalar(rest))
                idx += 1
        return out, idx

    def _parse_mapping(idx: int, map_indent: int) -> tuple[dict[str, Any], int]:
        out: dict[str, Any] = {}
        while True:
            idx = _next_significant(idx)
            if idx >= n:
                break
            line = _strip_yaml_comment(raw_lines[idx])
            indent = _indent_of(line)
            if indent < map_indent:
                break
            if indent > map_indent:
                raise ValueError(f"unexpected indentation in YAML mapping at line {idx + 1}")
            stripped = line[indent:]
            if stripped == "-" or stripped.startswith("- "):
                break
            cidx = _find_mapping_colon(stripped)
            if cidx == -1:
                raise ValueError(f"expected `key: value` at line {idx + 1}: {stripped!r}")
            key = str(_parse_scalar(stripped[:cidx]))
            val_raw = stripped[cidx + 1:].strip()
            if val_raw in ("|", "|-", ">", ">-"):
                val, idx = _parse_block_scalar(idx + 1, map_indent, val_raw)
                out[key] = val
            elif not val_raw:
                nxt = _next_significant(idx + 1)
                if nxt < n:
                    nxt_line = _strip_yaml_comment(raw_lines[nxt])
                    nxt_ind = _indent_of(nxt_line)
                    nxt_stripped = nxt_line[nxt_ind:]
                    if nxt_ind > map_indent or (
                        nxt_ind == map_indent and (nxt_stripped == "-" or nxt_stripped.startswith("- "))
                    ):
                        val, idx = _parse_node(nxt, nxt_ind)
                        out[key] = val
                        continue
                out[key] = None
                idx += 1
            else:
                out[key] = _parse_scalar(val_raw)
                idx += 1
        return out, idx

    root, _ = _parse_node(0, 0)
    if root is None:
        return {}
    if not isinstance(root, dict):
        raise ValueError("top-level YAML document must be a mapping")
    return root


def parse_pragent_yaml(text: str, *, use_pyyaml: bool = True) -> RepoConfig:
    """Parse `.pragent.yaml` text into a validated `RepoConfig`.

    Uses `yaml.safe_load` when PyYAML is available and `use_pyyaml=True`,
    falling back to the pure-Python YAML subset parser otherwise.
    """
    if len(text.encode("utf-8", errors="ignore")) > _MAX_CONFIG_BYTES:
        raise ValueError("repo config exceeds maximum allowed size")
    data: Any = None
    parsed_with_pyyaml = False
    if use_pyyaml:
        try:
            import yaml  # type: ignore

            data = yaml.safe_load(text)
            parsed_with_pyyaml = True
        except ImportError:
            parsed_with_pyyaml = False
    if not parsed_with_pyyaml:
        data = parse_yaml_subset(text)
    if data is None:
        return RepoConfig()
    if not isinstance(data, dict):
        raise ValueError("`.pragent.yaml` root must be a YAML mapping")
    return RepoConfig.model_validate(data)


# ---------------------------------------------------------------------------
# YAML renderer
# ---------------------------------------------------------------------------

_YAML_SPECIAL_RE = re.compile(r"[:#\[\]{},&*!|>?%@`'\"\\\n\r\t]")
_YAML_BOOL_LIKE = frozenset({"true", "false", "null", "yes", "no", "on", "off", "~"})


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value)
    if (
        not s
        or s != s.strip()
        or s.lower() in _YAML_BOOL_LIKE
        or _YAML_SPECIAL_RE.search(s)
        or re.fullmatch(r"[+-]?[0-9]+(\.[0-9]+)?", s)
        or s.startswith("-")
    ):
        escaped = (
            s.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\t", "\\t")
        )
        return f'"{escaped}"'
    return s


def _emit_yaml_lines(obj: Any, indent: int = 0) -> list[str]:
    pad = " " * indent
    lines: list[str] = []
    if isinstance(obj, dict):
        if not obj:
            return [f"{pad}{{}}"]
        for key, val in obj.items():
            k = _yaml_scalar(str(key))
            if isinstance(val, dict):
                if not val:
                    lines.append(f"{pad}{k}: {{}}")
                else:
                    lines.append(f"{pad}{k}:")
                    lines.extend(_emit_yaml_lines(val, indent + 2))
            elif isinstance(val, list):
                if not val:
                    lines.append(f"{pad}{k}: []")
                else:
                    lines.append(f"{pad}{k}:")
                    lines.extend(_emit_yaml_lines(val, indent + 2))
            elif isinstance(val, str) and "\n" in val:
                lines.append(f"{pad}{k}: |-")
                for sub in val.splitlines():
                    lines.append(f"{pad}  {sub}")
            else:
                lines.append(f"{pad}{k}: {_yaml_scalar(val)}")
        return lines
    if isinstance(obj, list):
        if not obj:
            return [f"{pad}[]"]
        for item in obj:
            if isinstance(item, dict) and item:
                entries = list(item.items())
                first_k, first_v = entries[0]
                fk = _yaml_scalar(str(first_k))
                if isinstance(first_v, (dict, list)) and first_v:
                    lines.append(f"{pad}- {fk}:")
                    lines.extend(_emit_yaml_lines(first_v, indent + 4))
                else:
                    lines.append(f"{pad}- {fk}: {_yaml_scalar(first_v)}")
                for rest_k, rest_v in entries[1:]:
                    rk = _yaml_scalar(str(rest_k))
                    if isinstance(rest_v, (dict, list)) and rest_v:
                        lines.append(f"{pad}  {rk}:")
                        lines.extend(_emit_yaml_lines(rest_v, indent + 4))
                    elif isinstance(rest_v, str) and "\n" in rest_v:
                        lines.append(f"{pad}  {rk}: |-")
                        for sub in rest_v.splitlines():
                            lines.append(f"{pad}    {sub}")
                    else:
                        lines.append(f"{pad}  {rk}: {_yaml_scalar(rest_v)}")
            else:
                lines.append(f"{pad}- {_yaml_scalar(item)}")
        return lines
    return [f"{pad}{_yaml_scalar(obj)}"]


def render_pragent_yaml(config_dict: dict[str, Any] | RepoConfig) -> str:
    """Export a clean, deterministic `.pragent.yaml` string."""
    if isinstance(config_dict, RepoConfig):
        raw = config_dict.model_dump()
    else:
        raw = RepoConfig.model_validate(config_dict or {}).model_dump()

    ordered: dict[str, Any] = {
        "profile": raw["profile"],
        "merge_gate": raw["merge_gate"],
        "score_threshold": raw["score_threshold"],
        "auto_review": raw["auto_review"],
        "path_filters": raw["path_filters"],
        "path_instructions": raw["path_instructions"],
        "memory": raw["memory"],
        "chat": raw["chat"],
        "labels": raw["labels"],
        "pre_merge_checks": raw["pre_merge_checks"],
    }
    if raw.get("tools"):
        ordered["tools"] = raw["tools"]
    if raw.get("weights"):
        ordered["weights"] = raw["weights"]
    header = "# PR Agent repository configuration (read from base branch only)\n"
    return header + "\n".join(_emit_yaml_lines(ordered)) + "\n"


# ---------------------------------------------------------------------------
# GitHub base-branch loader with Redis blob-SHA caching
# ---------------------------------------------------------------------------


async def load_repo_config_with_source(
    repo: str,
    base_ref: str,
    github_token: str,
    *,
    client: Any = None,
) -> tuple[RepoConfig, str]:
    """Fetch `.pragent.yaml` or `.pragent.yml` from `base_ref` ONLY.

    Cached in Redis at `datamodel.repo_config_key(repo)` keyed by GitHub blob SHA.
    Returns `(RepoConfig, source)` where `source` is `"repo"` or `"default"`.
    Never reads from the PR head branch and never raises on malformed user config.
    """
    if not github_token or not base_ref:
        return RepoConfig(), "default"
    try:
        repo = safe_repo(repo)
        base_ref = safe_git_ref(base_ref)
    except UnsafeSandboxInput:
        log.warning("review.config: unsafe repo/base_ref; using defaults")
        return RepoConfig(), "default"

    cache_key = dm.repo_config_key(repo)
    cached = store.rget_json(cache_key)
    cached_dict = cached if isinstance(cached, dict) else {}

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {github_token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    own = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    try:
        for filename in CONFIG_FILENAMES:
            req_headers = dict(headers)
            if (
                cached_dict.get("path") == filename
                and cached_dict.get("ref") == base_ref
                and cached_dict.get("sha")
            ):
                req_headers["If-None-Match"] = f'"{cached_dict["sha"]}"'
            try:
                resp = await client.get(
                    f"{GITHUB_API}/repos/{repo}/contents/{filename}",
                    headers=req_headers,
                    params={"ref": base_ref},
                )
            except Exception as exc:
                log.warning("review.config: failed fetching %s for %s (%s)", filename, repo, type(exc).__name__)
                return RepoConfig(), "default"

            if resp.status_code == 304 and isinstance(cached_dict.get("config"), dict):
                try:
                    return RepoConfig.model_validate(cached_dict["config"]), "repo"
                except Exception:
                    return RepoConfig(), "default"

            if resp.status_code == 404:
                continue
            if resp.status_code != 200:
                log.warning("review.config: GitHub returned HTTP %s for %s/%s@%s", resp.status_code, repo, filename, base_ref)
                return RepoConfig(), "default"

            payload = resp.json()
            if not isinstance(payload, dict):
                return RepoConfig(), "default"

            blob_sha = str(payload.get("sha") or "").strip()
            if (
                blob_sha
                and cached_dict.get("sha") == blob_sha
                and isinstance(cached_dict.get("config"), dict)
            ):
                try:
                    return RepoConfig.model_validate(cached_dict["config"]), "repo"
                except Exception:
                    pass

            encoded = payload.get("content")
            if not isinstance(encoded, str):
                return RepoConfig(), "default"
            try:
                raw_bytes = base64.b64decode(encoded)
                if len(raw_bytes) > _MAX_CONFIG_BYTES:
                    log.warning("review.config: %s in %s exceeds size limit; ignoring", filename, repo)
                    return RepoConfig(), "default"
                yaml_text = raw_bytes.decode("utf-8")
                parsed = parse_pragent_yaml(yaml_text)
            except Exception as exc:
                log.warning("review.config: invalid %s in %s@%s (%s); using defaults", filename, repo, base_ref, exc)
                return RepoConfig(), "default"

            # Store only the fields that were explicitly present (plus full validated dict
            # via exclude_unset=False? Preserve `explicit_fields` so merge knows what was set).
            stored = {
                "sha": blob_sha,
                "path": filename,
                "ref": base_ref,
                "config": parsed.model_dump(exclude_unset=True),
            }
            try:
                store.rset_json(cache_key, stored, ex=REPO_CONFIG_TTL_S)
            except Exception as exc:
                log.warning("review.config: could not cache config for %s (%s)", repo, exc)
            return parsed, "repo"
    finally:
        if own and hasattr(client, "aclose"):
            await client.aclose()

    return RepoConfig(), "default"


async def load_repo_config(
    repo: str,
    base_ref: str,
    github_token: str,
    *,
    client: Any = None,
) -> RepoConfig:
    """Fetch `.pragent.yaml`/`.pragent.yml` from the PR's base branch only."""
    cfg, _ = await load_repo_config_with_source(repo, base_ref, github_token, client=client)
    return cfg


# ---------------------------------------------------------------------------
# Merging AgentConfig v2 + RepoConfig + helper checks
# ---------------------------------------------------------------------------


def merge_agent_and_repo_config(
    agent_config: dict[str, Any] | BaseModel | None,
    repo_config: RepoConfig | dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge AgentConfig v2 settings with `.pragent.yaml` (safe defaults when absent).

    Precedence:
    - Fields explicitly set in `.pragent.yaml` (`repo_config.model_fields_set`) take
      precedence over AgentConfig defaults so repo maintainers stay in control.
    - Fields omitted in `.pragent.yaml` inherit from `agent_config`, then safe defaults.
    - `path_instructions` and `tools` from both sources are combined and deduplicated.
    """
    if isinstance(agent_config, BaseModel):
        agent: dict[str, Any] = agent_config.model_dump()
    elif isinstance(agent_config, dict):
        agent = dict(agent_config)
    else:
        agent = {}

    if repo_config is None:
        rc = RepoConfig()
        repo_set: set[str] = set()
    elif isinstance(repo_config, RepoConfig):
        rc = repo_config
        repo_set = set(rc.model_fields_set)
    elif isinstance(repo_config, dict):
        rc = RepoConfig.model_validate(repo_config)
        repo_set = set(rc.model_fields_set)
    else:
        rc = RepoConfig()
        repo_set = set()

    def _pick(field_name: str, agent_val: Any, repo_val: Any, default: Any) -> Any:
        if field_name in repo_set:
            return repo_val
        if agent_val is not None:
            return agent_val
        return default

    profile = _pick("profile", agent.get("profile"), rc.profile, "chill")
    if profile not in ("chill", "assertive"):
        profile = "chill"

    merge_gate = _pick("merge_gate", agent.get("merge_gate"), rc.merge_gate, "advisory")
    if merge_gate not in ("off", "advisory", "required"):
        merge_gate = "advisory"

    raw_threshold = _pick("score_threshold", agent.get("score_threshold"), rc.score_threshold, 85)
    try:
        score_threshold = max(0, min(100, int(raw_threshold)))
    except (TypeError, ValueError):
        score_threshold = 85

    memory_enabled = (
        rc.memory.enabled
        if ("memory" in repo_set and "enabled" in rc.memory.model_fields_set)
        else (agent.get("memory_enabled") if agent.get("memory_enabled") is not None else True)
    )
    chat_enabled = (
        rc.chat.enabled
        if ("chat" in repo_set and "enabled" in rc.chat.model_fields_set)
        else (agent.get("chat_enabled") if agent.get("chat_enabled") is not None else True)
    )

    # Combine path_instructions from agent + repo without duplicates
    combined_instructions: list[dict[str, str]] = []
    seen_instr: set[tuple[str, str]] = set()
    for source_list in (agent.get("path_instructions") or [], rc.path_instructions):
        for item in source_list:
            if isinstance(item, PathInstruction):
                pat, instr = item.path.strip(), item.instructions.strip()
            elif isinstance(item, dict):
                pat = str(item.get("path") or "").strip()
                instr = str(item.get("instructions") or "").strip()
            else:
                continue
            if pat and instr and (pat, instr) not in seen_instr:
                seen_instr.add((pat, instr))
                combined_instructions.append({"path": pat, "instructions": instr})

    # Combine tools from agent + repo without duplicates
    combined_tools: list[str] = []
    seen_tools: set[str] = set()
    for t in list(agent.get("tools") or []) + list(rc.tools):
        ts = str(t or "").strip().lower()
        if ts and _TOOL_RE.fullmatch(ts) and ts not in seen_tools:
            seen_tools.add(ts)
            combined_tools.append(ts)

    return {
        "id": str(agent.get("id") or ""),
        "name": str(agent.get("name") or agent.get("id") or "pr-agent"),
        "system_prompt": str(agent.get("system_prompt") or "").strip(),
        "runtime_mode": str(agent.get("runtime_mode") or "review_only"),
        "autofix": str(agent.get("autofix") or "suggest"),
        "profile": profile,
        "merge_gate": merge_gate,
        "score_threshold": score_threshold,
        "memory_enabled": bool(memory_enabled),
        "memory": {
            "enabled": bool(memory_enabled),
            "auto_approve": bool(rc.memory.auto_approve),
        },
        "chat_enabled": bool(chat_enabled),
        "chat": {"enabled": bool(chat_enabled)},
        "labels": rc.labels.model_dump(),
        "auto_review": rc.auto_review.model_dump(),
        "path_filters": rc.path_filters.model_dump(),
        "path_instructions": combined_instructions[:50],
        "pre_merge_checks": [c.model_dump() for c in rc.pre_merge_checks],
        "tools": combined_tools[:20],
        "weights": dict(rc.weights),
        "merge_weights": dict(rc.weights),
    }


def filter_paths_by_config(paths: list[str], path_filters: PathFilters | dict[str, Any] | None) -> list[str]:
    """Filter repo paths according to `path_filters` (include globs then exclude globs)."""
    if not path_filters:
        return list(paths)
    pf = path_filters if isinstance(path_filters, PathFilters) else PathFilters.model_validate(path_filters)
    out: list[str] = []
    for path in paths:
        if pf.include and not any(_glob_matches(path, pat) for pat in pf.include):
            continue
        if pf.exclude and any(_glob_matches(path, pat) for pat in pf.exclude):
            continue
        out.append(path)
    return out


def should_auto_review(pr_meta: dict[str, Any], merged_config: dict[str, Any]) -> tuple[bool, str]:
    """Check `auto_review` config (`enabled`, `drafts`, `base_branches`, `ignore_title_keywords`)."""
    ar = merged_config.get("auto_review") if isinstance(merged_config.get("auto_review"), dict) else {}
    if ar.get("enabled") is False:
        return False, "auto_review disabled in repo config"
    if pr_meta.get("draft") and not ar.get("drafts", False):
        return False, "draft PR skipped by auto_review.drafts=false"
    base_branches = ar.get("base_branches") or []
    base_ref = str(pr_meta.get("base_ref") or "").strip()
    if base_branches and base_ref:
        if not any(_glob_matches(base_ref, str(pat)) for pat in base_branches):
            return False, f"base branch '{base_ref}' not in auto_review.base_branches"
    title = str(pr_meta.get("title") or "")
    for kw in ar.get("ignore_title_keywords") or []:
        kw_s = str(kw or "").strip()
        if kw_s and kw_s.lower() in title.lower():
            return False, f"PR title matched ignore_title_keywords '{kw_s}'"
    return True, ""
