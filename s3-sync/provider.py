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
  short shard is corruption, so :meth:`pull` refuses any truncated object — it raises,
  naming the object and the cap — instead of handing back a prefix of it.

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
import socket
import ssl
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlparse

from personalclaw.sdk.net import sentence_with_detail
from personalclaw.sdk.sync import (
    ConnectionResult,
    PushResult,
    RemoteRef,
    SyncObject,
    SyncTransportProvider,
    is_routing_key,
    sync_egress_policy,
)

#: The single shared registry object every machine compare-and-swaps. Matches dir-sync and
#: git-sync; core's ``ROUTING_KEYS`` names it as a plaintext routing key.
_REGISTRY_KEY = "registry.json"

#: Where this transport's own settings (Endpoint URL, Bucket, Region, the access keys) are set.
_ON_CARD = "on the S3 Sync card in Settings → Providers"
#: Said after a failure the sync cycle retries (a ``transient`` outcome).
_RETRIES = "Sync tries again on its next run."

#: SigV4 constants. ``s3`` is the signing service name; the algorithm label is fixed.
_ALGORITHM = "AWS4-HMAC-SHA256"
_SERVICE = "s3"

#: How many keys one ListObjectsV2 page asks for. The transport paginates, so this only
#: trades round trips against response size.
_LIST_PAGE_SIZE = 1000

#: S3's codes for the 409 it answers a conditional write with while another conditional write
#: to the same key is still in progress. That is not "already present" (a 412): the key may not
#: exist yet, so the write is tried again. ``""`` is a 409 that came with no S3 error body.
_WRITE_IN_PROGRESS = ("ConditionalRequestConflict", "OperationAborted", "")


class S3RequestFailed(RuntimeError):
    """A request a listing, a read or the registry swap needed that didn't get its answer — it
    raised, the store refused it, or what came back can't be used — said as what is wrong and
    what to do, then the store's (or the request's) own words.

    ``list_remote``, ``pull`` and ``cas_registry`` raise it, having no outcome to carry a
    sentence. Answering empty, or ``False``, instead read as a bucket with nothing in it, or as a
    swap another machine won: the sync cycle took an empty registry, published as if this were
    the first machine, and reported its registry swap lost five times over — to no other machine
    at all. Raised, the cycle records it as its failure, in this text.
    """

    def __init__(self, sentence: str, words: object) -> None:
        super().__init__(sentence_with_detail(sentence, words))
        self.sentence = sentence


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

    def _policy(self) -> Any:
        """The egress policy every request runs under. Derived per request from the CONFIGURED
        endpoint, never cached and never hand-built: the operator can change
        ``security.egress`` under a long-lived process and the next request must reflect it."""
        return sync_egress_policy(self._endpoint)

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
        ``EgressBlocked`` and ``SyncEndpointRefused``) — callers turn those into a push's typed
        outcome, or into :class:`S3RequestFailed` said as what went wrong, and never let the
        bare exception escape into the sync cycle.
        """
        from personalclaw.sdk.net import fetch

        query = query or {}
        policy = self._policy()
        full_url = url
        if query:
            qs = "&".join(
                f"{_uri_encode(k)}={_uri_encode(v)}" for k, v in sorted(query.items())
            )
            full_url = f"{url}?{qs}"
        headers = self._signed_headers(method, url, query, payload, extra_headers)
        return _run(fetch(full_url, policy=policy, method=method, headers=headers, data=payload))

    def _request_failed(self, exc: BaseException) -> str:
        """What a request that raised instead of answering means, and what to do — the
        exception's own words ("Cannot connect to host …", a guard's reason) say neither.

        The sentence alone: the caller says whether the cycle retries, and adds those words.
        Classified on the standard-library cause an HTTP client error wraps (``os_error``,
        ``certificate_error``), since this module imports no HTTP client of its own."""
        from personalclaw.sdk.net import EgressBlocked

        from personalclaw.sdk.sync import SyncEndpointRefused  # noqa: PLC0415

        endpoint = self._endpoint
        host = urlparse(endpoint).hostname or endpoint
        not_found = (
            f"{host}, the host in Endpoint URL, can't be found from this machine. Check Endpoint "
            f"URL {_ON_CARD}, and that this machine is online."
        )
        if isinstance(exc, SyncEndpointRefused):
            return (
                f"PersonalClaw won't use {endpoint} as a sync endpoint. Set Endpoint URL "
                f"{_ON_CARD} to your store's http:// or https:// address — or, if its host is "
                "under Denied hosts in Settings → Security, take it off that list."
            )
        if isinstance(exc, EgressBlocked):
            category = getattr(getattr(exc, "decision", None), "category", "")
            if category == "unresolvable":
                return not_found
            if category in ("metadata", "link_local"):
                return (
                    f"{host}, the host in Endpoint URL, points at a cloud metadata or link-local "
                    "address, which PersonalClaw never lets anything reach. Check Endpoint URL "
                    f"{_ON_CARD}, and that host's DNS record."
                )
            if category == "not_listed":
                return (
                    f"The store at {endpoint} sent S3 Sync on to a different host, and a sync "
                    "transport only reaches the host in its Endpoint URL. Set Endpoint URL "
                    f"{_ON_CARD} to the address the store redirects to."
                )
            return (
                "PersonalClaw's network egress rules stopped a request S3 Sync made to "
                f"{endpoint}, or to where the store redirected it. Check that Endpoint URL "
                f"{_ON_CARD} is the store's own address, and Network egress in Settings → "
                "Security."
            )
        if type(exc).__name__ == "LiveWriteDisabled":
            # Core's live-writes refusal is not published through the SDK, so it is known by
            # its name here; its own words (which name the variable) follow as the detail.
            return (
                "PersonalClaw is running with live writes turned off "
                "(PERSONALCLAW_DISABLE_LIVE_WRITES is set), so S3 Sync may not write to the "
                "store. Unset it and restart PersonalClaw to sync."
            )
        cause = getattr(exc, "os_error", None) or getattr(exc, "certificate_error", None) or exc
        if isinstance(cause, ConnectionRefusedError):
            return (
                f"The store at {endpoint} refused the connection. Check that it is running, and "
                f"that Endpoint URL {_ON_CARD} has the right host and port."
            )
        if isinstance(cause, TimeoutError):
            return (
                f"The store at {endpoint} didn't answer in time. Check that it is reachable from "
                f"this machine, and that Endpoint URL {_ON_CARD} is right."
            )
        if isinstance(cause, socket.gaierror):
            return not_found
        if isinstance(cause, ssl.SSLError):
            return (
                f"S3 Sync couldn't make a secure connection to {endpoint}: the TLS handshake "
                "failed, or this machine doesn't trust the store's certificate. Check that "
                f"Endpoint URL {_ON_CARD} is right, and that the store's certificate is valid "
                "for that host."
            )
        return (
            f"S3 Sync's request to the store at {endpoint} didn't complete. Check that the store "
            f"is running and reachable from this machine, and the settings {_ON_CARD}."
        )

    def _store_refused(
        self,
        status: int,
        code: str,
        *,
        action: str = "write",
        key: str = "",
        condition: str = "If-None-Match",
    ) -> str:
        """What a store's non-2xx answer means, and what to do — by S3's own error ``code``
        where that pins the fix to one setting, else by the status's class. ``action`` is what S3
        Sync asked for: a ``write`` (a push, or the registry swap, conditional on the
        ``condition`` header), a ``read`` of the object ``key``, or a ``list`` of the bucket — any
        listing, the connection test's among them. The sentence alone: the caller says whether
        the cycle retries, and adds the store's words."""
        endpoint, bucket = self._endpoint, self._bucket
        request = {"write": "write", "read": "read", "list": "request"}[action]
        if status == 409 and action == "write" and code in _WRITE_IN_PROGRESS:
            return (
                f"The store at {endpoint} was still busy with another conditional write to the "
                "same object when S3 Sync's write arrived, so it turned this one away."
            )
        if code == "InvalidAccessKeyId":
            return (
                f"The store at {endpoint} doesn't recognise the access key ID S3 Sync signs with. "
                f"Check Access key ID {_ON_CARD}."
            )
        if code == "SignatureDoesNotMatch":
            # Some S3-compatible stores answer a signature made for the wrong region this way,
            # rather than with AuthorizationHeaderMalformed naming the region they expect.
            return (
                f"The store at {endpoint} didn't accept S3 Sync's request signature: the secret "
                "access key doesn't belong to the access key ID, or — on some S3-compatible "
                f"stores — Region ({self._region}) isn't the one the store is set up for. Check "
                f"Secret access key {_ON_CARD}; if it is right, set Region there to the store's "
                "region."
            )
        if code == "RequestTimeTooSkewed":
            return (
                f"The store at {endpoint} refused S3 Sync's request because this machine's clock "
                "is too far from the store's. Set this machine's clock to the correct time."
            )
        if code in ("ExpiredToken", "InvalidToken"):
            return (
                f"The store at {endpoint} didn't accept the session token S3 Sync signs with — it "
                f"has expired, or isn't valid. Set a fresh Session token {_ON_CARD}, or leave it "
                "empty and use a long-lived access key."
            )
        if code == "AuthorizationHeaderMalformed":
            return (
                f"The store at {endpoint} expects requests for bucket {bucket} signed for a "
                f"different region than {self._region}. Set Region {_ON_CARD} to the region the "
                "store's answer names."
            )
        if code == "NoSuchBucket":
            return (
                f"There is no bucket named {bucket} at {endpoint}. Create it, or set Bucket "
                f"{_ON_CARD} to one that exists."
            )
        if code == "RequestTimeout" or status == 408:
            what = "upload" if action == "write" else "request"
            return (
                f"The store at {endpoint} gave up waiting for S3 Sync's {what} to arrive. If it "
                "keeps happening, check this machine's connection to the store."
            )
        if status in (429, 503):
            return (
                f"The store at {endpoint} was too busy to take S3 Sync's {request} — it is "
                "limiting how fast it takes requests. If it keeps happening, check the store's "
                "load and any request limits on the bucket."
            )
        if status in (401, 403) and action == "read":
            return (
                f"The store at {endpoint} refused S3 Sync's read of {key} in bucket {bucket}. "
                f"Check Access key ID and Secret access key {_ON_CARD}, and that the key's policy "
                "lets it read objects in that bucket."
            )
        if status in (401, 403):
            may = "list" if action == "list" else "write objects to"
            return (
                f"The store at {endpoint} refused S3 Sync's {request} to bucket {bucket}. Check "
                f"Access key ID and Secret access key {_ON_CARD}, and that the key's policy lets "
                f"it {may} that bucket."
            )
        if status == 404:
            return (
                f"The store at {endpoint} answered \"not found\" for bucket {bucket}: the bucket "
                "doesn't exist there, or Endpoint URL doesn't point at an S3 API. Check Bucket and "
                f"Endpoint URL {_ON_CARD}."
            )
        if status == 501 and action == "list":
            return (
                f"The store at {endpoint} doesn't support ListObjectsV2, the listing S3 Sync "
                "reads the bucket with. Use a store, or a version of it, that supports it."
            )
        if action == "write" and (status == 501 or code == "NotImplemented"):
            return (
                f"The store at {endpoint} doesn't support conditional writes ({condition}), "
                "which S3 Sync relies on to never overwrite an object. Use a store, or a version "
                "of it, that supports them."
            )
        if 300 <= status < 400:
            return (
                f"The store at {endpoint} redirected S3 Sync's {request} — usually because bucket "
                f"{bucket} is in another region, reached through a different endpoint. Set "
                f"Endpoint URL {_ON_CARD} to the endpoint the store's answer names, and Region to "
                "match."
            )
        if status >= 500:
            return (
                f"The store at {endpoint} failed while handling S3 Sync's {request}. That trouble "
                "is the store's own; if it keeps happening, check the store."
            )
        return (
            f"The store at {endpoint} refused S3 Sync's {request}. Check Endpoint URL, Bucket and "
            f"Region {_ON_CARD}."
        )

    def _too_large(self, key: str) -> str:
        """What an object larger than the most PersonalClaw downloads from a sync store says —
        that most is the cap of the policy the request ran under — with the step that fits the
        object: a shard is data a machine synced, and anything else S3 Sync never writes that
        large."""
        what = (
            f"The object {self._prefix}{key} in bucket {self._bucket} is larger than "
            f"{_size(self._policy().max_bytes)}, the most PersonalClaw downloads from a sync "
            "store, so S3 Sync can't read it"
        )
        if not is_routing_key(key):
            return (
                f"{what}: a machine's synced data has grown past that. To keep syncing it, use a "
                "transport without that limit, such as Rsync Sync."
            )
        where = f"{self._prefix} in bucket {self._bucket}" if self._prefix else "the bucket"
        return (
            f"{what}, and S3 Sync never writes one that size, so something else put it there. "
            f"Move it out of {where}."
        )

    def _not_a_listing(self) -> str:
        """What a 2xx answer to a listing that isn't an S3 listing says."""
        return (
            f"The store at {self._endpoint} answered S3 Sync's listing of bucket {self._bucket} "
            "with something that isn't an S3 listing. Check that Endpoint URL "
            f"{_ON_CARD} is the address of the store's S3 API."
        )

    def _raised(self, exc: BaseException) -> S3RequestFailed:
        """The error a listing, a read or the registry swap raises for a request that raised:
        said as a push's is, and that the cycle retries when a push's would be retried."""
        sentence = self._request_failed(exc)
        if _outcome_for(exc) == "transient":
            sentence = f"{sentence} {_RETRIES}"
        return S3RequestFailed(sentence, exc)

    def _refusal(self, resp: Any, code: str, words: str, **how: str) -> S3RequestFailed:
        """The error a listing, a read or the registry swap raises for the store's non-2xx
        answer: said as :meth:`_store_refused` says it for ``how``, and that the cycle retries
        when a push's would be retried."""
        sentence = self._store_refused(resp.status, code, **how)
        if _outcome_for_status(resp.status, code) == "transient":
            sentence = f"{sentence} {_RETRIES}"
        return S3RequestFailed(sentence, words)

    def _get(self, key: str) -> Any:
        """GET the object ``key``: the store's whole answer, or None when the store doesn't have
        it — a 404, for which the contract drops the ref, unless S3's code says the bucket itself
        is gone. Anything else raises :class:`S3RequestFailed`: a request that raised, a refusal,
        or a body cut off at the download cap."""
        try:
            resp = self._request("GET", self._object_url(key))
        except Exception as e:  # noqa: BLE001 — every failure is said, then raised
            raise self._raised(e) from e
        if 200 <= resp.status < 300:
            if resp.truncated:
                # `fetch` caps the body at the policy's max_bytes and REPORTS the cap rather than
                # raising. Handing back a prefix of a shard would be silent corruption that the
                # merge would happily apply.
                size = _header(resp.headers, "Content-Length")
                words = f"HTTP {resp.status}, Content-Length {size}" if size else ""
                raise S3RequestFailed(self._too_large(key), words or f"HTTP {resp.status}")
            return resp
        code, words = _answer(resp)
        if resp.status == 404 and code != "NoSuchBucket":
            return None
        raise self._refusal(resp, code, words, action="read", key=f"{self._prefix}{key}")

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
                outcome = _outcome_for(e)
                sentence = self._request_failed(e)
                if outcome == "transient":
                    sentence = f"{sentence} {_RETRIES}"
                return PushResult(
                    pushed=pushed,
                    skipped=skipped,
                    outcome=outcome,
                    detail=sentence_with_detail(sentence, e),
                )
            if resp.status == 412:
                # Already present — insert-only means this is a no-op, not a failure. (A 409 is
                # not: it says another conditional write to this key is still in progress, and
                # the key may not exist at all — see ``_WRITE_IN_PROGRESS``.)
                skipped += 1
                continue
            if 200 <= resp.status < 300:
                pushed += 1
                continue
            code, words = _answer(resp)
            outcome = _outcome_for_status(resp.status, code)
            sentence = self._store_refused(resp.status, code)
            if outcome == "transient":
                sentence = f"{sentence} {_RETRIES}"
            return PushResult(
                pushed=pushed,
                skipped=skipped,
                outcome=outcome,
                detail=sentence_with_detail(sentence, words),
            )
        return PushResult(pushed=pushed, skipped=skipped, outcome="delivered")

    def list_remote(self, prefix: str = "") -> list[RemoteRef]:
        # EMPTY only when there is nothing there: an unconfigured transport, or a listing the
        # store answered with no objects in it — a fresh bucket, before any machine has synced.
        # A listing that fails raises, said as what went wrong — see :class:`S3RequestFailed`.
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
            except Exception as e:  # noqa: BLE001 — every failure is said, then raised
                raise self._raised(e) from e
            if not (200 <= resp.status < 300):
                code, words = _answer(resp)
                raise self._refusal(resp, code, words, action="list")
            try:
                root = ET.fromstring(resp.body)
            except ET.ParseError:
                root = None
            if root is None or root.tag.rsplit("}", 1)[-1] != "ListBucketResult":
                raise S3RequestFailed(self._not_a_listing(), _answer(resp)[1])
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
                # The rest of the listing, with no way to ask for it: what came back so far
                # would read as all there is, and every object after it as gone.
                raise S3RequestFailed(
                    f"The store at {self._endpoint} said its listing of bucket {self._bucket} "
                    "goes on, but not where it continues, so S3 Sync can't read the rest. Use a "
                    "store, or a version of it, that supports ListObjectsV2.",
                    f"HTTP {resp.status}, IsTruncated true with no NextContinuationToken",
                )
        return refs

    def pull(self, refs: list[RemoteRef]) -> list[SyncObject]:
        if not self.configured:
            return []
        out: list[SyncObject] = []
        for ref in refs:
            resp = self._get(ref.key)
            if resp is not None:  # None is a ref the store no longer has: dropped, not raised
                out.append(SyncObject(key=ref.key, data=resp.body))
        return out

    def cas_registry(self, expected_sha: str | None, data: bytes) -> bool:
        """Compare-and-swap ``registry.json`` using S3 conditional writes.

        ``expected_sha is None`` (expect absent) becomes ``If-None-Match: *``; an expected
        sha becomes a read (to learn the current ETag and verify the sha the caller
        expected) followed by ``If-Match: <etag>``. Both conditions are evaluated by the
        store, so two machines racing cannot both win.

        ``False`` is a lost race and nothing else: the condition failed (a 412), the registry
        holds other bytes than the caller expected, or it is gone when the caller expected some.
        Anything else that stops the swap raises :class:`S3RequestFailed`, said as what is
        wrong — a request that raised, a refusal, a read cut off at the download cap, a read
        with no ETag to make the write conditional on, a store without conditional writes —
        rather than reading as a race core retries to no purpose, then reports lost.

        It never falls back to an unconditional PUT, however the store answers: an
        unconditional registry write silently discards another machine's registration.
        """
        if not self.configured:
            return False
        url = self._object_url(_REGISTRY_KEY)
        condition: dict[str, str]
        if expected_sha is None:
            header, condition = "If-None-Match", {"if-none-match": "*"}
        else:
            current = self._get(_REGISTRY_KEY)
            if current is None or hashlib.sha256(current.body).hexdigest() != expected_sha:
                # Gone, or swapped by someone else since the caller read it: re-pull and retry.
                return False
            etag = _header(current.headers, "ETag")
            if not etag:
                # No ETag means the write cannot be made conditional, and an unconditional one
                # could clobber a peer. Refused, and said.
                sent = ", ".join(sorted({str(k) for k in current.headers or {}})) or "none"
                raise S3RequestFailed(
                    f"The store at {self._endpoint} sent {self._prefix}{_REGISTRY_KEY} back "
                    "without an ETag, so S3 Sync can't make its registry write conditional — and "
                    "it never writes the registry unconditionally, which could discard another "
                    "machine's registration. If a proxy sits between this machine and the store, "
                    "let it pass the ETag header through; otherwise use a store that sends one.",
                    f"HTTP {current.status} with headers {sent}",
                )
            header, condition = "If-Match", {"if-match": etag}
        try:
            resp = self._request("PUT", url, payload=data, extra_headers=condition)
        except Exception as e:  # noqa: BLE001 — every failure is said, then raised
            raise self._raised(e) from e
        if 200 <= resp.status < 300:
            return True
        if resp.status == 412:
            return False  # the condition failed: another machine swapped it first
        code, words = _answer(resp)
        raise self._refusal(resp, code, words, condition=header)

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
            return ConnectionResult(
                ok=False, detail=sentence_with_detail(self._request_failed(e), e)
            )
        if 200 <= resp.status < 300:
            where = f"{self._bucket}/{self._prefix}" if self._prefix else self._bucket
            return ConnectionResult(
                ok=True,
                detail=f"bucket reachable at {where}",
                extra={"endpoint": self._endpoint, "region": self._region},
            )
        # Said as a push's refusal is — by S3's own code, then the status — for the listing.
        code, words = _answer(resp)
        sentence = self._store_refused(resp.status, code, action="list")
        return ConnectionResult(ok=False, detail=sentence_with_detail(sentence, words))


def _outcome_for_status(status: int, code: str = "") -> str:
    """Map a store's HTTP status (and S3's error ``code``) to the outbox's typed verdict.

    ``transient`` is what can clear by itself: the store throttling (429, 503 ``SlowDown``),
    timing out an upload (408, and S3's ``RequestTimeout``, which it sends as a 400), a
    temporary redirect (307), or failing (5xx). Everything else — an auth or permission
    refusal, a bucket that isn't there, a malformed request, a missing feature (501: no
    conditional writes), a permanent redirect — is a setup the next try would meet unchanged,
    so ``permanent``: retrying an unauthorized PUT forever is the error loop §4.4 bans.
    """
    if code == "RequestTimeout" or status in (307, 408, 429):
        return "transient"
    if status == 409 and code in _WRITE_IN_PROGRESS:
        return "transient"  # another conditional write to the key, still in progress
    return "transient" if status >= 500 and status != 501 else "permanent"


def _answer(resp: Any) -> tuple[str, str]:
    """A store's non-2xx answer as S3's own error ``code`` and the words kept after the sentence:
    ``HTTP <status>``, then what the store said (see :func:`_store_error`)."""
    code, said = _store_error(resp.body)
    return code, f"HTTP {resp.status} {said}".strip()


def _size(n: int) -> str:
    """``n`` bytes, said the way a limit reads: in megabytes, once it is some."""
    return f"{n / 1_000_000:g} MB" if n >= 1_000_000 else f"{n:,} bytes"


def _store_error(body: bytes) -> tuple[str, str]:
    """S3's own ``Code`` for an error answer, and its words: ``Code (Endpoint: …) — Message``,
    the endpoint or region it names first, since the detail is cut to a couple of hundred
    characters and those are what the fix needs. A dash, not a colon, after the code: the
    credential redactor reads ``InvalidAccessKeyId: The …`` as a key and its value.
    ``("", body text)`` when the body isn't an S3 error document (a proxy's page, say)."""
    try:
        root = ET.fromstring(body or b"")
    except ET.ParseError:
        return "", " ".join((body or b"").decode("utf-8", "replace").split())
    code = (root.findtext("{*}Code") or "").strip()
    if not code:
        return "", " ".join("".join(root.itertext()).split())
    named = [
        f"{field}: {value}"
        for field in ("Endpoint", "Region")
        if (value := (root.findtext(f"{{*}}{field}") or "").strip())
    ]
    said = f"{code} ({', '.join(named)})" if named else code
    message = (root.findtext("{*}Message") or "").strip()
    return code, f"{said} — {message}" if message else said


def _outcome_for(exc: BaseException) -> str:
    """Map an exception to the outbox's typed verdict.

    An egress refusal or an unusable endpoint is a **configuration** fault, which retrying
    cannot fix — it is permanent until the operator changes a setting. Network errors are
    transient — and a host the guard couldn't resolve is one: DNS failing, or this machine
    being offline, reads exactly like it, so it is retried rather than given up.
    """
    from personalclaw.sdk.net import EgressBlocked

    from personalclaw.sdk.sync import SyncEndpointRefused  # noqa: PLC0415

    if isinstance(exc, EgressBlocked):
        category = getattr(getattr(exc, "decision", None), "category", "")
        return "transient" if category == "unresolvable" else "permanent"
    if isinstance(exc, SyncEndpointRefused):
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
