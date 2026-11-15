---
title: FAQ & troubleshooting
description: Advanced solutions to common issues.
---


## The music is lagging / stuttering

**Playify is heavily optimized and almost never lags the host machine.** 
The bot runs its FFmpeg audio processing with `IDLE_PRIORITY_CLASS` (on Windows) or `os.nice(19)` (on Linux/Mac) to ensure it yields CPU time to your other applications. 

If the audio is stuttering in Discord, it is almost **always** a network connection issue between the bot's server and Discord's voice servers.
1. Try changing the voice channel's region in Discord settings.
2. If the bot gets stuck in a "zombie state", use the `/reconnect` command.

## The bot is in the voice channel but no audio plays

**Fastest check: does the bot's avatar show the green speaking ring while nobody hears anything?**
If yes, the audio leaves the bot but cannot be decrypted by the listeners, and since 2 November 2026
the usual cause is **`davey` 0.2**: it moved DAVE (Discord's end-to-end voice encryption) key
rotation to `prepare_transition()` / `execute_transition()`, which discord.py 2.7.x never calls.
Playify now pins `davey>=0.1.6,<0.2` and `/diagnose` reports
`davey ❌ 0.2.0 — cannot rotate DAVE keys…` in that case:

```bash
pip install "davey>=0.1.6,<0.2"
```

`/diagnose` also reports the live **E2EE (DAVE)** state: `protocol 1/1 · session ready · ✅ encrypting: yes`.
If it says `encrypting: no` the bot is sending plain audio into an encrypted call and nobody can hear
it: run `/reconnect`. `python tools/doctor.py` has a **Dependency pins** section that compares what is
installed with `requirements.txt` and runs `pip check`, so the dependency question is settled by one
command instead of a guess.

Run **`/diagnose`** in the channel first (or **`!diagnose`** if the slash command has not reached
your client yet — Discord caches global commands; Playify now also pushes a copy into every server at
startup and logs the full list as `Slash commands available: …`): it reports the voice connection, the current source, the
volume and whether `ffmpeg`/`PyNaCl`/`davey` are installed, in one private embed. If you prefer a
terminal, `python tools/doctor.py` checks the environment at once.

If `/diagnose` shows everything ✅, the volume is above 2 % and **Audio measurement** says
`audible ✅`, then the bot really is sending sound into the channel and the problem is on the client
side. In that order:

1. **Your own per-user volume for the bot.** Right-click Playify in the voice channel → **Volume**.
   This slider lives only in your client: the bot cannot see it, and it silences just you. It is the
   most common cause of "everyone else hears it".
2. **You are deafened** (the `You` field in `/diagnose` says so) or Discord is sending the audio to
   another output device.
3. Ask somebody else in the channel: if they hear the music, it is not the bot.

Otherwise the cause is one of these:

1. **A dependency is missing.** discord.py needs `PyNaCl` to encrypt voice and `davey` to talk in
   end-to-end encrypted channels, while `ffmpeg` decodes the audio. Look for
   `PYNACL IS NOT INSTALLED` / `DAVEY IS NOT INSTALLED` in the log at startup, or for
   `VOICE SUPPORT IS NOT READY` (Playify prints a boxed warning when one of them is missing).
   ```bash
   pip install -r requirements.txt
   sudo apt install -y ffmpeg      # or: apk add ffmpeg on Alpine
   ```
2. **The voice connection died "quietly".** Discord keeps showing the bot in the channel while the
   underlying voice client is already gone: the controller counts down but nothing is audible, and
   every command complains about the voice channel. Playify now watches for this and rebuilds the
   connection on its own within ~25 seconds, resuming the track where it stopped. You can force it
   immediately with `/reconnect`, or from the Web UI with the **Reconnect** button that appears in
   the red banner.
3. **The volume is at (or near) zero.** Playify shows `0% · muted` in the Web UI when the volume is
   under 2 %, and every `Playing …` log line now carries the volume it started with. Raise it with
   `/volume 100` (or the slider in the Web UI): at 0 % FFmpeg happily decodes audio nobody hears.
4. **Permissions.** The bot needs *Connect* and *Speak* in that channel, and *Use Voice Activity*
   (or push-to-talk bypass) if the channel enforces it.

If the log says `Voice support ready (ffmpeg=…, PyNaCl=yes, davey=yes)`, the environment is fine and
the problem is on the Discord side (channel permissions, region, or a muted/deafened bot).

## How does `/reconnect` work?

Discord's voice connection drops frequently, especially for bots running 24/7.
Playify's `/reconnect` command is special: it saves the exact millisecond (`seek_time`) of the current track, drops the connection entirely, reconnects, and resumes the audio *exactly* where it left off. It's the ultimate fix for audio desync or silence bugs without skipping the song.

## The bot says "FFmpeg is not found"

Playify normally downloads FFmpeg automatically. If it failed:
1. You can manually download `ffmpeg.exe` and place it in the `bin/` folder inside your Playify directory (create the `bin` folder if it doesn't exist).
2. [Download FFmpeg 6.1.1 here](https://www.videohelp.com/download/ffmpeg-6.1.1-full_build.7z?r=zndlFgBq) (extract and grab `ffmpeg.exe` from the `bin` folder).

## Do I need to enter my tokens every time?

Nope! The setup script saves your tokens in a `.env` file. The next time you want to start the bot, just double-click `start.bat` (or run `start.sh`). It will instantly start the bot without asking for tokens again.

## Why does playing a Spotify link stream from YouTube?

Spotify uses DRM (Widevine) to encrypt all audio files and does **not** provide direct audio streaming through their Web API. Spotify Developer tokens only grant access to track and playlist metadata (song titles, artists, albums, etc.).

This is why **every Discord music bot** (including Playify, FredBoat, Rythm, etc.) works by extracting the metadata from Spotify and streaming the matched audio track from YouTube or SoundCloud. 

Playify preserves all Spotify metadata: the music controller displays the genuine Spotify title and artist, links directly back to the song on Spotify, and displays `Source: Spotify 🟢` in the controller footer.
