# Web UI smoke tests

The panel is vanilla JS with no build step, so the fastest way to catch a
regression is to drive the real `index.html` + `app.js` in jsdom: sign in,
receive a snapshot, click, drag, type, and inspect what reaches the server.

```bash
cd tests/ui
npm install     # once
npm test
```

Every check prints `ok`/`FAIL`; the process exits non-zero on the first failure
so CI notices. The suite covers boot and login, the connection banner, the
single-notification rule, the volume readout (including the `muted` state),
search (debounce, both platforms, stale responses), the access gate, the
automatic switch to a server where the bot is playing, 19-digit guild ids,
theme switching, keyboard shortcuts and queue drag and drop.
