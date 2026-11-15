/* Playify Web UI — vanilla JS, no build step, no dependencies. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const el = (tag, props = {}, ...children) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key === "html") node.innerHTML = value;
      else if (key.startsWith("on") && typeof value === "function") {
        node.addEventListener(key.slice(2), value);
      } else if (key === "dataset") {
        Object.assign(node.dataset, value);
      } else node.setAttribute(key, value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child.nodeType ? child : document.createTextNode(String(child)));
    }
    return node;
  };

  const svg = (name) =>
    `<svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-${name}"/></svg>`;

  const app = {
    me: null,
    config: { oauth: true, dev_bypass: false },
    sessions: [],
    guildId: null,
    snapshot: null,
    ws: null,
    wsRetry: 0,
    wsWanted: false,
    pending: new Map(),
    reqId: 0,
    searchTimer: null,
    suggestTimer: null,
    searchAbort: null,
    searchSeq: 0,
    searchCache: new Map(),
    suggestions: { query: "", items: [] },
    draggingSeek: false,
    draggingVolume: false,
    draggingDefaultVolume: false,
    volumeTimer: null,
    defaultVolumeTimer: null,
    pos: 0,
    posAt: 0,
    playing: false,
    duration: 0,
    toastTimer: null,
    gateTimer: null,
    pollTimer: null,
    lastRecovery: 0,
    lastQueueKey: "",
    dragFrom: null,
    lastVolume: 70,
    lyrics: { key: "", lines: [], synced: false, index: -1, loading: null, follow: true },
  };

  /* ------------------------------------------------------------ formatting */

  /* Discord snowflakes exceed Number.MAX_SAFE_INTEGER: 19-digit ids must stay
     strings or they get silently rounded (…100 -> …104) and stop matching. */
  const sameId = (a, b) => String(a ?? "") === String(b ?? "");

  const fmtTime = (seconds) => {
    const value = Math.max(0, Math.floor(Number(seconds) || 0));
    const h = Math.floor(value / 3600);
    const m = Math.floor((value % 3600) / 60);
    const s = value % 60;
    return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
  };

  const fmtTotal = (seconds) => {
    const value = Math.max(0, Math.round(Number(seconds) || 0));
    if (value < 60) return `${value}s`;
    const h = Math.floor(value / 3600);
    const m = Math.round((value % 3600) / 60);
    if (!h) return `${m} min`;
    return m ? `${h} h ${m} min` : `${h} h`;
  };

  /* an almost-zero volume is the #1 cause of "the bot is silent" reports */
  const setVolumeLabel = (node, value) => {
    if (!node) return;
    const level = Number(value ?? 100);
    node.textContent = level < 2 ? `${level}% · muted` : `${level}%`;
    node.classList.toggle("is-muted", level < 2);
  };

  const setFill = (input, ratio) => {
    const clamped = Math.max(0, Math.min(1, ratio));
    input.style.setProperty("--fill", `${(clamped * 100).toFixed(2)}%`);
  };

  /* ----------------------------------------------------------------- toast */

  const toast = (message, kind = "ok") => {
    if (!message) return;
    const box = $("toasts");
    const node = el(
      "div",
      { class: `toast is-${kind}` },
      el("span", { html: svg(kind === "error" ? "alert" : "spark") }),
      el("span", { text: message })
    );
    box.append(node);
    setTimeout(() => {
      node.classList.add("is-out");
      setTimeout(() => node.remove(), 260);
    }, kind === "error" ? 5200 : 3200);
    while (box.children.length > 4) box.firstElementChild.remove();
  };

  /* ------------------------------------------------------------------ gate */

  const showGate = (options = {}) => {
    const gate = $("gate");
    const action = $("gateAction");
    const retry = $("gateRetry");
    const title = options.title || "Sign in to control Playify";
    const text = options.text || "Use your Discord account to continue.";
    $("gateTitle").textContent = title;
    document.title = `Playify · ${title}`;
    $("gateText").textContent = text;
    $("gateNote").textContent =
      options.note || "Access is limited to listeners sitting in Playify's voice channel.";

    if (options.actionText) {
      action.hidden = false;
      action.textContent = options.actionText;
      action.href = options.actionHref || "/auth/login";
    } else {
      action.hidden = true;
    }
    retry.hidden = !options.showRetry;

    gate.hidden = false;
    $("app").hidden = true;
    $("boot").classList.add("is-hidden");
    stopGatePolling();

    if (options.poll) {
      app.gateTimer = setInterval(async () => {
        try {
          const response = await fetch("/api/me", { credentials: "same-origin" });
          if (response.ok) {
            const data = await response.json();
            if (data.sessions && data.sessions.length) {
              stopGatePolling();
              await enterApp(data);
            }
          }
        } catch (_) {
          /* keep waiting */
        }
      }, 4000);
    }
  };

  const stopGatePolling = () => {
    if (app.gateTimer) clearInterval(app.gateTimer);
    app.gateTimer = null;
  };

  const gateForLogin = (errorCode) => {
    const messages = {
      oauth_cancelled: "You cancelled the Discord sign-in.",
      oauth_failed: "Discord sign-in failed. Please try again.",
    };
    if (messages[errorCode]) toast(messages[errorCode], "error");

    if (!app.config.oauth && !app.config.dev_bypass) {
      showGate({
        title: "Web UI login is not configured",
        text: "This Playify instance has no Discord OAuth credentials yet. Add DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET and WEBUI_BASE_URL to the .env file, then restart the bot.",
        note: "See the Web UI documentation for the two-minute setup.",
        showRetry: true,
      });
      return;
    }

    showGate({
      actionText: "Continue with Discord",
      actionHref: "/auth/login",
      showRetry: true,
      text: "Sign in with Discord to control the music. You get access only while you are in the same voice channel as Playify.",
    });
  };

  const gateForVoice = (message) => {
    const channels = (app.me && app.me.connected_channels) || [];
    const alive = channels.filter((channel) => channel.connected !== false);
    const named = (alive.length ? alive : channels)
      .map((channel) => `#${channel.channel_name} (${channel.guild_name})`)
      .join(", ");

    let title = "Join Playify's voice channel";
    let text =
      message ||
      "Playify is not playing in a voice channel with you right now. Join the bot's voice channel and this page will unlock automatically.";

    if (channels.length && alive.length) {
      text += ` Playify is currently in ${named}. This page refreshes by itself.`;
    } else if (channels.length) {
      title = "Playify's voice connection is down";
      text = `Playify is listed in ${named} but its voice connection is not responding. The bot rebuilds it automatically within a few seconds — you can also force it with /reconnect in Discord.`;
    }

    showGate({
      title,
      text,
      showRetry: true,
      poll: true,
      note: "Listening in the same channel is what grants access — leaving it revokes access instantly.",
    });
  };

  /* When the panel was opened for a server where the bot is not playing, look
     at /api/me again: if the visitor has a session in another server, hop over
     instead of leaving them on a dead end. */
  const recoverSession = async () => {
    if (Date.now() - (app.lastRecovery || 0) < 5000) return false;
    app.lastRecovery = Date.now();
    try {
      const response = await fetch("/api/me", { credentials: "same-origin" });
      if (!response.ok) return false;
      const data = await response.json();
      app.me = data;
      app.sessions = data.sessions || [];
      const target = app.sessions.find(
        (session) => !sameId(session.guild_id, app.guildId)
      );
      if (!target || !app.sessions.length) return false;
      app.guildId = String(target.guild_id);
      localStorage.setItem("playify.guild", String(app.guildId));
      stopGatePolling();
      $("gate").hidden = true;
      $("app").hidden = false;
      $("boot").classList.add("is-hidden");
      renderIdentity();
      renderSessions();
      connect();
      toast(`Switched to ${target.guild_name || "another server"}`, "ok");
      return true;
    } catch (_) {
      return false;
    }
  };

  /* Access denied by the server, with its own explanation (not connected,
     wrong channel, different server…). */
  const gateForDenied = (message) => {
    stopPolling();
    showGate({
      title: "Can't unlock yet",
      text: message || "Join Playify's voice channel to control the player.",
      showRetry: true,
      poll: true,
      note: "Access is granted only while you sit in the same voice channel as the bot. This page re-checks by itself.",
    });
  };

  const gateForRevoked = (message) => {
    stopPolling();
    showGate({
      title: "Access closed",
      text: message || "You left Playify's voice channel, so the controls were locked.",
      showRetry: true,
      poll: true,
    });
  };

  /* -------------------------------------------------------------- app boot */

  const enterApp = async (data) => {
    app.me = data;
    app.sessions = data.sessions || [];
    if (!app.sessions.length) {
      gateForVoice();
      return;
    }

    const stored = localStorage.getItem("playify.guild") || "";
    const known = app.sessions.some((session) => sameId(session.guild_id, stored));
    app.guildId = known ? stored : String(app.sessions[0].guild_id);
    localStorage.setItem("playify.guild", String(app.guildId));

    stopGatePolling();
    $("gate").hidden = true;
    $("app").hidden = false;
    $("boot").classList.add("is-hidden");
    document.title = "Playify · Web control";

    renderIdentity();
    renderSessions();
    connect();
  };

  const load = async () => {
    const params = new URLSearchParams(location.search);
    const errorCode = params.get("error");
    if (errorCode) {
      history.replaceState({}, "", location.pathname);
    }

    try {
      const configResponse = await fetch("/api/config", { credentials: "same-origin" });
      if (configResponse.ok) app.config = await configResponse.json();
    } catch (_) {
      /* keep defaults */
    }

    try {
      const response = await fetch("/api/me", { credentials: "same-origin" });
      if (response.status === 401) {
        gateForLogin(errorCode);
        return;
      }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      app.config = { ...app.config, ...(data.login || {}) };
      if (errorCode) toast("Signed in", "ok");
      await enterApp(data);
    } catch (error) {
      showGate({
        title: "Cannot reach Playify",
        text: "The web interface could not talk to the bot. Check that the bot is running, then refresh.",
        showRetry: true,
      });
    }
  };

  /* --------------------------------------------------------------- render */

  const renderIdentity = () => {
    $("logout").hidden = !!(app.me.login && app.me.login.dev_bypass && !app.me.login.oauth);
  };

  const renderSessions = () => {
    const button = $("sessionSwitch");
    const session = currentSession();
    if (!session) return;
    $("sessionLabel").textContent = `${session.guild_name} · #${session.channel_name}`;
    button.hidden = app.sessions.length < 2;
    $("footerGuild").textContent = `${session.guild_name} · #${session.channel_name}`;
  };

  const currentSession = () =>
    app.sessions.find((session) => sameId(session.guild_id, app.guildId)) ||
    app.sessions[0] ||
    null;

  const renderListeners = (session) => {
    const box = $("listeners");
    if (!session) {
      box.replaceChildren();
      return;
    }
    const listeners = (session.listeners || []).filter((member) => !member.bot);
    const signature = listeners.map((member) => member.id + ":" + member.avatar).join("|");
    if (box.dataset.signature === signature) {
      box.title = listeners.length
        ? `${listeners.length} listener${listeners.length === 1 ? "" : "s"} in the channel`
        : "No listeners in the channel";
      return;
    }
    box.dataset.signature = signature;
    box.replaceChildren();
    const shown = listeners.slice(0, 4);
    for (const member of shown) {
      if (member.avatar) {
        const img = el("img", { class: "avatar", src: member.avatar, alt: member.name, title: member.name });
        img.loading = "lazy";
        box.append(img);
      } else {
        box.append(
          el("span", { class: "avatar", title: member.name, text: initials(member.name) })
        );
      }
    }
    if (listeners.length > shown.length) {
      box.append(
        el("span", {
          class: "avatar avatar-more",
          title: `${listeners.length - shown.length} more listeners`,
          text: `+${listeners.length - shown.length}`,
        })
      );
    }
    box.title = listeners.length
      ? `${listeners.length} listener${listeners.length === 1 ? "" : "s"} in the channel`
      : "No listeners in the channel";
  };

  const initials = (name) =>
    (name || "?")
      .split(/\s+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((part) => part[0])
      .join("")
      .toUpperCase();

  const renderConnBar = (snapshot) => {
    const bar = $("connBar");
    if (!bar) return;
    const session = snapshot.session || {};
    const healthy = session.connected !== false;
    bar.hidden = healthy;
    if (healthy) return;
    const channel = session.channel_name || "voice";
    $("connBarText").textContent = `Voice connection to #${channel} dropped — audio is paused until it is rebuilt.`;
  };

  const renderSnapshot = (snapshot) => {
    app.snapshot = snapshot;
    renderConnBar(snapshot);

    // The server is the authority on which guild this is: the page may have
    // asked with a stale id and been served a different (valid) session.
    const sessionGuild = snapshot.session && snapshot.session.guild_id;
    if (sessionGuild && !sameId(sessionGuild, app.guildId)) {
      app.guildId = String(sessionGuild);
      localStorage.setItem("playify.guild", app.guildId);
    }
    if (snapshot.session) {
      const index = app.sessions.findIndex(
        (session) => sameId(session.guild_id, snapshot.session.guild_id)
      );
      if (index >= 0) app.sessions[index] = { ...app.sessions[index], ...snapshot.session };
      renderSessions();
      renderListeners(snapshot.session);
    }

    const playback = snapshot.playback || {};
    const current = playback.current || null;
    const settings = snapshot.settings || {};

    /* now playing */
    $("trackTitle").textContent = current ? current.title : "Nothing playing";
    $("trackArtist").textContent = current
      ? [current.artist, current.requester ? `added by ${current.requester}` : ""]
          .filter(Boolean)
          .join(" · ") || "Unknown artist"
      : "Search for a song to get started";

    const art = $("art");
    const fallback = $("artFallback");
    const artUrl = current && current.thumbnail ? thumbUrl(current.thumbnail) : "";
    if (artUrl) {
      if (art.getAttribute("src") !== artUrl) art.src = artUrl;
      art.hidden = false;
      fallback.hidden = true;
    } else {
      art.hidden = true;
      art.removeAttribute("src");
      fallback.hidden = false;
      $("artHint").textContent = current ? "No artwork" : "Nothing playing";
    }

    $("liveBadge").hidden = !playback.is_live;

    /* progress */
    app.playing = playback.state === "playing";
    app.duration = Number((current && current.duration) || 0);
    app.pos = Number(playback.position || 0);
    app.posAt = performance.now();

    const seek = $("seek");
    seek.max = String(Math.max(1, Math.round(app.duration || 1)));
    seek.disabled = !snapshot.capabilities || !snapshot.capabilities.can_seek || !current;
    if (!app.draggingSeek) {
      seek.value = String(Math.min(app.pos, Number(seek.max)));
      setFill(seek, app.duration ? app.pos / app.duration : 0);
      $("pos").textContent = fmtTime(app.pos);
      $("dur").textContent = playback.is_live ? "live" : fmtTime(app.duration);
    }

    /* play / pause button */
    const playPause = $("playPause");
    playPause.innerHTML = svg(app.playing ? "pause" : "play");
    playPause.title = app.playing ? "Pause" : "Play";
    playPause.disabled = !current && (snapshot.queue?.count || 0) === 0;
    $("eq").dataset.active = app.playing ? "true" : "false";

    /* volume */
    const volume = $("volume");
    if (!app.draggingVolume) {
      volume.value = String(settings.volume ?? 100);
      setVolumeLabel($("volumeValue"), settings.volume ?? 100);
      setFill(volume, (settings.volume ?? 100) / 200);
    }

    /* toggles */
    toggleChip("loop", settings.loop);
    toggleChip("autoplay", settings.autoplay);
    toggleChip("mode24_7", settings.mode_24_7);

    /* filters */
    renderFilters(snapshot.filters || [], settings.filters || []);

    /* sleep timer */
    const sleepLeft = Number(settings.sleep_seconds_left || 0);
    $("sleepHint").textContent = sleepLeft
      ? `stops in ${fmtTime(sleepLeft)}`
      : "";

    /* server settings (only for people who can manage the server) */
    const capabilities = snapshot.capabilities || {};
    $("serverBox").hidden = !capabilities.is_manager;
    if (capabilities.is_manager) {
      toggleChip("kawaii", settings.kawaii);
      const defaultVolume = $("defaultVolume");
      if (!app.draggingDefaultVolume) {
        defaultVolume.value = String(settings.default_volume ?? 100);
        setVolumeLabel($("defaultVolumeValue"), settings.default_volume ?? 100);
        setFill(defaultVolume, (settings.default_volume ?? 100) / 200);
      }
    }

    /* lyrics: drop them when the track changes, reload if the panel is open */
    const lyricsFor = lyricsKey(current);
    $("lyricsBox").hidden = !current;
    if (lyricsFor !== app.lyrics.key) {
      app.lyrics.key = lyricsFor;
      app.lyrics.lines = [];
      app.lyrics.synced = false;
      app.lyrics.index = -1;
      app.lyrics.loading = null;
      $("lyricsLines").textContent = "";
      $("lyricsLines").hidden = true;
      $("lyricsHint").textContent = "";
      if (current) {
        setLyricsState("Open the lyrics to load them.");
        if ($("lyricsBox").open) loadLyrics();
      } else {
        setLyricsState("Nothing is playing right now.");
      }
    }

    /* diagnostics: only managers can act on them, and only if there is news */
    renderDiagnostics(snapshot.diagnostics || {}, capabilities);

    /* queue */
    renderQueue(snapshot.queue || { items: [], count: 0, total_duration: 0 });

    /* controls availability */
    document.querySelectorAll('[data-action="previous"]').forEach((button) => {
      button.disabled = !snapshot.history || snapshot.history.count < 2;
    });
    document.querySelectorAll('[data-action="skip"]').forEach((button) => {
      button.disabled = !snapshot.capabilities || !snapshot.capabilities.can_skip;
    });
  };

  const toggleChip = (name, active) => {
    const button = document.querySelector(`[data-toggle="${name}"]`);
    if (!button) return;
    button.classList.toggle("is-on", !!active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  };

  const renderFilters = (filters, active) => {
    const box = $("filterChips");
    const signature = `${filters.map((f) => f.id).join(",")}|${active.join(",")}`;
    if (box.dataset.signature === signature) return;
    box.dataset.signature = signature;
    box.replaceChildren();
    for (const filter of filters) {
      const on = active.includes(filter.id);
      const button = el(
        "button",
        {
          class: `chip${on ? " is-on" : ""}`,
          type: "button",
          "aria-pressed": on ? "true" : "false",
          onclick: () => run("filter", { filter: filter.id, enabled: !on }),
        },
        el("span", { text: filter.label })
      );
      box.append(button);
    }
    $("filterSummary").textContent = active.length ? active.join(", ") : "";
  };

  const clearDropTargets = () => {
    document
      .querySelectorAll(".queue-item.is-drop-target")
      .forEach((node) => node.classList.remove("is-drop-target"));
  };

  const attachDragHandlers = (row, index) => {
    row.addEventListener("dragstart", (event) => {
      app.dragFrom = index;
      row.classList.add("is-dragging");
      if (event.dataTransfer) {
        event.dataTransfer.effectAllowed = "move";
        try {
          event.dataTransfer.setData("text/plain", String(index));
        } catch (_) {
          /* some browsers refuse (private mode); the closure carries the index */
        }
      }
    });

    row.addEventListener("dragend", () => {
      app.dragFrom = null;
      row.classList.remove("is-dragging");
      clearDropTargets();
    });

    row.addEventListener("dragover", (event) => {
      if (app.dragFrom === null || app.dragFrom === index) return;
      event.preventDefault();
      clearDropTargets();
      row.classList.add("is-drop-target");
    });

    row.addEventListener("dragleave", () => row.classList.remove("is-drop-target"));

    row.addEventListener("drop", async (event) => {
      event.preventDefault();
      const from =
        app.dragFrom ?? Number(event.dataTransfer && event.dataTransfer.getData("text/plain"));
      clearDropTargets();
      row.classList.remove("is-drop-target");
      app.dragFrom = null;
      if (!Number.isInteger(from) || from === index || from < 0) return;
      await run("move", { from, to: index }, { silent: true });
    });
  };

  /* ------------------------------------------------------------------ lyrics */

  const lyricsKey = (current) =>
    current ? `${current.title || ""}|${current.artist || ""}` : "";

  const setLyricsState = (text) => {
    const state = $("lyricsState");
    state.textContent = text;
    state.hidden = !text;
  };

  const renderLyrics = (payload) => {
    const list = $("lyricsLines");
    list.textContent = "";
    app.lyrics.index = -1;
    app.lyrics.lines = payload && payload.lines ? payload.lines : [];
    app.lyrics.synced = Boolean(payload && payload.synced && app.lyrics.lines.length);

    if (!app.lyrics.synced && payload && payload.plain) {
      // No timestamps: show the words, evenly spaced, without highlighting.
      const lines = String(payload.plain).split("\n").filter((line) => line.trim());
      list.hidden = false;
      list.dataset.synced = "false";
      lines.forEach((line) => {
        const item = document.createElement("li");
        item.className = "lyrics-line";
        item.textContent = line;
        list.append(item);
      });
      setLyricsState("");
      $("lyricsHint").textContent = "";
      return;
    }

    if (!app.lyrics.synced) {
      list.hidden = true;
      list.dataset.synced = "false";
      setLyricsState("No lyrics found for this track.");
      $("lyricsHint").textContent = "";
      return;
    }

    list.hidden = false;
    list.dataset.synced = "true";
    app.lyrics.lines.forEach((line, index) => {
      const item = document.createElement("li");
      item.className = "lyrics-line";
      item.dataset.time = String(line.time);
      item.dataset.index = String(index);
      item.textContent = line.text;
      item.tabIndex = 0;
      item.setAttribute("role", "button");
      item.title = `Jump to ${fmtTime(line.time)}`;
      list.append(item);
    });
    setLyricsState("");
    $("lyricsHint").textContent = "synced";
  };

  const loadLyrics = async () => {
    const current = app.snapshot && app.snapshot.playback && app.snapshot.playback.current;
    if (!current) {
      setLyricsState("Nothing is playing right now.");
      return;
    }
    const key = lyricsKey(current);
    if (app.lyrics.loading === key) return;
    app.lyrics.loading = key;
    setLyricsState("Looking for lyrics…");
    $("lyricsLines").hidden = true;
    try {
      const response = await fetch(
        `/api/lyrics?guild_id=${encodeURIComponent(app.guildId)}`,
        { credentials: "same-origin" }
      );
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        setLyricsState(payload.message || "Lyrics are not available right now.");
        return;
      }
      if (lyricsKey(
        app.snapshot && app.snapshot.playback && app.snapshot.playback.current
      ) !== key) {
        return; // the track changed while we were waiting
      }
      renderLyrics(payload);
    } catch (error) {
      setLyricsState("Lyrics are not available right now.");
    } finally {
      app.lyrics.loading = null;
    }
  };

  const highlightLyrics = (position) => {
    if (!app.lyrics.synced) return;
    const lines = app.lyrics.lines;
    let index = -1;
    for (let candidate = 0; candidate < lines.length; candidate += 1) {
      if (Number(lines[candidate].time) <= position) index = candidate;
      else break;
    }
    if (index === app.lyrics.index) return;
    app.lyrics.index = index;

    const list = $("lyricsLines");
    const previous = list.querySelector(".lyrics-line.is-current");
    if (previous) previous.classList.remove("is-current");
    const item = index >= 0 ? list.children[index] : null;
    if (!item) return;
    item.classList.add("is-current");
    if (app.lyrics.follow && typeof item.scrollIntoView === "function") {
      item.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  };

  const renderDiagnostics = (diagnostics, capabilities) => {
    const box = $("diagnosticsBox");
    const list = $("diagnosticsList");
    const total = Number(diagnostics.total || 0);
    const visible = Boolean(capabilities && capabilities.is_manager) && total > 0;
    box.hidden = !visible;
    if (!visible) {
      list.textContent = "";
      return;
    }
    const errors = Number(diagnostics.errors || 0);
    const warnings = Number(diagnostics.warnings || 0);
    const parts = [];
    if (errors) parts.push(`${errors} ${errors === 1 ? "error" : "errors"}`);
    if (warnings) parts.push(`${warnings} ${warnings === 1 ? "warning" : "warnings"}`);
    $("diagnosticsSummary").textContent = parts.join(", ");

    const now = Number(app.snapshot && app.snapshot.server_time) || Date.now() / 1000;
    list.textContent = "";
    (diagnostics.recent || []).forEach((entry) => {
      const item = document.createElement("li");
      item.className = "diagnostics-item";
      item.dataset.level = entry.level === "error" ? "error" : "warning";

      const when = document.createElement("span");
      when.className = "diagnostics-when";
      const ago = Math.max(0, now - Number(entry.time || now));
      when.textContent = ago < 60 ? "just now" : fmtTime(ago) + " ago";

      const message = document.createElement("span");
      message.className = "diagnostics-message";
      message.textContent = String(entry.message || "");

      item.append(when, message);
      list.append(item);
    });
  };

  const renderQueue = (queue) => {
    const list = $("queueList");
    const items = queue.items || [];
    const key = items.map((item) => `${item.index}:${item.title}`).join("|") + `#${queue.count}`;
    const meta = queue.count
      ? `${queue.count} track${queue.count === 1 ? "" : "s"}${
          queue.total_duration ? ` · ${fmtTotal(queue.total_duration)}` : ""
        }`
      : "Queue is empty";
    $("queueMeta").textContent = meta;
    $("queueEmpty").hidden = queue.count > 0;
    $("clearQueue").disabled = queue.count === 0;

    if (app.lastQueueKey === key) return;
    app.lastQueueKey = key;
    list.replaceChildren();

    if (!items.length) return;

    const visible = items.slice(0, 150);
    for (const item of visible) {
      const art = item.thumbnail
        ? el("img", { class: "queue-art", src: thumbUrl(item.thumbnail), alt: "" })
        : el("div", { class: "queue-art is-empty", html: svg("music") });
      if (item.thumbnail) art.loading = "lazy";

      const metaParts = [item.artist, item.platform].filter(Boolean);

      const row = el(
        "li",
        { class: "queue-item", draggable: "true", dataset: { index: String(item.index) } },
          el("span", { class: "queue-index", text: String(item.index + 1) }),
          art,
          el(
            "div",
            { class: "queue-body" },
            el("div", { class: "queue-title", text: item.title }),
            el("div", {
              class: "queue-meta",
              text: metaParts.length ? metaParts.join(" · ") : "Unknown artist",
            })
          ),
          el("span", { class: "queue-duration", text: item.duration ? fmtTime(item.duration) : "" }),
          el(
            "div",
            { class: "queue-actions" },
            el("button", {
              class: "q-btn",
              type: "button",
              title: "Play this track now",
              "aria-label": "Play this track now",
              dataset: { role: "jump" },
              html: svg("play"),
            }),
            el("button", {
              class: "q-btn",
              type: "button",
              title: "Remove from queue",
              "aria-label": "Remove from queue",
              dataset: { role: "remove" },
              html: svg("x"),
            })
          )
        );
      attachDragHandlers(row, item.index);
      list.append(row);
    }

    if (items.length > visible.length) {
      list.append(
        el("li", {
          class: "queue-more",
          text: `${items.length - visible.length} more track${
            items.length - visible.length === 1 ? "" : "s"
          } in the queue`,
        })
      );
    }
  };

  /* ------------------------------------------------------------- websocket */

  const setConnection = (state, label) => {
    $("conn").dataset.state = state;
    $("connLabel").textContent = label || state;
  };

  /* Fallback for proxies that do not forward WebSocket upgrades: poll instead. */
  const stopPolling = () => {
    if (app.pollTimer) clearInterval(app.pollTimer);
    app.pollTimer = null;
  };

  const startPolling = () => {
    if (app.pollTimer || !app.guildId) return;
    app.pollTimer = setInterval(async () => {
      try {
        const response = await fetch(
          `/api/state?guild_id=${encodeURIComponent(app.guildId)}`,
          { credentials: "same-origin" }
        );
        if (response.status === 403) {
          const payload = await response.json().catch(() => ({}));
          stopPolling();
          if (payload.error === "unauthenticated") {
            gateForLogin();
            return;
          }
          if (payload.error === "bot_not_in_this_server" && (await recoverSession())) {
            return;
          }
          gateForDenied(payload.message);
          return;
        }
        if (!response.ok) return;
        const payload = await response.json();
        if (payload.state) {
          setConnection("online", "Live");
          renderSnapshot(payload.state);
        }
      } catch (_) {
        /* keep trying */
      }
    }, 2000);
  };

  const connect = () => {
    if (!app.guildId) return;
    app.wsWanted = true;
    if (app.ws) {
      app.ws.onclose = null;
      app.ws.close();
      app.ws = null;
    }
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(
      `${protocol}//${location.host}/ws?guild_id=${encodeURIComponent(app.guildId)}`
    );
    app.ws = socket;
    setConnection("connecting", "Connecting");

    socket.onopen = () => {
      app.wsRetry = 0;
      stopPolling();
      setConnection("online", "Live");
    };

    socket.onmessage = (event) => {
      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch (_) {
        return;
      }
      if (payload.type === "hello") return;
      if (payload.type === "state") {
        setConnection("online", "Live");
        renderSnapshot(payload.data);
        return;
      }
      if (payload.type === "revoked") {
        app.wsWanted = false;
        setConnection("offline", "Locked");
        gateForRevoked(payload.message);
        return;
      }
      if (payload.type === "result") {
        // Only hand the answer over: run() owns the feedback, otherwise every
        // action would toast twice (once here, once there).
        const resolve = app.pending.get(payload.request_id);
        if (resolve) {
          app.pending.delete(payload.request_id);
          resolve(payload);
        }
      }
    };

    socket.onclose = (event) => {
      app.ws = null;
      if (!app.wsWanted) return;
      if (event.code === 4003) {
        app.wsWanted = false;
        setConnection("offline", "Locked");
        const reason = event.reason || "";
        if (/another server|not in a voice channel in this server/i.test(reason)) {
          recoverSession().then((switched) => {
            if (!switched) gateForDenied(reason);
          });
          return;
        }
        gateForDenied(reason);
        return;
      }
      setConnection("offline", "Reconnecting");
      const delay = Math.min(1000 * 2 ** app.wsRetry, 10000);
      app.wsRetry += 1;
      if (app.wsRetry >= 3) startPolling();
      setTimeout(() => {
        if (app.wsWanted) connect();
      }, delay);
    };

    socket.onerror = () => {
      setConnection("offline", "Reconnecting");
    };
  };

  /* --------------------------------------------------------------- actions */

  /* Cover art is proxied by the bot: the browser must never talk to the
     platform CDNs, which would leak every listener's IP and taste. */
  const thumbUrl = (url) => {
    const raw = String(url || "").trim();
    if (!raw) return "";
    if (raw.startsWith("/")) return raw;
    if (!/^https:\/\//i.test(raw)) return "";
    return `/thumb?u=${encodeURIComponent(raw)}`;
  };

  const sendAction = (action, data = {}) =>
    new Promise((resolve) => {
      if (!app.guildId) {
        resolve({ ok: false, message: "No active server." });
        return;
      }
      const id = ++app.reqId;
      const finish = (value) => {
        if (app.pending.has(id)) app.pending.delete(id);
        resolve(value);
      };
      app.pending.set(id, finish);

      if (app.ws && app.ws.readyState === WebSocket.OPEN) {
        app.ws.send(JSON.stringify({ type: "action", request_id: id, action, data }));
      } else {
        fetch("/api/action", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ guild_id: String(app.guildId), action, data }),
        })
          .then(async (response) => {
            const payload = await response.json().catch(() => ({}));
            return { ok: response.ok && payload.ok !== false, ...payload };
          })
          .then(finish)
          .catch(() => finish({ ok: false, message: "Network error." }));
      }

      setTimeout(() => {
        if (app.pending.has(id)) finish({ ok: false, message: "No response from the bot." });
      }, 20000);
    });

  const run = async (action, data = {}, options = {}) => {
    const button = options.button;
    if (button) button.classList.add("is-busy");
    const result = await sendAction(action, data);
    if (button) button.classList.remove("is-busy");
    if (result.ok === false) {
      toast(result.message || "That action failed.", "error");
    } else if (!options.silent) {
      if (result.message) toast(result.message, "ok");
      (result.notices || []).forEach((notice) => toast(notice, "ok"));
    }
    return result;
  };

  /* -------------------------------------------------------------- handlers */

  const onControlClick = (event) => {
    const button = event.target.closest("[data-action]");
    if (!button || button.disabled) return;
    event.preventDefault();
    const action = button.dataset.action;
    if (action === "stop") {
      if (!confirm("Stop playback and disconnect Playify from the voice channel?")) return;
    }
    const data = {};
    if (button.dataset.minutes !== undefined) data.minutes = Number(button.dataset.minutes);
    run(action, data, { button, silent: action === "shuffle" });
  };

  const onToggleClick = (event) => {
    const button = event.target.closest("[data-toggle]");
    if (!button) return;
    const name = button.dataset.toggle;
    const settings = (app.snapshot && app.snapshot.settings) || {};
    const map = {
      loop: "loop",
      autoplay: "autoplay",
      mode24_7: "mode_24_7",
      kawaii: "kawaii",
    };
    const current = !!settings[map[name]];
    run(name, { enabled: !current }, { button });
  };

  const updateSeekPreview = () => {
    const seek = $("seek");
    const value = Number(seek.value);
    $("pos").textContent = fmtTime(value);
    setFill(seek, app.duration ? value / app.duration : 0);
  };

  /* ------------------------------------------------------------ chrome --- */

  const THEME_META = { dark: "#0a0b10", light: "#f4f5f9" };

  const currentTheme = () =>
    document.documentElement.dataset.theme === "light" ? "light" : "dark";

  const applyTheme = (theme) => {
    const next = theme === "light" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    const icon = $("themeIcon") && $("themeIcon").querySelector("use");
    if (icon) icon.setAttribute("href", next === "light" ? "#i-sun" : "#i-moon");
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", THEME_META[next]);
    try {
      localStorage.setItem("playify.theme", next);
    } catch (_) {
      /* private mode */
    }
  };

  const toggleTheme = () => applyTheme(currentTheme() === "light" ? "dark" : "light");

  const shortcutsOpen = () => !$("shortcuts").hidden;

  const setShortcuts = (open) => {
    $("shortcuts").hidden = !open;
    if (open) {
      const close = $("shortcutsClose");
      if (close) close.focus();
    }
  };

  const toggleMute = async () => {
    const settings = (app.snapshot && app.snapshot.settings) || {};
    const level = Number(settings.volume ?? 100);
    if (level >= 2) {
      app.lastVolume = level;
      await run("volume", { volume: 0 });
    } else {
      await run("volume", { volume: app.lastVolume || 70 });
    }
  };

  const seekBy = (delta) => {
    if (!app.snapshot || !app.duration) return;
    if ((app.snapshot.playback || {}).is_live) return;
    const target = Math.max(0, Math.min(app.duration - 1, app.pos + delta));
    app.pos = target;
    app.posAt = performance.now();
    const seek = $("seek");
    seek.value = String(Math.round(target));
    $("pos").textContent = fmtTime(target);
    setFill(seek, app.duration ? target / app.duration : 0);
    run("seek", { position: Math.round(target) }, { silent: true });
  };

  const registerServiceWorker = () => {
    if (!("serviceWorker" in navigator)) return;
    navigator.serviceWorker.register("/sw.js").catch(() => {
      /* offline shell is a bonus: the panel works without it */
    });
  };

  /* -------------------------------------------------------------- events --- */

  const setupEvents = () => {
    $("playerCard").addEventListener("click", onControlClick);
    $("lyricsBox").addEventListener("toggle", () => {
      if ($("lyricsBox").open) {
        app.lyrics.follow = true;
        if (!app.lyrics.lines.length && !app.lyrics.loading) loadLyrics();
      }
    });
    $("lyricsLines").addEventListener("click", (event) => {
      const line = event.target.closest(".lyrics-line");
      if (!line || !app.lyrics.synced) return;
      const time = Number(line.dataset.time || 0);
      app.lyrics.follow = false;
      run("seek", { position: Math.round(time) }, { silent: true });
    });
    $("lyricsLines").addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      const line = event.target.closest(".lyrics-line");
      if (!line) return;
      event.preventDefault();
      line.click();
    });
    $("queue").addEventListener("click", onQueueClick);
    $("toggles").addEventListener("click", onToggleClick);

    $("seek").addEventListener("pointerdown", () => {
      app.draggingSeek = true;
    });
    $("seek").addEventListener("input", updateSeekPreview);
    $("seek").addEventListener("change", async () => {
      const value = Number($("seek").value);
      app.draggingSeek = false;
      app.pos = value;
      app.posAt = performance.now();
      await run("seek", { position: value }, { silent: true });
    });

    const volume = $("volume");
    const applyVolume = (commit) => {
      const value = Number(volume.value);
      setVolumeLabel($("volumeValue"), value);
      setFill(volume, value / 200);
      clearTimeout(app.volumeTimer);
      if (commit) {
        app.draggingVolume = false;
        run("volume", { volume: value }, { silent: true });
      } else {
        app.draggingVolume = true;
        app.volumeTimer = setTimeout(() => {
          app.draggingVolume = false;
          run("volume", { volume: Number(volume.value) }, { silent: true });
        }, 180);
      }
    };
    volume.addEventListener("input", () => applyVolume(false));
    volume.addEventListener("change", () => applyVolume(true));

    const defaultVolume = $("defaultVolume");
    const applyDefaultVolume = (commit) => {
      const value = Number(defaultVolume.value);
      $("defaultVolumeValue").textContent = `${value}%`;
      setFill(defaultVolume, value / 200);
      clearTimeout(app.defaultVolumeTimer);
      if (commit) {
        app.draggingDefaultVolume = false;
        run("default_volume", { volume: value }, { silent: true });
      } else {
        app.draggingDefaultVolume = true;
        app.defaultVolumeTimer = setTimeout(() => {
          app.draggingDefaultVolume = false;
          run("default_volume", { volume: Number(defaultVolume.value) }, { silent: true });
        }, 650);
      }
    };
    defaultVolume.addEventListener("input", () => applyDefaultVolume(false));
    defaultVolume.addEventListener("change", () => applyDefaultVolume(true));

    $("clearQueue").addEventListener("click", async () => {
      if (!confirm("Remove every track from the queue?")) return;
      await run("clear_queue", {}, { button: $("clearQueue") });
    });

    $("reconnect").addEventListener("click", () => run("reconnect", {}, { button: $("reconnect") }));

    $("themeToggle").addEventListener("click", toggleTheme);
    $("shortcutsButton").addEventListener("click", () => setShortcuts(!shortcutsOpen()));
    $("shortcutsClose").addEventListener("click", () => setShortcuts(false));
    $("shortcuts").addEventListener("click", (event) => {
      if (event.target === $("shortcuts")) setShortcuts(false);
    });

    $("logout").addEventListener("click", async () => {
      try {
        await fetch("/auth/logout", { method: "POST", credentials: "same-origin" });
      } catch (_) {
        /* ignore */
      }
      location.href = "/";
    });

    $("gateRetry").addEventListener("click", () => location.reload());

    $("sessionSwitch").addEventListener("click", (event) => {
      event.stopPropagation();
      const existing = document.querySelector(".popover");
      if (existing) {
        existing.remove();
        return;
      }
      const popover = el("div", { class: "popover" });
      for (const session of app.sessions) {
        popover.append(
          el(
            "button",
            {
              type: "button",
              "aria-current": sameId(session.guild_id, app.guildId) ? "true" : "false",
              onclick: () => {
                popover.remove();
                selectGuild(String(session.guild_id));
              },
            },
            el(
              "span",
              {},
              el("span", { text: session.guild_name }),
              el("small", { text: `#${session.channel_name}` })
            )
          )
        );
      }
      document.querySelector(".topbar").append(popover);
      document.addEventListener(
        "click",
        () => popover.remove(),
        { once: true }
      );
    });

    const connAction = $("connBarAction");
    if (connAction) {
      connAction.addEventListener("click", async () => {
        connAction.classList.add("is-busy");
        const result = await run("reconnect", {}, { silent: true });
        connAction.classList.remove("is-busy");
        if (result.ok) toast("Voice connection restored", "ok");
      });
    }

    /* search */
    const input = $("searchInput");
    input.addEventListener("input", () => {
      const query = input.value.trim();
      clearTimeout(app.searchTimer);
      clearTimeout(app.suggestTimer);
      if (query.length < 2) {
        hideResults();
        return;
      }
      app.suggestTimer = setTimeout(() => loadSuggestions(query), 110);
      app.searchTimer = setTimeout(() => search(query), 260);
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Escape") hideResults();
    });
    $("searchForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const query = input.value.trim();
      if (!query) return;
      hideResults();
      const result = await run("play", { query }, { button: $("searchSubmit") });
      if (result.ok !== false) input.value = "";
    });

    document.addEventListener("click", (event) => {
      if (!event.target.closest(".search")) hideResults();
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        hideResults();
        setShortcuts(false);
        return;
      }
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
      if (typing || !app.snapshot) return;

      if (event.key === " " || event.code === "Space") {
        event.preventDefault();
        run("toggle", {}, { silent: true });
      } else if (event.key === "ArrowRight" && event.shiftKey) {
        event.preventDefault();
        seekBy(5);
      } else if (event.key === "ArrowLeft" && event.shiftKey) {
        event.preventDefault();
        seekBy(-5);
      } else if (event.key === "ArrowRight") {
        run("skip", {}, { silent: true });
      } else if (event.key === "ArrowLeft") {
        run("previous", {}, { silent: true });
      } else if (event.key === "m" || event.key === "M") {
        event.preventDefault();
        toggleMute();
      } else if (event.key === "l" || event.key === "L") {
        event.preventDefault();
        const box = $("lyricsBox");
        box.open = !box.open;
        if (box.open && !app.lyrics.lines.length) loadLyrics();
      } else if (event.key === "/") {
        event.preventDefault();
        $("searchInput").focus();
      } else if (event.key === "?") {
        event.preventDefault();
        setShortcuts(!shortcutsOpen());
      }
    });

    /* ---------------------------------------------------------- touch niceties */

    // Pull to refresh: only when the page is already at the top, so normal
    // scrolling is untouched. Refreshing means re-reading the state, not
    // reloading the page (the websocket is already live).
    let pullStart = null;
    let pullArmed = false;
    const PULL_TRIGGER = 70;

    const onTouchStart = (event) => {
      if (event.touches.length !== 1) return;
      const atTop = (window.scrollY || window.pageYOffset || 0) <= 0;
      pullStart = atTop ? event.touches[0].clientY : null;
      pullArmed = false;
    };

    const onTouchMove = (event) => {
      if (pullStart === null) return;
      const delta = event.touches[0].clientY - pullStart;
      const armed = delta > PULL_TRIGGER;
      $("pullHint").dataset.visible = delta > 12 ? "true" : "false";
      pullArmed = armed;
    };

    const onTouchEnd = async () => {
      const shouldRefresh = pullArmed;
      pullStart = null;
      pullArmed = false;
      $("pullHint").dataset.visible = "false";
      if (!shouldRefresh) return;
      stopPolling();
      app.snapshot = null;
      app.lastQueueKey = "";
      if (app.wsWanted) connect();
      else startPolling();
    };

    document.addEventListener("touchstart", onTouchStart, { passive: true });
    document.addEventListener("touchmove", onTouchMove, { passive: true });
    document.addEventListener("touchend", onTouchEnd, { passive: true });

    // Keep the search box above the on-screen keyboard.
    const viewport = window.visualViewport;
    if (viewport) {
      const syncKeyboard = () => {
        const shrunk = window.innerHeight - viewport.height > 140;
        document.body.classList.toggle("keyboard-open", shrunk);
      };
      viewport.addEventListener("resize", syncKeyboard);
      viewport.addEventListener("scroll", syncKeyboard);
    }
    document.addEventListener("focusin", (event) => {
      if (!event.target.closest || !event.target.closest(".search")) return;
      const scroll = () =>
        event.target.scrollIntoView({ block: "center", behavior: "smooth" });
      if (typeof event.target.scrollIntoView === "function") setTimeout(scroll, 220);
    });

    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible" && app.wsWanted && !app.ws) connect();
    });
  };

  const selectGuild = (guildId) => {
    if (sameId(guildId, app.guildId)) return;
    app.guildId = guildId;
    localStorage.setItem("playify.guild", String(guildId));
    app.snapshot = null;
    app.lastQueueKey = "";
    renderSessions();
    connect();
  };

  const onQueueClick = async (event) => {
    const button = event.target.closest(".q-btn");
    if (!button) return;
    const row = button.closest(".queue-item");
    const index = Number(row.dataset.index);
    if (button.dataset.role === "remove") {
      await run("remove", { index }, { button });
    } else if (button.dataset.role === "jump") {
      await run("jump", { index }, { button });
    }
  };

  /* ---------------------------------------------------------------- search */

  const SEARCH_CACHE_TTL = 120000;

  const hideResults = () => {
    const box = $("searchResults");
    box.hidden = true;
    box.replaceChildren();
    app.searchSeq += 1;
    if (app.searchAbort) app.searchAbort.abort();
    app.searchAbort = null;
  };

  const cacheSearch = (query, results) => {
    app.searchCache.set(query.toLowerCase(), { at: Date.now(), results });
    if (app.searchCache.size > 120) {
      const oldest = app.searchCache.keys().next().value;
      app.searchCache.delete(oldest);
    }
  };

  const cachedSearch = (query) => {
    const key = query.toLowerCase();
    const entry = app.searchCache.get(key);
    if (!entry) return null;
    if (Date.now() - entry.at > SEARCH_CACHE_TTL) {
      app.searchCache.delete(key);
      return null;
    }
    return entry.results;
  };

  /* Results already in memory are painted without waiting for the network:
     that is what makes the box feel instantaneous while you type. */
  const paintResults = (results, options = {}) => {
    const box = $("searchResults");
    box.hidden = false;
    const nodes = [];

    if (options.suggestions && options.suggestions.length) {
      nodes.push(
        el(
          "div",
          { class: "suggest-row" },
          options.suggestions.map((text) =>
            el("button", {
              class: "suggest-chip",
              type: "button",
              text,
              onclick: () => {
                $("searchInput").value = text;
                search(text);
              },
            })
          )
        )
      );
    }

    if (options.loading && !results.length) {
      nodes.push(
        el("div", { class: "results-state" }, el("span", { class: "spinner" }), "Searching YouTube and Spotify…")
      );
    } else if (!results.length && !options.error) {
      nodes.push(
        el("div", { class: "results-state", text: "No results. Try another search." })
      );
    } else if (options.error) {
      nodes.push(el("div", { class: "results-state", text: options.error }));
    } else {
      for (const result of results) nodes.push(renderResult(result));
      if (options.loading) {
        nodes.push(el("div", { class: "results-foot", text: "Refreshing…" }));
      }
    }

    box.replaceChildren(...nodes);
    applySuggestions();
  };

  const chipRow = (suggestions) =>
    el(
      "div",
      { class: "suggest-row" },
      suggestions.map((text) =>
        el("button", {
          class: "suggest-chip",
          type: "button",
          text,
          onclick: () => {
            $("searchInput").value = text;
            search(text);
          },
        })
      )
    );

  /* Suggestions can land before the results: remember them and re-apply the
     chips after every repaint, so they never disappear while typing. */
  const applySuggestions = () => {
    const box = $("searchResults");
    const value = $("searchInput").value.trim().toLowerCase();
    if (!box || box.hidden) return;
    const entry = app.suggestions;
    const existing = box.querySelector(".suggest-row");
    if (existing) existing.remove();
    if (!entry || !entry.items.length || entry.query !== value) return;
    box.prepend(chipRow(entry.items));
  };

  const renderSuggestions = (query, suggestions) => {
    app.suggestions = { query: query.trim().toLowerCase(), items: suggestions };
    applySuggestions();
  };

  const loadSuggestions = async (query) => {
    try {
      const response = await fetch(
        `/api/suggest?guild_id=${encodeURIComponent(app.guildId)}&q=${encodeURIComponent(query)}`,
        { credentials: "same-origin" }
      );
      if (!response.ok) return;
      const payload = await response.json().catch(() => ({}));
      const items = (payload.suggestions || [])
        .filter((text) => text.trim().toLowerCase() !== query.trim().toLowerCase())
        .slice(0, 5);
      renderSuggestions(query, items);
    } catch (_) {
      /* suggestions are a nicety, never a blocker */
    }
  };

  const search = async (rawQuery) => {
    const query = (rawQuery || "").trim();
    if (query.length < 2) {
      hideResults();
      return;
    }

    const box = $("searchResults");
    box.hidden = false;
    const cached = cachedSearch(query);
    paintResults(cached || [], { loading: !cached });

    if (app.searchAbort) app.searchAbort.abort();
    const controller = new AbortController();
    app.searchAbort = controller;
    const seq = ++app.searchSeq;

    loadSuggestions(query);

    try {
      const response = await fetch(
        `/api/search?guild_id=${encodeURIComponent(app.guildId)}&q=${encodeURIComponent(query)}`,
        { credentials: "same-origin", signal: controller.signal }
      );
      const payload = await response.json().catch(() => ({}));
      if (seq !== app.searchSeq) return; // a newer keystroke already won
      if (!response.ok) throw new Error(payload.message || "Search failed.");
      const results = payload.results || [];
      cacheSearch(query, results);
      paintResults(results, {});
    } catch (error) {
      if (error.name === "AbortError") return;
      if (seq !== app.searchSeq) return;
      paintResults(cached || [], { error: error.message || "Search failed." });
    }
  };

  const renderResult = (result) => {
    const add = async (position) => {
      hideResults();
      await run("play", { query: result.url, position });
    };
    return el(
      "div",
      { class: "result", onclick: () => add("end") },
      result.thumbnail
        ? el("img", { class: "result-art", src: thumbUrl(result.thumbnail), alt: "" })
        : el("div", { class: "result-art" }),
      el(
        "div",
        { class: "result-body" },
        el("div", { class: "result-title", text: result.title }),
        el(
          "div",
          { class: "result-meta" },
          result.platform
            ? el("span", {
                class: `platform platform-${String(result.platform).toLowerCase()}`,
                text: result.platform,
              })
            : null,
          [result.artist, result.duration ? fmtTime(result.duration) : ""]
            .filter(Boolean)
            .join(" · ")
        )
      ),
      el(
        "div",
        { class: "result-actions" },
        el("button", {
          class: "result-btn",
          type: "button",
          title: "Play next",
          "aria-label": "Play next",
          html: svg("chevron"),
          onclick: (event) => {
            event.stopPropagation();
            add("next");
          },
        }),
        el("button", {
          class: "result-btn",
          type: "button",
          title: "Add to queue",
          "aria-label": "Add to queue",
          html: svg("plus"),
          onclick: (event) => {
            event.stopPropagation();
            add("end");
          },
        })
      )
    );
  };

  /* ----------------------------------------------------------- progress loop */

  const tick = () => {
    if (app.playing && !app.draggingSeek) {
      const elapsed = (performance.now() - app.posAt) / 1000;
      let position = app.pos + elapsed;
      if (app.duration) position = Math.min(position, app.duration);
      const seek = $("seek");
      seek.value = String(Math.min(position, Number(seek.max)));
      setFill(seek, app.duration ? position / app.duration : 0);
      $("pos").textContent = fmtTime(position);
      if (!$("lyricsBox").hidden && $("lyricsBox").open) highlightLyrics(position);
    }
    requestAnimationFrame(tick);
  };

  /* ------------------------------------------------------------------ init */

  const init = () => {
    applyTheme(currentTheme());
    registerServiceWorker();
    setupEvents();
    requestAnimationFrame(tick);
    load();
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
