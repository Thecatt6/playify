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
    lastQueueKey: "",
  };

  /* ------------------------------------------------------------ formatting */

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

    const stored = Number(localStorage.getItem("playify.guild") || 0);
    const known = app.sessions.some((session) => Number(session.guild_id) === stored);
    app.guildId = known ? stored : Number(app.sessions[0].guild_id);
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
    app.sessions.find((session) => Number(session.guild_id) === Number(app.guildId)) ||
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
    if (snapshot.session) {
      const index = app.sessions.findIndex(
        (session) => Number(session.guild_id) === Number(snapshot.session.guild_id)
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
    if (current && current.thumbnail) {
      if (art.getAttribute("src") !== current.thumbnail) art.src = current.thumbnail;
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
      $("volumeValue").textContent = `${settings.volume ?? 100}%`;
      setFill(volume, (settings.volume ?? 100) / 200);
    }

    /* toggles */
    toggleChip("loop", settings.loop);
    toggleChip("autoplay", settings.autoplay);
    toggleChip("mode24_7", settings.mode_24_7);

    /* filters */
    renderFilters(snapshot.filters || [], settings.filters || []);

    /* server settings (only for people who can manage the server) */
    const capabilities = snapshot.capabilities || {};
    $("serverBox").hidden = !capabilities.is_manager;
    if (capabilities.is_manager) {
      toggleChip("kawaii", settings.kawaii);
      const defaultVolume = $("defaultVolume");
      if (!app.draggingDefaultVolume) {
        defaultVolume.value = String(settings.default_volume ?? 100);
        $("defaultVolumeValue").textContent = `${settings.default_volume ?? 100}%`;
        setFill(defaultVolume, (settings.default_volume ?? 100) / 200);
      }
    }

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
        ? el("img", { class: "queue-art", src: item.thumbnail, alt: "" })
        : el("div", { class: "queue-art is-empty", html: svg("music") });
      if (item.thumbnail) art.loading = "lazy";

      const metaParts = [item.artist, item.platform].filter(Boolean);

      list.append(
        el(
          "li",
          { class: "queue-item", dataset: { index: String(item.index) } },
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
        )
      );
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
          } else {
            gateForDenied(payload.message);
          }
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
        const resolve = app.pending.get(payload.request_id);
        if (resolve) {
          app.pending.delete(payload.request_id);
          resolve(payload);
        }
        if (payload.ok) {
          if (payload.message) toast(payload.message, "ok");
          (payload.notices || []).forEach((notice) => toast(notice, "ok"));
        } else if (payload.message) {
          toast(payload.message, "error");
        }
      }
    };

    socket.onclose = (event) => {
      app.ws = null;
      if (!app.wsWanted) return;
      if (event.code === 4003) {
        app.wsWanted = false;
        setConnection("offline", "Locked");
        gateForDenied(event.reason);
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
          body: JSON.stringify({ guild_id: Number(app.guildId), action, data }),
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
    if (result.ok === false) toast(result.message || "That action failed.", "error");
    else if (result.message && !options.silent) toast(result.message, "ok");
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
    run(action, {}, { button, silent: action === "shuffle" });
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

  const setupEvents = () => {
    $("playerCard").addEventListener("click", onControlClick);
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
      $("volumeValue").textContent = `${value}%`;
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
              "aria-current": Number(session.guild_id) === Number(app.guildId) ? "true" : "false",
              onclick: () => {
                popover.remove();
                selectGuild(Number(session.guild_id));
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
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
      if (event.key === "Escape") {
        hideResults();
        return;
      }
      if (typing) return;
      if (event.key === " " || event.code === "Space") {
        if (!app.snapshot) return;
        event.preventDefault();
        run("toggle", {}, { silent: true });
      } else if (event.key === "ArrowRight") {
        run("skip", {}, { silent: true });
      } else if (event.key === "ArrowLeft") {
        run("previous", {}, { silent: true });
      } else if (event.key === "/") {
        event.preventDefault();
        $("searchInput").focus();
      }
    });

    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible" && app.wsWanted && !app.ws) connect();
    });
  };

  const selectGuild = (guildId) => {
    if (Number(guildId) === Number(app.guildId)) return;
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
        ? el("img", { class: "result-art", src: result.thumbnail, alt: "" })
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
    }
    requestAnimationFrame(tick);
  };

  /* ------------------------------------------------------------------ init */

  const init = () => {
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
