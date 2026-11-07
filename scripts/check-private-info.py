#!/usr/bin/env python3
"""Refuse local-environment details in what goes into the public repository.

Checks file contents for LAN addresses, mDNS (.local) hostnames, home-directory
paths, real e-mail addresses, tailnet names, dev hostnames and values shaped like
secrets (tokens, API keys, webhook URLs, private keys), and checks commit
author/committer e-mails. Local notes belong in AGENTS.local.md (gitignored).

Adapted from biblioflow's script of the same name.

  scripts/check-private-info.py                 # all tracked files (CI)
  scripts/check-private-info.py --staged        # staged files (pre-commit hook)
  scripts/check-private-info.py --commits A..B  # also commit e-mails and messages in A..B (CI; HEAD = all history)
  scripts/check-private-info.py --author        # the e-mail the next commit will use (pre-commit hook)
  scripts/check-private-info.py --message FILE  # a commit message (commit-msg hook)
"""

from __future__ import annotations

import argparse
import fnmatch
import ipaddress
import re
import subprocess
import sys

SKIP_FILES = ["*.lock", "*lock.json", "*.png", "*.jpg", "*.ico", "*.pdf", "*.woff*"]

# Files that list private ranges on purpose (none yet; an SSRF guard and its tests will go here).
IP_ALLOWED_FILES: list[str] = []
# Addresses used as documentation examples.
IP_ALLOWED = {"192.168.1.10"}
CGNAT = ipaddress.ip_network("100.64.0.0/10")  # Tailscale addresses
THIS_NET = ipaddress.ip_network("0.0.0.0/8")

EMAIL_ALLOWED = re.compile(
    r"@(([a-z0-9-]+\.)*example\.(com|org|net)|users\.noreply\.github\.com|anthropic\.com|github\.com)$|^noreply@",
    re.I,
)
PUBLIC_HOSTS = {"sounding.pathvector.dev"}
MDNS_ALLOWED: set[str] = set()
HOME_PLACEHOLDERS = {"you", "me", "user", "username", "runner", "example", "<you>"}
# Commits already on the public main whose message has a known, accepted finding: {sha: reason}.
KNOWN_MESSAGES: dict[str, str] = {}

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.]|/\d)")
MDNS = re.compile(r"(?<![\w.-])([A-Za-z0-9][A-Za-z0-9-]*\.local)(?![\w.-])")
HOME = re.compile(r"(?:/Users|/home)/([A-Za-z0-9][A-Za-z0-9._-]*)")
TAILNET = re.compile(r"\b[a-z0-9-]+\.(?:tail[0-9a-f]+|[a-z0-9-]+)\.ts\.net\b")
OWN_DOMAIN = re.compile(r"\b(?:[a-z0-9-]+\.)+pathvector\.dev\b")
# Values shaped like secrets. Only the kind is reported, never the value.
SECRETS = [
    ("JWT or Cloudflare tunnel token", re.compile(r"\beyJ[A-Za-z0-9_-]{30,}")),
    ("LLM API key", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Discord webhook URL", re.compile(r"discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]{20,}")),
    ("Slack webhook URL", re.compile(r"hooks\.slack\.com/services/T[A-Z0-9]+/[A-Z0-9]+/[A-Za-z0-9]{10,}")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("secret in an assignment", re.compile(
        r"\b[A-Z0-9_]*(?:PASSWORD|SECRET|TOKEN|API_KEY|AUTHKEY|WEBHOOK_URL)=['\"]?(?!change-me|https://example\.com)"
        r"[A-Za-z0-9+/_.:-]{24,}")),
]


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def problems_in(text: str, path: str) -> list[str]:
    out: list[str] = []
    ip_ok = any(fnmatch.fnmatch(path, p) for p in IP_ALLOWED_FILES)
    for n, line in enumerate(text.splitlines(), 1):
        where = f"{path}:{n}"
        for m in IPV4.finditer(line):
            try:
                ip = ipaddress.ip_address(m.group(1))
            except ValueError:
                continue
            lan = (ip.is_private or ip in CGNAT) and not (ip.is_loopback or ip.is_link_local or ip in THIS_NET)
            if lan and not ip_ok and str(ip) not in IP_ALLOWED:
                out.append(f"{where}: LAN address {ip} (use 192.168.1.10 in examples)")
        for m in MDNS.finditer(line):
            if m.group(1).lower() not in MDNS_ALLOWED:
                out.append(f"{where}: mDNS hostname {m.group(1)}")
        for m in HOME.finditer(line):
            if m.group(1).lower() not in HOME_PLACEHOLDERS:
                out.append(f"{where}: home directory path {m.group(0)} (use ~ or a placeholder)")
        for m in EMAIL.finditer(line):
            if not EMAIL_ALLOWED.search(m.group(0)):
                out.append(f"{where}: e-mail address {m.group(0)} (use you@example.com)")
        for m in TAILNET.finditer(line):
            out.append(f"{where}: tailnet hostname {m.group(0)} (use <host>.<tailnet>.ts.net)")
        for m in OWN_DOMAIN.finditer(line):
            if m.group(0) not in PUBLIC_HOSTS:
                out.append(f"{where}: dev hostname {m.group(0)} (use digest.example.com)")
        for kind, pattern in SECRETS:
            if pattern.search(line):
                out.append(f"{where}: looks like a {kind} (keep real values in ~/.config/sounding, see LOCAL_DEV.md)")
    return out


def skipped(path: str) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in SKIP_FILES) or path == "scripts/check-private-info.py"


def check_files(staged: bool) -> list[str]:
    out: list[str] = []
    if staged:
        paths = git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").split("\0")
    else:
        paths = git("ls-files", "-z").split("\0")
    for path in filter(None, paths):
        if skipped(path):
            continue
        try:
            raw = subprocess.run(["git", "show", f":{path}"], check=True, capture_output=True).stdout \
                if staged else open(path, "rb").read()
        except (OSError, subprocess.CalledProcessError):
            continue
        if b"\0" in raw[:8192]:
            continue
        out += problems_in(raw.decode("utf-8", "replace"), path)
    return out


def check_email(email: str, what: str) -> list[str]:
    return [] if EMAIL_ALLOWED.search(email) else [f"{what}: {email} (use your GitHub noreply address)"]


def check_commits(rng: str) -> list[str]:
    out: list[str] = []
    for rec in git("log", "--format=%H%x1f%ae%x1f%ce%x1f%B%x1e", rng).split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        sha, ae, ce, body = rec.split("\x1f", 3)
        out += check_email(ae, f"commit {sha[:8]} author") + check_email(ce, f"commit {sha[:8]} committer")
        if sha not in KNOWN_MESSAGES:
            out += problems_in(body, f"commit {sha[:8]} message")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--staged", action="store_true")
    ap.add_argument("--commits")
    ap.add_argument("--author", action="store_true")
    ap.add_argument("--message")
    a = ap.parse_args()
    if a.message:
        found = problems_in(open(a.message, encoding="utf-8", errors="replace").read(), "commit message")
    else:
        found = check_files(a.staged)
    if a.commits:
        found += check_commits(a.commits)
    if a.author:
        ident = git("var", "GIT_AUTHOR_IDENT")
        email = ident[ident.find("<") + 1 : ident.find(">")]
        found += check_email(email, "git user.email")
    for p in found:
        print(p, file=sys.stderr)
    if found:
        print("\nLocal-environment details must not go into the public repository; "
              "keep them in AGENTS.local.md and secrets in ~/.config/sounding (see LOCAL_DEV.md).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
