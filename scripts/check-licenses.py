#!/usr/bin/env python3
"""Fail if a production dependency has a license we do not ship (copyleft beyond LGPL, source-available).

  scripts/check-licenses.py python                           # packages installed in the current env
  scripts/check-licenses.py npm path/to/package-lock.json    # npm production packages (dev ones skipped)

Sounding is MIT, so GPL / AGPL / SSPL / BUSL / Elastic / Commons Clause / non-commercial licenses
are refused (AGENTS.md 4.2). LGPL is allowed (psycopg, used unmodified from its PyPI wheel; listed
in THIRD_PARTY_NOTICES.md). A package without a license is refused too, unless it is in KNOWN
below with the reason.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REFUSED = re.compile(
    r"(?<!L)(?<!Lesser )\bA?GPL|\bGNU (?:Affero )?General Public|SSPL|Server Side Public|BUSL|Business Source"
    r"|\bElastic License|Commons Clause|\bRSAL|CC-BY-NC|CC-BY-SA|NonCommercial",
    re.IGNORECASE,
)
# Packages whose metadata has no usable license field, checked by hand.
KNOWN: dict[str, str] = {}


def check(entries: list[tuple[str, str]]) -> int:
    bad = []
    for name, lic in entries:
        if not lic.strip():
            if name in KNOWN:
                continue
            bad.append(f"{name}: no license in metadata")
        elif REFUSED.search(lic):
            bad.append(f"{name}: {lic}")
    for line in bad:
        print(f" ✗ {line}")
    print(f" {'✗' if bad else '✓'} {len(entries)} packages checked, {len(bad)} refused")
    return 1 if bad else 0


def npm(lockfile: str) -> list[tuple[str, str]]:
    data = json.loads(Path(lockfile).read_text())
    out = []
    for path, meta in data.get("packages", {}).items():
        if not path or meta.get("dev") or meta.get("devOptional") or meta.get("link"):
            continue
        name = path.rsplit("node_modules/", 1)[-1]
        lic = meta.get("license", "")
        if isinstance(lic, dict):
            lic = lic.get("type", "")
        out.append((f"{name}@{meta.get('version', '?')}", str(lic)))
    return out


def python() -> list[tuple[str, str]]:
    from importlib.metadata import distributions

    out = []
    for dist in distributions():
        meta = dist.metadata
        name = meta.get("Name", "?")
        if name in {"pip", "setuptools", "wheel"}:
            continue
        lic = meta.get("License-Expression") or ""
        if not lic:
            classifiers = [c.split(" :: ")[-1] for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
            raw = (meta.get("License") or "").strip()
            lic = "; ".join(classifiers) or (raw.splitlines()[0] if raw else "")
        out.append((f"{name}@{meta.get('Version', '?')}", lic))
    return out


def main(argv: list[str]) -> int:
    if argv[:1] == ["npm"] and len(argv) == 2:
        return check(npm(argv[1]))
    if argv == ["python"]:
        return check(python())
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
