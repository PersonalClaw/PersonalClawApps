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
let loads = 0;

function event() {
  const listeners = [];
  return {
    addListener(fn) {
      listeners.push(fn);
    },
    async fire(...args) {
      for (const fn of listeners) await fn(...args);
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
    runtime: { onMessage: event() },
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

// The gateway's connector routes, and the browser's own debugger list, on loopback.
function makeGateway(browser) {
  const requests = [];
  let runs = [];
  let attached = true;
  // What the tabs route answers a browser that is not the connector: null, or [status, code].
  let refusal = null;

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
    requests.push({ method, path: u.pathname, body });
    if (u.pathname === "/api/browse/connector") {
      attached = method !== "DELETE";
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

function setup(options) {
  const browser = makeBrowser(options);
  return { browser, gateway: makeGateway(browser) };
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
