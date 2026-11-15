#!/usr/bin/env python3
"""Playify doctor — tells you why the bot is silent or the Web UI refuses to start.

Run it inside the same environment as the bot::

    python tools/doctor.py

Every line is a check with a ✅/⚠️/❌ marker and, when something is wrong, the
exact command that fixes it. Exit code is 1 when a critical check fails, so it can
be used from a start-up script or a Docker healthcheck.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OK = "✅"
WARN = "⚠️ "
FAIL = "❌"

failures = 0


def line(marker: str, text: str, hint: str = "") -> None:
    global failures
    if marker == FAIL:
        failures += 1
    print(f"{marker} {text}")
    if hint:
        for chunk in hint.splitlines():
            print(f"     {chunk}")


def section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


def package_version(dist: str) -> str | None:
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return None
    except Exception:  # pragma: no cover - broken metadata
        return None


def module_present(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:  # pragma: no cover
        return False


def read_env() -> dict[str, str]:
    """Read .env without importing the bot (so the doctor always works)."""
    values: dict[str, str] = {}
    env_file = ROOT / ".env"
    if env_file.exists():
        for raw in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            text = raw.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            key, _, value = text.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    for key, value in os.environ.items():
        values.setdefault(key, value)
    return values


def check_python() -> None:
    section("Runtime")
    version = sys.version_info
    text = f"Python {version.major}.{version.minor}.{version.micro}"
    if version < (3, 10):
        line(FAIL, text, "Playify needs Python 3.10 or newer.")
    else:
        line(OK, text)
    line(OK, f"Project root: {ROOT}")
    if (ROOT / ".venv").exists() or "VIRTUAL_ENV" in os.environ:
        line(OK, "Running inside a virtual environment")
    else:
        line(WARN, "No virtual environment detected", "python -m venv .venv && source .venv/bin/activate")


def check_voice() -> None:
    section("Voice (this is what makes audio work)")

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        try:
            out = subprocess.run(
                [ffmpeg, "-version"], capture_output=True, text=True, timeout=5
            ).stdout.splitlines()
            line(OK, f"ffmpeg: {ffmpeg}", out[0] if out else "")
        except Exception:
            line(OK, f"ffmpeg: {ffmpeg}")
    else:
        line(
            FAIL,
            "ffmpeg is not installed or not in PATH → nothing can be decoded",
            "Debian/Ubuntu: sudo apt install -y ffmpeg\n"
            "Alpine: apk add ffmpeg\n"
            "Docker: the bundled image already includes it (rebuild with --build)",
        )

    if ffmpeg:
        # Decode a generated tone through the same filter playback uses: proves
        # the decode chain works, not just that the binary exists.
        try:
            probe = subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-nostdin",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=1",
                    "-af",
                    "volumedetect",
                    "-f",
                    "null",
                    "-",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            stderr = probe.stderr or ""
            if "mean_volume:" in stderr:
                mean = stderr.split("mean_volume:")[1].split("dB")[0].strip()
                line(OK, f"ffmpeg decodes audio (test tone: mean {mean} dB)")
            else:
                line(FAIL, "ffmpeg cannot decode audio", "reinstall ffmpeg")
        except Exception as error:  # pragma: no cover - environment specific
            line(WARN, f"could not run the ffmpeg self-test ({type(error).__name__})")

    if module_present("nacl"):
        line(OK, f"PyNaCl {package_version('PyNaCl') or ''}".strip())
    else:
        line(
            FAIL,
            "PyNaCl is missing → discord.py cannot send any audio",
            "pip install PyNaCl",
        )

    if module_present("davey"):
        davey_version = package_version("davey") or "?"
        if davey_version.startswith("0.2") or davey_version.startswith("1."):
            line(
                FAIL,
                f"davey {davey_version} cannot rotate DAVE keys with discord.py "
                f"{package_version('discord.py') or '?'} → the bot plays and nobody hears it",
                'pip install "davey>=0.1.6,<0.2"',
            )
        else:
            line(OK, f"davey {davey_version}")
    else:
        line(
            FAIL,
            "davey is missing → voice fails on channels with end-to-end encryption",
            "pip install davey\n"
            "If pip fails on your platform, install it from a wheel or use the Docker image.",
        )

    discord_version = package_version("discord.py")
    if discord_version:
        line(OK, f"discord.py {discord_version}")
        if discord_version.split(".")[0].isdigit() and int(discord_version.split(".")[0]) < 2:
            line(FAIL, "discord.py 1.x is too old", "pip install -U 'discord.py>=2.3'")
    else:
        line(FAIL, "discord.py is not installed", "pip install -r requirements.txt")

    if module_present("yt_dlp"):
        line(OK, f"yt-dlp {package_version('yt-dlp') or ''}".strip())
    else:
        line(FAIL, "yt-dlp is missing → no track can be resolved", "pip install yt-dlp")


def check_environment() -> None:
    section("Configuration")
    env = read_env()

    if not (ROOT / ".env").exists():
        line(WARN, "No .env file found", "cp .env.example .env and fill it in")
    else:
        line(OK, ".env file found")

    if env.get("DISCORD_TOKEN"):
        line(OK, "DISCORD_TOKEN is set")
    else:
        line(FAIL, "DISCORD_TOKEN is missing", "Add your bot token to .env")

    spotify = bool(env.get("SPOTIFY_CLIENT_ID") and env.get("SPOTIFY_CLIENT_SECRET"))
    if spotify:
        line(OK, "Spotify credentials are set (Spotify search in the Web UI works)")
    else:
        line(
            WARN,
            "Spotify credentials are missing → Spotify search is skipped",
            "Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET from the Spotify developer dashboard.",
        )

    if not (env.get("GENIUS_TOKEN") or "").strip():
        line(WARN, "/lyrics falls back to the default provider (no GENIUS_TOKEN)")


def _port_available(host: str, port: int) -> tuple[bool, str]:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    probe = socket.socket(family, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind((host, port))
        return True, ""
    except OSError as exc:
        return False, str(exc)
    finally:
        probe.close()


def check_webui() -> None:
    section("Web UI")
    env = read_env()

    enabled = (env.get("WEBUI_ENABLED") or "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    if not enabled:
        line(OK, "Disabled with WEBUI_ENABLED=false")
        return

    host = env.get("WEBUI_HOST") or "0.0.0.0"
    try:
        port = int(env.get("WEBUI_PORT") or 8080)
    except ValueError:
        port = -1
    base_url = (env.get("WEBUI_BASE_URL") or "").strip()

    if port <= 0:
        line(FAIL, f"WEBUI_PORT is not a valid port: {env.get('WEBUI_PORT')!r}")
    else:
        free, error = _port_available(host, port)
        if free:
            line(OK, f"Port {port} is free on {host}")
        else:
            line(
                FAIL,
                f"Port {port} is already in use on {host} ({error})",
                "Pick another port (e.g. WEBUI_PORT=8090) or stop the service using it:\n"
                f"  sudo ss -ltnp 'sport = :{port}'",
            )

    if not base_url:
        line(
            FAIL,
            "WEBUI_BASE_URL is not set → the OAuth redirect URI cannot be built",
            "Set it to the public address, e.g. WEBUI_BASE_URL=https://music.example.com",
        )
    elif base_url.startswith("http://") and not any(
        part in base_url for part in ("localhost", "127.0.0.1")
    ):
        line(
            FAIL,
            f"WEBUI_BASE_URL is plain HTTP ({base_url}) → Discord rejects the login",
            "Discord only allows http for localhost. Put an HTTPS reverse proxy in front\n"
            "(Caddy/nginx + Let's Encrypt) and point WEBUI_BASE_URL at it.",
        )
    else:
        line(OK, f"Public URL: {base_url}")
        line(OK, f"Register this redirect in the Discord developer portal: {base_url}/auth/callback")

    if env.get("DISCORD_CLIENT_ID") and env.get("DISCORD_CLIENT_SECRET"):
        line(OK, "Discord OAuth credentials are set")
    else:
        line(
            FAIL,
            "DISCORD_CLIENT_ID / DISCORD_CLIENT_SECRET are missing → login is impossible",
            "Create an OAuth2 client in the Discord developer portal.",
        )

    if (env.get("WEBUI_DEV_BYPASS_USER_ID") or "").strip():
        line(
            WARN,
            "WEBUI_DEV_BYPASS_USER_ID is set → anyone can act as that user",
            "Remove it in production.",
        )


def version_tuple(text: str | None) -> tuple[int, ...]:
    """``"2.7.1"`` -> ``(2, 7, 1)``; stops at the first non-numeric chunk."""
    if not text:
        return ()
    parts: list[int] = []
    for chunk in re.split(r"[.\-+]", text):
        match = re.match(r"\d+", chunk)
        if not match:
            break
        parts.append(int(match.group()))
    return tuple(parts)


def read_requirements() -> dict[str, str]:
    """``{"davey": ">=0.1.6,<0.2", ...}`` from requirements.txt, comments stripped."""
    specs: dict[str, str] = {}
    path = ROOT / "requirements.txt"
    if not path.exists():
        return specs
    for raw in path.read_text(encoding="utf-8").splitlines():
        text = raw.split("#", 1)[0].strip()
        if not text or text.startswith("-"):
            continue
        match = re.match(r"([A-Za-z0-9_.\-]+)\s*(\[[^\]]*\])?\s*(.*)$", text)
        if not match:
            continue
        specs[match.group(1).lower()] = match.group(3).strip()
    return specs


def satisfies(version: str | None, spec: str) -> bool | None:
    """Does ``version`` satisfy a pip specifier? ``None`` when it cannot be parsed."""
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
        padded = installed + (0,) * max(0, len(wanted) - len(installed))
        wanted_padded = wanted + (0,) * max(0, len(installed) - len(wanted))
        if operator == ">=" and not padded >= wanted_padded:
            return False
        if operator == "<=" and not padded <= wanted_padded:
            return False
        if operator == ">" and not padded > wanted_padded:
            return False
        if operator == "<" and not padded < wanted_padded:
            return False
        if operator == "==" and padded != wanted_padded:
            return False
        if operator == "!=" and padded == wanted_padded:
            return False
        if operator == "~=" and not (padded >= wanted_padded and padded[: len(wanted)] == wanted):
            return False
    return True


def check_pins() -> None:
    """Installed versions against the pins in requirements.txt, plus pip check."""
    section("Dependency pins")
    specs = read_requirements()
    critical = [
        ("discord.py", "discord.py"),
        ("davey", "davey"),
        ("pynacl", "PyNaCl"),
        ("yt-dlp", "yt-dlp"),
        ("aiohttp", "aiohttp"),
    ]
    for dist, dist_metadata_name in critical:
        spec = specs.get(dist, "")
        installed = package_version(dist_metadata_name)
        if not installed:
            line(FAIL, f"{dist_metadata_name} is not installed", "pip install -r requirements.txt")
            continue
        verdict = satisfies(installed, spec) if spec else True
        if verdict is False:
            line(
                FAIL,
                f"{dist_metadata_name} {installed} does not satisfy '{dist}{spec}'",
                "pip install -r requirements.txt",
            )
        else:
            line(OK, f"{dist_metadata_name} {installed} ({spec or 'unpinned'})")

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "check"],
            capture_output=True,
            text=True,
            timeout=90,
        )
    except Exception as error:  # pragma: no cover - pip missing
        line(WARN, f"could not run 'pip check' ({type(error).__name__})")
        return
    output = (result.stdout or "").strip()
    if result.returncode == 0:
        line(OK, "pip check: no broken or conflicting requirements")
    else:
        line(FAIL, "pip check found problems", output or result.stderr.strip())


def check_install() -> None:
    section("Installation")
    for name, dist in (
        ("aiohttp", "aiohttp"),
        ("spotipy", "spotipy"),
        ("yaml", "PyYAML"),
        ("requests", "requests"),
        ("aiosqlite", "aiosqlite"),
    ):
        if module_present(name):
            line(OK, f"{name} {package_version(dist) or ''}".strip())
        else:
            line(FAIL, f"{name} is missing", "pip install -r requirements.txt")

    data_dir = ROOT / "data"
    if data_dir.exists():
        writable = os.access(data_dir, os.W_OK)
        line(OK if writable else WARN, f"data/ directory {'writable' if writable else 'is not writable'}")
    else:
        line(WARN, "data/ does not exist yet (it is created on first run)")


def main() -> int:
    print("Playify doctor — diagnosing voice, playback and the Web UI")
    check_python()
    check_voice()
    check_environment()
    check_webui()
    check_pins()
    check_install()

    print()
    if failures:
        print(f"{FAIL} {failures} critical problem(s) found. Fix them in order, top to bottom.")
        return 1
    print(f"{OK} Everything looks good. Start the bot and check the log for 'Voice support ready'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
