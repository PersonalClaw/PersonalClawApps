// The run's own tab: the only tab a browse run against this browser ever touches.
//
// When the owner grants a browse task, core asks this browser for a tab of the run's own: the run
// appears on GET /api/browse/connector/tabs, named by core's task group name. The worker then
//   • opens a NEW tab in the background (active: false) inside a tab group carrying that name, or,
//     in a browser with no tab groups, in a window of its own (opened unfocused);
//   • finds that tab's own page target on the browser's loopback debugger, by the unique address
//     the tab was opened at, and announces it for that run, so core binds the grant to it. No other
//     page the debugger lists is ever announced;
//   • reports the tab's end: closing its group (or its window, or the tab itself) STOPS the run,
//     and the owner bringing the tab to the front is a TAKE-OVER, which pauses the run.
// If it cannot open a tab of the run's own, it reports that and core refuses the run with a
// sentence. It never falls back to a tab the owner already has open.
//
// Every contract verb names the run it is for and acts on that run's tab id only. A run core no
// longer lists has ended: its tab stays where it is, as the owner's, and no verb reaches it.

import {
  announcePayload,
  announceUrl,
  buildRequest,
  isRunId,
  runTabsUrl,
  runTabUrl,
} from "./contract.js";

// Loopback defaults; the operator can override the gateway URL and the browser's
// remote-debugging port from extension storage. Both stay loopback (contract.js refuses others).
export const DEFAULTS = { gatewayBaseUrl: "http://127.0.0.1:10000", debugPort: 9222 };

// While attached, how often the worker asks the gateway whether a run needs a tab. Each round
// calls an extension API, which also keeps the worker from being stopped while it is attached.
export const POLL_INTERVAL_MS = 1000;

// A new tab's page target appears on the debugger once its first page commits.
export const TARGET_LOOKUP_ATTEMPTS = 30;
export const TARGET_LOOKUP_DELAY_MS = 100;

// The address a run's tab opens at. Unique per run, so the tab's own debugger target can be told
// apart from every other page by its URL alone; the run navigates away from it at its first step.
export function runTabAddress(runId) {
  return `about:blank#personalclaw-run-${runId}`;
}

function debuggerListUrl(debugPort) {
  const port = Number(debugPort);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error("the debugger port is not a port number");
  }
  return `http://127.0.0.1:${port}/json/list`;
}

const JSON_HEADERS = { "Content-Type": "application/json" };

export function createConnector({ api, fetch, sleep }) {
  // run id -> { tabId, groupId, windowId, cdpUrl, announced }. Mirrored into session storage, so
  // a worker the browser stopped and started again still knows which tab is which run's.
  const runs = new Map();
  let restored = null;
  let polling = false;

  function sessionArea() {
    return api.storage && api.storage.session;
  }

  function restore() {
    if (!restored) {
      restored = (async () => {
        const area = sessionArea();
        if (!area) return;
        try {
          const { runTabs } = await area.get({ runTabs: {} });
          for (const [id, run] of Object.entries(runTabs || {})) {
            if (!runs.has(id)) runs.set(id, run);
          }
        } catch (_e) {
          // Nothing to restore. A run whose tab this worker no longer knows is stopped by core's
          // own per-step check once the tab's end is reported or its debugger target goes away.
        }
      })();
    }
    return restored;
  }

  async function persist() {
    const area = sessionArea();
    if (area) await area.set({ runTabs: Object.fromEntries(runs) });
  }

  async function config() {
    const stored = await api.storage.local.get(DEFAULTS);
    return { ...DEFAULTS, ...stored };
  }

  async function postJson(url, body) {
    return fetch(url, {
      method: "POST",
      headers: JSON_HEADERS,
      credentials: "include", // the paired device session cookie rides here
      body: JSON.stringify(body),
    });
  }

  async function report(runId, body) {
    const { gatewayBaseUrl } = await config();
    return postJson(runTabUrl(gatewayBaseUrl, runId), body);
  }

  // Best-effort: an event handler must not throw, and a report core can no longer take (the run
  // already ended) changes nothing here.
  async function reportQuietly(runId, body) {
    try {
      await report(runId, body);
    } catch (_e) {
      // the gateway is unreachable; the run's own per-step check fails toward stop
    }
  }

  // Attach this browser as the connector. It announces no page: a page target is announced only
  // for a tab a granted run asked for, once this worker has opened it.
  async function attach() {
    await restore();
    const { gatewayBaseUrl } = await config();
    const resp = await postJson(announceUrl(gatewayBaseUrl), {});
    if (!resp.ok) throw new Error(`connector attach failed: HTTP ${resp.status}`);
    await api.storage.local.set({ attached: true });
    return resp.json();
  }

  async function detach() {
    await restore();
    await api.storage.local.set({ attached: false });
    runs.clear();
    await persist();
    const { gatewayBaseUrl } = await config();
    await fetch(announceUrl(gatewayBaseUrl), { method: "DELETE", credentials: "include" });
  }

  // The run's own container: a background tab in a group named after the task, or, with no tab
  // groups, an unfocused window of its own. Returns { tabId, groupId, windowId }.
  async function openContainer(address, group) {
    if (api.tabGroups && typeof api.tabs.group === "function") {
      const tab = await api.tabs.create({ url: address, active: false });
      try {
        const groupId = await api.tabs.group({ tabIds: [tab.id] });
        await api.tabGroups.update(groupId, { title: group });
        return { tabId: tab.id, groupId, windowId: null };
      } catch (err) {
        await api.tabs.remove(tab.id).catch(() => {});
        throw err;
      }
    }
    const win = await api.windows.create({ url: address, focused: false });
    const tab = win && Array.isArray(win.tabs) ? win.tabs[0] : null;
    if (!tab) {
      if (win) await api.windows.remove(win.id).catch(() => {});
      throw new Error("the run's window opened without a tab");
    }
    return { tabId: tab.id, groupId: null, windowId: win.id };
  }

  async function closeContainer(run) {
    if (run.windowId !== null && run.windowId !== undefined) {
      await api.windows.remove(run.windowId).catch(() => {});
    } else {
      await api.tabs.remove(run.tabId).catch(() => {});
    }
  }

  // The page target of the tab opened at `address`, and no other: matched on that unique URL.
  async function findPageTarget(debugPort, address) {
    const url = debuggerListUrl(debugPort);
    for (let attempt = 0; attempt < TARGET_LOOKUP_ATTEMPTS; attempt += 1) {
      const resp = await fetch(url);
      if (resp.ok) {
        const targets = await resp.json();
        const own = (Array.isArray(targets) ? targets : []).find(
          (t) => t && t.type === "page" && t.url === address && t.webSocketDebuggerUrl,
        );
        if (own) return own.webSocketDebuggerUrl;
      }
      await sleep(TARGET_LOOKUP_DELAY_MS);
    }
    throw new Error("the browser's debugger never listed the run's tab");
  }

  async function openRunTab(runId, group) {
    const address = runTabAddress(runId);
    let container = null;
    try {
      container = await openContainer(address, group);
      runs.set(runId, { ...container, cdpUrl: "", announced: false });
      const { debugPort } = await config();
      const cdpUrl = await findPageTarget(debugPort, address);
      const resp = await report(runId, announcePayload(cdpUrl));
      if (!resp.ok) throw new Error(`the run's tab was not accepted: HTTP ${resp.status}`);
      if (!runs.has(runId)) return; // the owner closed it while it was being announced
      runs.set(runId, { ...container, cdpUrl, announced: true });
      await persist();
    } catch (_err) {
      // Forgotten FIRST, so closing what was half-opened is not reported as the owner closing it.
      runs.delete(runId);
      await persist();
      if (container) await closeContainer(container);
      await reportQuietly(runId, { state: "unavailable" });
    }
  }

  // Stop asking, and act for no run: until the owner attaches this browser again.
  async function stopAsking() {
    await api.storage.local.set({ attached: false });
    runs.clear();
    await persist();
  }

  async function errorCode(resp) {
    try {
      const body = await resp.json();
      return (body && body.error && body.error.code) || "";
    } catch (_e) {
      return "";
    }
  }

  // One round: open a tab for each run that asked for one, and forget each run core no longer
  // lists. The gateway no longer having this browser attached (it restarted) is answered by
  // attaching again; another browser attached in its place, or this one no longer paired, by
  // going quiet rather than attaching over it or asking forever.
  async function pollOnce() {
    await restore();
    const { gatewayBaseUrl } = await config();
    const resp = await fetch(runTabsUrl(gatewayBaseUrl), { credentials: "include" });
    if (resp.status === 409) {
      if ((await errorCode(resp)) === "browse_connector_not_attached") await attach();
      else await stopAsking();
      return;
    }
    if (resp.status === 403) {
      await stopAsking();
      return;
    }
    if (!resp.ok) throw new Error(`run-tab poll failed: HTTP ${resp.status}`);
    const body = await resp.json();
    const listed = Array.isArray(body && body.runs) ? body.runs : [];
    const live = new Set();
    for (const entry of listed) {
      const runId = entry && entry.request_id;
      if (!isRunId(runId)) continue;
      live.add(runId);
      if (entry.state === "requested" && !runs.has(runId)) {
        await openRunTab(runId, String(entry.group || "").slice(0, 80) || "browse task");
      }
    }
    let changed = false;
    for (const runId of [...runs.keys()]) {
      if (!live.has(runId)) {
        runs.delete(runId);
        changed = true;
      }
    }
    if (changed) await persist();
  }

  async function startPolling() {
    if (polling) return;
    polling = true;
    try {
      while ((await api.storage.local.get({ attached: false })).attached) {
        try {
          await pollOnce();
        } catch (_e) {
          // the gateway may be restarting; the next round asks again
        }
        await sleep(POLL_INTERVAL_MS);
      }
    } finally {
      polling = false;
    }
  }

  // ── the tab's end, and the take-over ──────────────────────────────────────────────────────

  async function ended(matches) {
    await restore();
    for (const [runId, run] of [...runs]) {
      if (matches(run)) {
        runs.delete(runId);
        await persist();
        await reportQuietly(runId, { state: "closed" });
      }
    }
  }

  async function takenOver(matches) {
    await restore();
    for (const [runId, run] of runs) {
      if (run.announced && matches(run)) await reportQuietly(runId, { state: "taken_over" });
    }
  }

  const onTabRemoved = (tabId) => ended((run) => run.tabId === tabId);
  const onGroupRemoved = (group) =>
    ended((run) => run.groupId !== null && run.groupId === (group && group.id));
  const onWindowRemoved = (windowId) =>
    ended((run) => run.windowId !== null && run.windowId === windowId);
  const onTabActivated = (info) => takenOver((run) => run.tabId === (info && info.tabId));
  const onWindowFocused = (windowId) =>
    takenOver((run) => run.windowId !== null && run.windowId === windowId);

  // ── the typed contract dispatch ───────────────────────────────────────────────────────────

  async function handleContractRequest(raw) {
    // Validate against the CLOSED vocabulary first — an unknown verb never reaches a page.
    const req = buildRequest(raw);
    await restore();
    const run = runs.get(req.run);
    if (!run || !run.announced) {
      throw new Error(
        `run ${req.run} has no tab of its own open; the connector acts only in a tab the run opened`,
      );
    }
    switch (req.method) {
      case "navigate":
        await api.tabs.update(run.tabId, { url: req.params.url });
        return { ok: true };
      case "close":
        await api.tabs.remove(run.tabId);
        return { ok: true };
      // read-outline / click / type act on the DOM, so the run tab's content script performs them.
      default:
        return api.tabs.sendMessage(run.tabId, { method: req.method, params: req.params });
    }
  }

  return {
    attach,
    detach,
    pollOnce,
    startPolling,
    handleContractRequest,
    onTabRemoved,
    onGroupRemoved,
    onWindowRemoved,
    onTabActivated,
    onWindowFocused,
  };
}
