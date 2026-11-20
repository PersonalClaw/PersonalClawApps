// The connector's popup: whether this browser is paired and attached, the pairing code, Attach and
// Detach, and the gateway's address. Every action is a message to the worker (background.js); the
// popup itself calls no gateway and holds no session.

import { WHERE_CODES_ARE } from "./runtab.js";

function sentence(error) {
  return String(error).replace(/^(Error: )+/, "");
}

export function wirePopup({ doc, send }) {
  const el = (id) => doc.getElementById(id);
  const state = el("state");
  const said = el("said");
  const pairing = el("pairing");
  const code = el("code");
  const gateway = el("gateway");
  const buttons = {
    pair: el("pair"),
    attach: el("attach"),
    detach: el("detach"),
    gateway: el("save-gateway"),
  };
  el("where").textContent = `Make a code in ${WHERE_CODES_ARE}.`;

  async function ask(message) {
    const answer = await send(message);
    if (!answer || !answer.ok) {
      throw new Error((answer && answer.error) || "the connector did not answer");
    }
    return answer.result;
  }

  async function refresh() {
    let now;
    try {
      now = await ask({ control: "status" });
    } catch (error) {
      state.textContent = "Can't reach the gateway";
      said.textContent = sentence(error);
      return;
    }
    gateway.value = now.gatewayBaseUrl;
    pairing.hidden = now.paired;
    buttons.attach.hidden = !now.paired || now.attached;
    buttons.detach.hidden = !now.attached;
    if (!now.paired) state.textContent = "Not paired";
    else if (now.attached) state.textContent = "Attached";
    else if (now.another) state.textContent = "Another browser is attached";
    else state.textContent = "Paired, not attached";
    said.textContent = now.attached ? "" : [now.reason, now.fix].filter(Boolean).join(" ");
  }

  // One action at a time: its buttons wait, and its refusal is shown in the worker's own words.
  async function act(message) {
    for (const button of Object.values(buttons)) button.disabled = true;
    try {
      await ask(message);
      return true;
    } catch (error) {
      await refresh();
      said.textContent = sentence(error);
      return false;
    } finally {
      for (const button of Object.values(buttons)) button.disabled = false;
    }
  }

  const done = (ok) => (ok ? refresh() : undefined);
  buttons.pair.addEventListener("click", async () => {
    const ok = await act({ control: "pair", code: code.value });
    if (ok) code.value = "";
    return done(ok);
  });
  buttons.attach.addEventListener("click", async () => done(await act({ control: "attach" })));
  buttons.detach.addEventListener("click", async () => done(await act({ control: "detach" })));
  buttons.gateway.addEventListener("click", async () =>
    done(await act({ control: "gateway", url: gateway.value })),
  );

  return { refresh };
}

const api = globalThis.browser || globalThis.chrome;
if (globalThis.document && api && api.runtime && api.runtime.sendMessage) {
  wirePopup({ doc: globalThis.document, send: (message) => api.runtime.sendMessage(message) })
    .refresh();
}
