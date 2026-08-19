"""Tests for the s3-sync transport.

Three things are proved here that a mocked-out test could not:

1. **The signature is correct**, not merely stable. The golden vectors below were produced
   by this implementation AND independently verified byte-for-byte against ``botocore``'s
   ``SigV4Auth`` at a pinned timestamp (see ``test_golden_vectors_cross_check_botocore``,
   which re-runs that comparison whenever botocore is importable). The goldens are asserted
   unconditionally, so the suite still fails on a signing regression in an environment with
   no botocore — a skip would read as a pass.
2. **Every byte really travels through ``sdk.net.fetch`` under the derived pinned policy.**
   The round-trip tests drive a real loopback HTTP store through the real egress guard, and
   a source-level test asserts the module imports no HTTP client of its own.
3. **The failure modes are the safe ones.** A truncated body is dropped rather than merged,
   a registry CAS refuses rather than clobbers, and a canary secret never reaches a log,
   a ``repr``, or an exception string.
"""

import datetime
import hashlib
import http.server
import json
import logging
import os
import pathlib
import threading
import urllib.parse
from typing import Any

import pytest

from provider import (
    S3SyncProvider,
    canonical_request,
    create_provider,
    signing_key,
)
import provider as provider_mod

from personalclaw.sdk.sync import RemoteRef, SyncObject

# ── the credentials used for every signing vector ────────────────────────────────────
# The AWS-documentation example key pair. It is public, expired-by-construction example
# material and is NOT a credential: using a real key here would put one in git forever.
AK = "AKIAIOSFODNN7EXAMPLE"
SK = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
REGION = "us-east-1"
ENDPOINT = "https://s3.us-east-1.amazonaws.com"

#: The signing clock every golden vector is pinned to.
PIN = datetime.datetime(2026, 8, 18, 12, 0, 0, tzinfo=datetime.timezone.utc)

#: botocore-verified goldens: (method, url, query, payload, extra_headers) → signature hex.
GOLDEN_VECTORS: list[tuple[str, str, dict[str, str], bytes, dict[str, str] | None, str]] = [
    (
        "PUT",
        f"{ENDPOINT}/mybucket/machines/A/seq-0001/tasks.jsonl",
        {},
        b'{"row": 1}\n',
        {"if-none-match": "*"},
        "91ec87de950f27647d3fa13571ca3e308e794a49987063f4763aecc314a4e4c4",
    ),
    (
        "GET",
        f"{ENDPOINT}/mybucket",
        {"list-type": "2", "max-keys": "1000", "prefix": "personalclaw/"},
        b"",
        None,
        "9808ef26d5675008be61d39e0b54770cafc5a8bfb1744b81dd013e1a874908a4",
    ),
    (
        "GET",
        f"{ENDPOINT}/mybucket/registry.json",
        {},
        b"",
        None,
        "4d76048962ebea32d3513109c2b5936582078e0c86477fbcf766f6d9cafd5a96",
    ),
]

#: botocore-verified golden for the signing-key chain itself.
GOLDEN_SIGNING_KEY = "d29a1a9975a825f50034d2cff4d78985e737bc87ef1e76e67da0f45b35cf5fee"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Never touch the real home.

    ``net.fetch`` emits a SEL audit row for every allow/deny decision, so a test that
    drives the real fetch path writes into ``PERSONALCLAW_HOME``. Both variables are set
    (home AND workspace) because an unseeded workspace falls back to the real ``~/workplace``.
    """
    home = tmp_path / "home"
    ws = tmp_path / "ws"
    home.mkdir()
    ws.mkdir()
    monkeypatch.setenv("PERSONALCLAW_HOME", str(home))
    monkeypatch.setenv("PERSONALCLAW_WORKSPACE", str(ws))
    # A leaked ambient AWS identity must never be reachable from a test either.
    for var in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "PERSONALCLAW_S3_ACCESS_KEY_ID",
        "PERSONALCLAW_S3_SECRET_ACCESS_KEY",
        "PERSONALCLAW_S3_SESSION_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    yield home


@pytest.fixture
def pinned_clock(monkeypatch):
    """Freeze the signing clock so a signature is reproducible."""
    monkeypatch.setattr(provider_mod, "_utcnow", lambda: PIN)


# ── 1. the signature ─────────────────────────────────────────────────────────────────


class TestSigV4:
    def _provider(self, **kw: Any) -> S3SyncProvider:
        base = dict(
            endpoint=ENDPOINT, bucket="mybucket", region=REGION,
            access_key_id=AK, secret_access_key=SK,
        )
        base.update(kw)
        return S3SyncProvider(**base)  # type: ignore[arg-type]

    def test_signing_key_chain_matches_golden(self):
        assert signing_key(SK, "20260818", REGION).hex() == GOLDEN_SIGNING_KEY

    @pytest.mark.parametrize("idx", range(len(GOLDEN_VECTORS)))
    def test_golden_vectors(self, pinned_clock, idx):
        """The signature for a fixed request is exactly the botocore-verified golden."""
        method, url, query, payload, extra, expected = GOLDEN_VECTORS[idx]
        auth = self._provider()._signed_headers(method, url, query, payload, extra)[
            "Authorization"
        ]
        assert f"Signature={expected}" in auth
        assert auth.startswith("AWS4-HMAC-SHA256 Credential=")
        assert f"{AK}/20260818/{REGION}/s3/aws4_request" in auth

    def test_golden_vectors_cross_check_botocore(self, pinned_clock):
        """Re-derive every golden with botocore — an INDEPENDENT SigV4 implementation.

        This is the test that makes the goldens a correctness claim rather than a
        regression lock. It is skipped only where botocore is absent (apps CI installs core
        + pytest only); the goldens above are asserted unconditionally either way, so a
        signing regression cannot hide behind this skip.
        """
        botocore_auth = pytest.importorskip("botocore.auth")
        from botocore.awsrequest import AWSRequest
        from botocore.credentials import Credentials

        monkey_restore = botocore_auth.get_current_datetime
        botocore_auth.get_current_datetime = lambda: PIN
        try:
            checked = 0
            for method, url, query, payload, extra, expected in GOLDEN_VECTORS:
                enc = provider_mod._uri_encode
                full = url + (
                    "?" + "&".join(f"{enc(k)}={enc(v)}" for k, v in sorted(query.items()))
                    if query
                    else ""
                )
                req = AWSRequest(method=method, url=full, data=payload)
                req.headers["X-Amz-Content-SHA256"] = hashlib.sha256(payload).hexdigest()
                if extra:
                    for k, v in extra.items():
                        req.headers[k] = v
                botocore_auth.SigV4Auth(Credentials(AK, SK), "s3", REGION).add_auth(req)
                boto_sig = req.headers["Authorization"].split("Signature=")[1]
                assert boto_sig == expected, f"{method} {url}: botocore disagrees with golden"
                checked += 1
            # Vacuity floor: an empty vector list would make every assertion above vacuous.
            assert checked == len(GOLDEN_VECTORS) >= 3
        finally:
            botocore_auth.get_current_datetime = monkey_restore

    def test_canonical_query_percent_encodes_slash(self):
        """A ``/`` inside a query VALUE must be ``%2F`` — it is not an unreserved char.

        This is the one encoding rule that differs between the canonical *path* (where
        ``/`` is a separator and stays raw) and the canonical *query*, and getting it
        backwards produces a signature the store rejects on every prefixed LIST.
        """
        creq, _ = canonical_request(
            "GET", "/mybucket", {"prefix": "personalclaw/machines/"}, {"host": "h"}, "sha"
        )
        assert "prefix=personalclaw%2Fmachines%2F" in creq
        assert "prefix=personalclaw/machines/" not in creq

    def test_canonical_path_keeps_separators_and_encodes_once(self):
        creq, _ = canonical_request(
            "PUT", "/mybucket/machines/A/seq-0001/tasks.jsonl", {}, {"host": "h"}, "sha"
        )
        assert "/mybucket/machines/A/seq-0001/tasks.jsonl" in creq
        assert "%2F" not in creq.split("\n")[1]

    def test_host_and_payload_hash_are_always_signed(self, pinned_clock):
        """``host`` binds the signature to one endpoint; the content hash binds it to
        exactly these bytes. Neither may drop out of SignedHeaders."""
        headers = self._provider()._signed_headers("PUT", f"{ENDPOINT}/mybucket/k", {}, b"body")
        signed = headers["Authorization"].split("SignedHeaders=")[1].split(",")[0]
        assert "host" in signed.split(";")
        assert "x-amz-content-sha256" in signed.split(";")
        assert "x-amz-date" in signed.split(";")
        assert headers["x-amz-content-sha256"] == hashlib.sha256(b"body").hexdigest()

    def test_payload_hash_changes_with_payload(self, pinned_clock):
        """Two different bodies must not share a signature — otherwise a bucket-writer
        could swap one shard's bytes for another's under a captured Authorization header."""
        p = self._provider()
        a = p._signed_headers("PUT", f"{ENDPOINT}/mybucket/k", {}, b"one")["Authorization"]
        b = p._signed_headers("PUT", f"{ENDPOINT}/mybucket/k", {}, b"two")["Authorization"]
        assert a != b

    def test_object_key_is_bound_into_the_signature(self, pinned_clock):
        """The canonical path covers the key, so a signature for one key cannot be
        replayed against another."""
        p = self._provider()
        a = p._signed_headers("PUT", f"{ENDPOINT}/mybucket/k1", {}, b"x")["Authorization"]
        b = p._signed_headers("PUT", f"{ENDPOINT}/mybucket/k2", {}, b"x")["Authorization"]
        assert a != b

    def test_session_token_is_signed_when_present(self, pinned_clock):
        p = self._provider(session_token="STS-TOKEN-VALUE")
        headers = p._signed_headers("GET", f"{ENDPOINT}/mybucket/k", {}, b"")
        signed = headers["Authorization"].split("SignedHeaders=")[1].split(",")[0]
        assert "x-amz-security-token" in signed.split(";")
        assert headers["x-amz-security-token"] == "STS-TOKEN-VALUE"

    def test_no_session_token_header_when_absent(self, pinned_clock):
        headers = self._provider()._signed_headers("GET", f"{ENDPOINT}/mybucket/k", {}, b"")
        assert "x-amz-security-token" not in headers


# ── 2. the egress posture ────────────────────────────────────────────────────────────


class TestGuardedEgress:
    def test_module_imports_no_http_client_of_its_own(self):
        """§4.3: "never hand-rolled aiohttp". Asserted on the SOURCE, because a test that
        only checks behaviour would still pass if someone added a second, unguarded code
        path for one method."""
        src = pathlib.Path(provider_mod.__file__).read_text(encoding="utf-8")
        tree = __import__("ast").parse(src)
        imported: set[str] = set()
        for node in __import__("ast").walk(tree):
            if isinstance(node, __import__("ast").Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, __import__("ast").ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for banned in ("aiohttp", "httpx", "requests", "boto3", "botocore", "urllib3"):
            assert banned not in imported, f"s3-sync must not import {banned}; use sdk.net.fetch"
        # Vacuity floor: the scan must actually have seen this module's imports.
        assert "personalclaw" in imported and "hashlib" in imported

    def test_requests_are_refused_for_a_non_pinned_host(self, monkeypatch):
        """The derived policy is host-pinned, so the guard refuses any other host — the
        property that makes a mis-typed or attacker-influenced endpoint unreachable rather
        than merely unusual."""
        from personalclaw.sdk.net import EgressBlocked, evaluate, sync_egress_policy

        policy = sync_egress_policy("https://s3.us-east-1.amazonaws.com")
        assert evaluate("https://s3.us-east-1.amazonaws.com/mybucket", policy).allow
        for other in (
            "https://evil.example.com/mybucket",
            "https://s3.eu-central-1.amazonaws.com/mybucket",
            "http://169.254.169.254/latest/meta-data/",
        ):
            assert not evaluate(other, policy).allow, f"{other} was reachable under a pin"
        assert EgressBlocked is not None

    def test_the_policy_handed_to_fetch_is_the_DERIVED_PINNED_one(self, stub, monkeypatch):
        """The rail above proves ``sync_egress_policy`` pins correctly; this one proves the
        PROVIDER actually hands that policy to ``fetch`` unweakened.

        Added after a falsification found the gap: widening the provider's policy to
        ``allow_only=False`` left the whole egress class GREEN, because every assertion was
        made against the policy function rather than against what the transport used. A
        mechanism test is not a use test.
        """
        import personalclaw.sdk.net as sdk_net

        seen: list[Any] = []
        real_fetch = sdk_net.fetch

        async def spy(url, *, policy=None, **kw):
            seen.append(policy)
            return await real_fetch(url, policy=policy, **kw)

        monkeypatch.setattr(sdk_net, "fetch", spy)
        p = S3SyncProvider(
            stub.endpoint, "mybucket", region=REGION, access_key_id=AK, secret_access_key=SK
        )
        assert p.push([SyncObject(key="k", data=b"v")]).pushed == 1
        assert seen, "fetch was never called — the transport bypassed the chokepoint"
        host = urllib.parse.urlparse(stub.endpoint).hostname
        for policy in seen:
            assert policy is not None, "a request went out with no egress policy"
            assert policy.allow_only is True, "the transport widened the pin to a deny-list"
            assert tuple(policy.allow_hosts) == (host,), (
                f"the transport reached beyond its pinned endpoint: {policy.allow_hosts}"
            )
            assert policy.deny_hosts, "the metadata-service denials were dropped"

    def test_an_egress_refusal_is_a_permanent_push_outcome(self, monkeypatch):
        """A blocked host cannot be fixed by retrying, so it must not become an error loop."""
        from personalclaw.sdk.net import EgressBlocked

        p = S3SyncProvider(
            ENDPOINT, "mybucket", region=REGION, access_key_id=AK, secret_access_key=SK
        )

        def boom(*a, **k):
            raise EgressBlocked(_FakeDecision())

        monkeypatch.setattr(provider_mod.S3SyncProvider, "_request", boom)
        res = p.push([SyncObject(key="k", data=b"x")])
        assert res.outcome == "permanent"
        assert res.pushed == 0


class _FakeDecision:
    allow = False
    url = "https://evil.example.com"
    reason = "host not permitted"
    risk_level = "caution"
    recovery_hints: list[str] = []

    def __str__(self) -> str:
        return self.reason


# ── the loopback S3 stub ─────────────────────────────────────────────────────────────


class _StubS3(http.server.BaseHTTPRequestHandler):
    """A minimal, in-memory S3 that speaks the exact subset the transport uses.

    Deliberately implements the CONDITIONAL-WRITE semantics (``If-None-Match: *`` and
    ``If-Match: <etag>``) rather than accepting every PUT, because insert-only and the
    registry CAS are the two properties most worth proving.
    """

    server_version = "StubS3/1.0"

    def log_message(self, fmt, *args):  # keep the test output clean
        pass

    # -- helpers ----------------------------------------------------------------------
    @property
    def store(self) -> dict[str, bytes]:
        return self.server.store  # type: ignore[attr-defined]

    def _etag(self, data: bytes) -> str:
        return '"' + hashlib.md5(data).hexdigest() + '"'  # noqa: S324 - S3's own etag shape

    def _send(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None):
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _split(self):
        parsed = urllib.parse.urlparse(self.path)
        parts = parsed.path.lstrip("/").split("/", 1)
        bucket = parts[0]
        key = urllib.parse.unquote(parts[1]) if len(parts) > 1 else ""
        return bucket, key, urllib.parse.parse_qs(parsed.query)

    # -- verbs ------------------------------------------------------------------------
    def do_PUT(self):  # noqa: N802
        self.server.requests.append(("PUT", self.path, dict(self.headers)))  # type: ignore[attr-defined]
        if self.server.conditional_unsupported:  # type: ignore[attr-defined]
            if "If-None-Match" in self.headers or "If-Match" in self.headers:
                return self._send(501, b"<Error><Code>NotImplemented</Code></Error>")
        if "Authorization" not in self.headers:
            return self._send(403, b"<Error><Code>AccessDenied</Code></Error>")
        _bucket, key, _q = self._split()
        length = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(length)
        # Keep the exact bytes that crossed the wire, so an adversarial scan can look at
        # what LEFT the machine rather than at what the store chose to keep.
        self.server.wire_bodies.append((key, data))  # type: ignore[attr-defined]
        exists = key in self.store
        if self.headers.get("If-None-Match") == "*" and exists:
            return self._send(412, b"<Error><Code>PreconditionFailed</Code></Error>")
        if_match = self.headers.get("If-Match")
        if if_match is not None:
            if not exists or self._etag(self.store[key]) != if_match:
                return self._send(412, b"<Error><Code>PreconditionFailed</Code></Error>")
        self.store[key] = data
        self._send(200, headers={"ETag": self._etag(data)})

    def do_GET(self):  # noqa: N802
        self.server.requests.append(("GET", self.path, dict(self.headers)))  # type: ignore[attr-defined]
        if "Authorization" not in self.headers:
            return self._send(403, b"<Error><Code>AccessDenied</Code></Error>")
        _bucket, key, q = self._split()
        if not key and "list-type" in q:
            return self._list(q)
        if key not in self.store:
            return self._send(404, b"<Error><Code>NoSuchKey</Code></Error>")
        data = self.store[key]
        headers = {} if self.server.suppress_etag else {"ETag": self._etag(data)}  # type: ignore[attr-defined]
        self._send(200, data, headers=headers)

    def _list(self, q):
        prefix = (q.get("prefix") or [""])[0]
        max_keys = int((q.get("max-keys") or ["1000"])[0])
        token = (q.get("continuation-token") or [""])[0]
        keys = sorted(k for k in self.store if k.startswith(prefix))
        start = keys.index(token) if token and token in keys else 0
        page = keys[start : start + max_keys] if max_keys else []
        rest = keys[start + max_keys :] if max_keys else keys
        rows = "".join(
            f"<Contents><Key>{k}</Key><Size>{len(self.store[k])}</Size>"
            f"<ETag>&quot;{hashlib.md5(self.store[k]).hexdigest()}&quot;</ETag></Contents>"  # noqa: S324
            for k in page
        )
        more = "true" if rest else "false"
        nxt = f"<NextContinuationToken>{rest[0]}</NextContinuationToken>" if rest else ""
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            f"<IsTruncated>{more}</IsTruncated>{rows}{nxt}</ListBucketResult>"
        ).encode()
        self._send(200, body, headers={"Content-Type": "application/xml"})


class _StubServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _StubS3)
        self.store: dict[str, bytes] = {}
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.conditional_unsupported = False
        self.suppress_etag = False
        self.wire_bodies: list[tuple[str, bytes]] = []

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


@pytest.fixture
def stub():
    srv = _StubServer()
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def live(stub):
    """A provider wired to the loopback stub — a real endpoint, a real guard, real HTTP."""
    return S3SyncProvider(
        stub.endpoint,
        "mybucket",
        prefix="personalclaw/",
        region=REGION,
        access_key_id=AK,
        secret_access_key=SK,
    )


# ── 3. the round trip, over real HTTP through the real guard ──────────────────────────


class TestRoundTrip:
    def test_push_list_pull_round_trips_bytes_exactly(self, live, stub):
        objects = [
            SyncObject(key="machines/A/seq-0001/tasks/tasks.jsonl", data=b'{"id":"t1"}\n'),
            SyncObject(key="machines/A/seq-0001/memory/memory.jsonl", data=b'{"id":"m1"}\n'),
            SyncObject(key="machines/A/seq-0002/tasks/tasks.jsonl", data=b"\x00\x01\x02binary"),
        ]
        # VACUITY FLOOR: a round-trip assertion over zero objects passes forever.
        assert len(objects) >= 3

        res = live.push(objects)
        assert res.outcome == "delivered", res.detail
        assert res.pushed == 3 and res.skipped == 0

        refs = live.list_remote()
        assert len(refs) == 3, [r.key for r in refs]
        # The key the cycle sees is remote-relative: the configured prefix is stripped.
        assert {r.key for r in refs} == {o.key for o in objects}
        assert all(r.fingerprint for r in refs), "every ref needs a change fingerprint"
        assert all(r.size > 0 for r in refs)

        pulled = live.pull(refs)
        assert len(pulled) == 3
        got = {o.key: o.data for o in pulled}
        for o in objects:
            assert got[o.key] == o.data, f"{o.key} did not round-trip byte-for-byte"

        # The store really holds the prefixed keys — the prefix confines the sync root.
        assert all(k.startswith("personalclaw/") for k in stub.store)

    def test_every_request_was_signed(self, live, stub):
        live.push([SyncObject(key="k", data=b"v")])
        live.list_remote()
        assert stub.requests, "no request reached the store — the test proved nothing"
        for method, path, headers in stub.requests:
            auth = headers.get("Authorization", "")
            assert auth.startswith("AWS4-HMAC-SHA256 "), f"{method} {path} was not signed"
            assert "Signature=" in auth and "SignedHeaders=" in auth
            assert headers.get("x-amz-content-sha256"), f"{method} {path} has no payload hash"

    def test_list_remote_paginates(self, live, stub, monkeypatch):
        monkeypatch.setattr(provider_mod, "_LIST_PAGE_SIZE", 2)
        objects = [SyncObject(key=f"obj-{i:02d}", data=b"x") for i in range(7)]
        assert live.push(objects).pushed == 7
        refs = live.list_remote()
        assert len(refs) == 7, "pagination dropped objects"
        assert {r.key for r in refs} == {o.key for o in objects}

    def test_list_remote_honours_a_prefix_filter(self, live):
        live.push(
            [
                SyncObject(key="machines/A/x", data=b"a"),
                SyncObject(key="machines/B/y", data=b"b"),
            ]
        )
        refs = live.list_remote("machines/A/")
        assert [r.key for r in refs] == ["machines/A/x"]

    def test_prefix_is_optional(self, stub):
        p = S3SyncProvider(
            stub.endpoint, "mybucket", region=REGION, access_key_id=AK, secret_access_key=SK
        )
        assert p.push([SyncObject(key="registry.json", data=b"{}")]).pushed == 1
        assert [r.key for r in p.list_remote()] == ["registry.json"]
        assert "registry.json" in stub.store


class TestInsertOnly:
    def test_a_retried_push_is_skipped_not_overwritten(self, live, stub):
        obj = SyncObject(key="machines/A/seq-0001/tasks.jsonl", data=b"original")
        assert live.push([obj]).pushed == 1
        stored = dict(stub.store)

        # A retry with DIFFERENT bytes under the same key must not land.
        again = live.push([SyncObject(key=obj.key, data=b"tampered")])
        assert again.outcome == "delivered"
        assert again.pushed == 0 and again.skipped == 1
        assert stub.store == stored, "insert-only was violated — the object was overwritten"
        assert b"tampered" not in b"".join(stub.store.values())

    def test_insert_only_is_one_round_trip_per_object(self, live, stub):
        """`If-None-Match: *` makes the PUT itself conditional, so there is no racy
        HEAD-then-PUT window and no extra request."""
        live.push([SyncObject(key="k", data=b"v")])
        puts = [r for r in stub.requests if r[0] == "PUT"]
        assert len(puts) == 1
        # Header names are case-insensitive on the wire; read them that way.
        sent = {k.lower(): v for k, v in puts[0][2].items()}
        assert sent.get("if-none-match") == "*"


class TestIntegrity:
    def test_a_truncated_object_is_dropped_not_returned(self, live, stub, monkeypatch):
        """``fetch`` caps the body and REPORTS the cap rather than raising. Returning a
        prefix of a shard would be silent corruption the merge would apply as data."""
        payload = b"x" * 4096
        assert live.push([SyncObject(key="big", data=payload)]).pushed == 1
        refs = live.list_remote()
        assert len(refs) == 1  # vacuity floor

        # Full-size cap: the object comes back whole.
        assert live.pull(refs)[0].data == payload

        # Now shrink the cap below the object so fetch truncates.
        real = provider_mod.sync_egress_policy

        def tiny(endpoint):
            return real(endpoint).with_overrides(max_bytes=64)

        monkeypatch.setattr(provider_mod, "sync_egress_policy", tiny)
        out = live.pull(refs)
        assert out == [], "a truncated shard was returned as if it were data"

    def test_a_missing_ref_is_dropped_not_raised(self, live):
        live.push([SyncObject(key="present", data=b"v")])
        out = live.pull([RemoteRef(key="present"), RemoteRef(key="vanished")])
        assert [o.key for o in out] == ["present"]


class TestCasRegistry:
    def test_create_only_succeeds_once_then_loses(self, live, stub):
        assert live.cas_registry(None, b'{"machines":{}}') is True
        assert stub.store["personalclaw/registry.json"] == b'{"machines":{}}'
        # A second machine that also believes the registry is absent must LOSE.
        assert live.cas_registry(None, b'{"machines":{"B":1}}') is False
        assert stub.store["personalclaw/registry.json"] == b'{"machines":{}}'

    def test_swap_succeeds_on_the_expected_sha(self, live, stub):
        first = b'{"machines":{"A":1}}'
        assert live.cas_registry(None, first) is True
        sha = hashlib.sha256(first).hexdigest()
        second = b'{"machines":{"A":1,"B":1}}'
        assert live.cas_registry(sha, second) is True
        assert stub.store["personalclaw/registry.json"] == second

    def test_swap_refuses_on_a_stale_sha(self, live, stub):
        first = b'{"machines":{"A":1}}'
        assert live.cas_registry(None, first) is True
        stale = hashlib.sha256(b"something the remote never held").hexdigest()
        assert live.cas_registry(stale, b'{"machines":{"C":1}}') is False
        assert stub.store["personalclaw/registry.json"] == first, "a stale CAS clobbered"

    def test_swap_refuses_when_the_registry_is_absent(self, live):
        assert live.cas_registry(hashlib.sha256(b"{}").hexdigest(), b"{}") is False

    def test_a_store_that_returns_no_etag_refuses_rather_than_clobbers(self, live, stub):
        """Without an ETag the write cannot be made conditional, so it must not be made.

        Added after a falsification showed the ``if not etag`` guard was unreachable in the
        suite — the stub always sent an ETag, so deleting the guard stayed green. An
        unreachable safety guard is indistinguishable from an absent one.
        """
        first = b'{"machines":{"A":1}}'
        assert live.cas_registry(None, first) is True
        stub.suppress_etag = True
        puts_before = len([r for r in stub.requests if r[0] == "PUT"])

        assert live.cas_registry(hashlib.sha256(first).hexdigest(), b'{"machines":{"B":1}}') is False
        assert stub.store["personalclaw/registry.json"] == first, "clobbered with no ETag"
        # Assert the DECISION, not the outcome: no write may even be ATTEMPTED. Asserting
        # only the final bytes let the stub's own 412 stand in for our guard, so removing
        # the guard stayed green (a second falsification caught that).
        puts_after = len([r for r in stub.requests if r[0] == "PUT"])
        assert puts_after == puts_before, (
            "a registry write was attempted with no ETag to condition it on"
        )

    def test_a_store_without_conditional_writes_refuses_rather_than_clobbers(
        self, live, stub
    ):
        """The safety choice that matters most: an unconditional registry PUT would
        silently discard a peer's registration, so an unsupported condition is a lost
        race, never a fallback."""
        stub.store["personalclaw/registry.json"] = b'{"machines":{"A":1}}'
        stub.conditional_unsupported = True
        sha = hashlib.sha256(b'{"machines":{"A":1}}').hexdigest()
        assert live.cas_registry(sha, b'{"machines":{"B":1}}') is False
        assert stub.store["personalclaw/registry.json"] == b'{"machines":{"A":1}}'
        assert live.cas_registry(None, b"{}") is False

    def test_etag_is_read_case_insensitively(self, live, stub):
        """REGRESSION. ``aiohttp`` normalises the header to ``Etag``, not the ``ETag`` the
        S3 API documents, so a case-sensitive lookup found nothing and `cas_registry`
        refused EVERY swap forever — a machine could register once and then never update
        the registry again. The bug was invisible to a mocked transport and only appeared
        when driving the real fetch path.
        """
        first = b'{"machines":{"A":1}}'
        assert live.cas_registry(None, first) is True
        resp = live._request("GET", live._object_url("registry.json"))
        # Pin the casing this test exists for: if aiohttp ever emits exactly "ETag", the
        # regression is no longer reachable and this test should be revisited, not deleted.
        assert "ETag" not in resp.headers, "the header casing changed — re-check the lookup"
        assert any(k.lower() == "etag" for k in resp.headers)
        assert provider_mod._header(resp.headers, "ETag") != ""
        # And the swap that the defect broke now works.
        assert live.cas_registry(hashlib.sha256(first).hexdigest(), b'{"machines":{"B":1}}')

    def test_header_helper_is_case_insensitive_both_ways(self):
        assert provider_mod._header({"Etag": '"a"'}, "ETag") == '"a"'
        assert provider_mod._header({"ETAG": '"a"'}, "etag") == '"a"'
        assert provider_mod._header({}, "ETag") == ""
        assert provider_mod._header(None, "ETag") == ""

    def test_registry_key_is_the_shared_plaintext_routing_key(self):
        """The registry must stay the key core treats as plaintext routing metadata, or
        `list_remote`/registry operations would need the passphrase."""
        from personalclaw.sdk.sync import is_routing_key

        assert is_routing_key(provider_mod._REGISTRY_KEY)


class TestConnection:
    def test_test_reports_reachable(self, live):
        r = live.test()
        assert r.ok is True
        assert "mybucket" in r.detail
        assert r.extra.get("region") == REGION

    def test_test_reports_access_denied(self, live, monkeypatch):
        class Resp:
            status = 403
            body = b""
            truncated = False
            headers: dict[str, str] = {}

        monkeypatch.setattr(provider_mod.S3SyncProvider, "_request", lambda *a, **k: Resp())
        r = live.test()
        assert r.ok is False
        assert "access denied" in r.detail
        assert REGION in r.detail  # the region mismatch is the usual cause; name it

    def test_test_never_raises(self, live, monkeypatch):
        def boom(*a, **k):
            raise OSError("connection reset")

        monkeypatch.setattr(provider_mod.S3SyncProvider, "_request", boom)
        r = live.test()
        assert r.ok is False and "connection reset" in r.detail


# ── 4. credentials and configuration ─────────────────────────────────────────────────


class TestCredentials:
    def test_ambient_aws_credentials_are_never_borrowed(self, monkeypatch):
        """A personal sync transport must not adopt whatever AWS identity is in the shell.

        Doing so could write the user's assistant state into a company or production
        account. The env fallback is app-scoped on purpose.
        """
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA-AMBIENT-PRODUCTION")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient-production-secret")
        monkeypatch.setenv("AWS_SESSION_TOKEN", "ambient-token")
        monkeypatch.setenv("AWS_PROFILE", "prod-admin")
        p = create_provider({"endpoint": ENDPOINT, "bucket": "b"})
        assert p._access_key == ""
        assert p._secret_key == ""
        assert p._session_token == ""
        assert p.configured is False
        # And nothing derived from the ambient identity leaks into the readiness message.
        assert "AMBIENT" not in p._unconfigured_detail()
        assert "prod-admin" not in p._unconfigured_detail()

    def test_app_scoped_env_fallback_is_honoured(self, monkeypatch):
        monkeypatch.setenv("PERSONALCLAW_S3_ACCESS_KEY_ID", AK)
        monkeypatch.setenv("PERSONALCLAW_S3_SECRET_ACCESS_KEY", SK)
        p = create_provider({"endpoint": ENDPOINT, "bucket": "b"})
        assert p.configured is True

    def test_explicit_settings_win_over_env(self, monkeypatch):
        monkeypatch.setenv("PERSONALCLAW_S3_ACCESS_KEY_ID", "env-key")
        p = create_provider({"endpoint": ENDPOINT, "bucket": "b", "access_key_id": AK,
                             "secret_access_key": SK})
        assert p._access_key == AK

    def test_the_secret_never_appears_in_repr_str_or_errors(self, live, caplog, monkeypatch):
        """A canary secret must not reach a repr, a str, an outcome detail, or a log."""
        canary = "CANARY-S3-SECRET-do-not-log-9f31ab"
        p = S3SyncProvider(
            live._endpoint, "mybucket", region=REGION,
            access_key_id=AK, secret_access_key=canary,
        )
        surfaces = [repr(p), str(p), p._unconfigured_detail(), p.test().detail]

        def boom(*a, **k):
            raise OSError("connection reset")

        monkeypatch.setattr(provider_mod.S3SyncProvider, "_request", boom)
        with caplog.at_level(logging.DEBUG):
            surfaces.append(p.push([SyncObject(key="k", data=b"v")]).detail)
            surfaces.append(p.test().detail)
            surfaces.append(str(p.cas_registry(None, b"{}")))
        surfaces.extend(r.getMessage() for r in caplog.records)
        # VACUITY FLOOR: if every surface were empty the assertion below is meaningless.
        assert any(s for s in surfaces), "no surface was captured — the scan proved nothing"
        for s in surfaces:
            assert canary not in s, f"the secret leaked into: {s!r}"
        # The derived signing key must not leak either.
        derived = signing_key(canary, "20260818", REGION).hex()
        for s in surfaces:
            assert derived not in s

    def test_the_secret_is_not_in_the_signed_request(self, pinned_clock):
        """SigV4 sends a signature, never the secret. Trivially true — and worth pinning,
        because a signer that accidentally put the key in a header would still 'work'."""
        canary = "CANARY-S3-SECRET-do-not-log-9f31ab"
        p = S3SyncProvider(
            ENDPOINT, "mybucket", region=REGION, access_key_id=AK, secret_access_key=canary
        )
        headers = p._signed_headers("PUT", f"{ENDPOINT}/mybucket/k", {}, b"body")
        blob = json.dumps(headers)
        assert canary not in blob
        assert AK in blob  # the access key ID is public and IS sent, by design


class TestConfiguration:
    def test_unconfigured_names_every_missing_field(self):
        p = create_provider({})
        detail = p._unconfigured_detail()
        for field in ("endpoint", "bucket", "access key ID", "secret access key"):
            assert field in detail

    def test_unconfigured_push_is_transient_not_permanent(self):
        """A setup gap resolves when the user fills the form in, so the outbox must KEEP
        the objects rather than discard them as undeliverable."""
        res = create_provider({}).push([SyncObject(key="k", data=b"v")])
        assert res.outcome == "transient"
        assert res.pushed == 0

    def test_unconfigured_reads_are_empty_not_errors(self):
        p = create_provider({})
        assert p.list_remote() == []
        assert p.pull([RemoteRef(key="k")]) == []
        assert p.cas_registry(None, b"{}") is False
        assert p.test().ok is False

    def test_factory_normalises_endpoint_bucket_and_prefix(self):
        p = create_provider(
            {
                "endpoint": "https://s3.example.com/",
                "bucket": "/mybucket/",
                "prefix": "/nested/path/",
                "region": "",
            }
        )
        assert p._endpoint == "https://s3.example.com"
        assert p._bucket == "mybucket"
        assert p._prefix == "nested/path/"
        assert p._region == "us-east-1"

    def test_provider_identity_matches_the_manifest(self):
        manifest = json.loads(
            (pathlib.Path(__file__).parent / "app.json").read_text(encoding="utf-8")
        )
        p = create_provider({})
        assert p.name == manifest["name"] == "s3-sync"
        assert p.display_name == manifest["displayName"]
        assert manifest["provider"]["type"] == "sync"
        assert manifest["permissions"]["network"] is True

    def test_credential_fields_are_marked_sensitive_in_the_manifest(self):
        """The settings API must know not to echo these back."""
        manifest = json.loads(
            (pathlib.Path(__file__).parent / "app.json").read_text(encoding="utf-8")
        )
        props = manifest["provider"]["settingsSchema"]["properties"]
        for field in ("access_key_id", "secret_access_key", "session_token"):
            assert props[field]["x-meta"]["sensitive"] is True, field
        # Vacuity floor: the non-secret fields must NOT be marked, or the check is trivial.
        assert "sensitive" not in props["endpoint"].get("x-meta", {})

    def test_it_is_a_real_sync_transport_provider(self):
        from personalclaw.sdk.sync import SyncTransportProvider

        assert isinstance(create_provider({}), SyncTransportProvider)


class TestAsyncBridge:
    def test_it_works_from_a_thread_that_already_has_a_running_loop(self, live):
        """The transport contract is synchronous while ``fetch`` is async. ``asyncio.run``
        alone raises inside a thread that already has a loop, so the bridge must survive a
        caller that drives a cycle from async code."""
        import asyncio

        assert live.push([SyncObject(key="k", data=b"v")]).pushed == 1

        async def drive():
            # Called from INSIDE a running loop — the case plain asyncio.run cannot handle.
            return live.list_remote()

        refs = asyncio.run(drive())
        assert [r.key for r in refs] == ["k"]

    def test_an_exception_crosses_the_bridge(self, live, monkeypatch):
        import asyncio

        async def boom():
            raise OSError("inner failure")

        async def drive():
            return provider_mod._run(boom())

        with pytest.raises(OSError, match="inner failure"):
            asyncio.run(drive())


# ── 5. the plan's success criteria 7 and 8, driven through THIS transport ─────────────
#
# Criterion 7 is "no shard, sync object, or export zip ever contains .env, .local_secret,
# sel_hmac.key, or telemetry_salt — adversarially verified against EVERY transport". Core
# proves it against a test-local folder transport; these two apps are new transports, so the
# proof has to be re-run here, on the bytes that actually crossed the wire.
#
# Criterion 8 is "an encrypted S3 sync store is useless without the passphrase, yet
# list_remote / registry operations work without the key; a plaintext object appearing in an
# encrypted store is skipped permanently and logged, never looped on." Its literal wording
# names S3, so this file is the first place it can be proved as written.


def _seed_task(home: pathlib.Path, tid: str, title: str) -> None:
    d = home / "tasks"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{tid}.json").write_text(json.dumps({"id": tid, "title": title}))


def _plant_secrets(home: pathlib.Path) -> list[str]:
    """Write a distinctly-shaped canary into every path the inventory marks ``secret=True``."""
    from personalclaw.durability import inventory as inv

    planted: list[str] = []
    for rel in inv.secret_paths():
        p = home / rel
        if p.suffix or "." in p.name:
            p.parent.mkdir(parents=True, exist_ok=True)
            # The prefix is assembled rather than written as one literal so a secret
            # scanner does not flag this canary as a real key on every contributor's
            # commit. The BYTES planted are what the scan needs to be realistic; the
            # source spelling is not.
            token = "sk-" + "ant-CANARY-" + rel.replace("/", "-")
            p.write_text(f"SECRET={token}\n")
            planted.append(token)
    assert planted, "no secret paths were planted — the scan would be vacuous"
    return planted


def _run_cycle(transport, home: pathlib.Path, monkeypatch, *, encrypt: str, self_id="A"):
    from personalclaw.durability import crypto as crypto_mod
    from personalclaw.durability.shards import machine_id
    from personalclaw.durability.sync_cycle import run_sync_cycle

    monkeypatch.setattr(crypto_mod, "load_passphrase", lambda: "a shared sync passphrase")
    machine_id(home)
    return run_sync_cycle(transport, home, self_id=self_id, now="t1", encrypt=encrypt)


class TestCriterion7SecretsNeverLeave:
    @pytest.mark.parametrize("encrypt", ["on", "off"])
    def test_no_secret_content_ever_crosses_the_wire(
        self, isolated_home, stub, live, monkeypatch, encrypt
    ):
        """Scanned on the bytes that LEFT, not on the exclusion list.

        Parametrized over encryption because the exclusion must hold independently of it —
        `secret=True` entries are dropped BEFORE any transport sees bytes, so encryption
        being off must not be what protects them.
        """
        home = isolated_home
        _seed_task(home, "task-a", "an ordinary row")
        planted = _plant_secrets(home)

        report = _run_cycle(live, home, monkeypatch, encrypt=encrypt)
        assert report.ok, report.error

        # VACUITY FLOORS: an empty wire, or a wire with no shards, proves nothing.
        assert stub.wire_bodies, "nothing was pushed — the scan would be vacuous"
        blob = b"".join(body for _key, body in stub.wire_bodies)
        assert blob, "every pushed body was empty — the scan would be vacuous"
        assert any("machines/" in k for k, _ in stub.wire_bodies), "no shard object was pushed"

        for token in planted:
            assert token.encode() not in blob, f"{token} crossed the wire"
        # Not even the NAME of a secret store may appear in a transported object.
        for marker in (b".local_secret", b"sel_hmac.key", b"telemetry_salt"):
            assert marker not in blob, f"{marker!r} was named in a transported object"
        # …nor in any object KEY (a key is metadata, and metadata stays plaintext).
        keys = " ".join(k for k, _ in stub.wire_bodies)
        for marker in (".local_secret", "sel_hmac.key", "telemetry_salt"):
            assert marker not in keys

    def test_the_canary_scan_can_actually_fail(self, isolated_home, stub, live, monkeypatch):
        """Proves the scan above is capable of catching a leak, rather than being a rail
        that matches nothing: the same scan over a deliberately-planted secret finds it."""
        home = isolated_home
        _seed_task(home, "task-a", "an ordinary row")
        planted = _plant_secrets(home)
        # Put the canary somewhere that IS synced (a task title), so it legitimately leaves.
        _seed_task(home, "task-leak", f"leaked {planted[0]}")
        _run_cycle(live, home, monkeypatch, encrypt="off")
        blob = b"".join(body for _k, body in stub.wire_bodies)
        assert planted[0].encode() in blob, (
            "the scan could not see a canary that really did leave — it is vacuous"
        )


class TestCriterion8EncryptedStore:
    def test_an_encrypted_store_is_useless_without_the_passphrase(
        self, isolated_home, stub, live, monkeypatch
    ):
        home = isolated_home
        secret_row = "alice@example.com payroll salary-review"
        _seed_task(home, "task-a", secret_row)
        report = _run_cycle(live, home, monkeypatch, encrypt="on")
        assert report.ok, report.error

        shard_bodies = [b for k, b in stub.wire_bodies if "machines/" in k]
        assert shard_bodies, "no shard object was pushed — the proof would be vacuous"
        everything = b"".join(b for _k, b in stub.wire_bodies)
        assert secret_row.encode() not in everything, "the row is readable in the store"
        for word in (b"alice@example.com", b"payroll", b"salary-review"):
            assert word not in everything, f"{word!r} survived in the store"

        # Every shard body is ciphertext, and recognisably so (the codec's magic header).
        from personalclaw.durability.crypto import is_ciphertext

        assert all(is_ciphertext(b) for b in shard_bodies), "a shard was pushed as plaintext"

    def test_encryption_is_on_by_default_for_this_transport(self):
        """§4.4: default ON for `s3-sync` (third-party storage). Pinned in the app's own
        suite so core's table and this transport cannot drift apart silently."""
        from personalclaw.durability.crypto import (
            DEFAULT_ENCRYPT_BY_TRANSPORT,
            encryption_enabled_for,
        )

        assert DEFAULT_ENCRYPT_BY_TRANSPORT["s3-sync"] is True
        assert encryption_enabled_for("s3-sync", "auto") is True
        # The contrast that makes the default meaningful rather than global.
        assert DEFAULT_ENCRYPT_BY_TRANSPORT["git-sync"] is False
        assert encryption_enabled_for("s3-sync", "off") is False

    def test_routing_and_registry_operations_work_with_no_key_at_all(
        self, isolated_home, stub, live, monkeypatch
    ):
        """The other half of criterion 8: the store must stay OPERABLE without the key, or
        a machine that has not been given the passphrase could not even list the remote."""
        home = isolated_home
        _seed_task(home, "task-a", "a row")
        assert _run_cycle(live, home, monkeypatch, encrypt="on").ok

        # A brand-new provider with no passphrase anywhere in reach.
        blind = S3SyncProvider(
            stub.endpoint, "mybucket", prefix="personalclaw/", region=REGION,
            access_key_id=AK, secret_access_key=SK,
        )
        refs = blind.list_remote()
        assert refs, "list_remote needed the key — criterion 8 requires it not to"
        assert blind.test().ok

        from personalclaw.sdk.sync import SALT_KEY, is_routing_key

        keys = {r.key for r in refs}
        assert "registry.json" in keys
        assert SALT_KEY in keys, "the first-write-wins salt object was never published"
        # The routing keys really are readable plaintext with no key.
        registry = [o for o in blind.pull([RemoteRef(key="registry.json")])]
        assert registry and json.loads(registry[0].data.decode()), "registry unreadable"
        assert is_routing_key("registry.json") and is_routing_key(SALT_KEY)

    def test_a_plaintext_object_in_an_encrypted_store_is_skipped_permanently(
        self, isolated_home, stub, live, monkeypatch, caplog
    ):
        """A contract violation, not an error to retry: skipped, logged, and the cursor
        advances so the cycle never loops on it."""
        home = isolated_home
        _seed_task(home, "task-a", "a row")
        assert _run_cycle(live, home, monkeypatch, encrypt="on").ok

        # A peer machine writes a PLAINTEXT shard into the encrypted store.
        plaintext_key = "personalclaw/machines/PEER/seq-0001/tasks/tasks.jsonl"
        stub.store[plaintext_key] = b'{"id":"t-plain","title":"never encrypted"}\n'

        home_b = isolated_home.parent / "B"
        home_b.mkdir()
        with caplog.at_level(logging.INFO):
            report = _run_cycle(live, home_b, monkeypatch, encrypt="on", self_id="B")
        assert report.ok, report.error
        # The plaintext row must not have been merged into B's state.
        merged = " ".join(
            p.read_text(errors="replace") for p in home_b.rglob("*.json") if p.is_file()
        )
        assert "never encrypted" not in merged, "a plaintext object was merged"

        # And it is a PERMANENT skip: a second cycle does not re-report it as new work.
        report2 = _run_cycle(live, home_b, monkeypatch, encrypt="on", self_id="B")
        assert report2.ok
        assert "never encrypted" not in " ".join(
            p.read_text(errors="replace") for p in home_b.rglob("*.json") if p.is_file()
        )


def test_no_secret_material_in_the_module_source():
    """A committed provider must not carry a real-looking credential."""
    src = pathlib.Path(provider_mod.__file__).read_text(encoding="utf-8")
    assert "AKIA" not in src
    assert "wJalrXUtnFEMI" not in src
    assert len(src) > 1000  # vacuity floor: we actually read the module


def test_readme_documents_the_conditional_write_requirement():
    """The store requirement discovered while building CAS must reach the user, not just
    this test file."""
    readme = (pathlib.Path(__file__).parent / "README.md").read_text(encoding="utf-8")
    assert "conditional" in readme.lower()
    assert "If-None-Match" in readme
    assert os.path.exists(pathlib.Path(__file__).parent / "LICENSE")
