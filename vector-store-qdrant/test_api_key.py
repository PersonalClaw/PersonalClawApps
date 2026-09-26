"""A Qdrant that requires an api key gets one: the key saved on Configure reaches the server.

``provider.py`` read its key through ``CredentialStore()``, which takes the home it reads from as
a required argument. So the call raised ``TypeError`` on every connect, the provider swallowed it,
and the credential store was never read: only an environment variable could reach the server.
The app also had no setting for the key, so its own Configure form could not take one. The key
is now the ``api_key`` setting, declared
``x-meta.sensitive``: ``ProviderSettings`` keeps the value in the credential store under a key
this app owns and writes only a reference into the settings file (core #3607), the factory hands
it to the client, and uninstalling the app removes it. ``QDRANT_API_KEY`` in the environment is
the fallback for an empty field.

Proven over a real socket: :class:`KeyedQdrant` answers the REST calls this provider makes, on
127.0.0.1, and refuses every request that does not carry the right ``api-key`` header, the way a
Qdrant started with an api key does. Saves go through core's own Configure handler, and the
provider is built the way core's vector-store type handler builds it when the app is enabled.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import math
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from provider import API_KEY_NAME, create_provider

from personalclaw.apps import app_manager
from personalclaw.dashboard.handlers.apps import api_app_config_put
from personalclaw.providers.settings import ProviderSettings
from personalclaw.sdk.vector_store import VectorRecord

_APP = "vector-store-qdrant"
_BUNDLE = Path(__file__).resolve().parent
#: Made up per run: the installed copy of the bundle carries this file, so a literal would be
#: found in it by every "where is the key on disk" check below.
KEY = secrets.token_hex(16)
DIM = 4
C1 = "0" * 31 + "1"

pytestmark = [
    pytest.mark.skipif(
        importlib.util.find_spec("qdrant_client") is None, reason="qdrant-client not installed"
    ),
    # The client warns about a key over plain http, and its background version probe warns when
    # the server refuses it. Both are the fake's doing, not the provider's.
    pytest.mark.filterwarnings("ignore:Api key is used with an insecure connection"),
    pytest.mark.filterwarnings("ignore:Failed to obtain server version"),
]


class KeyedQdrant:
    """Just enough of Qdrant's REST API for this provider, behind an api key.

    A request without the ``api-key`` header gets 401 and one with the wrong key gets 403, before
    it is routed. ``seen`` records the header every request arrived with, so a test can say that
    each call the provider made was authenticated, not only that one was.
    """

    def __init__(self, key: str) -> None:
        self.key = key
        self.seen: list[tuple[str, str, str | None]] = []
        self.collections: dict[str, dict[str, tuple[list[float], dict]]] = {}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # the test output is not a request log
                pass

            def _answer(self) -> None:
                path = urlsplit(self.path).path
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else {}
                key = self.headers.get("api-key")
                fake.seen.append((self.command, path, key))
                if key is None:
                    status, doc = 401, {"status": {"error": "Must provide an API key"}}
                elif key != fake.key:
                    status, doc = 403, {"status": {"error": "Invalid API key"}}
                else:
                    status, doc = fake.route(self.command, path, body)
                data = json.dumps(doc).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_PUT = do_POST = _answer

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def calls(self) -> list[tuple[str, str, str | None]]:
        """Every request except the client's version probe, which it sends from a thread of its
        own and so may or may not have arrived yet."""
        return [row for row in self.seen if row[1] != "/"]

    def route(self, method: str, path: str, body: dict) -> tuple[int, dict]:
        parts = [p for p in path.split("/") if p]
        if not parts:
            return 200, {"title": "qdrant - vector search engine", "version": "1.19.0"}
        if parts[0] != "collections" or len(parts) < 2:
            return 404, {"status": {"error": "not found"}}
        name, rest = parts[1], parts[2:]
        points = self.collections.get(name)
        if method == "GET" and rest == ["exists"]:
            return 200, _ok({"exists": points is not None})
        if method == "PUT" and not rest:
            self.collections[name] = {}
            return 200, _ok(True)
        if points is None:
            return 404, {"status": {"error": f"Collection `{name}` doesn't exist!"}}
        if method == "PUT" and rest == ["points"]:
            for p in body["points"]:
                points[str(p["id"])] = (p["vector"], p.get("payload") or {})
            return 200, _ok({"operation_id": 1, "status": "completed"})
        if method == "POST" and rest == ["points", "query"]:
            q = body["query"]["nearest"]  # the client sends a nearest-neighbour query object
            scored = sorted(
                (
                    {"id": pid, "version": 1, "score": _cosine(q, vec), "payload": payload}
                    for pid, (vec, payload) in points.items()
                ),
                key=lambda hit: hit["score"],
                reverse=True,
            )
            return 200, _ok({"points": scored[: int(body.get("limit") or 10)]})
        return 404, {"status": {"error": "not found"}}


def _ok(result) -> dict:
    return {"result": result, "status": "ok", "time": 0.0}


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


@pytest.fixture
def home(tmp_path, monkeypatch):
    """The real bundle installed into a scratch home, nothing configured."""
    monkeypatch.setenv("PERSONALCLAW_HOME", str(tmp_path))
    # setenv first so teardown restores the variable even if the code under test sets it.
    monkeypatch.setenv(API_KEY_NAME, "")
    monkeypatch.delenv(API_KEY_NAME)
    # A saved key goes through core's credential store, whose reads consult the OS keychain
    # whenever `keyring` is importable. Keep every test off the real one.
    monkeypatch.setattr("personalclaw.config.credentials._usable_keyring", lambda: None)
    assert app_manager.install(_BUNDLE, confirm=True).ok
    yield tmp_path
    app_manager.force_uninstall(_APP)  # a no-op once the test removed it


@pytest.fixture
def qdrant():
    server = KeyedQdrant(KEY)
    yield server
    server.close()


def _configure_save(values: dict) -> None:
    """The Apps page's Configure → Save: core's PUT /api/apps/{name}/config handler."""

    class _Request(dict):
        match_info = {"name": _APP}

        async def json(self):
            return values

    resp = asyncio.run(api_app_config_put(_Request()))
    assert resp.status == 200, resp.text


def _built_on_enable():
    """What core's ``VectorStoreTypeHandler.create`` does when the app is enabled."""
    return create_provider(ProviderSettings.load(_APP))


def _hits(home: Path, needle: str) -> list[str]:
    """Every file under the home whose bytes contain ``needle``."""
    return sorted(
        str(p.relative_to(home))
        for p in home.rglob("*")
        if p.is_file() and not p.is_symlink() and needle.encode() in p.read_bytes()
    )


def _record() -> VectorRecord:
    return VectorRecord(
        chunk_id=C1, item_id="item-a", chunk_index=0, vector=[1.0, 0.0, 0.0, 0.0],
        section="s0", line_start=1, line_end=2,
    )


def test_the_api_key_is_a_sensitive_setting_and_never_required():
    """The declaration is what masks the key on every read route and renders a password input."""
    schema = json.loads((_BUNDLE / "app.json").read_text())["provider"]["settingsSchema"]
    assert schema["properties"]["api_key"]["x-meta"]["sensitive"] is True
    assert "api_key" not in schema.get("required", [])


def test_a_key_saved_on_configure_authenticates_every_call(home, qdrant):
    _configure_save({"url": qdrant.url, "collection": "kb", "api_key": KEY})
    store = _built_on_enable()

    info = store.describe()
    assert info.reachable is True, info.detail
    assert store.upsert([_record()]) == 1
    assert [hit.chunk_id for hit in store.query([1.0, 0.0, 0.0, 0.0], k=1)] == [C1]

    calls = qdrant.calls()
    assert {(m, p) for m, p, _ in calls} >= {
        ("GET", "/collections/kb/exists"),
        ("PUT", "/collections/kb"),
        ("PUT", "/collections/kb/points"),
        ("POST", "/collections/kb/points/query"),
    }
    assert {key for _, _, key in calls} == {KEY}, "a call went out without the saved key"


def test_the_saved_key_wins_over_the_environment(home, qdrant, monkeypatch):
    monkeypatch.setenv(API_KEY_NAME, "not-the-key")
    _configure_save({"url": qdrant.url, "collection": "kb", "api_key": KEY})

    assert _built_on_enable().describe().reachable is True
    assert {key for _, _, key in qdrant.calls()} == {KEY}


def test_an_empty_field_falls_back_to_the_environment(home, qdrant, monkeypatch):
    monkeypatch.setenv(API_KEY_NAME, KEY)
    _configure_save({"url": qdrant.url, "collection": "kb"})

    assert _built_on_enable().describe().reachable is True


@pytest.mark.parametrize(
    ("saved", "status"), [({}, "401"), ({"api_key": "wrong-key"}, "403")], ids=["none", "wrong"]
)
def test_the_fake_refuses_a_call_without_the_right_key(home, qdrant, saved, status):
    """The control that gives the tests above their meaning: this server does refuse."""
    _configure_save({"url": qdrant.url, "collection": "kb", **saved})

    info = _built_on_enable().describe()

    assert info.reachable is False
    assert status in info.detail, info.detail
    assert "wrong-key" not in info.detail


def test_the_key_stays_out_of_the_settings_file_and_uninstall_removes_it(home, qdrant):
    _configure_save({"url": qdrant.url, "collection": "kb", "api_key": KEY})

    assert _hits(home, KEY) == [".env"], "the key is somewhere besides the credential store"
    assert "{{secret:PCSECRET_APP_" in ProviderSettings.config_path(_APP).read_text()

    assert app_manager.uninstall_keep_data(_APP) is True

    assert _hits(home, KEY) == []
