// The typed loopback contract — the JS mirror of connector.py. `test_contract.py` asserts the
// two declare the SAME closed vocabulary, so this file and the Python module cannot drift.
//
// The gateway drives the operator's own browser through exactly these five verbs, carried over
// a CDP page-target endpoint the browser exposes on loopback. The vocabulary is CLOSED: a verb
// outside it is refused, never guessed, because a wider surface is a wider blast radius on a
// session the operator is already logged into.

export const CONTRACT_METHODS = ["navigate", "read-outline", "click", "type", "close"];

export const REQUIRED_PARAMS = {
  "navigate": ["url"],
  "read-outline": [],
  "click": ["ref"],
  "type": ["ref", "value"],
  "close": [],
};

export function buildRequest(method, params = {}) {
  if (!CONTRACT_METHODS.includes(method)) {
    throw new Error(`unknown contract method ${method}; the vocabulary is ${CONTRACT_METHODS}`);
  }
  for (const key of REQUIRED_PARAMS[method]) {
    const value = params[key];
    if (value === undefined || value === null || String(value).trim() === "") {
      throw new Error(`${method} is missing required param ${key}`);
    }
  }
  return { method, params };
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

// The write body sent to /api/browse/connector — refuses a non-loopback endpoint so a public
// cdp_url can never leave the bundle.
export function announcePayload(cdpUrl) {
  const value = (cdpUrl || "").trim();
  if (!isLoopbackWsUrl(value)) {
    throw new Error("cdp_url must be a loopback ws(s) page-target endpoint");
  }
  return { cdp_url: value };
}

// The gateway route to announce to — refuses a non-loopback gateway (loopback rail only).
export function announceUrl(gatewayBaseUrl) {
  const base = (gatewayBaseUrl || "").replace(/\/+$/, "");
  if (!isLoopbackHttpUrl(base)) {
    throw new Error("the connector announces to a loopback gateway only");
  }
  return `${base}/api/browse/connector`;
}
