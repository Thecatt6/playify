"""The lock file must stay in step with requirements.txt.

`requirements.txt` is what humans edit; `requirements.lock` is what Docker
installs. If someone adds a dependency and forgets to regenerate the lock, the
image silently keeps the old set — exactly the kind of drift that made the
davey/discord.py mismatch so hard to diagnose.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "requirements.txt"
LOCK = ROOT / "requirements.lock"

sys.path.insert(0, str(ROOT / "tools"))
from freeze_lock import lock_lines, render  # noqa: E402


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def _requirement_names(text: str) -> list[str]:
    names = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if match:
            names.append(match.group(1))
    return names


def test_every_direct_requirement_is_pinned_in_the_lock():
    names = {_canonical(name) for name in _requirement_names(REQUIREMENTS.read_text("utf-8"))}
    locked = {
        _canonical(line.split("==", 1)[0])
        for line in LOCK.read_text("utf-8").splitlines()
        if "==" in line and not line.startswith("#")
    }
    missing = sorted(names - locked)
    assert not missing, f"not in requirements.lock: {missing}"


def test_the_lock_pins_exact_versions_only():
    for line in LOCK.read_text("utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        assert re.fullmatch(r"[A-Za-z0-9._-]+==[^\s=<>!~]+", line.strip()), line


def test_the_lock_carries_the_davey_guard():
    """The pair that broke the audio must be visible in the lock."""
    lock = LOCK.read_text("utf-8")
    davey = re.search(r"^davey==(\d+)\.(\d+)", lock, re.MULTILINE)
    assert davey, "davey is missing from the lock"
    major, minor = int(davey.group(1)), int(davey.group(2))
    assert (major, minor) < (0, 2), "davey 0.2 needs discord.py support for DAVE transitions"


def test_the_generator_renders_sorted_pins_with_a_header():
    report = {
        "install": [
            {"metadata": {"name": "Requests", "version": "2.32.3"}},
            {"metadata": {"name": "aiohttp", "version": "3.14.5"}},
            {"metadata": {"name": "requests", "version": "2.32.3"}},
            {"metadata": {"name": "broken"}},
        ]
    }
    assert lock_lines(report) == ["aiohttp==3.14.5", "Requests==2.32.3"]
    text = render(report)
    assert text.startswith("#")
    assert "2 packages." in text
    assert text.rstrip().endswith("Requests==2.32.3")


def test_the_generator_reads_a_report_from_a_file(tmp_path):
    report = {"install": [{"metadata": {"name": "aiohttp", "version": "1.0"}}]}
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), "utf-8")
    from freeze_lock import main

    assert main(["freeze_lock.py", str(path)]) == 0
