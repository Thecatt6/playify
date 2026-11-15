/* jsdom smoke test for the Playify Web UI.

   Run it with `npm test` from this folder (needs `npm install` once).
   It loads the real index.html and app.js, stubs fetch and WebSocket, and
   walks the panel the way a listener would. */
const fs = require("fs");
const path = require("path");
const { JSDOM, VirtualConsole } = require("jsdom");

const STATIC = path.resolve(__dirname, "../../src/playify/webui/static");
const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
const appJs = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");

let passed = 0;
const failures = [];
const check = (name, condition, extra = "") => {
  if (condition) {
    passed += 1;
    console.log(`  ok   ${name}`);
  } else {
    failures.push(name);
    console.log(`  FAIL ${name}${extra ? " — " + extra : ""}`);
  }
};
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const SESSION = {
  guild_id: "1",
  guild_name: "Demo Server",
  guild_icon: null,
  channel_id: "2",
  channel_name: "wine & vinyl",
  listeners: [
    { id: "7", name: "Listener", avatar: "", bot: false },
    { id: "9", name: "Playify", avatar: "", bot: true },
  ],
  listener_count: 1,
  is_manager: true,
  connected: true,
};

const OTHER_SESSION = {
  ...SESSION,
  guild_id: "2",
  guild_name: "Second Server",
  channel_id: "3",
  channel_name: "lofi",
  listeners: [],
  is_manager: false,
};

const snapshot = (overrides = {}) => ({
  server_time: Date.now() / 1000,
  bot_uptime: 120,
  session: { ...SESSION, ...(overrides.session || {}) },
  playback: {
    state: "playing",
    is_live: false,
    position: 42,
    current: {
      title: "Midnight City",
      artist: "M83",
      duration: 244,
      thumbnail: "",
      requester: "Listener",
    },
    requester: "Listener",
  },
  queue: { count: 3, total_duration: 600, tracks: [] },
  history: { count: 0, previous: null },
  settings: {
    volume: 70,
    default_volume: 80,
    loop: false,
    autoplay: false,
    mode_24_7: false,
    kawaii: false,
    filters: [],
  },
  filters: [],
  diagnostics: { total: 0, warnings: 0, errors: 0, recent: [] },
  capabilities: { can_seek: true, can_skip: true, is_manager: true },
});

function makeFakeWebSocket(sockets) {
  return class FakeWebSocket {
    static OPEN = 1;
    static CLOSED = 3;
    constructor(url) {
      this.url = url;
      this.readyState = 1;
      this.sent = [];
      sockets.push(this);
      setTimeout(() => this.onopen && this.onopen({}), 5);
    }
    send(raw) {
      const payload = JSON.parse(raw);
      this.sent.push(payload);
      if (payload.type === "action") {
        setTimeout(
          () =>
            this.push({
              type: "result",
              request_id: payload.request_id,
              ok: true,
              message: "ok",
              notices: [],
            }),
          5
        );
      }
    }
    close() {
      this.readyState = 3;
      if (this.onclose) this.onclose({ code: 1000 });
    }
    push(payload) {
      if (this.onmessage) this.onmessage({ data: JSON.stringify(payload) });
    }
  };
}

function makeDom({ fetch: fetchImpl }) {
  const virtualConsole = new VirtualConsole();
  virtualConsole.on("jsdomError", (error) => console.log("  jsdom error:", error.message));
  const dom = new JSDOM(html, {
    url: "http://localhost/",
    runScripts: "outside-only",
    pretendToBeVisual: true,
    virtualConsole,
  });
  dom.window.fetch = fetchImpl;
  dom.window.eval(appJs);
  return dom;
}

const jsonResponse = (body, status = 200) => ({
  ok: status < 400,
  status,
  json: async () => body,
});

/* ------------------------------------------------------------------ main --- */

async function mainScenario() {
  const calls = [];
  const sockets = [];
  let searchDelays = [];
  let sessionState = snapshot();

  const fetchStub = async (url) => {
    calls.push(String(url));
    if (url.startsWith("/api/config")) return jsonResponse({ oauth: true, dev_bypass: false });
    if (url.startsWith("/api/me")) {
      return jsonResponse({
        ok: true,
        user: { id: "7", name: "Listener", avatar: "", dev: false },
        bot: { name: "Playify", avatar: "" },
        sessions: [SESSION],
        connected_channels: [
          { guild_id: "1", guild_name: "Demo Server", channel_name: "wine & vinyl", connected: true },
        ],
        login: { oauth: true, dev_bypass: false },
      });
    }
    if (url.startsWith("/api/state")) return jsonResponse({ ok: true, state: sessionState });
    if (url.startsWith("/api/suggest")) {
      return jsonResponse({ ok: true, suggestions: ["midnight city remix", "midnight city live"] });
    }
    if (url.startsWith("/api/search")) {
      const delay = searchDelays.length ? searchDelays.shift() : 40;
      const query = new URL(url, "http://localhost").searchParams.get("q") || "";
      await wait(delay);
      return jsonResponse({
        ok: true,
        results: [
          { title: `YT ${query}`, artist: "Artist", duration: 210, thumbnail: "", url: "yt://1", platform: "YouTube" },
          { title: `SP ${query}`, artist: "Artist", duration: 190, thumbnail: "", url: "sp://1", platform: "Spotify" },
        ],
      });
    }
    if (url.startsWith("/api/action")) return jsonResponse({ ok: true, message: "done" });
    return jsonResponse({});
  };

  const dom = makeDom({ fetch: fetchStub });
  dom.window.WebSocket = makeFakeWebSocket(sockets);
  const { window } = dom;
  const $ = (id) => window.document.getElementById(id);

  await wait(120);

  console.log("\n[boot]");
  check("app is visible after login", $("app").hidden === false);
  check("gate stays hidden", $("gate").hidden === true);
  check("websocket opened for the guild", sockets.length === 1 && sockets[0].url.includes("guild_id=1"));

  sockets[0].push({ type: "state", data: snapshot() });
  await wait(60);
  check("now playing title rendered", $("trackTitle").textContent === "Midnight City");

  console.log("\n[one notification per action]");
  const actionsBefore = (sockets[0].sent || []).filter((payload) => payload.type === "action").length;
  $("playPause").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await wait(90);
  const actionsAfter = (sockets[0].sent || []).filter((payload) => payload.type === "action").length;
  check("a control sends exactly one action", actionsAfter === actionsBefore + 1, `${actionsBefore} -> ${actionsAfter}`);
  check("the action shows one notification, not two", $("toasts").children.length === 1, String($("toasts").children.length));
  check(
    "the notification carries the result message",
    ($("toasts").textContent || "").trim() === "ok",
    $("toasts").textContent
  );
  $("toasts").replaceChildren();
  check("the volume readout is plain when audible", $("volumeValue").textContent === "70%", $("volumeValue").textContent);
  sockets[0].push({ type: "state", data: { ...snapshot(), settings: { ...snapshot().settings, volume: 0 } } });
  await wait(60);
  check(
    "a muted volume is called out in the readout",
    /muted/i.test($("volumeValue").textContent) && $("volumeValue").classList.contains("is-muted"),
    $("volumeValue").textContent
  );
  sockets[0].push({ type: "state", data: snapshot() });
  await wait(60);

  console.log("\n[theme]");
  const root = window.document.documentElement;
  check("the panel starts in a known theme", ["dark", "light"].includes(root.dataset.theme), root.dataset.theme);
  $("themeToggle").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  check("the toggle switches to the other theme", root.dataset.theme === "light", root.dataset.theme);
  check("the choice is persisted", window.localStorage.getItem("playify.theme") === "light");
  $("themeToggle").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  check("toggling twice comes back", root.dataset.theme === "dark", root.dataset.theme);

  console.log("\n[shortcuts]");
  const press = (key, options = {}) =>
    window.document.dispatchEvent(
      new window.KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...options })
    );
  const lastAction = () =>
    (sockets[0].sent || []).filter((payload) => payload.type === "action").slice(-1)[0] || {};

  press("?");
  check("? opens the shortcut sheet", $("shortcuts").hidden === false);
  press("Escape");
  check("escape closes the shortcut sheet", $("shortcuts").hidden === true);

  press("m");
  await wait(60);
  check(
    "m mutes the player",
    lastAction().action === "volume" && lastAction().data && lastAction().data.volume === 0,
    JSON.stringify(lastAction())
  );
  sockets[0].push({ type: "state", data: { ...snapshot(), settings: { ...snapshot().settings, volume: 0 } } });
  await wait(60);
  press("m");
  await wait(60);
  check(
    "m unmutes back to the previous level",
    lastAction().action === "volume" && lastAction().data.volume === 70,
    JSON.stringify(lastAction())
  );
  $("toasts").replaceChildren();
  sockets[0].push({ type: "state", data: snapshot() });
  await wait(60);

  press("ArrowRight", { shiftKey: true });
  await wait(60);
  check(
    "shift+arrow seeks forward",
    lastAction().action === "seek" && lastAction().data.position >= 45,
    JSON.stringify(lastAction())
  );

  console.log("\n[diagnostics]");
  check("the card is hidden when everything is fine", $("diagnosticsBox").hidden);

  const problems = {
    ...snapshot(),
    diagnostics: {
      total: 3,
      warnings: 2,
      errors: 1,
      recent: [
        { time: Date.now() / 1000 - 5, level: "error", logger: "playify.voice", message: "Reconnect failed" },
        { time: Date.now() / 1000 - 90, level: "warning", logger: "playify.spotify", message: "Token expired" },
      ],
    },
  };
  sockets[0].push({ type: "state", data: problems });
  await wait(80);
  check("the card appears for a manager when something failed", !$("diagnosticsBox").hidden);
  check(
    "the summary counts errors and warnings",
    $("diagnosticsSummary").textContent === "1 error, 2 warnings",
    $("diagnosticsSummary").textContent
  );
  const items = $("diagnosticsList").querySelectorAll(".diagnostics-item");
  check("both recent entries are listed", items.length === 2, String(items.length));
  check(
    "the newest entry comes first with its message",
    items[0].dataset.level === "error" && items[0].textContent.includes("Reconnect failed"),
    items[0].textContent
  );
  check(
    "a recent entry says just now",
    items[0].querySelector(".diagnostics-when").textContent === "just now",
    items[0].querySelector(".diagnostics-when").textContent
  );
  check(
    "an older entry shows how long ago it happened",
    items[1].querySelector(".diagnostics-when").textContent === "1:30 ago",
    items[1].querySelector(".diagnostics-when").textContent
  );

  sockets[0].push({ type: "state", data: { ...problems, capabilities: { ...problems.capabilities, is_manager: false } } });
  await wait(60);
  check("a plain listener does not see the internals", $("diagnosticsBox").hidden);

  sockets[0].push({ type: "state", data: snapshot() });
  await wait(60);

  console.log("\n[the clock]");
  const idle = { ...snapshot(), playback: { ...snapshot().playback, state: "idle", current: null, position: 0 } };
  sockets[0].push({ type: "state", data: idle });
  await wait(120);
  const frozen = $("pos").textContent;
  await wait(220);
  check("a finished track shows 0:00", frozen === "0:00", frozen);
  check("the timer does not run when nothing plays", $("pos").textContent === frozen, $("pos").textContent);

  const nearlyOver = {
    ...snapshot(),
    playback: { ...snapshot().playback, state: "playing", position: 119.5 },
    settings: snapshot().settings,
  };
  nearlyOver.playback.current = { ...snapshot().playback.current, duration: 120 };
  sockets[0].push({ type: "state", data: nearlyOver });
  await wait(80);
  await wait(900);
  check(
    "the timer stops at the end of the track",
    $("pos").textContent === "2:00",
    $("pos").textContent
  );

  sockets[0].push({ type: "state", data: snapshot() });
  await wait(60);

  console.log("\n[queue drag and drop]");
  const queueSnapshot = (titles) => ({
    ...snapshot(),
    queue: {
      count: titles.length,
      total_duration: 300,
      items: titles.map((title, index) => ({
        index,
        title,
        artist: "Artist",
        duration: 100,
        thumbnail: "",
        platform: "YouTube",
        url: `yt://${index}`,
      })),
    },
  });
  sockets[0].push({ type: "state", data: queueSnapshot(["One", "Two", "Three"]) });
  await wait(80);
  const rows = [...$("queueList").querySelectorAll(".queue-item")];
  check("the queue renders the tracks", rows.length === 3, String(rows.length));
  rows[0].dispatchEvent(new window.Event("dragstart", { bubbles: true }));
  await wait(20);
  rows[2].dispatchEvent(new window.Event("dragover", { bubbles: true, cancelable: true }));
  rows[2].dispatchEvent(new window.Event("drop", { bubbles: true, cancelable: true }));
  await wait(80);
  check(
    "dropping a track sends a move action",
    lastAction().action === "move" && lastAction().data.from === 0 && lastAction().data.to === 2,
    JSON.stringify(lastAction())
  );
  check(
    "the drop target is highlighted while dragging",
    $("queueList").querySelectorAll(".queue-item.is-drop-target").length === 0
  );

  console.log("\n[connection banner]");
  check("banner hidden while healthy", $("connBar").hidden === true);
  sockets[0].push({ type: "state", data: snapshot({ session: { connected: false } }) });
  await wait(60);
  check("banner shows a dropped connection", $("connBar").hidden === false);
  check(
    "banner explains what happened",
    /dropped/i.test($("connBarText").textContent) && $("connBarText").textContent.includes("wine & vinyl")
  );
  $("connBarAction").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await wait(80);
  check(
    "reconnect button triggers the reconnect action",
    (sockets[0].sent || []).some((payload) => payload.type === "action" && payload.action === "reconnect")
  );
  sockets[0].push({ type: "state", data: snapshot() });
  await wait(60);
  check("banner hides again once reconnected", $("connBar").hidden === true);

  console.log("\n[search]");
  const input = $("searchInput");
  input.value = "mid";
  input.dispatchEvent(new window.Event("input", { bubbles: true }));
  await wait(120);
  check(
    "suggestions are requested right away",
    calls.some((url) => url.startsWith("/api/suggest") && url.includes("q=mid"))
  );
  await wait(220);
  check("search fires for the query", calls.some((url) => url.startsWith("/api/search") && url.includes("q=mid")));
  check("suggestion chips rendered", $("searchResults").querySelectorAll(".suggest-chip").length === 2);
  check("results rendered", $("searchResults").querySelectorAll(".result").length === 2);
  const pills = [...$("searchResults").querySelectorAll(".platform")].map((node) => node.textContent);
  check("both platforms are shown", pills.includes("YouTube") && pills.includes("Spotify"), pills.join(","));

  console.log("\n[cache + stale responses]");
  const before = calls.filter((url) => url.startsWith("/api/search")).length;
  input.value = "midnight";
  searchDelays = [180, 20];
  input.dispatchEvent(new window.Event("input", { bubbles: true }));
  await wait(300);
  check("a new query triggers a new request", calls.filter((url) => url.startsWith("/api/search")).length > before);
  await wait(400);
  const titles = [...$("searchResults").querySelectorAll(".result-title")].map((n) => n.textContent);
  check("the newest query wins over a stale response", titles.every((t) => t.includes("midnight")), titles.join(" | "));

  console.log("\n[denied socket, no alternative server]");
  sockets[0].readyState = 3;
  sockets[0].onclose({
    code: 4003,
    reason:
      "Playify is not in a voice channel in this server right now \u2014 it is playing in #lofi in Other Server. Join that channel there to use the panel, or run /play in a voice channel here to start playing in this server.",
  });
  await wait(150);
  check("gate is shown on a denied socket", $("gate").hidden === false);
  check("app is hidden while denied", $("app").hidden === true);
  check(
    "the server explanation is displayed",
    $("gateText").textContent.includes("Other Server") && $("gateText").textContent.includes("/play"),
    $("gateText").textContent
  );
  check("no pointless switch when no other session exists", sockets.length === 1);

  console.log("\n[hiding]");
  input.value = "";
  input.dispatchEvent(new window.Event("input", { bubbles: true }));
  await wait(80);
  check("clearing the field hides the panel", $("searchResults").hidden === true);

  window.close();
}

/* ------------------------------------------------ switching to another guild */

async function switchScenario() {
  const sockets = [];
  const calls = [];

  const fetchStub = async (url) => {
    calls.push(String(url));
    if (url.startsWith("/api/config")) return jsonResponse({ oauth: true, dev_bypass: false });
    if (url.startsWith("/api/me")) {
      return jsonResponse({
        ok: true,
        user: { id: "7", name: "Listener", avatar: "", dev: false },
        bot: { name: "Playify", avatar: "" },
        sessions: [SESSION, OTHER_SESSION],
        connected_channels: [
          { guild_id: "2", guild_name: "Second Server", channel_name: "lofi", connected: true },
        ],
        login: { oauth: true, dev_bypass: false },
      });
    }
    if (url.startsWith("/api/state")) return jsonResponse({ ok: true, state: snapshot() });
    return jsonResponse({});
  };

  const dom = makeDom({ fetch: fetchStub });
  dom.window.WebSocket = makeFakeWebSocket(sockets);
  const { window } = dom;
  const $ = (id) => window.document.getElementById(id);

  await wait(120);
  check("initial socket targets the stored guild", sockets[0].url.includes("guild_id=1"));

  sockets[0].readyState = 3;
  sockets[0].onclose({
    code: 4003,
    reason:
      "Playify is not in a voice channel in this server right now \u2014 it is playing in #lofi in Second Server.",
  });
  await wait(250);

  check("an alternative session triggers a re-check", calls.some((url) => url.startsWith("/api/me")));
  check("a new socket is opened", sockets.length > 1);
  check("the socket targets the other guild", sockets[sockets.length - 1].url.includes("guild_id=2"), sockets[sockets.length - 1].url);
  check("the gate is dismissed after switching", $("gate").hidden === true);
  check("the app is visible after switching", $("app").hidden === false);

  // 19-digit snowflake: the page must keep it as a string, never a Number.
  const bigId = "1137104646637359100";
  const lastSocket = sockets[sockets.length - 1];
  lastSocket.push({ type: "state", data: { ...snapshot(), session: { ...SESSION, guild_id: bigId } } });
  await wait(60);
  check(
    "a 19-digit guild id is stored without rounding",
    window.localStorage.getItem("playify.guild") === bigId,
    window.localStorage.getItem("playify.guild")
  );

  window.close();
}

(async () => {
  await mainScenario();
  console.log("\n[auto switch to a server where the bot plays]");
  await switchScenario();

  console.log(`\n${passed}/${passed + failures.length} checks passed`);
  if (failures.length) {
    console.log("failed:", failures.join(", "));
    process.exitCode = 1;
  }
})();
