// Content script: the DOM half of the typed contract (read-outline / click / type).
//
// navigate and close are the worker's (they act on the tab, not the document); this script
// performs the three verbs that touch the page. Elements are addressed by a STABLE ref that
// read-outline stamps onto each interactive node, so a "click e7" issued after an outline
// read means the same node it named — the same stable-ref idea core's extraction layer uses.
//
// It answers messages from the background worker and does nothing on its own; it opens no
// connection and reads no network.

const REF_ATTR = "data-pcx-ref";
const INTERACTIVE = "a[href], button, input, textarea, select, [role=button], [role=link], [contenteditable=true]";

function labelFor(el) {
  const aria = el.getAttribute && el.getAttribute("aria-label");
  const text = (aria || el.value || el.placeholder || el.innerText || "").trim();
  return text.replace(/\s+/g, " ").slice(0, 120);
}

function readOutline() {
  const nodes = Array.from(document.querySelectorAll(INTERACTIVE));
  const elements = [];
  nodes.forEach((el, i) => {
    const ref = `e${i + 1}`;
    el.setAttribute(REF_ATTR, ref);
    elements.push({
      ref,
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute("role") || "",
      label: labelFor(el),
    });
  });
  return {
    url: location.href,
    title: document.title,
    elements: elements.slice(0, 200), // a bound, like the compression layer's token budget
  };
}

function resolve(ref) {
  const el = document.querySelector(`[${REF_ATTR}="${CSS.escape(String(ref))}"]`);
  if (!el) throw new Error(`no element for ref ${ref}; read-outline first`);
  return el;
}

function clickRef(ref) {
  resolve(ref).click();
  return { ok: true, ref };
}

function typeRef(ref, value) {
  const el = resolve(ref);
  // Set the value the way a framework-backed input expects, then fire input/change so React
  // and friends observe it — never touch a password field's value (the credential invariant).
  if (el.type === "password") throw new Error("the connector never types into a password field");
  el.focus();
  if ("value" in el) {
    el.value = value;
  } else if (el.isContentEditable) {
    el.textContent = value;
  }
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
  return { ok: true, ref };
}

const api = globalThis.browser || globalThis.chrome;

api.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  try {
    switch (message && message.method) {
      case "read-outline":
        sendResponse({ ok: true, result: readOutline() });
        break;
      case "click":
        sendResponse({ ok: true, result: clickRef(message.params.ref) });
        break;
      case "type":
        sendResponse({ ok: true, result: typeRef(message.params.ref, message.params.value) });
        break;
      default:
        sendResponse({ ok: false, error: `content script does not handle ${message && message.method}` });
    }
  } catch (e) {
    sendResponse({ ok: false, error: String(e) });
  }
  return true;
});
