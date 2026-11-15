#!/usr/bin/env python3
"""Turn a ``pip install --report`` JSON file into ``requirements.lock``.

Usage::

    python -m venv /tmp/lock
    /tmp/lock/bin/pip install --dry-run --report /tmp/lock-report.json -r requirements.txt
    python tools/freeze_lock.py /tmp/lock-report.json > requirements.lock

The report is the resolution pip would perform in an empty environment, so it
includes the transitive dependencies and their exact versions. Pass ``-`` to
read from stdin.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

HEADER = """\
# Fully resolved dependency set for the Docker image.
#
# requirements.txt stays the human-readable source of truth (it carries the
# comments about davey and discord.py); this file is what actually gets
# installed, so a rebuild months from now produces the same environment and
# a bug report can name the exact versions that shipped.
#
# Regenerate after changing requirements.txt:
#   python -m venv /tmp/lock && /tmp/lock/bin/pip install --dry-run \\
#       --report /tmp/lock-report.json -r requirements.txt
#   python tools/freeze_lock.py /tmp/lock-report.json > requirements.lock
#
# {count} packages.
"""


def lock_lines(report: dict[str, Any]) -> list[str]:
    """Return sorted ``name==version`` lines for every package in a pip report."""
    resolved: dict[str, tuple[str, str]] = {}
    for item in report.get("install", []) or []:
        metadata = item.get("metadata") or {}
        name = metadata.get("name")
        version = metadata.get("version")
        if not name or not version:
            continue
        # Lowercase keys collapse the duplicate entries a package gets when two
        # extras pull it in; the first spelling of the name is kept.
        resolved.setdefault(name.lower(), (str(name), str(version)))
    return [
        f"{name}=={version}" for _, (name, version) in sorted(resolved.items())
    ]


def render(report: dict[str, Any]) -> str:
    lines = lock_lines(report)
    header = HEADER.format(count=len(lines)).rstrip("\n").splitlines()
    return "\n".join(header + [""] + lines) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    raw = sys.stdin.read() if argv[1] == "-" else Path(argv[1]).read_text("utf-8")
    report = json.loads(raw)
    sys.stdout.write(render(report))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
