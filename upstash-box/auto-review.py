"""Auto-review loop — runs INSIDE the Upstash Box (python runtime, stdlib only).

For every repo in REPOS, lists open PRs, reviews each new head SHA with the
OpenRouter free model, and posts the review as a PR comment. State file
skips PRs already reviewed at the same head SHA.

Env (set via Box.create env or /workspace/home/.box-env, never committed):
  GITHUB_TOKEN, OPENROUTER_API_KEY, REPOS="owner/repo,owner/repo2",
  MODEL="thinkingmachines/inkling-2-small:free" (default), MAX_DIFF_CHARS
Optional (recommended once the cloud backend runs the decision layer):
  BACKEND_URL, BACKEND_API_TOKEN — before reviewing a head commit this loop claims
  (repo, pr_number, head_sha) at POST {BACKEND_URL}/decisions/claim. If the webhook
  path, the desktop daemon or another runner already handled that commit, it is
  skipped, so the same commit is never reviewed twice.
State: /workspace/home/.reviewed.json
"""

import json
import os
import sys
import urllib.error
import urllib.request

HOME = os.path.expanduser("~")
for env_file in ("/workspace/home/.box-env", os.path.join(HOME, ".box-env")):
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    # The file is refreshed on every provision (reused boxes included), so it
                    # must win over variables baked into the box when it was first created.
                    os.environ[k.strip()] = v.strip().strip('"').strip("'")

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
REPOS = [r.strip() for r in os.environ.get("REPOS", "").replace(",", " ").split() if r.strip()]


def normalize_openrouter_model(model):
    m = (model or "").strip()
    if m.startswith("openrouter/"):
        rest = m[len("openrouter/"):]
        if "/" in rest:
            return rest
    return m or "thinkingmachines/inkling-2-small:free"


MODEL = normalize_openrouter_model(os.environ.get("MODEL", "thinkingmachines/inkling-2-small:free"))
FALLBACK_MODEL = "openrouter/free"
RETRY_CODES = {404, 429, 500, 502, 503, 504}
MAX_DIFF_CHARS = int(os.environ.get("MAX_DIFF_CHARS", "24000"))
STATE_PATH = os.environ.get("STATE_PATH", "/workspace/home/.reviewed.json")
BACKEND_URL = os.environ.get("BACKEND_URL", "").strip().rstrip("/")
BACKEND_API_TOKEN = os.environ.get("BACKEND_API_TOKEN", "").strip()


def gh(path, method="GET", payload=None, diff=False):
    req = urllib.request.Request(
        "https://api.github.com" + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={
            "Authorization": "Bearer " + GITHUB_TOKEN,
            "Accept": "application/vnd.github.diff" if diff else "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "pr-agent-box-runner",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read().decode("utf-8", "replace")
        return json.loads(body) if not diff else body


def _call_openrouter(model_id, prompt):
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps({
            "model": model_id,
            "messages": [{"role": "user", "content": prompt}],
        }).encode(),
        method="POST",
        headers={
            "Authorization": "Bearer " + OPENROUTER_API_KEY,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        body = json.loads(r.read().decode())
    return (body.get("choices") or [{}])[0].get("message", {}).get("content", "")


def review_with_openrouter(title, diff):
    prompt = (
        "You are a senior code reviewer. Review this GitHub PR diff and reply in markdown:\n"
        "1) Summary (2-3 sentences) 2) Top risks/bugs with file references "
        "3) Missing tests 4) Verdict: LGTM / Needs changes.\n"
        "Be concrete, skip nitpicks.\n\nPR title: " + title + "\n\nDiff (truncated):\n" + diff[:MAX_DIFF_CHARS]
    )
    try:
        content = _call_openrouter(MODEL, prompt)
        if content or MODEL == FALLBACK_MODEL:
            return content
    except urllib.error.HTTPError as e:
        if e.code not in RETRY_CODES or MODEL == FALLBACK_MODEL:
            raise
    return _call_openrouter(FALLBACK_MODEL, prompt)


def save_state(done):
    """Persist the reviewed-set. A failed write must never abort the run or undo a review
    that was already posted (the backend claim is the cross-runner guard)."""
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(done, f)
    except OSError as e:
        print(f"could not save {STATE_PATH}: {e}", flush=True)


def claim_call(payload):
    headers = {"Content-Type": "application/json", "User-Agent": "pr-agent-box-runner"}
    if BACKEND_API_TOKEN:
        headers["Authorization"] = "Bearer " + BACKEND_API_TOKEN
    req = urllib.request.Request(
        BACKEND_URL + "/decisions/claim", data=json.dumps(payload).encode(), method="POST", headers=headers,
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def claim(repo, number, sha):
    """(ok_to_review, claim_id, state). With no BACKEND_URL this runner is standalone (its
    state file is the only guard). Once BACKEND_URL is set the claim is mandatory: if the
    backend cannot be reached we cannot know the commit is unreviewed, so we wait for the
    next run rather than risk a duplicate review."""
    if not BACKEND_URL:
        return True, None, None
    try:
        got = claim_call({"action": "claim", "repo": repo, "pr_number": number, "head_sha": sha, "owner": "box-cron"})
    except Exception as e:
        print(f"[{repo}#{number}] claim failed ({e}); will retry next run", flush=True)
        return False, None, "backend_unreachable"
    return bool(got.get("claimed")), got.get("claim_id"), got.get("state")


def finish_claim(repo, number, sha, claim_id, action):
    if not (BACKEND_URL and claim_id):
        return
    try:
        claim_call({"action": action, "repo": repo, "pr_number": number, "head_sha": sha, "claim_id": claim_id})
    except Exception as e:
        print(f"[{repo}#{number}] claim {action} failed: {e}", flush=True)


def main():
    if not GITHUB_TOKEN or not OPENROUTER_API_KEY or not REPOS:
        print("missing env: need GITHUB_TOKEN, OPENROUTER_API_KEY, REPOS", flush=True)
        return 1
    try:
        with open(STATE_PATH) as f:
            done = json.load(f)
    except (OSError, ValueError):
        done = {}
    for repo in REPOS:
        try:
            prs = gh(f"/repos/{repo}/pulls?state=open&per_page=20")
        except Exception as e:
            print(f"[{repo}] list PRs failed: {e}", flush=True)
            continue
        for pr in prs:
            key = f"{repo}#{pr['number']}@{pr['head']['sha']}"
            if key in done:
                continue
            sha = pr["head"]["sha"]
            ok, claim_id, state = claim(repo, pr["number"], sha)
            if not ok:
                print(f"[{repo}#{pr['number']}] already handled elsewhere ({state}); skipping", flush=True)
                if state not in ("external_claimed", "backend_unreachable"):  # these can still change
                    done[key] = True
                    save_state(done)
                continue
            published = False
            try:
                diff = gh(f"/repos/{repo}/pulls/{pr['number']}", diff=True)
                review = review_with_openrouter(pr.get("title", ""), diff)
                gh(f"/repos/{repo}/issues/{pr['number']}/comments",
                   method="POST",
                   payload={"body": "## 🤖 Auto-review (" + MODEL + ")\n\n" + review})
                published = True
                done[key] = True
                save_state(done)
                finish_claim(repo, pr["number"], sha, claim_id, "complete")
                print(f"[{repo}#{pr['number']}] reviewed", flush=True)
            except Exception as e:
                # Once the comment is out the commit counts as reviewed, whatever else fails;
                # releasing then would let another runner post a duplicate.
                finish_claim(repo, pr["number"], sha, claim_id, "complete" if published else "release")
                print(f"[{repo}#{pr['number']}] failed: {e}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
