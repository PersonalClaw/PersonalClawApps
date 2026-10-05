"""Tests for the redesigned Growth Tracker backend — artifacts + growth areas + rubric lens.

The backend now installs the SDK proxy-signature middleware: every request must
carry a valid ``X-PersonalClaw-Proxy`` HMAC or it is refused fail-closed. So the test
client SIGNS every request with a known secret (the same secret the gateway supervisor
would inject via ``PERSONALCLAW_APP_SECRET``) — the tests run WITH the middleware, not
around it — and one test asserts an unsigned request is rejected.
"""

from __future__ import annotations

import importlib
import json as _json
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer
from personalclaw.sdk.security import PROXY_SIGNATURE_HEADER, sign_proxy_request
from yarl import URL

pytestmark = pytest.mark.asyncio

_SECRET = "f" * 64


class _SignedClient:
    """Wraps a TestClient, attaching a valid proxy signature to every request.

    ``.raw`` exposes the underlying unsigned client so a test can prove an unsigned
    request is refused.
    """

    def __init__(self, client: TestClient, secret: str) -> None:
        self.raw = client
        self._secret = secret

    async def _req(self, method, path, *, json=None):
        if json is not None:
            body = _json.dumps(json).encode()
            headers = {"Content-Type": "application/json"}
        else:
            body, headers = b"", {}
        path_qs = URL(path).raw_path_qs
        headers[PROXY_SIGNATURE_HEADER] = sign_proxy_request(self._secret, method, path_qs, body)
        return await self.raw.request(method, path, data=body or None, headers=headers)

    async def get(self, path):
        return await self._req("GET", path)

    async def post(self, path, json=None):
        return await self._req("POST", path, json=json)

    async def patch(self, path, json=None):
        return await self._req("PATCH", path, json=json)

    async def put(self, path, json=None):
        return await self._req("PUT", path, json=json)

    async def delete(self, path):
        return await self._req("DELETE", path)


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PERSONALCLAW_APP_DATA_DIR", str(tmp_path))
    # The middleware reads the secret from the env at make_app() time.
    monkeypatch.setenv("PERSONALCLAW_APP_SECRET", _SECRET)
    sys.path.insert(0, str(Path(__file__).parent / "backend"))
    if "server" in sys.modules:
        del sys.modules["server"]
    server = importlib.import_module("server")
    importlib.reload(server)
    c = TestClient(TestServer(server.make_app()))
    await c.start_server()
    yield _SignedClient(c, _SECRET), server
    await c.close()


async def test_started_without_a_data_folder_it_keeps_its_data_in_the_home_in_use(
    tmp_path, monkeypatch
):
    """PersonalClaw names this server's data folder when it starts it
    (``PERSONALCLAW_APP_DATA_DIR``). Started without one, by hand, the server keeps its data in
    this app's folder in the PersonalClaw home in use, the one ``PERSONALCLAW_HOME`` names, and
    not in a ``~/.personalclaw`` worked out from the account's home. ``HOME`` is a folder of the
    test's own, so the account's home can be looked in."""
    home, account = tmp_path / "pclaw-home", tmp_path / "account"
    account.mkdir()
    monkeypatch.setenv("PERSONALCLAW_HOME", str(home))
    monkeypatch.setenv("HOME", str(account))
    monkeypatch.delenv("PERSONALCLAW_APP_DATA_DIR", raising=False)
    monkeypatch.setenv("PERSONALCLAW_APP_SECRET", _SECRET)
    monkeypatch.syspath_prepend(str(Path(__file__).parent / "backend"))
    monkeypatch.delitem(sys.modules, "server", raising=False)
    server = importlib.import_module("server")
    c = TestClient(TestServer(server.make_app()))
    await c.start_server()
    try:
        made = await _SignedClient(c, _SECRET).post("/artifacts", json={"title": "Shipped it"})
        assert made.status == 200, await made.text()
    finally:
        await c.close()

    data = home.resolve() / "apps" / "growth" / "data"
    assert server.DATA_DIR == data
    assert (data / "growth.db").is_file()
    assert sorted(account.rglob("*")) == [], "the server wrote into the account's home"


async def test_health(env):
    c, _ = env
    assert (await (await c.get("/health")).json())["ok"] is True


async def test_unsigned_request_is_refused_but_health_is_exempt(env):
    c, _ = env
    # A raw (unsigned) request to a real route is refused fail-closed.
    assert (await c.raw.get("/artifacts")).status == 401
    assert (await c.raw.post("/artifacts", json={"title": "x"})).status == 401
    # /health is exempt (the gateway watchdog probes it directly).
    assert (await c.raw.get("/health")).status == 200


async def test_default_rubric_is_neutral(env):
    c, _ = env
    d = await (await c.get("/rubric")).json()
    assert d["is_override"] is False
    assert "Execution" in d["rubric"]["dimensions"]
    assert not any("SDE" in dim or "L4" in dim for dim in d["rubric"]["dimensions"])


async def test_classify_maps_keywords(env):
    _, server = env
    dims = server.classify("Shipped and released the migration; reduced p99 latency")
    assert "Execution" in dims and "Impact" in dims


async def test_sourced_detection_via_evidence_link(env):
    _, server = env
    # a PClaw-source evidence link (non-external) is inherently sourced
    assert server.is_sourced("did a thing", [{"kind": "project", "ref": "p-1", "label": "X"}]) is True
    # external URL / PR ref in text also counts
    assert server.is_sourced("shipped PR #42", []) is True
    assert server.is_sourced("just chatted", [{"kind": "external", "ref": "", "label": ""}]) is False


async def test_artifact_crud_autoclassify_and_evidence(env):
    c, _ = env
    r = await c.post("/artifacts", json={
        "title": "Drove the caching RFC to alignment",
        "behavior": "wrote and presented the RFC; the org aligned",
        "impact": "unblocked three downstream teams",
        "evidence": [{"kind": "chat", "ref": "chat-1-abc", "label": "RFC session"},
                     {"kind": "external", "ref": "https://wiki/rfc", "label": "doc"}]})
    a = await r.json()
    assert a["id"].startswith("a_")
    assert a["sourced"] is True                      # chat evidence link
    assert a["dimensions"]                            # auto-classified
    assert a["period"]                                # derived quarter
    assert len(a["evidence"]) == 2
    aid = a["id"]
    lst = (await (await c.get("/artifacts")).json())["artifacts"]
    assert any(x["id"] == aid for x in lst)
    # patch re-classifies + preserves evidence
    a2 = await (await c.patch(f"/artifacts/{aid}", json={"impact": "reduced load, improved adoption"})).json()
    assert "Impact" in a2["dimensions"]
    await c.delete(f"/artifacts/{aid}")
    assert (await (await c.get("/artifacts")).json())["artifacts"] == []


async def test_artifact_requires_title(env):
    c, _ = env
    assert (await c.post("/artifacts", json={"behavior": "x"})).status == 400


async def test_growth_areas_crud_and_linking(env):
    c, _ = env
    ga = await (await c.post("/areas", json={"name": "Cross-team influence",
                                             "target": "Lead an org-wide initiative",
                                             "dimension": "Scope & Influence"})).json()
    assert ga["id"].startswith("ga_")
    gid = ga["id"]
    # link two artifacts to the area
    for i in range(2):
        await c.post("/artifacts", json={"title": f"influence work {i}", "area_id": gid,
                                         "behavior": "aligned the org"})
    areas = (await (await c.get("/areas")).json())["areas"]
    mine = next(a for a in areas if a["id"] == gid)
    assert mine["artifact_count"] == 2
    # filter artifacts by area
    linked = (await (await c.get(f"/artifacts?area_id={gid}")).json())["artifacts"]
    assert len(linked) == 2
    # deleting the area unlinks artifacts but keeps them
    await c.delete(f"/areas/{gid}")
    assert (await (await c.get("/areas")).json())["areas"] == []
    still = (await (await c.get("/artifacts")).json())["artifacts"]
    assert len(still) == 2 and all(x["area_id"] == "" for x in still)


async def test_sources_dismiss_roundtrip(env):
    c, _ = env
    assert (await (await c.get("/dismissed")).json())["dismissed"] == []
    await c.post("/dismissed", json={"ref": "project:p-9"})
    await c.post("/dismissed", json={"ref": "project:p-9"})  # idempotent
    d = (await (await c.get("/dismissed")).json())["dismissed"]
    assert d == ["project:p-9"]
    assert (await c.post("/dismissed", json={})).status == 400


async def test_readiness(env):
    c, _ = env
    for i in range(3):
        await c.post("/artifacts", json={"title": f"shipped feature {i}",
                                         "impact": "improved adoption",
                                         "evidence": [{"kind": "task", "ref": f"t-{i}", "label": "task"}],
                                         "date": f"2026-0{i+1}-15"})
    readiness = await (await c.get("/readiness")).json()
    assert "dimensions" in readiness and 0 <= readiness["overall_pct"] <= 100
    ex = next(d for d in readiness["dimensions"] if d["dimension"] == "Execution")
    assert ex["actual"] >= 3 and ex["status"] == "Consistent"


async def test_digest_requires_content_and_delete(env):
    c, _ = env
    assert (await c.post("/digests", json={"period": "2026-Q3", "content_md": "  "})).status == 400
    d = await (await c.post("/digests", json={"period": "2026-Q3", "content_md": "# Done"})).json()
    assert d["id"].startswith("d_")
    assert len((await (await c.get("/digests")).json())["digests"]) == 1
    await c.delete(f"/digests/{d['id']}")
    assert (await (await c.get("/digests")).json())["digests"] == []


async def test_rubric_override_validation(env):
    c, _ = env
    assert (await c.put("/rubric", json={"dimensions": []})).status == 400
    ok = {"label": "Custom", "dimensions": ["Craft"],
          "requirements": [{"code": "C1", "dim": "Craft", "threshold": 1, "keywords": ["built"]}]}
    assert (await c.put("/rubric", json=ok)).status == 200
    d = await (await c.get("/rubric")).json()
    assert d["is_override"] is True and d["rubric"]["dimensions"] == ["Craft"]
    await c.post("/rubric/reset")
    assert (await (await c.get("/rubric")).json())["is_override"] is False


# ── design-system rail ────────────────────────────────────────────────────────────────
# These are sync, and the module-level ``pytestmark = pytest.mark.asyncio`` does not
# apply to them: pytest-asyncio only wraps coroutine functions.

_APP_DIR = Path(__file__).parent
_UI_SRC = _APP_DIR / "ui" / "src" / "index.tsx"


def _ui_code_lines() -> str:
    """The UI source with comment-only lines dropped, so a text scan can't read design
    rationale as markup (the same skip the shared token lint applies, for the same reason)."""
    return "\n".join(
        ln for ln in _UI_SRC.read_text(encoding="utf-8").split("\n")
        if not ln.strip().startswith(("//", "*", "/*"))
    )


def test_growth_schedules_no_job_its_tier_could_not_run():
    """A scheduled job runs its agent at its app's agent tier, and Growth's is ``text``: a job's
    model would be handed its message and nothing else, with no tools, so a capture could neither
    read the day nor file an artifact. So Growth declares no job and no permission for one, its
    page's tasks keep the tier they need, and nothing the manifest says goes unread."""
    from personalclaw.sdk.manifest import AppManifest

    raw = _json.loads((_APP_DIR / "app.json").read_text(encoding="utf-8"))
    assert raw["permissions"]["agent"] == "text"
    assert "crons" not in raw and "cron" not in raw["permissions"]
    assert "sidebar" not in raw["ui"], "nothing reads ui.sidebar"
    assert not (_APP_DIR / "DAILY_CAPTURE.md").exists(), "the capture's instructions outlived it"
    assert AppManifest.from_dict(raw).validate() == []


def test_ui_declares_the_capability_its_import_depends_on():
    """The bundle imports ``@personalclaw/app-sdk/ui``, and the host's loader leaves that
    specifier UNREWRITTEN (so it fails to resolve at mount) unless the manifest declares
    ``shell-primitives``. Import and declaration are therefore one fact, and this asserts
    the CALL SITE of both — drop either and the page white-screens."""
    src = _UI_SRC.read_text(encoding="utf-8")
    assert "from '@personalclaw/app-sdk/ui'" in src, "the UI no longer imports the host primitives"
    caps = _json.loads((_APP_DIR / "app.json").read_text(encoding="utf-8")).get("uiCapabilities")
    assert caps == ["shell-primitives"], f"manifest declares {caps!r}"
    # The subpath is a DIFFERENT module from the base SDK (the alias was removed), so
    # importing the base module is not evidence that the primitives resolved.
    assert "from '@personalclaw/app-sdk'" in src


def test_frontend_is_token_clean_under_the_shared_lint():
    """0 violations under the ONE token-lint rule (``apps/token_lint_rules.json``), which is
    what ``quality.designSystem: "v2"`` claims. Imported, never reimplemented: a second copy
    of the rule here would be the drift this app just removed."""
    from personalclaw.apps.quality import frontend_sources, token_lint_bundle

    # Vacuity guard: "0 violations" is also the answer for an EMPTY input set, so pin that
    # the linter actually saw this bundle's one frontend source before trusting the zero.
    seen = [p.relative_to(_APP_DIR).as_posix() for p in frontend_sources(_APP_DIR)]
    assert seen == ["ui/src/index.tsx"], f"linter input set was {seen!r}"
    assert token_lint_bundle(_APP_DIR) == {}


def test_no_local_re_declaration_of_a_host_primitive():
    """This bundle's copies of the host component spec were deleted. A new one would fork
    the design system again silently — the whole failure mode the migration removed."""
    src = _ui_code_lines()
    for name in ("primaryBtn", "smallBtn", "linkBtn", "tabStyle", "tabActive", "cardStyle"):
        assert name not in src, f"{name} is back — use the host Button/Surface instead"
    # Vacuity guard: the scan above passes trivially on an empty/renamed file, so pin that
    # what replaced those constants is present.
    assert "<Button " in src and "<Surface " in src


def test_page_does_not_draw_a_second_h1():
    """``AppFrame`` renders the page ``<h1>`` (``PageTitle``). An app page that draws its own
    gives the document two, so the screen heading is an ``<h2>``.

    Scanned over CODE lines only. A raw text scan reads the rationale comment above
    ``Header`` — which says the words ``<h1>`` — as a live element, and fails on prose.
    Same line-skip discipline as the shared token lint, for the same reason.
    """
    code = _ui_code_lines()
    assert "<h1" not in code, "the app page draws its own h1; AppFrame already renders one"
    # Vacuity guard: the heading was DEMOTED, not deleted — otherwise this passes on a page
    # that lost its heading structure entirely. And it proves the comment strip above left
    # the JSX behind rather than eating the file.
    assert '<h2 data-type="title-l"' in code
