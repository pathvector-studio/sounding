#!/usr/bin/env python3
"""PreToolUse guard for Bash: catch the forms that permission rules miss.

Permission rules match the start of each subcommand, so `git -C . push`, `sh -c '...'` or `env git push` slip past
`Bash(git push*)`. This hook looks at the whole command text and turns the operations that need the user's approval
(outside an approved plan, or never plan-level: tags, deploys, secrets, new deps) into an "ask", and reads of secret
files into a "deny". It is a second layer, not a security boundary (see specs/agent-workflow.md).
Adapted from biblioflow's hook.
"""

import json
import re
import sys

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


def main() -> int:
    data = json.load(sys.stdin)
    if data.get("tool_name") != "Bash":
        return 0
    cmd = data.get("tool_input", {}).get("command", "")
    for pattern, label in DENY:
        if re.search(pattern, cmd):
            return decide("deny", f"guard: {label} is not allowed (see specs/agent-workflow.md and AGENTS.md 4.3)")
    for pattern, label in ASK:
        if re.search(pattern, cmd):
            return decide("ask", f"guard: {label} needs the user's approval")
    return 0


def decide(decision: str, reason: str) -> int:
    json.dump(
        {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                "permissionDecisionReason": reason}},
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
