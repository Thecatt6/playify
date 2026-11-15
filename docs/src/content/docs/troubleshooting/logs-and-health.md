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

## The diagnostics card in the panel

Log files are for forensics; the people sitting in the voice channel need to know *now*. Every
warning and error the bot logs is also kept in a small in-memory ring buffer (50 entries, messages
truncated to 300 characters) and shipped with the panel state. Managers see it as a **Diagnostics**
section inside the player card, collapsed until something goes wrong:

* the counter reads `1 error, 2 warnings`;
* each line shows how long ago it happened and the message, newest first;
* listeners without the *Manage Server* permission do not see it at all.

Nothing is written to disk twice: this is the same stream that lands in `logs/playify.log`, just
filtered to `WARNING` and above. Restarting the bot clears it.

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

### requirements.txt vs requirements.lock

`requirements.txt` is the file you edit: it carries the ranges and the comments explaining why
`davey` must stay below 0.2. `requirements.lock` is the frozen resolution of that file — every
direct and transitive dependency, pinned to an exact version — and it is what the Docker image
installs. A rebuild six months from now therefore produces the same environment as today, and a bug
report can name the exact versions that shipped.

After changing `requirements.txt`, regenerate the lock:

```bash
python -m venv /tmp/lock
/tmp/lock/bin/pip install --dry-run --report /tmp/lock-report.json -r requirements.txt
python tools/freeze_lock.py /tmp/lock-report.json > requirements.lock
```

`pytest` fails if a direct requirement is missing from the lock, so the two cannot drift apart.

## Metrics for Prometheus

`GET /metrics` publishes aggregate numbers in the Prometheus text format, ready for a
`scrape_config` entry:

| metric | meaning |
| --- | --- |
| `playify_uptime_seconds` | seconds since the process started |
| `playify_guilds`, `playify_voice_clients` | guilds and voice connections |
| `playify_webui_connections` | open panel websockets |
| `playify_tracks_played_total` | tracks started since boot (counter) |
| `playify_warnings_total`, `playify_errors_total` | same feed as the diagnostics card |
| `playify_queue_tracks{guild_id}`, `playify_voice_listeners{guild_id}` | per guild |
| `playify_playback_state{guild_id}` | 0 idle, 1 playing, 2 paused, 3 keepalive, 4 reconnecting |

No track titles, no names, no user ids: only counts. The endpoint is open by default because the
numbers are harmless; set `WEBUI_METRICS_TOKEN` to require
`Authorization: Bearer <token>` instead (Prometheus supports `bearer_token` in the scrape config).

```yaml
scrape_configs:
  - job_name: playify
    scheme: https
    static_configs:
      - targets: ["server.tail5751af.ts.net:10000"]
    bearer_token: change-me
```

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
