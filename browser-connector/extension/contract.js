// The typed loopback contract — the JS mirror of connector.py. `test_contract.py` asserts the
// two declare the SAME closed vocabulary, so this file and the Python module cannot drift.
//
// The gateway drives the operator's own browser through exactly these five verbs, carried over
// the page-target endpoint of a tab the run opened for itself. The vocabulary is CLOSED: a verb
// outside it is refused, never guessed, because a wider surface is a wider blast radius on a
// session the operator is already logged into. Every verb is ADDRESSED to one run, by the id core
// gave that run's tab, and acts on that run's own tab and nothing else: never on whichever tab
// happens to have focus.

export const CONTRACT_METHODS = ["navigate", "read-outline", "click", "type", "close"];

export const REQUIRED_PARAMS = {
  "navigate": ["url"],
  "read-outline": [],
  "click": ["ref"],
  "type": ["ref", "value"],
  "close": [],
};

// What the browser may report about a run's tab once core has asked for one: it could not open a
// tab of the run's own, the person closed it (the tab, its group or its window), or the person
// brought it to the front to take over.
export const RUN_TAB_REPORTS = ["unavailable", "closed", "taken_over"];

// A run's id is core's, and it rides in a route path, so it is held to a closed alphabet.
export function isRunId(value) {
  return typeof value === "string" && /^[A-Za-z0-9_-]{1,64}$/.test(value);
}

// Validate one addressed request: the run it is for, a verb from the closed vocabulary, and that
// verb's required params. Returns `{ run, method, params }`.
export function buildRequest(message) {
  const run = message && message.run;
  const method = message && message.method;
  const params = (message && message.params) || {};
  if (!isRunId(run)) {
    throw new Error("a contract request must name the run whose tab it acts on");
  }
  if (!CONTRACT_METHODS.includes(method)) {
    throw new Error(`unknown contract method ${method}; the vocabulary is ${CONTRACT_METHODS}`);
  }
  for (const key of REQUIRED_PARAMS[method]) {
    const value = params[key];
    if (value === undefined || value === null || String(value).trim() === "") {
      throw new Error(`${method} is missing required param ${key}`);
    }
  }
  return { run, method, params };
}

// ── the loopback rail (mirrors connector.py) ────────────────────────────────────────────────

export function isLoopbackHost(host) {
  if (!host) return false;
  const stripped = host.replace(/^\[|\]$/g, "");
  if (stripped === "localhost" || stripped.endsWith(".localhost")) return true;
  if (stripped === "::1") return true;
  const m = stripped.match(/^(\d+)\.(\d+)\.(\d+)\.(\d+)$/);
  return Boolean(m) && Number(m[1]) === 127;
}

export function isLoopbackWsUrl(url) {
  try {
    const u = new URL(url);
    return (u.protocol === "ws:" || u.protocol === "wss:") && isLoopbackHost(u.hostname);
  } catch (_e) {
    return false;
  }
}

export function isLoopbackHttpUrl(url) {
  try {
    const u = new URL(url);
    return (u.protocol === "http:" || u.protocol === "https:") && isLoopbackHost(u.hostname);
  } catch (_e) {
    return false;
  }
}

// The body that announces a run's own tab — refuses a non-loopback endpoint so a public cdp_url
// can never leave the bundle.
export function announcePayload(cdpUrl) {
  const value = (cdpUrl || "").trim();
  if (!isLoopbackWsUrl(value)) {
    throw new Error("cdp_url must be a loopback ws(s) page-target endpoint");
  }
  return { cdp_url: value };
}

function gatewayBase(gatewayBaseUrl) {
  const base = (gatewayBaseUrl || "").replace(/\/+$/, "");
  if (!isLoopbackHttpUrl(base)) {
    throw new Error("the connector announces to a loopback gateway only");
  }
  return base;
}

// The route that attaches this browser as the connector — refuses a non-loopback gateway.
export function announceUrl(gatewayBaseUrl) {
  return `${gatewayBase(gatewayBaseUrl)}/api/browse/connector`;
}

// The route that lists the runs which asked this browser for a tab of their own.
export function runTabsUrl(gatewayBaseUrl) {
  return `${announceUrl(gatewayBaseUrl)}/tabs`;
}

// The route one run's tab is announced and reported on.
export function runTabUrl(gatewayBaseUrl, runId) {
  if (!isRunId(runId)) throw new Error("not a run id");
  return `${runTabsUrl(gatewayBaseUrl)}/${runId}`;
}
