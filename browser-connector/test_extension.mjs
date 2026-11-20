// The extension's worker, driven in a browser double.
//
// Loads the real extension/background.js against a fake browser API (tabs, tab groups, windows,
// storage) and a fake loopback gateway and debugger, then checks what the worker does: which page
// it announces, which tab each contract verb touches, and what it reports when a run's tab closes
// or is taken over. `test_extension.py` runs this file with Node's own test runner and holds each
// case below to a pass by name. No npm install and no browser.

import assert from "node:assert/strict";
import { test } from "node:test";

const EXTENSION = new URL("./extension/", import.meta.url);
const DEBUG_PORT = 9222;
// What the gateway hands a connector that pairs, and the code the owner made for it.
const TOKEN = "connector-session";
const PAIR = "connector-pair";
const CODE = "ABCD-EFGH";
// This extension's own id, as the browser gives its pages' messages.
const EXTENSION_ID = "personalclaw-browser-connector";
let loads = 0;

function event() {
  const listeners = [];
  return {
    addListener(fn) {
      listeners.push(fn);
    },
    // What each listener returned: a message listener's `true` keeps its answer coming.
    async fire(...args) {
      const returned = [];
      for (const fn of listeners) returned.push(await fn(...args));
      return returned;
    },
    clear() {
      listeners.length = 0;
    },
  };
}

function storageArea() {
  const data = {};
  return {
    data,
    async get(defaults) {
      const out = {};
      for (const [key, value] of Object.entries(defaults || {})) {
        out[key] = key in data ? structuredClone(data[key]) : value;
      }
      return out;
    },
    async set(values) {
      Object.assign(data, structuredClone(values));
    },
  };
}

// A browser with the owner's own window open: two tabs, the second focused. Every call that acts
// on a tab is recorded in `touched`.
function makeBrowser({ tabGroups = true } = {}) {
  let nextTab = 1;
  let nextWindow = 1;
  let nextGroup = 1;
  const tabs = new Map();
  const groups = new Map();
  const windows = new Map();
  const touched = [];
  const storage = { local: storageArea(), session: storageArea() };

  function addWindow(focused) {
    const win = { id: nextWindow++, focused };
    windows.set(win.id, win);
    return win;
  }
  function addTab(windowId, url, active) {
    const tab = { id: nextTab++, windowId, url, active, groupId: -1 };
    tabs.set(tab.id, tab);
    return tab;
  }

  const ownerWindow = addWindow(true);
  const ownerFirst = addTab(ownerWindow.id, "https://mail.example.com/inbox", false);
  const ownerFocused = addTab(ownerWindow.id, "https://bank.example.com/accounts", true);
  const ownerTabs = new Set([ownerFirst.id, ownerFocused.id]);
  // Set to make the debugger list only the owner's own tabs, never one opened since.
  const debuggerOptions = { listsNewTabs: true };

  const api = {
    runtime: { id: EXTENSION_ID, onMessage: event() },
    storage,
    tabs: {
      onActivated: event(),
      onRemoved: event(),
      async create({ url, active }) {
        const tab = addTab(ownerWindow.id, url, Boolean(active));
        touched.push({ op: "create", tabId: tab.id, active: Boolean(active) });
        return { ...tab };
      },
      async query(queryInfo) {
        touched.push({ op: "query", queryInfo });
        return [...tabs.values()].filter((t) => t.active).map((t) => ({ ...t }));
      },
      async update(tabId, props) {
        touched.push({ op: "update", tabId, props });
        const tab = tabs.get(tabId);
        if (!tab) throw new Error(`No tab with id: ${tabId}`);
        if (props.url) tab.url = props.url;
        return { ...tab };
      },
      async remove(tabId) {
        touched.push({ op: "remove", tabId });
        await removeTab(tabId);
      },
      async sendMessage(tabId, message) {
        touched.push({ op: "sendMessage", tabId, message });
        return { ok: true };
      },
    },
    windows: {
      onRemoved: event(),
      onFocusChanged: event(),
      async create({ url, focused }) {
        const win = addWindow(Boolean(focused));
        const tab = addTab(win.id, url, true);
        touched.push({ op: "createWindow", windowId: win.id, focused: Boolean(focused) });
        return { id: win.id, focused: win.focused, tabs: [{ ...tab }] };
      },
      async remove(windowId) {
        touched.push({ op: "removeWindow", windowId });
        await closeWindow(windowId);
      },
    },
  };
  if (tabGroups) {
    api.tabs.group = async ({ tabIds }) => {
      const group = { id: nextGroup++, title: "", windowId: tabs.get(tabIds[0]).windowId };
      groups.set(group.id, group);
      for (const id of tabIds) tabs.get(id).groupId = group.id;
      touched.push({ op: "group", tabIds, groupId: group.id });
      return group.id;
    };
    api.tabGroups = {
      onRemoved: event(),
      async update(groupId, props) {
        const group = groups.get(groupId);
        Object.assign(group, props);
        return { ...group };
      },
    };
  }

  // What the browser does when a tab goes: the tab, then a group left empty.
  async function removeTab(tabId) {
    const tab = tabs.get(tabId);
    if (!tab) throw new Error(`No tab with id: ${tabId}`);
    tabs.delete(tabId);
    await api.tabs.onRemoved.fire(tabId, { windowId: tab.windowId, isWindowClosing: false });
    if (tab.groupId !== -1 && ![...tabs.values()].some((t) => t.groupId === tab.groupId)) {
      const group = groups.get(tab.groupId);
      groups.delete(tab.groupId);
      await api.tabGroups.onRemoved.fire({ ...group });
    }
  }

  async function closeGroup(groupId) {
    for (const tab of [...tabs.values()]) if (tab.groupId === groupId) await removeTab(tab.id);
  }

  // The owner closing one tab by hand.
  async function closeTab(tabId) {
    await removeTab(tabId);
  }

  // The browser stopping the worker: every listener it added goes with it.
  function dropListeners() {
    for (const area of [api.tabs, api.windows, api.tabGroups || {}, api.runtime]) {
      for (const value of Object.values(area)) if (value && value.clear) value.clear();
    }
  }

  async function closeWindow(windowId) {
    for (const tab of [...tabs.values()]) {
      if (tab.windowId === windowId) {
        tabs.delete(tab.id);
        await api.tabs.onRemoved.fire(tab.id, { windowId, isWindowClosing: true });
      }
    }
    windows.delete(windowId);
    await api.windows.onRemoved.fire(windowId);
  }

  async function focusTab(tabId) {
    const tab = tabs.get(tabId);
    for (const other of tabs.values()) if (other.windowId === tab.windowId) other.active = false;
    tab.active = true;
    await api.tabs.onActivated.fire({ tabId, windowId: tab.windowId });
  }

  async function focusWindow(windowId) {
    for (const win of windows.values()) win.focused = win.id === windowId;
    await api.windows.onFocusChanged.fire(windowId);
  }

  function pageUrl(tabId) {
    return `ws://127.0.0.1:${DEBUG_PORT}/devtools/page/page-${tabId}`;
  }

  // The loopback debugger's page list, the owner's tabs first.
  function debuggerTargets() {
    const listed = [...tabs.values()].filter(
      (t) => debuggerOptions.listsNewTabs || ownerTabs.has(t.id),
    );
    return listed.map((t) => ({
      id: `page-${t.id}`,
      type: "page",
      title: "",
      url: t.url,
      webSocketDebuggerUrl: pageUrl(t.id),
    }));
  }

  return {
    api,
    tabs,
    groups,
    windows,
    touched,
    ownerFirst,
    ownerFocused,
    debuggerOptions,
    closeGroup,
    closeTab,
    closeWindow,
    dropListeners,
    focusTab,
    focusWindow,
    pageUrl,
    debuggerTargets,
  };
}

function reply(body, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

// The gateway's pairing and connector routes, and the browser's own debugger list, on loopback.
// The connector routes take only the connector's own session: its token as a Bearer, with its pair.
function makeGateway(browser) {
  const requests = [];
  let runs = [];
  let attached = true;
  let attachedBy = "workstation";
  // What the tabs route answers a browser that is not the connector: null, or [status, code].
  let refusal = null;
  const codes = new Set([CODE]);
  // The session the gateway takes, or "" once the device is signed out.
  let session = TOKEN;

  function signedIn(init) {
    const headers = init.headers || {};
    return (
      Boolean(session) &&
      headers.Authorization === `Bearer ${session}` &&
      headers["X-PersonalClaw-Pair"] === PAIR
    );
  }

  async function fetch(url, init = {}) {
    const u = new URL(url);
    const method = (init.method || "GET").toUpperCase();
    if (u.port === String(DEBUG_PORT)) {
      if (u.pathname === "/json" || u.pathname === "/json/list") {
        return reply(browser.debuggerTargets());
      }
      return reply({}, 404);
    }
    const body = init.body === undefined ? undefined : JSON.parse(init.body);
    requests.push({
      method,
      path: u.pathname,
      body,
      headers: init.headers || {},
      credentials: init.credentials,
    });
    if (u.pathname === "/api/devices/pair/complete" && method === "POST") {
      if (!codes.has(body && body.code)) {
        const message = "The pairing code did not verify.";
        return reply({ error: { code: "device_pair_code_invalid", message } }, 401);
      }
      codes.delete(body.code);
      session = TOKEN;
      return reply({ ok: true, device_id: "workstation", kind: "connector", pair: PAIR, token: TOKEN });
    }
    if (u.pathname.startsWith("/api/browse/connector") && !signedIn(init)) {
      return reply({ error: { code: "auth_required" } }, 401);
    }
    if (u.pathname === "/api/browse/connector" && method === "GET") {
      return reply(
        attached
          ? { connected: true, device_id: attachedBy, reason: "", fix: "" }
          : {
              connected: false,
              device_id: "",
              reason: "No browser is attached.",
              fix: "Attach one from its extension.",
            },
      );
    }
    if (u.pathname === "/api/browse/connector") {
      attached = method !== "DELETE";
      attachedBy = "workstation";
      refusal = null;
      return reply({ ok: true, device_id: "workstation" });
    }
    if (u.pathname === "/api/browse/connector/tabs") {
      if (refusal) return reply({ error: { code: refusal[1] } }, refusal[0]);
      return attached
        ? reply({ runs })
        : reply({ error: { code: "browse_connector_not_attached" } }, 409);
    }
    const match = u.pathname.match(/^\/api\/browse\/connector\/tabs\/([^/]+)$/);
    if (match && method === "POST") {
      const run = runs.find((r) => r.request_id === match[1]);
      if (!run) return reply({ error: { code: "unknown" } }, 404);
      run.state = body.cdp_url ? "open" : body.state;
      return reply({ ok: true });
    }
    return reply({}, 404);
  }

  return {
    fetch,
    requests,
    ask(runId, group) {
      runs.push({ request_id: runId, group, state: "requested" });
    },
    end(runId) {
      runs = runs.filter((r) => r.request_id !== runId);
    },
    restart() {
      attached = false;
      runs = [];
    },
    // Another browser attached in this one's place.
    replace() {
      refusal = [409, "browse_connector_replaced"];
      attachedBy = "another-browser";
    },
    // The device's session ended: it expired, or the owner signed it out.
    signOut() {
      session = "";
    },
    // The owner revoked this browser's pairing.
    revoke() {
      refusal = [403, "browse_connector_unpaired"];
    },
    // What the worker posted on one run's tab route, in order.
    reports(runId) {
      return requests
        .filter((r) => r.method === "POST" && r.path === `/api/browse/connector/tabs/${runId}`)
        .map((r) => r.body);
    },
    // Every page target the worker announced, on any route.
    announced() {
      return requests
        .filter((r) => r.body && r.body.cdp_url !== undefined)
        .map((r) => ({ path: r.path, cdpUrl: r.body.cdp_url }));
    },
  };
}

// A browser and its gateway. The connector has paired already unless `paired: false`.
function setup({ paired = true, ...options } = {}) {
  const browser = makeBrowser(options);
  if (paired) Object.assign(browser.api.storage.local.data, { token: TOKEN, pair: PAIR, deviceId: "workstation" });
  return { browser, gateway: makeGateway(browser) };
}

// The gateway calls the worker made, without the debugger's.
function gatewayCalls(env) {
  return env.gateway.requests;
}

// A fresh worker, as the browser starts it: the module's top level runs again.
async function startWorker({ browser, gateway }) {
  globalThis.chrome = browser.api;
  delete globalThis.browser;
  globalThis.fetch = gateway.fetch;
  loads += 1;
  return import(new URL(`background.js?worker=${loads}`, EXTENSION).href);
}

function created(browser) {
  return browser.touched.filter((c) => c.op === "create");
}

const TAB_ACTIONS = new Set(["update", "remove", "sendMessage"]);

function tabActions(browser) {
  return browser.touched.filter((c) => TAB_ACTIONS.has(c.op));
}

// Attach, then let one run ask for a tab and the worker answer it. Returns the run's tab.
async function grantRun(env, worker, runId, group) {
  await worker.attach();
  env.gateway.ask(runId, group);
  await worker.pollOnce();
  const [own] = created(env.browser);
  assert.ok(own, "the worker opened no tab for the run");
  return env.browser.tabs.get(own.tabId);
}

test("the attach announces the run's own tab, never the first listed page", async () => {
  const env = setup();
  const worker = await startWorker(env);
  await worker.attach();
  assert.deepEqual(env.gateway.announced(), [], "attaching announced a page the run never opened");

  env.gateway.ask("run-a1", "Check the lamp prices");
  await worker.pollOnce();
  const opened = created(env.browser);
  assert.equal(opened.length, 1, "the run opened exactly one tab of its own");
  const runTab = opened[0].tabId;
  assert.notEqual(runTab, env.browser.ownerFirst.id);
  assert.notEqual(runTab, env.browser.ownerFocused.id);
  assert.equal(env.browser.debuggerTargets()[0].webSocketDebuggerUrl, env.browser.pageUrl(1));
  assert.deepEqual(env.gateway.announced(), [
    { path: "/api/browse/connector/tabs/run-a1", cdpUrl: env.browser.pageUrl(runTab) },
  ]);
});

test("a granted run's tab opens in the background, in a group named after the task", async () => {
  const env = setup();
  const worker = await startWorker(env);
  const runTab = await grantRun(env, worker, "run-a1", "Check the lamp prices");
  assert.equal(created(env.browser)[0].active, false, "the run's tab took focus when it opened");
  assert.ok(env.browser.tabs.get(env.browser.ownerFocused.id).active, "the owner's tab lost focus");
  assert.notEqual(runTab.groupId, -1, "the run's tab is in no group");
  assert.equal(env.browser.groups.get(runTab.groupId).title, "Check the lamp prices");
  assert.equal(env.browser.ownerFirst.groupId, -1);
  assert.equal(env.browser.ownerFocused.groupId, -1);
});

test("every verb acts on the run's tab while another tab has focus", async () => {
  const env = setup();
  const worker = await startWorker(env);
  const verbs = [
    { method: "navigate", params: { url: "https://shop.example.com/lamps" } },
    { method: "read-outline", params: {} },
    { method: "click", params: { ref: "e1" } },
    { method: "type", params: { ref: "e2", value: "brass lamp" } },
    { method: "close", params: {} },
  ];
  assert.deepEqual(verbs.map((v) => v.method), [...worker.CONTRACT_METHODS]);

  // Before the run has a tab of its own, a verb for it touches no tab at all.
  const early = await worker.handleContractRequest({ run: "run-a1", ...verbs[0] }).then(
    () => "acted",
    () => "refused",
  );
  assert.deepEqual(tabActions(env.browser), [], "a verb for a run with no tab acted on a tab");
  assert.equal(early, "refused");

  const runTab = await grantRun(env, worker, "run-a1", "Check the lamp prices");
  const focused = env.browser.ownerFocused.id;
  env.browser.touched.length = 0;
  for (const verb of verbs) {
    assert.ok(env.browser.tabs.get(focused).active, "the owner's tab must hold focus throughout");
    await worker.handleContractRequest({ run: "run-a1", ...verb });
  }
  const acted = tabActions(env.browser);
  assert.equal(acted.length, verbs.length);
  for (const call of acted) {
    assert.equal(call.tabId, runTab.id, `${call.op} acted on tab ${call.tabId}, not the run's`);
  }
  assert.ok(!env.browser.touched.some((c) => c.op === "query"), "a verb looked up the focused tab");
});

test("a verb for another run, or for no run, is refused", async () => {
  const env = setup();
  const worker = await startWorker(env);
  await grantRun(env, worker, "run-a1", "Check the lamp prices");
  env.browser.touched.length = 0;
  const read = { method: "read-outline", params: {} };
  await assert.rejects(worker.handleContractRequest({ run: "run-b2", ...read }));
  await assert.rejects(worker.handleContractRequest(read));
  await assert.rejects(worker.handleContractRequest({ run: "run-a1", method: "eval", params: {} }));
  assert.deepEqual(tabActions(env.browser), []);
});

test("closing the group ends the run", async () => {
  const env = setup();
  const worker = await startWorker(env);
  const runTab = await grantRun(env, worker, "run-a1", "Check the lamp prices");
  await env.browser.closeGroup(runTab.groupId);
  assert.deepEqual(env.gateway.reports("run-a1"), [
    { cdp_url: env.browser.pageUrl(runTab.id) },
    { state: "closed" },
  ]);
  await assert.rejects(
    worker.handleContractRequest({ run: "run-a1", method: "read-outline", params: {} }),
  );
  assert.ok(env.browser.tabs.has(env.browser.ownerFocused.id), "the owner's tabs stay open");
});

test("closing just the run's tab ends the run too", async () => {
  const env = setup();
  const worker = await startWorker(env);
  const runTab = await grantRun(env, worker, "run-a1", "Check the lamp prices");
  await env.browser.closeTab(runTab.id);
  assert.deepEqual(env.gateway.reports("run-a1").at(-1), { state: "closed" });
  assert.equal(env.gateway.reports("run-a1").length, 2, "the end was reported more than once");
});

test("bringing the run's tab to the front is a take-over", async () => {
  const env = setup();
  const worker = await startWorker(env);
  const runTab = await grantRun(env, worker, "run-a1", "Check the lamp prices");
  await env.browser.focusTab(env.browser.ownerFirst.id);
  assert.equal(env.gateway.reports("run-a1").length, 1, "focusing the owner's own tab paused the run");
  await env.browser.focusTab(runTab.id);
  assert.deepEqual(env.gateway.reports("run-a1").at(-1), { state: "taken_over" });
});

test("a browser with no tab groups uses a window of its own", async () => {
  const env = setup({ tabGroups: false });
  const worker = await startWorker(env);
  await worker.attach();
  env.gateway.ask("run-a1", "Check the lamp prices");
  await worker.pollOnce();

  assert.deepEqual(created(env.browser), [], "the run put a tab in the owner's window");
  const windows = env.browser.touched.filter((c) => c.op === "createWindow");
  assert.equal(windows.length, 1, "the run opened no window of its own");
  assert.equal(windows[0].focused, false, "the run's window took focus when it opened");
  const runWindow = windows[0].windowId;
  const runTab = [...env.browser.tabs.values()].find((t) => t.windowId === runWindow);
  assert.deepEqual(env.gateway.announced(), [
    { path: "/api/browse/connector/tabs/run-a1", cdpUrl: env.browser.pageUrl(runTab.id) },
  ]);

  await env.browser.focusWindow(runWindow);
  assert.deepEqual(env.gateway.reports("run-a1").at(-1), { state: "taken_over" });
  await env.browser.closeWindow(runWindow);
  assert.deepEqual(env.gateway.reports("run-a1").at(-1), { state: "closed" });
});

test("a run whose tab cannot be opened is refused, never moved to an open tab", async () => {
  const env = setup();
  const worker = await startWorker(env);
  env.browser.api.tabs.create = async () => {
    throw new Error("tabs cannot be created in this window");
  };
  await worker.attach();
  env.gateway.ask("run-a1", "Check the lamp prices");
  await worker.pollOnce();
  assert.deepEqual(env.gateway.reports("run-a1"), [{ state: "unavailable" }]);
  assert.deepEqual(env.gateway.announced(), []);
  assert.deepEqual(tabActions(env.browser), [], "the run acted on a tab it did not open");
});

test("a tab the debugger never lists is closed again and the run refused", async () => {
  const env = setup();
  const worker = await startWorker(env);
  const realSetTimeout = globalThis.setTimeout;
  globalThis.setTimeout = (fn) => realSetTimeout(fn, 0);
  try {
    env.browser.debuggerOptions.listsNewTabs = false;
    await worker.attach();
    env.gateway.ask("run-a1", "Check the lamp prices");
    await worker.pollOnce();
  } finally {
    globalThis.setTimeout = realSetTimeout;
  }
  const [own] = created(env.browser);
  assert.ok(own, "the worker never tried a tab of the run's own");
  assert.equal(env.browser.tabs.has(own.tabId), false, "the half-opened tab was left behind");
  assert.deepEqual(env.gateway.reports("run-a1"), [{ state: "unavailable" }]);
  assert.deepEqual(env.gateway.announced(), []);
});

test("a run that ended is forgotten, and its tab is left to the owner", async () => {
  const env = setup();
  const worker = await startWorker(env);
  const runTab = await grantRun(env, worker, "run-a1", "Check the lamp prices");
  env.gateway.end("run-a1");
  await worker.pollOnce();
  await assert.rejects(
    worker.handleContractRequest({ run: "run-a1", method: "read-outline", params: {} }),
  );
  assert.ok(env.browser.tabs.has(runTab.id), "the ended run's tab was closed on the owner");
  await env.browser.focusTab(runTab.id);
  assert.equal(env.gateway.reports("run-a1").length, 1, "an ended run still reported a take-over");
});

test("a worker started again still knows each run's tab", async () => {
  const env = setup();
  const first = await startWorker(env);
  const runTab = await grantRun(env, first, "run-a1", "Check the lamp prices");
  env.browser.dropListeners();
  const second = await startWorker(env);
  try {
    await env.browser.closeGroup(runTab.groupId);
    assert.deepEqual(env.gateway.reports("run-a1"), [
      { cdp_url: env.browser.pageUrl(runTab.id) },
      { state: "closed" },
    ]);
  } finally {
    await second.detach();
  }
});

test("after the gateway restarts, the worker attaches again", async () => {
  const env = setup();
  const worker = await startWorker(env);
  await worker.attach();
  env.gateway.restart();
  await worker.pollOnce();
  const attaches = env.gateway.requests.filter(
    (r) => r.method === "POST" && r.path === "/api/browse/connector",
  );
  assert.equal(attaches.length, 2, "the worker went quiet instead of attaching again");
  assert.deepEqual(attaches[1].body, {});
});

test("a worker another browser replaced stops asking, and attaches nothing over it", async () => {
  const env = setup();
  const worker = await startWorker(env);
  await grantRun(env, worker, "run-a1", "Check the lamp prices");
  env.gateway.replace();
  await worker.pollOnce();
  const attaches = env.gateway.requests.filter(
    (r) => r.method === "POST" && r.path === "/api/browse/connector",
  );
  assert.equal(attaches.length, 1, "the worker attached over the browser that replaced it");
  assert.equal(env.browser.api.storage.local.data.attached, false);
  await assert.rejects(
    worker.handleContractRequest({ run: "run-a1", method: "read-outline", params: {} }),
  );
});

test("a worker whose pairing was revoked stops asking", async () => {
  const env = setup();
  const worker = await startWorker(env);
  await worker.attach();
  env.gateway.revoke();
  await worker.pollOnce();
  assert.equal(env.browser.api.storage.local.data.attached, false);
});

// ── pairing, the connector's own session, and the popup ───────────────────────────────────────

test("pairing redeems the owner's code and keeps the session the gateway hands back", async () => {
  const env = setup({ paired: false });
  const worker = await startWorker(env);
  await worker.pairWith(` ${CODE} `);
  const [redeem] = gatewayCalls(env);
  assert.equal(redeem.path, "/api/devices/pair/complete");
  assert.deepEqual(redeem.body, { code: CODE, device_name: "Browser connector" });
  assert.equal(redeem.credentials, "omit", "the pairing sent the browser's cookies");
  assert.equal(redeem.headers.Authorization, undefined);
  const kept = env.browser.api.storage.local.data;
  assert.equal(kept.token, TOKEN);
  assert.equal(kept.pair, PAIR);
  assert.equal(kept.deviceId, "workstation");
});

test("every call to the gateway carries the connector's own session and none of the browser's cookies", async () => {
  const env = setup();
  const worker = await startWorker(env);
  const tab = await grantRun(env, worker, "run-a1", "Check the lamp prices");
  await env.browser.closeTab(tab.id);
  await worker.status();
  await worker.detach();
  const calls = gatewayCalls(env);
  assert.ok(calls.length >= 5, calls.map((c) => `${c.method} ${c.path}`).join(", "));
  for (const call of calls) {
    assert.equal(call.credentials, "omit", `${call.method} ${call.path} sent the browser's cookies`);
    assert.equal(call.headers.Authorization, `Bearer ${TOKEN}`, `${call.method} ${call.path}`);
    assert.equal(call.headers["X-PersonalClaw-Pair"], PAIR, `${call.method} ${call.path}`);
  }
});

test("a browser that never paired attaches nothing and says to pair", async () => {
  const env = setup({ paired: false });
  const worker = await startWorker(env);
  await assert.rejects(worker.attach(), /not paired yet: make a code in Settings → Devices & Access → Devices/);
  assert.deepEqual(gatewayCalls(env), []);
  assert.deepEqual(await worker.status(), {
    gatewayBaseUrl: "http://127.0.0.1:10000",
    paired: false,
    attached: false,
  });
});

test("a code the gateway refuses keeps nothing and says why", async () => {
  const env = setup({ paired: false });
  const worker = await startWorker(env);
  await assert.rejects(worker.pairWith("WRNG-CODE"), /The pairing code did not verify\./);
  await assert.rejects(worker.pairWith("  "), /enter the code from Settings → Devices & Access → Devices/);
  assert.equal(env.browser.api.storage.local.data.token, undefined);
});

test("a session the gateway no longer takes is forgotten, and the worker stops asking", async () => {
  const env = setup();
  const worker = await startWorker(env);
  await worker.attach();
  env.gateway.signOut();
  await worker.pollOnce();
  const kept = env.browser.api.storage.local.data;
  assert.equal(kept.attached, false);
  assert.equal(kept.token, "");
  assert.equal((await worker.status()).paired, false);
});

test("the gateway's address stays on this computer, and changing it forgets the pairing", async () => {
  const env = setup();
  const worker = await startWorker(env);
  for (const bad of ["http://192.168.1.9:10000", "https://example.com", "file:///x", ""]) {
    await assert.rejects(worker.setGateway(bad), /on this computer/);
  }
  assert.equal(env.browser.api.storage.local.data.token, TOKEN, "a refused address forgot the pairing");
  await worker.setGateway("http://127.0.0.1:10000/");
  assert.equal(env.browser.api.storage.local.data.token, TOKEN, "the same address forgot the pairing");
  await worker.setGateway("http://localhost:10001");
  const kept = env.browser.api.storage.local.data;
  assert.equal(kept.gatewayBaseUrl, "http://localhost:10001");
  assert.equal(kept.token, "");
});

// A popup page the double can drive: each element by its id, and a click that waits for its work.
function popupPage() {
  const elements = {};
  const doc = {
    getElementById(id) {
      if (!elements[id]) {
        const handlers = [];
        elements[id] = {
          value: "",
          textContent: "",
          hidden: false,
          disabled: false,
          addEventListener(type, fn) {
            if (type === "click") handlers.push(fn);
          },
          async click() {
            for (const fn of handlers) await fn();
          },
        };
      }
      return elements[id];
    },
  };
  return { doc, el: (id) => doc.getElementById(id) };
}

async function openPopup(env) {
  const page = popupPage();
  const { wirePopup } = await import(new URL(`popup.js?popup=${loads}`, EXTENSION).href);
  // Sent as the popup's messages are: from this extension's own page, never a tab.
  const send = (message) =>
    new Promise((resolve) =>
      env.browser.api.runtime.onMessage.fire(message, { id: EXTENSION_ID }, resolve));
  const popup = wirePopup({ doc: page.doc, send });
  await popup.refresh();
  return page;
}

test("the popup pairs and attaches this browser, then offers Detach", async () => {
  const env = setup({ paired: false });
  const worker = await startWorker(env);
  const page = await openPopup(env);
  assert.equal(page.el("state").textContent, "Not paired");
  assert.equal(page.el("pairing").hidden, false);
  assert.equal(page.el("where").textContent, "Make a code in Settings → Devices & Access → Devices.");
  page.el("code").value = CODE;
  await page.el("pair").click();
  assert.equal(page.el("state").textContent, "Attached");
  assert.equal(page.el("pairing").hidden, true);
  assert.equal(page.el("code").value, "");
  assert.equal(page.el("detach").hidden, false);
  assert.equal(page.el("attach").hidden, true);
  await page.el("detach").click();
  assert.equal(page.el("state").textContent, "Paired, not attached");
  assert.equal(page.el("said").textContent, "No browser is attached. Attach one from its extension.");
  assert.equal(page.el("attach").hidden, false);
  await worker.detach();
});

test("the popup shows a refusal in the gateway's own words", async () => {
  const env = setup({ paired: false });
  await startWorker(env);
  const page = await openPopup(env);
  page.el("code").value = "WRNG-CODE";
  await page.el("pair").click();
  assert.equal(page.el("state").textContent, "Not paired");
  assert.equal(page.el("said").textContent, "The pairing code did not verify.");
  assert.equal(page.el("pair").disabled, false);
  page.el("gateway").value = "http://192.168.1.9:10000";
  await page.el("save-gateway").click();
  assert.match(page.el("said").textContent, /on this computer/);
});

test("a message from a tab or another extension controls nothing in the worker", async () => {
  const env = setup({ paired: false });
  const worker = await startWorker(env);
  const before = structuredClone(env.browser.api.storage.local.data);
  const answered = [];
  const senders = [
    { id: EXTENSION_ID, tab: { id: 1 }, url: "https://bank.example.com/accounts" },
    { id: "another-extension" },
    {},
  ];
  const messages = [
    { control: "pair", code: CODE },
    { control: "gateway", url: "http://localhost:10001" },
    { control: "status" },
    { control: "attach" },
    { control: "detach" },
    { control: "contract", request: { method: "tabs.list" } },
  ];
  try {
    for (const sender of senders) {
      for (const message of messages) {
        const [kept] = await env.browser.api.runtime.onMessage.fire(
          message, sender, (answer) => answered.push(answer));
        assert.equal(kept, false, `${message.control} from ${JSON.stringify(sender)} was taken`);
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.deepEqual(answered, []);
    assert.deepEqual(gatewayCalls(env), []);
    assert.deepEqual(env.browser.api.storage.local.data, before);
  } finally {
    // A worker that took a tab's pair would be polling now: stopped, so the run still ends.
    await new Promise((resolve) => setTimeout(resolve, 50));
    await worker.detach().catch(() => {});
  }
});
