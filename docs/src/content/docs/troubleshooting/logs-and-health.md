---
title: Logs, health and dependency drift
description: Everything Playify writes down, and how to see a problem before your listeners do.
---

A music bot fails quietly: nobody complains until the channel is silent. These three mechanisms make
the failure visible while it is still cheap.

## The log file

Playify writes to stdout **and** to a rotating file, `logs/playify.log`, capped at 10 MB with 5
backups (50 MB total). Rotation means an old crash is never truncated away by a busy week.

| Variable | Default | What it does |
| :--- | :--- | :--- |
| `PLAYIFY_LOG_FILE` | `logs/playify.log` | Where the file goes. `off` keeps stdout only, an absolute path moves it elsewhere. |
| `LOG_LEVEL` | `INFO` | `DEBUG` adds the fine-grained voice/yt-dlp details. |

```bash
tail -f logs/playify.log            # watch it live
grep -i "not ready\|cannot encrypt\|error" logs/playify.log
```

In Docker the file lives inside the container unless you mount it; `docker-compose.yml` already
mounts `./logs` and `./data` next to the compose file, so both survive a rebuild.

## `/healthz` and the healthcheck

The panel answers `GET /healthz` with JSON:

```json
{ "status": "ok", "uptime": 1284.2, "oauth": true }
```

`tools/healthcheck.py` probes it and exits non-zero when the panel is dead — the bot can be running
with a Web UI that crashed on start, and Docker would otherwise call that healthy:

```bash
python tools/healthcheck.py
# healthy: uptime 140.5s, oauth configured
```

The `Dockerfile` declares it as a `HEALTHCHECK` (30 s interval, 45 s grace after start) and
`docker-compose.yml` declares the same for `docker compose ps`. When `WEBUI_ENABLED=false` there is
nothing to probe and the check passes immediately, so it never turns into a restart loop.

## Dependency drift is reported at startup

`requirements.txt` pins the versions that actually work together — `discord.py>=2.7,<2.8` and
`davey>=0.1.6,<0.2` exist because `davey` 0.2 broke DAVE key rotation and made every listener hear
silence while the bot looked perfect. On every start Playify compares what is installed with those
pins and, when they differ, prints a boxed warning:

```
INSTALLED PACKAGES DO NOT MATCH requirements.txt:
  • davey 0.2.0 does not satisfy 'davey>=0.1.6,<0.2'
Fix with: pip install -r requirements.txt
```

`python tools/doctor.py` prints the same comparison with the installed versions and also runs
`pip check`. `/diagnose` in Discord shows the live picture, including the DAVE state of the call.

## Continuous integration

Every push and pull request runs two jobs (the definition is in
`tools/ci/github-workflow.yml`; copy it to `.github/workflows/ci.yml` once, see `tools/ci/README.md`):

* **python tests** — `pytest` on Python 3.12 with real `ffmpeg`, after installing
  `requirements.txt`;
* **web ui (jsdom)** — `tests/ui`, which drives the real panel in jsdom: boot and login, the access
  gate, search, notifications, theme, shortcuts, drag and drop.

```bash
python -m pytest -q              # the whole Python suite
cd tests/ui && npm install && npm test   # the panel, ~3 seconds
```
