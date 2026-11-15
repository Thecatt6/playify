---
title: Web UI
description: Control Playify from a modern, minimal browser interface — restricted to the people sitting in the bot's voice channel.
---

Playify ships with an optional browser interface for everything you would otherwise do with slash commands: adding songs, skipping, seeking, volume, filters, the queue, 24/7 mode and more.

It is **locked down by design**: the only way in is a Discord login, and the bot then verifies that you are *currently connected to the same voice channel as itself*. Leave the channel and the interface closes instantly.

## What you get

* Live "now playing" card with artwork, progress, seek and volume.
* Full queue management: play a track now, remove it, shuffle, or clear everything.
* Song search with instant results — or paste any link Playify supports (YouTube, SoundCloud, Spotify, Deezer, Apple Music, Tidal, Amazon Music, Bandcamp, direct audio files).
* Playback toggles: loop, autoplay, 24/7 mode and audio filters.
* Server settings for managers (`Manage Server`): default volume and kawaii mode, saved to the database like the slash commands do.
* Listener avatars, connection health and a `Reconnect` action for a laggy voice connection.
* Every change is mirrored to the Discord controller message, so both stay in sync.

:::tip
There are two ways to open it: the **🌐 Web UI** button on the player controller (it appears automatically next to *Lyrics / Karaoke / Show Queue / Jump to…* once the interface is running), or the `/webui` slash command, which replies privately with the link.
:::

## 1. Create the OAuth credentials

The interface needs to know *who* you are, which is exactly what Discord OAuth2 provides.

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) and select the application that powers your bot.
2. Go to **OAuth2** → copy the **Client ID** and **Client Secret**.
3. In **OAuth2 → Redirects**, add the callback URL of your instance:
   * Local: `http://localhost:8080/auth/callback`
   * Public: `https://music.example.com/auth/callback`

The `identify` scope is used — Playify never asks for anything else.

## 2. Configure `.env`

```env
WEBUI_ENABLED=true
WEBUI_HOST=0.0.0.0
WEBUI_PORT=8080

# The address people will open in their browser.
# It must match the redirect URL registered above.
WEBUI_BASE_URL=https://music.example.com

DISCORD_CLIENT_ID=123456789012345678
DISCORD_CLIENT_SECRET=your_client_secret
```

Restart the bot. You should see:

```text
🌐 Playify Web UI listening on https://music.example.com (OAuth redirect URI: https://music.example.com/auth/callback)
```

If OAuth credentials are missing, the bot keeps running normally and simply skips the Web UI — the rest of Playify is unaffected.

## 3. Use it

Open `WEBUI_BASE_URL` (or press the **Web UI** button under the player controller), sign in with Discord and join the voice channel where Playify is playing. The page unlocks automatically (it polls every few seconds); no refresh needed.

If you use a reverse proxy and change the public URL later, remember to update `WEBUI_BASE_URL`: the controller button and `/webui` both use it.

## How access control works

| Step | Check |
| :--- | :--- |
| Login | Discord OAuth2 (`identify`) with a signed, `HttpOnly`, `SameSite=Lax` session cookie. |
| Every request | The bot resolves the Discord user and looks for a voice channel where **both** the user and the bot are connected. |
| Every WebSocket tick | Membership is re-checked once per second. If you leave the channel, the socket receives a `revoked` message and closes. |
| Origin | State-changing calls reject cross-origin `Origin` headers; the WebSocket enforces the same origin. |
| Rate limits | Song additions and searches are throttled per user to protect yt-dlp. |

There is no password, no API key and no "admin panel": if you are not in the voice channel, you are nobody.

## Configuration reference

| Variable | Default | Description |
| :--- | :--- | :--- |
| `WEBUI_ENABLED` | `true` | Set to `false` to disable the interface entirely. |
| `WEBUI_HOST` | `0.0.0.0` | Interface to bind. Use `127.0.0.1` when a reverse proxy runs on the same host. |
| `WEBUI_PORT` | `8080` | TCP port. |
| `WEBUI_BASE_URL` | *(derived from the request)* | Public URL; used to build the OAuth redirect. Strongly recommended. |
| `DISCORD_CLIENT_ID` | — | OAuth2 client id. |
| `DISCORD_CLIENT_SECRET` | — | OAuth2 client secret. |
| `WEBUI_SECRET` | auto-generated | Secret used to sign session cookies. Generated once into `data/webui_secret`. |
| `WEBUI_SESSION_TTL` | `43200` | Session lifetime in seconds (12 hours). |
| `WEBUI_SECURE_COOKIES` | `true` if `WEBUI_BASE_URL` is https | Marks cookies as `Secure`. |
| `WEBUI_TRUST_PROXY` | `true` if `WEBUI_BASE_URL` is set | Honours `X-Forwarded-Proto` / `X-Forwarded-Host`. |
| `WEBUI_ALLOW_ANY_ORIGIN` | `false` | Disables the Origin check. **Development only** — needed only by the demo script. |

## Reverse proxy (recommended)

Discord requires HTTPS redirect URLs unless you use `localhost`, so put the interface behind a proxy in production. The WebSocket endpoint (`/ws`) must be proxied too.

:::caution[HTTPS]
Never expose the plain HTTP port to the internet: session cookies are only marked `Secure` when `WEBUI_BASE_URL` starts with `https://`.
:::

**Caddy**

```caddyfile
music.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

**Nginx**

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

With a proxy on the same machine, set `WEBUI_HOST=127.0.0.1`, `WEBUI_BASE_URL=https://music.example.com` and keep `WEBUI_TRUST_PROXY` enabled.

## Docker

`docker-compose.yml` already publishes port `8080`. Add the variables to your `.env` file:

```yaml
services:
  playify-discord-bot:
    env_file: .env
    ports:
      - "8080:8080"
```

## Troubleshooting

<details>
<summary><b>The interface does not start</b></summary>

Look for a `Web UI not started` line in the logs. The usual causes are missing `DISCORD_CLIENT_ID` / `DISCORD_CLIENT_SECRET`, `WEBUI_ENABLED=false`, or the port already being used (`Web UI could not bind …`). Change `WEBUI_PORT` and restart.
</details>

<details>
<summary><b>"Invalid OAuth2 redirect_uri"</b></summary>

The redirect registered in the Discord Developer Portal must match `WEBUI_BASE_URL` **exactly** — same scheme, host, port and `/auth/callback` path. If you changed the port, update both sides.
</details>

<details>
<summary><b>I stay on "Join Playify's voice channel"</b></summary>

That screen is the access rule working as intended. The bot must be connected to voice; start playback with `/play`, then join that channel. The page unlocks on its own within a few seconds.
</details>

<details>
<summary><b>Buttons do nothing / "Live" turns into "Reconnecting"</b></summary>

Usually a proxy that does not forward WebSocket upgrades. Enable the `Upgrade`/`Connection` headers (see the Nginx snippet) or use Caddy, which does it automatically.
</details>

## Developing the interface

The front-end is plain HTML/CSS/JS — no build step. To iterate without a Discord token:

```bash
python tools/webui_demo.py
```

This runs the real HTTP/WebSocket layer against a simulated bot (fake queue that advances on its own, canned search results) and logs you in automatically as a fake listener on <http://localhost:8080>. It is a development toy: never expose it publicly.

The automated tests use the same fakes:

```bash
pip install pytest pytest-asyncio aiohttp
pytest -q
```

They cover access control, session signing, the snapshot builder, the player actions and the WebSocket live loop — no Discord token required.
