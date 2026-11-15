#!/usr/bin/env python3
"""Docker healthcheck for the Playify control panel.

    python tools/healthcheck.py

Exit codes: ``0`` healthy (or nothing to check), ``1`` unhealthy.

The bot itself can be fine while the Web UI is dead (port taken, crash in the
aiohttp app, OAuth misconfigured), so this probes ``/healthz`` the way a
browser would. When ``WEBUI_ENABLED=false`` there is nothing to probe and the
check reports healthy instead of failing forever.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    if not _truthy(_env("WEBUI_ENABLED", "true")):
        print("Web UI disabled (WEBUI_ENABLED=false): nothing to probe.")
        return 0

    port = _env("WEBUI_PORT", "8080") or "8080"
    url = f"http://127.0.0.1:{port}/healthz"

    try:
        with urllib.request.urlopen(url, timeout=4) as response:
            body = response.read().decode("utf-8", "replace")
            if response.status != 200:
                print(f"unhealthy: {url} answered {response.status}")
                return 1
    except urllib.error.URLError as error:
        print(f"unhealthy: {url} is not reachable ({error.reason})")
        return 1
    except Exception as error:  # pragma: no cover - environment specific
        print(f"unhealthy: {url} failed ({type(error).__name__}: {error})")
        return 1

    try:
        payload = json.loads(body)
    except ValueError:
        print(f"unhealthy: {url} did not answer JSON")
        return 1

    if payload.get("status") != "ok":
        print(f"unhealthy: /healthz reported {payload.get('status')!r}")
        return 1

    print(
        "healthy: uptime {uptime}s, oauth {oauth}".format(
            uptime=payload.get("uptime", "?"),
            oauth="configured" if payload.get("oauth") else "missing",
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
