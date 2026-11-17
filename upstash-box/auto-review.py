"""Auto-review loop — runs INSIDE the Upstash Box (python runtime, stdlib only).

For every repo in REPOS, lists open PRs, reviews each new head SHA with the
OpenRouter free model, and posts the review as a PR comment. State file
skips PRs already reviewed at the same head SHA.

Env (set via Box.create env or /workspace/home/.box-env, never committed):
  GITHUB_TOKEN, OPENROUTER_API_KEY, REPOS="owner/repo,owner/repo2",
  MODEL="thinkingmachines/inkling-2-small:free" (default), MAX_DIFF_CHARS
State: /workspace/home/.reviewed.json
"""

import json
import os
import sys
import urllib.request

HOME = os.path.expanduser("~")
for env_file in ("/workspace/home/.box-env", os.path.join(HOME, ".box-env")):
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
REPOS = [r.strip() for r in os.environ.get("REPOS", "").replace(",", " ").split() if r.strip()]
MODEL = os.environ.get("MODEL", "thinkingmachines/inkling-2-small:free")
MAX_DIFF_CHARS = int(os.environ.get("MAX_DIFF_CHARS", "24000"))
STATE_PATH = os.environ.get("STATE_PATH", "/workspace/home/.reviewed.json")


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


def review_with_openrouter(title, diff):
    prompt = (
        "You are a senior code reviewer. Review this GitHub PR diff and reply in markdown:\n"
        "1) Summary (2-3 sentences) 2) Top risks/bugs with file references "
        "3) Missing tests 4) Verdict: LGTM / Needs changes.\n"
        "Be concrete, skip nitpicks.\n\nPR title: " + title + "\n\nDiff (truncated):\n" + diff[:MAX_DIFF_CHARS]
    )
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps({
            "model": MODEL,
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
    return body["choices"][0]["message"]["content"]


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
            try:
                diff = gh(f"/repos/{repo}/pulls/{pr['number']}", diff=True)
                review = review_with_openrouter(pr.get("title", ""), diff)
                gh(f"/repos/{repo}/issues/{pr['number']}/comments",
                   method="POST",
                   payload={"body": "## 🤖 Auto-review (" + MODEL + ")\n\n" + review})
                done[key] = True
                with open(STATE_PATH, "w") as f:
                    json.dump(done, f)
                print(f"[{repo}#{pr['number']}] reviewed", flush=True)
            except Exception as e:
                print(f"[{repo}#{pr['number']}] failed: {e}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
