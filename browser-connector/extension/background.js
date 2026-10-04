// Background service worker for the PersonalClaw browser connector.
//
// What it does, and just as importantly what it does NOT:
//   • It ATTACHES this browser to the local gateway as a paired device (the operator redeems a
//     pairing code once, which mints the ordinary session cookie the device-session machinery
//     already uses). Attaching announces no page.
//   • A browse run works only in a tab it opened for itself (runtab.js). When the operator grants a
//     task, the worker opens a new background tab inside a tab group named after the task (or, in a
//     browser with no tab groups, in a window of its own), and announces THAT tab's own debugger
//     target, which core binds the grant to. It never announces, drives or closes a tab the
//     operator already has open, whichever tab has focus.
//   • Closing the run's group, window or tab stops the run; bringing the run's tab to the front is a
//     take-over, and the run pauses. If the worker cannot open a tab of the run's own, the run is
//     refused with a sentence instead of falling back to an existing tab.
//   • The five contract verbs (contract.js) each name the run they are for and act on that run's
//     tab only: navigate / close here in the worker (tabs API), read-outline / click / type
//     forwarded to the content script in that tab.
//   • It opens NO listening socket. Every network action is an OUTBOUND loopback request, and the
//     only inbound channel is intra-extension messaging (runtime.onMessage), not a port.
//
// Loopback is enforced by contract.js (which refuses a non-loopback gateway or endpoint) AND by
// the manifest's loopback-only host_permissions — the extension cannot fetch anything else.

import { CONTRACT_METHODS } from "./contract.js";
import { createConnector } from "./runtab.js";

const api = globalThis.browser || globalThis.chrome;

const connector = createConnector({
  api,
  fetch: (...args) => globalThis.fetch(...args),
  sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
});

// A listener's work runs to completion on its own; a failure in it is not the browser's to see.
const settled = (promise) => promise.catch(() => {});

// Added synchronously at the top level, so an event that wakes a stopped worker reaches them.
api.tabs.onActivated.addListener((info) => settled(connector.onTabActivated(info)));
api.tabs.onRemoved.addListener((tabId) => settled(connector.onTabRemoved(tabId)));
if (api.tabGroups) {
  api.tabGroups.onRemoved.addListener((group) => settled(connector.onGroupRemoved(group)));
}
api.windows.onRemoved.addListener((windowId) => settled(connector.onWindowRemoved(windowId)));
api.windows.onFocusChanged.addListener((windowId) => settled(connector.onWindowFocused(windowId)));

// Intra-extension control only (a popup or a command), NOT a network port.
api.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  const control = message && message.control;
  if (control === "attach") {
    connector.attach()
      .then((r) => {
        settled(connector.startPolling());
        sendResponse({ ok: true, result: r });
      })
      .catch((e) => sendResponse({ ok: false, error: String(e) }));
    return true;
  }
  if (control === "detach") {
    connector.detach().then(() => sendResponse({ ok: true }))
      .catch((e) => sendResponse({ ok: false, error: String(e) }));
    return true;
  }
  if (control === "contract") {
    connector.handleContractRequest(message.request)
      .then((r) => sendResponse({ ok: true, result: r }))
      .catch((e) => sendResponse({ ok: false, error: String(e) }));
    return true;
  }
  return false;
});

// A worker that starts while attached (a browser restart, or the browser waking it for an event)
// picks the polling back up.
settled(connector.startPolling());

const { attach, detach, pollOnce, handleContractRequest } = connector;

// Exposed for the popup/tests to introspect what this build speaks.
export { CONTRACT_METHODS, attach, detach, handleContractRequest, pollOnce };
