"""S3 sync transport — carries durability shard objects through an S3-compatible store.

Point every machine's s3-sync at the same bucket + prefix and the durability layer
converges through it. The transport moves bytes only; the merge, the machine-seq registry
contents, and the outbox all live above it in core, and **encryption is applied above it
too** — by the sync cycle, at the transport boundary — so this module never sees a key, a
passphrase, or a plaintext shard it could accidentally log.

Two properties are worth stating up front, because they are the reason this app is shaped
the way it is rather than as a thin boto3 wrapper:

**Every request goes through ``sdk.net.fetch`` under ``sync_egress_policy(endpoint)``.**
Never a hand-rolled ``aiohttp``/``httpx``/``boto3`` client. That derived policy is
host-pinned to the one configured endpoint, carries the operator's ``security.egress``
posture, denies the cloud metadata services, and raises (does not remove) the body cap.
Consequences that are easy to miss and are load-bearing here:

* **Path-style addressing is mandatory, not a preference.** Virtual-host style
  (``https://<bucket>.s3.../<key>``) puts the bucket in the *hostname*, which is not the
  host the policy pinned — so every request would be refused by the guard. Keys are
  therefore addressed as ``<endpoint>/<bucket>/<key>``, which is also what MinIO and most
  compatible stores prefer.
* **A truncated body is an integrity failure, never data.** ``fetch`` caps the body at
  ``policy.max_bytes`` and reports ``truncated=True`` rather than raising. A silently
  short shard is corruption, so :meth:`pull` drops any truncated object instead of
  handing back a prefix of it.

**Credentials are explicit, never ambient.** The env fallbacks are
``PERSONALCLAW_S3_*`` — deliberately NOT ``AWS_ACCESS_KEY_ID`` / ``AWS_PROFILE`` / the
instance-role chain. A personal sync transport that silently adopted whatever AWS identity
happened to be in the operator's shell could write the user's assistant state into a
company or production account that neither they nor we intended; the metadata-service
denial in the ``SYNC`` policy closes the same hole from the other side. Configure the keys
or the transport stays idle.
"""

import asyncio
import hashlib
import hmac
import os
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlparse

from personalclaw.sdk.sync import (
    ConnectionResult,
    PushResult,
    RemoteRef,
    SyncObject,
    SyncTransportProvider,
    sync_egress_policy,
)

#: The single shared registry object every machine compare-and-swaps. Matches dir-sync and
#: git-sync; core's ``ROUTING_KEYS`` names it as a plaintext routing key.
_REGISTRY_KEY = "registry.json"

#: SigV4 constants. ``s3`` is the signing service name; the algorithm label is fixed.
_ALGORITHM = "AWS4-HMAC-SHA256"
_SERVICE = "s3"

#: How many keys one ListObjectsV2 page asks for. The transport paginates, so this only
#: trades round trips against response size.
_LIST_PAGE_SIZE = 1000


def _utcnow() -> datetime:
    """Current UTC time. Separate function so a test can pin the signing timestamp."""
    return datetime.now(timezone.utc)


def _header(headers: Any, name: str) -> str:
    """Case-insensitively read one response header.

    HTTP header names are case-insensitive and clients normalise them differently —
    ``aiohttp`` hands back ``Etag``, not the ``ETag`` the S3 API documents. A
    case-SENSITIVE lookup here silently returned ``""`` for every ETag, which made
    :meth:`S3SyncProvider.cas_registry` refuse every registry swap forever: sync would
    register a machine once and then never be able to update the registry again. Found by
    driving the real fetch path against a store, which is the only place the casing shows up.
    """
    if not headers:
        return ""
    for key, value in headers.items():
        if key.lower() == name.lower():
            return str(value).strip()
    return ""


def _uri_encode(value: str, *, encode_slash: bool = True) -> str:
    """RFC 3986 percent-encoding as SigV4 defines it (``UriEncode``).

    Unreserved characters (``A-Za-z0-9-._~``) pass through; everything else is encoded as
    uppercase ``%XX``. ``encode_slash=False`` is used for the canonical *path*, where ``/``
    is a real separator — S3 signs the path encoded exactly ONCE, so the object key's own
    special characters are encoded here and nowhere else.
    """
    safe = "-._~" if encode_slash else "-._~/"
    return quote(value, safe=safe)


def _sign(key: bytes, msg: str) -> bytes:
    """One HMAC-SHA256 link of the SigV4 signing-key chain."""
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret_access_key: str, datestamp: str, region: str) -> bytes:
    """Derive the SigV4 signing key: ``AWS4<secret>`` → date → region → service → terminator.

    Exposed (rather than inlined) so a test can pin the derivation against an independent
    implementation without reaching into a private helper. It returns raw key material, so
    it must never be logged or rendered.
    """
    k_date = _sign(f"AWS4{secret_access_key}".encode(), datestamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, _SERVICE)
    return _sign(k_service, "aws4_request")


def canonical_request(
    method: str,
    path: str,
    query: dict[str, str],
    headers: dict[str, str],
    payload_sha256: str,
) -> tuple[str, str]:
    """Build the SigV4 canonical request and its signed-header list.

    Returns ``(canonical_request, signed_headers)``. ``headers`` is signed in full: every
    header handed here ends up in ``SignedHeaders``, so the caller decides what is covered.
    ``host`` and ``x-amz-content-sha256`` are always among them, which is what binds a
    signature to one endpoint and one exact payload.
    """
    canonical_uri = _uri_encode(path, encode_slash=False)
    # Query string: sorted by key, key AND value percent-encoded, joined with "&".
    canonical_query = "&".join(
        f"{_uri_encode(k)}={_uri_encode(v)}" for k, v in sorted(query.items())
    )
    lowered = {k.lower().strip(): " ".join(str(v).split()) for k, v in headers.items()}
    canonical_headers = "".join(f"{k}:{lowered[k]}\n" for k in sorted(lowered))
    signed_headers = ";".join(sorted(lowered))
    creq = "\n".join(
        [
            method.upper(),
            canonical_uri,
            canonical_query,
            canonical_headers,
            signed_headers,
            payload_sha256,
        ]
    )
    return creq, signed_headers


class S3SyncProvider(SyncTransportProvider):
    """A durability sync transport backed by an S3-compatible object store."""

    name = "s3-sync"
    display_name = "S3 Sync"

    def __init__(
        self,
        endpoint: str = "",
        bucket: str = "",
        *,
        prefix: str = "",
        region: str = "us-east-1",
        access_key_id: str = "",
        secret_access_key: str = "",
        session_token: str = "",
    ) -> None:
        self._endpoint = (endpoint or "").strip().rstrip("/")
        self._bucket = (bucket or "").strip().strip("/")
        # Normalise the prefix to "" or "some/path/" so key joining is a plain concat.
        pfx = (prefix or "").strip().strip("/")
        self._prefix = f"{pfx}/" if pfx else ""
        self._region = (region or "us-east-1").strip() or "us-east-1"
        # Explicit settings win; the fallback env names are app-scoped ON PURPOSE (see the
        # module docstring) so an ambient AWS identity is never borrowed.
        self._access_key = access_key_id or os.environ.get("PERSONALCLAW_S3_ACCESS_KEY_ID", "")
        self._secret_key = secret_access_key or os.environ.get(
            "PERSONALCLAW_S3_SECRET_ACCESS_KEY", ""
        )
        self._session_token = session_token or os.environ.get("PERSONALCLAW_S3_SESSION_TOKEN", "")

    # ── configuration / readiness ────────────────────────────────────────────────────

    @property
    def configured(self) -> bool:
        """True when endpoint, bucket and both credential halves are all present."""
        return bool(self._endpoint and self._bucket and self._access_key and self._secret_key)

    def _unconfigured_detail(self) -> str:
        """Which specific setting is missing — a setup error names the field, not 'failed'."""
        missing = [
            name
            for name, value in (
                ("endpoint", self._endpoint),
                ("bucket", self._bucket),
                ("access key ID", self._access_key),
                ("secret access key", self._secret_key),
            )
            if not value
        ]
        return f"s3-sync is not configured — missing: {', '.join(missing)}"

    # ── the guarded request path ─────────────────────────────────────────────────────

    def _object_url(self, key: str) -> str:
        """Path-style URL for one object key (see the module docstring on why path-style)."""
        full = f"{self._prefix}{key}"
        # Encode each segment; "/" stays a separator so nested keys are real S3 paths.
        return f"{self._endpoint}/{self._bucket}/{_uri_encode(full, encode_slash=False)}"

    def _signed_headers(
        self,
        method: str,
        url: str,
        query: dict[str, str],
        payload: bytes,
        extra: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Sign one request, returning the full header set to hand to ``fetch``.

        Every header returned here is covered by the signature except the ones the HTTP
        client adds itself (``User-Agent``, ``Content-Length``) — those are deliberately
        NOT signed, because we do not control them and a mismatch would break every
        request for no security gain.
        """
        parsed = urlparse(url)
        host = parsed.netloc  # host:port — the signed value must match the Host header sent
        now = _utcnow()
        amzdate = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(payload).hexdigest()

        headers = {
            "host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amzdate,
        }
        if self._session_token:
            # STS credentials must cover the token, or the store rejects the signature.
            headers["x-amz-security-token"] = self._session_token
        if extra:
            headers.update({k.lower(): v for k, v in extra.items()})

        creq, signed = canonical_request(method, parsed.path, query, headers, payload_hash)
        scope = f"{datestamp}/{self._region}/{_SERVICE}/aws4_request"
        string_to_sign = "\n".join(
            [_ALGORITHM, amzdate, scope, hashlib.sha256(creq.encode("utf-8")).hexdigest()]
        )
        signature = hmac.new(
            signing_key(self._secret_key, datestamp, self._region),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers["Authorization"] = (
            f"{_ALGORITHM} Credential={self._access_key}/{scope}, "
            f"SignedHeaders={signed}, Signature={signature}"
        )
        return headers

    def _request(
        self,
        method: str,
        url: str,
        *,
        query: dict[str, str] | None = None,
        payload: bytes = b"",
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        """Sign and perform one request through the guarded egress chokepoint.

        Returns the ``FetchResponse``. Raises whatever ``fetch`` raises (notably
        ``EgressBlocked`` and ``SyncEndpointRefused``) — callers turn those into typed
        transport outcomes rather than letting them escape into the sync cycle.
        """
        from personalclaw.sdk.net import fetch

        query = query or {}
        # The policy is derived per request from the CONFIGURED endpoint, never cached and
        # never hand-built: the operator can change `security.egress` under a long-lived
        # process and the next request must reflect it.
        policy = sync_egress_policy(self._endpoint)
        full_url = url
        if query:
            qs = "&".join(
                f"{_uri_encode(k)}={_uri_encode(v)}" for k, v in sorted(query.items())
            )
            full_url = f"{url}?{qs}"
        headers = self._signed_headers(method, url, query, payload, extra_headers)
        return _run(fetch(full_url, policy=policy, method=method, headers=headers, data=payload))

    # ── SyncTransportProvider contract ───────────────────────────────────────────────

    def push(self, objects: list[SyncObject]) -> PushResult:
        if not self.configured:
            # A setup gap is transient, not permanent: it resolves when the user fills the
            # settings in, and the outbox should keep the objects rather than drop them.
            return PushResult(outcome="transient", detail=self._unconfigured_detail())
        pushed = skipped = 0
        for obj in objects:
            try:
                # Insert-only in ONE round trip: `If-None-Match: *` makes the PUT succeed
                # only if the key does not exist, so a retried push is a 412, not an
                # overwrite. A HEAD-then-PUT would be two requests AND racy.
                resp = self._request(
                    "PUT",
                    self._object_url(obj.key),
                    payload=obj.data,
                    extra_headers={"if-none-match": "*"},
                )
            except Exception as e:  # noqa: BLE001 — every failure becomes a typed outcome
                return PushResult(
                    pushed=pushed,
                    skipped=skipped,
                    outcome=_outcome_for(e),
                    detail=f"{type(e).__name__}: {e}",
                )
            if resp.status in (412, 409):
                # Already present — insert-only means this is a no-op, not a failure.
                skipped += 1
                continue
            if 200 <= resp.status < 300:
                pushed += 1
                continue
            return PushResult(
                pushed=pushed,
                skipped=skipped,
                outcome=_outcome_for_status(resp.status),
                detail=f"PUT {obj.key} failed (HTTP {resp.status})",
            )
        return PushResult(pushed=pushed, skipped=skipped, outcome="delivered")

    def list_remote(self, prefix: str = "") -> list[RemoteRef]:
        # An unconfigured or unreachable store is an EMPTY remote, not an exception: a
        # fresh machine legitimately has nothing there yet, and the cycle reconciles.
        if not self.configured:
            return []
        refs: list[RemoteRef] = []
        token = ""
        base = f"{self._endpoint}/{self._bucket}"
        while True:
            query = {
                "list-type": "2",
                "max-keys": str(_LIST_PAGE_SIZE),
                "prefix": f"{self._prefix}{prefix}",
            }
            if token:
                query["continuation-token"] = token
            try:
                resp = self._request("GET", base, query=query)
            except Exception:  # noqa: BLE001 — an unreachable store lists as empty
                return refs
            if not (200 <= resp.status < 300):
                return refs
            try:
                root = ET.fromstring(resp.body)
            except ET.ParseError:
                return refs
            for node in root.findall("{*}Contents"):
                key = (node.findtext("{*}Key") or "").strip()
                if not key:
                    continue
                # Strip the configured prefix so the key the cycle sees is remote-relative,
                # exactly the key it pushed — the round-trip contract in `SyncObject`.
                if self._prefix:
                    if not key.startswith(self._prefix):
                        continue
                    key = key[len(self._prefix) :]
                if not key:
                    continue
                try:
                    size = int((node.findtext("{*}Size") or "0").strip() or 0)
                except ValueError:
                    size = 0
                # ETag is the store's own change fingerprint; the cycle compares, never
                # parses it, so the quoting the API includes is stripped for cleanliness.
                etag = (node.findtext("{*}ETag") or "").strip().strip('"')
                refs.append(RemoteRef(key=key, size=size, fingerprint=etag))
            if (root.findtext("{*}IsTruncated") or "").strip().lower() != "true":
                break
            token = (root.findtext("{*}NextContinuationToken") or "").strip()
            if not token:
                break
        return refs

    def pull(self, refs: list[RemoteRef]) -> list[SyncObject]:
        if not self.configured:
            return []
        out: list[SyncObject] = []
        for ref in refs:
            try:
                resp = self._request("GET", self._object_url(ref.key))
            except Exception:  # noqa: BLE001 — a ref we cannot fetch is dropped, not raised
                continue
            if not (200 <= resp.status < 300):
                continue
            if resp.truncated:
                # `fetch` caps the body at policy.max_bytes and REPORTS the cap rather than
                # raising. Handing back a prefix of a shard would be silent corruption that
                # the merge would happily apply, so a truncated object is dropped. It is
                # reported as an absence, which the cycle retries, not as data.
                continue
            out.append(SyncObject(key=ref.key, data=resp.body))
        return out

    def cas_registry(self, expected_sha: str | None, data: bytes) -> bool:
        """Compare-and-swap ``registry.json`` using S3 conditional writes.

        ``expected_sha is None`` (expect absent) becomes ``If-None-Match: *``; an expected
        sha becomes a read (to learn the current ETag and verify the sha the caller
        expected) followed by ``If-Match: <etag>``. Both conditions are evaluated by the
        store, so two machines racing cannot both win.

        A store that does NOT implement conditional writes makes this return ``False``
        (a lost race) rather than falling back to an unconditional PUT. That is deliberate:
        an unconditional registry write silently discards the other machine's registration,
        and a visibly stalled CAS is a far better failure than losing a peer's state.
        """
        if not self.configured:
            return False
        url = self._object_url(_REGISTRY_KEY)
        condition: dict[str, str]
        if expected_sha is None:
            condition = {"if-none-match": "*"}
        else:
            try:
                current = self._request("GET", url)
            except Exception:  # noqa: BLE001
                return False
            if current.status == 404:
                # Caller expected a specific sha but the registry is gone — a lost race.
                return False
            if not (200 <= current.status < 300) or current.truncated:
                return False
            if hashlib.sha256(current.body).hexdigest() != expected_sha:
                # Someone else swapped it since the caller read it: re-pull and retry.
                return False
            etag = _header(current.headers, "ETag")
            if not etag:
                # No ETag means we cannot make the write conditional, and an unconditional
                # one could clobber a peer. Refuse.
                return False
            condition = {"if-match": etag}
        try:
            resp = self._request("PUT", url, payload=data, extra_headers=condition)
        except Exception:  # noqa: BLE001
            return False
        if 200 <= resp.status < 300:
            return True
        # 412 = the condition failed (a real lost race). 501/400 = the store does not
        # support conditional writes; both refuse rather than clobber.
        return False

    def test(self) -> ConnectionResult:
        if not self.configured:
            return ConnectionResult(ok=False, detail=self._unconfigured_detail())
        # A zero-key LIST is the cheapest request that exercises DNS, the egress pin, TLS,
        # the signature and the bucket policy all at once.
        try:
            resp = self._request(
                "GET",
                f"{self._endpoint}/{self._bucket}",
                query={"list-type": "2", "max-keys": "0", "prefix": self._prefix},
            )
        except Exception as e:  # noqa: BLE001 — a probe never raises
            return ConnectionResult(ok=False, detail=f"{type(e).__name__}: {e}")
        if 200 <= resp.status < 300:
            where = f"{self._bucket}/{self._prefix}" if self._prefix else self._bucket
            return ConnectionResult(
                ok=True,
                detail=f"bucket reachable at {where}",
                extra={"endpoint": self._endpoint, "region": self._region},
            )
        if resp.status in (401, 403):
            return ConnectionResult(
                ok=False,
                detail=(
                    f"access denied (HTTP {resp.status}) — check the access key, its policy "
                    f"for this bucket, and that the region ({self._region}) matches the bucket"
                ),
            )
        if resp.status == 404:
            return ConnectionResult(
                ok=False, detail=f"bucket {self._bucket!r} not found at {self._endpoint}"
            )
        return ConnectionResult(ok=False, detail=f"unexpected response (HTTP {resp.status})")


def _outcome_for_status(status: int) -> str:
    """Map an HTTP status to the outbox's typed verdict.

    Auth/permission and malformed-request failures are ``permanent`` — retrying an
    unauthorized PUT forever is the error loop §4.4 bans. Everything else (throttling,
    5xx, a store mid-restart) is ``transient``.
    """
    if status in (400, 401, 403, 405) or status == 501:
        return "permanent"
    return "transient"


def _outcome_for(exc: BaseException) -> str:
    """Map an exception to the outbox's typed verdict.

    An egress refusal or an unusable endpoint is a **configuration** fault, which retrying
    cannot fix — it is permanent until the operator changes a setting. Network errors are
    transient.
    """
    from personalclaw.sdk.net import EgressBlocked

    from personalclaw.sdk.sync import SyncEndpointRefused  # noqa: PLC0415

    if isinstance(exc, (EgressBlocked, SyncEndpointRefused)):
        return "permanent"
    return "transient"


def _run(coro: Any) -> Any:
    """Run one coroutine to completion from this synchronous transport method.

    ``SyncTransportProvider`` is a synchronous contract (core's sync cycle is a plain
    function run on a job thread) while ``sdk.net.fetch`` is async, so a bridge is
    unavoidable. ``asyncio.run`` alone is NOT enough: it raises if the calling thread
    already has a running loop, which is exactly what would happen if a future caller
    drove a cycle from a route handler. So a loop-bearing thread hands the coroutine to a
    dedicated worker thread with its own loop, and the common (job-thread) case stays a
    plain ``asyncio.run``.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, Any] = {}

    def _worker() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as e:  # noqa: BLE001 — re-raised on the calling thread below
            result["error"] = e

    t = threading.Thread(target=_worker, name="s3-sync-fetch", daemon=True)
    t.start()
    t.join()
    if "error" in result:
        raise result["error"]
    return result.get("value")


def create_provider(config: dict[str, Any] | None = None) -> S3SyncProvider:
    """Extension factory — builds the S3 transport from user settings."""
    config = config or {}
    return S3SyncProvider(
        endpoint=str(config.get("endpoint", "") or ""),
        bucket=str(config.get("bucket", "") or ""),
        prefix=str(config.get("prefix", "") or ""),
        region=str(config.get("region", "") or "us-east-1"),
        access_key_id=str(config.get("access_key_id", "") or ""),
        secret_access_key=str(config.get("secret_access_key", "") or ""),
        session_token=str(config.get("session_token", "") or ""),
    )
