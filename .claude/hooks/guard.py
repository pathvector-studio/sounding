#!/usr/bin/env python3
"""PreToolUse guard for Bash: catch the forms that permission rules miss.

Permission rules match the start of each subcommand, so `git -C . push`, `sh -c '...'` or `env git push` slip past
`Bash(git push*)`. This hook looks at the whole command text and turns the operations that need the user's approval
(outside an approved plan, or never plan-level: tags, deploys, secrets, new deps) into an "ask", and reads of secret
files into a "deny". It is a second layer, not a security boundary (see specs/agent-workflow.md).
Adapted from biblioflow's hook.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

# Loop mode (specs/agent-workflow.md "ループ"): a session whose project dir is the loop worktree runs unattended,
# so every "ask" becomes a "deny" (the loop parks the item instead of waiting on a prompt nobody sees), and the
# guard's own files and other gate paths cannot be changed or merged from there.
LOOP_DIR_MARK = "/.claude/worktrees/loop"

# Paths whose PRs the user merges themself: production (site, installer), the guard rails, and new specs.
GATED_PR_PATHS = (r"^site/", r"^install\.sh$", r"^\.claude/", r"^\.githooks/", r"^\.github/",
                  r"^scripts/check-private-info\.py$", r"^specs/agent-workflow\.md$")
# Paths the loop session must not edit in place (its own hook and settings would take effect immediately).
_S = r"(^|[/\s'\"=])"
LOOP_PROTECTED = (_S + r"\.claude/(settings[^/\s]*\.json|hooks/)", _S + r"\.githooks/",
                  _S + r"scripts/check-private-info\.py\b", _S + r"specs/agent-workflow\.md\b")

# `git` plus global options (-C <dir>, -c <k=v>, --git-dir=...) before the subcommand.
GIT = r"\bgit(?:\s+(?:-[Cc]\s+\S+|--\S+))*\s+"

ASK = [
    (r"\bgh\s+(release|workflow\s+run|repo\s+(edit|delete|create))\b", "gh release / workflow run / repo change"),
    (r"\bgh\s+api\b.*(-X|--method)\s*(POST|PUT|PATCH|DELETE)", "gh api write"),
    (GIT + r"tag\s+(?!-l\b|--list\b)\S", "git tag"),
    (GIT + r"(clean|reset\s+--hard)\b", "git clean / reset --hard"),
    (r"\brm\s+(-\w*r|--recursive)", "rm -r"),
    (r"\bwrangler\b", "wrangler"),
    (r"\bop\s+(run|read|inject|item|signin)\b", "1Password CLI"),
    (r"\b(npm|pnpm|yarn)\s+(install|i|add)\s+(?!-)\S", "new npm dependency"),
    (r"\buv\s+(add|pip\s+install)\b", "new Python dependency"),
]

# Reading the secret files themselves (compose passes values without printing them).
DENY = [
    (GIT + r"push\b[^|;&\n]*(--force|\s-f\b|\s\+\S)", "force push (main is protected; update branches with git merge)"),
    (r"\b(cat|less|more|head|tail|bat|grep|rg|awk|sed|cp|base64|xxd|strings)\b[^|;&\n]*(\.env\b(?!\.example)|\.config/sounding)",
     "reading a secret file (print key names only)"),
]


def is_loop(data: dict) -> bool:
    dirs = (os.environ.get("CLAUDE_PROJECT_DIR", ""), data.get("cwd", ""))
    return any(LOOP_DIR_MARK + "/" in d + "/" for d in dirs if d)


def main() -> int:
    data = json.load(sys.stdin)
    tool = data.get("tool_name")
    loop = is_loop(data)
    if tool in ("Edit", "Write", "NotebookEdit"):
        path = data.get("tool_input", {}).get("file_path", "")
        if loop and any(re.search(p, path) for p in LOOP_PROTECTED):
            return decide("deny", "guard: the loop may not edit its own guard rails; park the item for the user")
        return 0
    if tool != "Bash":
        return 0
    cmd = data.get("tool_input", {}).get("command", "")
    ask = "deny" if loop else "ask"
    park = " (loop: park the item in specs/backlog.md and move on)" if loop else ""
    for pattern, label in DENY:
        if re.search(pattern, cmd):
            return decide("deny", f"guard: {label} is not allowed (see specs/agent-workflow.md and AGENTS.md 4.3)")
    for pattern, label in ASK:
        if re.search(pattern, cmd):
            return decide(ask, f"guard: {label} needs the user's approval{park}")
    if re.search(r"\bgh\s+pr\s+merge\b.*--admin", cmd):
        return decide("deny", "guard: merging with --admin bypasses the main ruleset")
    if loop:
        if re.search(GIT + r"push\b[^|;&\n]*(\s|:)main(?![\w/-])", cmd):
            return decide("deny", "guard: the loop pushes only its own loop/* branches")
        if any(re.search(p, cmd) for p in LOOP_PROTECTED) and re.search(
                r"(>|\btee\b|\bsed\s+-i|\bperl\s+-[pi]|\bmv\b|\bcp\b|\brm\b|\bchmod\b|\bgit\s+(checkout|restore)\b)", cmd):
            return decide("deny", "guard: the loop may not change its own guard rails")
    m = re.search(r"\bgh\s+pr\s+merge\b\s*(\d+)?", cmd)
    if m:
        gated = gated_files(m.group(1))
        if gated is None:
            return decide(ask, f"guard: could not list the PR's files to check the merge gates{park}")
        if gated:
            return decide(ask, f"guard: this PR changes {', '.join(gated[:5])}; the user merges it{park}")
    return 0


def gated_files(number: str | None) -> list[str] | None:
    """Files of the PR that need the user's merge: gate paths, and in any case newly added specs."""
    args = ["gh", "pr", "view", "--json", "files"] + ([number] if number else [])
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=30, check=True).stdout
        files = json.loads(out)["files"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return None
    hits = [f["path"] for f in files if any(re.search(p, f["path"]) for p in GATED_PR_PATHS)]
    hits += [f["path"] for f in files if f["path"].startswith("specs/") and f.get("additions", 0) > 0
             and f.get("deletions", 0) == 0 and f["path"] not in hits and is_new_spec(f["path"])]
    return hits


def is_new_spec(path: str) -> bool:
    """A spec file that does not exist on origin/main yet is a new spec: the user approves it by merging."""
    if path == "specs/backlog.md":
        return False
    r = subprocess.run(["git", "cat-file", "-e", f"origin/main:{path}"], capture_output=True)
    return r.returncode != 0


def decide(decision: str, reason: str) -> int:
    json.dump(
        {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                "permissionDecisionReason": reason}},
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
