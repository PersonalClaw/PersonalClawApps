// Background service worker for the PersonalClaw browser connector (BA-8).
//
// What it does, and just as importantly what it does NOT:
//   • It PAIRS with the local gateway (the operator redeems a code once, which mints the
//     ordinary session cookie the shipped device-session machinery already uses) and then
//     ANNOUNCES, over loopback, the browser's own CDP page-target endpoint to
//     POST /api/browse/connector — the write BA-7's register_connector consumes.
//   • It DRIVES the operator's browser through the closed five-verb contract in contract.js:
//     navigate / close here in the worker (tabs API), read-outline / click / type forwarded
//     to the content script.
//   • It opens NO listening socket. Every network action is an OUTBOUND loopback request, and
//     the only inbound channel is intra-extension messaging (runtime.onMessage), not a port.
//
// Loopback is enforced by contract.js's announceUrl/announcePayload (which refuse a
// non-loopback gateway or endpoint) AND by the manifest's loopback-only host_permissions —
// the extension literally cannot fetch anything else.
//
// The per-task grant flow, the task-named tab group and close-to-kill are BA-9; this bundle
// establishes the connection and the contract.

import {
  announcePayload,
  announceUrl,
  buildRequest,
  CONTRACT_METHODS,
} from "./contract.js";

const api = globalThis.browser || globalThis.chrome;

// Loopback defaults; the operator can override the gateway URL and the browser's
// remote-debugging port from extension storage. Both must stay loopback (enforced below).
const DEFAULTS = { gatewayBaseUrl: "http://127.0.0.1:10000", debugPort: 9222 };

async function config() {
  const stored = await api.storage.local.get(DEFAULTS);
  return { ...DEFAULTS, ...stored };
}

// Discover the current page's CDP page-target endpoint from the browser's OWN
// remote-debugging server on loopback (the operator launches the browser with
// --remote-debugging-port; the extension cannot and does not open that surface itself).
async function discoverCdpUrl(debugPort) {
  const resp = await fetch(`http://127.0.0.1:${debugPort}/json`);
  if (!resp.ok) throw new Error(`debugger discovery failed: HTTP ${resp.status}`);
  const targets = await resp.json();
  const page = (Array.isArray(targets) ? targets : []).find(
    (t) => t && t.type === "page" && t.webSocketDebuggerUrl,
  );
  if (!page) throw new Error("no page target exposed by the local debugger");
  return page.webSocketDebuggerUrl; // ws://127.0.0.1:<port>/devtools/page/<id>
}

// Announce (write) the loopback endpoint to the gateway. `announcePayload` refuses a
// non-loopback cdp_url and `announceUrl` refuses a non-loopback gateway, so this cannot
// ship an endpoint reference off-box even if misconfigured.
async function attach() {
  const { gatewayBaseUrl, debugPort } = await config();
  const cdpUrl = await discoverCdpUrl(debugPort);
  const resp = await fetch(announceUrl(gatewayBaseUrl), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include", // the paired device session cookie rides here
    body: JSON.stringify(announcePayload(cdpUrl)),
  });
  if (!resp.ok) throw new Error(`connector attach failed: HTTP ${resp.status}`);
  return resp.json();
}

async function detach() {
  const { gatewayBaseUrl } = await config();
  await fetch(announceUrl(gatewayBaseUrl), { method: "DELETE", credentials: "include" });
}

// ── the typed contract dispatch ─────────────────────────────────────────────────────────

async function activeTabId() {
  const [tab] = await api.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab) throw new Error("no active tab to drive");
  return tab.id;
}

async function handleContractRequest(raw) {
  // Validate against the CLOSED vocabulary first — an unknown verb never reaches the page.
  const req = buildRequest(raw && raw.method, (raw && raw.params) || {});
  const tabId = await activeTabId();
  switch (req.method) {
    case "navigate":
      await api.tabs.update(tabId, { url: req.params.url });
      return { ok: true };
    case "close":
      await api.tabs.remove(tabId);
      return { ok: true };
    // read-outline / click / type act on the DOM, so the content script performs them.
    default:
      return api.tabs.sendMessage(tabId, req);
  }
}

// Intra-extension control only (a popup or a command), NOT a network port.
api.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  const control = message && message.control;
  if (control === "attach") {
    attach().then((r) => sendResponse({ ok: true, result: r }))
      .catch((e) => sendResponse({ ok: false, error: String(e) }));
    return true;
  }
  if (control === "detach") {
    detach().then(() => sendResponse({ ok: true }))
      .catch((e) => sendResponse({ ok: false, error: String(e) }));
    return true;
  }
  if (control === "contract") {
    handleContractRequest(message.request)
      .then((r) => sendResponse({ ok: true, result: r }))
      .catch((e) => sendResponse({ ok: false, error: String(e) }));
    return true;
  }
  return false;
});

// Exposed for the popup/tests to introspect what this build speaks.
export { CONTRACT_METHODS, attach, detach, handleContractRequest };
