"""Compare what is installed with the pins in ``requirements.txt``.

Playify's nastiest failures were dependency drift, not bugs in the code: a
``davey`` 0.2 released upstream made every listener hear silence while the bot
looked perfectly healthy. This module turns "the requirements say X" into a
warning in the log at startup, so the next drift is visible before it costs an
evening of guessing.
"""

from __future__ import annotations

import importlib.metadata
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger("playify.deps")

ROOT = Path(__file__).resolve().parents[3]
REQUIREMENTS_PATH = ROOT / "requirements.txt"

# Distributions whose version actually matters for voice playback: a mismatch in
# any of them is a silent (or missing) bot, so they are reported first.
CRITICAL = ("discord.py", "davey", "pynacl", "yt-dlp", "aiohttp")


def version_tuple(text: str | None) -> tuple[int, ...]:
    """``"2.7.1"`` → ``(2, 7, 1)``; stops at the first non-numeric chunk."""
    if not text:
        return ()
    parts: list[int] = []
    for chunk in re.split(r"[.\-+]", text):
        match = re.match(r"\d+", chunk)
        if not match:
            break
        parts.append(int(match.group()))
    return tuple(parts)


def read_requirements(path: Path | None = None) -> dict[str, str]:
    """``{"davey": ">=0.1.6,<0.2", ...}`` from the requirements file."""
    target = path or REQUIREMENTS_PATH
    specs: dict[str, str] = {}
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return specs
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = re.match(r"([A-Za-z0-9_.\-]+)\s*(\[[^\]]*\])?\s*(.*)$", line)
        if not match:
            continue
        specs[match.group(1).lower()] = match.group(3).strip()
    return specs


def satisfies(version: str | None, spec: str) -> bool | None:
    """Does ``version`` satisfy a pip specifier? ``None`` when it cannot be read."""
    installed = version_tuple(version)
    if not installed:
        return None
    for clause in spec.split(","):
        clause = clause.strip()
        if not clause:
            continue
        match = re.match(r"(>=|<=|==|!=|~=|>|<)\s*([0-9][0-9A-Za-z.\-+]*)", clause)
        if not match:
            return None
        operator, wanted_text = match.group(1), match.group(2)
        wanted = version_tuple(wanted_text)
        if not wanted:
            return None
        length = max(len(installed), len(wanted))
        left = installed + (0,) * (length - len(installed))
        right = wanted + (0,) * (length - len(wanted))
        if operator == ">=" and not left >= right:
            return False
        if operator == "<=" and not left <= right:
            return False
        if operator == ">" and not left > right:
            return False
        if operator == "<" and not left < right:
            return False
        if operator == "==" and left != right:
            return False
        if operator == "!=" and left == right:
            return False
        if operator == "~=" and not (left >= right and left[: len(wanted)] == wanted):
            return False
    return True


def installed_version(distribution: str) -> str | None:
    try:
        return importlib.metadata.version(distribution)
    except Exception:  # pragma: no cover - broken metadata
        return None


def pin_problems(
    requirements: dict[str, str] | None = None,
    *,
    version_of: Any = None,
) -> list[str]:
    """Human-readable list of pinned packages that do not match reality."""
    specs = read_requirements() if requirements is None else requirements
    lookup = version_of or installed_version
    problems: list[str] = []
    for dist, spec in specs.items():
        installed = lookup(dist)
        if installed is None:
            problems.append(f"{dist} is in requirements.txt but is not installed")
            continue
        verdict = satisfies(installed, spec) if spec else None
        if verdict is False:
            problems.append(f"{dist} {installed} does not satisfy '{dist}{spec}'")
    problems.sort(key=lambda text: (text.split()[0].lower() not in CRITICAL, text))
    return problems


def log_pin_problems() -> list[str]:
    """Log once, at startup, so drift is visible without a manual check."""
    problems = pin_problems()
    if not problems:
        logger.debug("Installed packages match requirements.txt.")
        return []
    logger.warning("=" * 70)
    logger.warning("INSTALLED PACKAGES DO NOT MATCH requirements.txt:")
    for problem in problems:
        logger.warning("  • %s", problem)
    logger.warning("Fix with: pip install -r requirements.txt")
    logger.warning("Run `python tools/doctor.py` for the full picture.")
    logger.warning("=" * 70)
    return problems
