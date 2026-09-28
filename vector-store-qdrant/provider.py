"""Qdrant as PersonalClaw's knowledge chunk-vector index.

The vendor half of the ``vector_store`` seam. Core carries no Qdrant client — the whole
client, the collection DDL, the payload shape and the credential lookup live here, which is
what ``docs/architecture/provider-boundary.md`` requires: Qdrant's REST dialect is one
vendor's API, not a de-facto multi-vendor protocol like ``/v1/chat/completions``.

WHAT CORE ASKS OF THIS FILE. Four methods (``personalclaw.sdk.vector_store``): upsert an
item's chunk vectors, delete an item's vectors, return the k nearest hits in DESCENDING
cosine similarity, and describe reachability. Core keeps the similarity floor, the max
roll-up to the parent document, the archived/active liveness filter and RRF fusion, so this
file cannot change what a search means — only where the nearest-neighbour work happens.

TWO MODES, ONE CLIENT CALL PATH. ``qdrant-client`` talks to a server over HTTP when given a
``url`` and runs Qdrant's engine in-process over a local folder when given a ``path``. Every
method below is identical in both; only the constructor differs. The local folder is how you
try this without installing anything, and it is what the test suite drives.

CREDENTIALS. The api key is the ``api_key`` setting, declared ``x-meta.sensitive``, so
``ProviderSettings`` keeps its value in the credential store under a key this app owns and
writes only a ``{{secret:…}}`` reference into
``~/.personalclaw/apps/vector-store-qdrant/data/config.json``; uninstalling the app removes it.
The factory receives the resolved value in ``config``, exactly as it receives the URL. An empty
field falls back to the ``QDRANT_API_KEY`` environment variable. (The key used to be read through
``CredentialStore()``, called without the home it requires: the ``TypeError`` was swallowed on
every connect, so the credential store was never read and only the environment was consulted.)

IDS. Qdrant point ids must be an unsigned integer or a UUID, and PersonalClaw chunk ids are
32-char hex (``uuid4().hex``). They are converted to canonical UUID form for the id and ALSO
carried verbatim in the payload, because the id is what makes an upsert idempotent while the
payload is what core joins back to its own ``chunks`` table.

FAILURES. Core's contract lets a method on an unreachable store raise or return empty; core
treats a raise as "this arm cannot answer" and logs it. Returning empty would read as "no
vectors" — a store that lost everything, or a query nothing matched — so a failed call raises,
said as what is wrong and what to do (:class:`QdrantStoreError`), never swallowed.
"""

from __future__ import annotations

import logging
import os
import socket
import ssl
import uuid
from collections.abc import Iterator, Sequence
from urllib.parse import urlsplit, urlunsplit

from personalclaw.sdk.net import sentence_with_detail
from personalclaw.sdk.vector_store import (
    VectorHit,
    VectorRecord,
    VectorStoreInfo,
    VectorStoreProvider,
)

logger = logging.getLogger("vector_store_qdrant")

#: The environment variable an empty ``api_key`` setting falls back to.
API_KEY_NAME = "QDRANT_API_KEY"

#: Where this app's own settings (Qdrant URL, Collection, Qdrant API Key, the local folder, the
#: timeout) are set.
_ON_CARD = "on the Qdrant Vector Store card in Settings → Providers"

#: Qdrant's own name for cosine distance. Cosine and not dot/euclid because core's
#: `_VECTOR_MIN_SIMILARITY` floor is calibrated on cosine similarity, and Qdrant returns a
#: COSINE metric score directly for this distance — no conversion, so nothing can drift.
_DISTANCE = "Cosine"


def _point_id(chunk_id: str) -> str:
    """A PersonalClaw chunk id as a Qdrant point id.

    Qdrant accepts an unsigned int or a UUID. Chunk ids are already 32 hex chars, so the
    conversion is a re-spelling and stays injective — two chunks can never collide onto one
    point. A chunk id that is not hex (nothing in core produces one, but an app must not crash
    on a shape it did not choose) is hashed into a UUID5 instead of raising.
    """
    try:
        return str(uuid.UUID(hex=chunk_id))
    except (ValueError, AttributeError, TypeError):
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"personalclaw-chunk:{chunk_id}"))


def _shown(url: str) -> str:
    """``url`` as a sentence may show it: without the ``user:password@`` it may carry."""
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    return urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[1]))


class _SizeMismatch(ValueError):
    """Vectors of another size than the collection was made for, found before they were sent."""

    def __init__(self, collection_size: int, vector_size: int) -> None:
        super().__init__(
            f"the collection's vectors have {collection_size} dimensions; these have {vector_size}"
        )


class QdrantStoreError(RuntimeError):
    """A Qdrant call that failed, said as what is wrong and what to do. Its text is that
    sentence and then the client's own words — the text core logs when this arm cannot answer —
    and the client's exception is chained as ``__cause__``."""


def _causes(exc: BaseException) -> Iterator[BaseException]:
    """``exc`` and what it wraps, nearest first: the client's own ``source`` wrapper, then
    ``__cause__`` / ``__context__`` — which is where the socket's own error sits."""
    seen: set[int] = set()
    node: BaseException | None = exc
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        yield node
        source = getattr(node, "source", None)
        node = source if isinstance(source, BaseException) else (node.__cause__ or node.__context__)


class QdrantVectorStore(VectorStoreProvider):
    """Chunk-vector index backed by Qdrant."""

    name = "vector-store-qdrant"

    def __init__(
        self,
        *,
        url: str = "http://localhost:6333",
        collection: str = "personalclaw_knowledge",
        path: str = "",
        timeout_secs: int = 10,
        api_key: str = "",
    ) -> None:
        self._url = (url or "").strip()
        self._path = os.path.expanduser((path or "").strip())
        self._collection = (collection or "personalclaw_knowledge").strip()
        self._timeout = max(1, int(timeout_secs or 10))
        self._api_key = (api_key or "").strip()
        self._client = None

    # ── client ───────────────────────────────────────────────────────────────────────

    def _connect(self):
        """The lazily-built client. One call path for both modes.

        Built lazily rather than in ``__init__`` because a provider is constructed at gateway
        boot, when the user's Qdrant may not be up yet; a failed connection then would take
        the app's enablement down with it instead of degrading one query.
        """
        if self._client is not None:
            return self._client
        from qdrant_client import QdrantClient

        if self._path:
            # Embedded: Qdrant's engine in-process over a local folder. No server, no socket.
            self._client = QdrantClient(path=self._path)
        else:
            # No key is not an error: a local Qdrant with no auth is the common case, and it
            # accepts the unauthenticated request.
            key = self._api_key or os.environ.get(API_KEY_NAME, "")
            self._client = QdrantClient(
                url=self._url,
                timeout=self._timeout,
                **({"api_key": key} if key else {}),
            )
        return self._client

    def _ensure_collection(self, dim: int) -> int | None:
        """Create the collection at *dim* if it is not there yet, and return the vector size it
        holds: *dim* for a new one, what an existing one was made with otherwise.

        The dimension comes from the vectors being written rather than from configuration:
        asking the user for it would be asking them to restate a property of the embedding
        model they already chose, and getting it wrong would produce a collection that silently
        rejects every write.
        """
        from qdrant_client.models import Distance, VectorParams

        client = self._connect()
        if client.collection_exists(self._collection):
            return self._collection_size(client)
        client.create_collection(
            collection_name=self._collection,
            vectors_config=VectorParams(size=dim, distance=Distance[_DISTANCE.upper()]),
        )
        logger.info("created Qdrant collection %r at dimension %d", self._collection, dim)
        return dim

    def _collection_size(self, client) -> int | None:
        """The vector size the existing collection was made with — read each time, since the
        collection can be dropped and made again at another size — or None when it holds
        named vectors, which this app never makes, and so has no one size to hold vectors to."""
        params = client.get_collection(self._collection).config.params.vectors
        size = getattr(params, "size", None)
        return int(size) if size else None

    # ── the seam's four methods ──────────────────────────────────────────────────────

    def upsert(self, records: Sequence[VectorRecord]) -> int:
        if not records:
            return 0  # an item that produced no embedded chunks is not an error
        try:
            from qdrant_client.models import PointStruct

            dim = len(records[0].vector)
            # The collection's own size, checked before anything is sent, so vectors of another
            # size are said as that — against a server and a local folder alike.
            size = self._ensure_collection(dim) or dim
            points = [
                PointStruct(
                    id=_point_id(r.chunk_id),
                    vector=list(r.vector),
                    payload={
                        # chunk_id verbatim: the point id is a re-spelled UUID, and core joins
                        # on the original.
                        "chunk_id": r.chunk_id,
                        "item_id": r.item_id,
                        "chunk_index": r.chunk_index,
                        "section": r.section,
                        "line_start": r.line_start,
                        "line_end": r.line_end,
                    },
                )
                for r in records
                if len(r.vector) == size
            ]
            if not points:
                # Not one vector the collection's size: the embedding model changed since it
                # was made. A record of another size among ones that fit is skipped, as ever.
                raise _SizeMismatch(size, dim)
            self._connect().upsert(collection_name=self._collection, points=points, wait=True)
        except Exception as exc:  # noqa: BLE001 - every failure is said, then raised for core
            raise self._failed(exc) from exc
        return len(points)

    def delete_item(self, item_id: str) -> int:
        """Delete by payload FILTER on ``item_id``, not by id list.

        Deleting by id would require knowing the ids, and the caller deletes precisely when it
        is about to mint new ones — a re-chunk. Filtering on the payload is what makes this
        idempotent for an item the store never held.
        """
        try:
            from qdrant_client.models import FieldCondition, Filter, FilterSelector, MatchValue

            client = self._connect()
            if not client.collection_exists(self._collection):
                return 0
            client.delete(
                collection_name=self._collection,
                points_selector=FilterSelector(
                    filter=Filter(
                        must=[FieldCondition(key="item_id", match=MatchValue(value=item_id))]
                    )
                ),
                wait=True,
            )
        except Exception as exc:  # noqa: BLE001 - every failure is said, then raised for core
            raise self._failed(exc) from exc
        # Qdrant's delete reports an operation status, not a row count.
        return 0

    def query(self, vector: Sequence[float], *, k: int) -> list[VectorHit]:
        try:
            client = self._connect()
            if not client.collection_exists(self._collection):
                return []
            # Checked before searching: a server refuses a query vector of another size, but a
            # local folder's engine fails with an error of its own that names no size at all.
            size = self._collection_size(client)
            if size is not None and len(vector) != size:
                raise _SizeMismatch(size, len(vector))
            res = client.query_points(
                collection_name=self._collection,
                query=list(vector),
                limit=max(1, int(k)),
                with_payload=True,
            )
        except Exception as exc:  # noqa: BLE001 - every failure is said, then raised for core
            raise self._failed(exc) from exc
        hits: list[VectorHit] = []
        for p in res.points:
            payload = p.payload or {}
            chunk_id = payload.get("chunk_id")
            if not chunk_id:
                continue  # a point this app did not write, or wrote before the payload existed
            hits.append(
                VectorHit(
                    chunk_id=str(chunk_id),
                    item_id=str(payload.get("item_id") or ""),
                    # Qdrant returns the COSINE metric score for a Cosine collection, which is
                    # cosine similarity — the scale core's floor is calibrated on. No
                    # conversion, so there is nothing here that can drift out of step with it.
                    similarity=float(p.score),
                )
            )
        # Qdrant already orders by descending score; re-sorting is the cheap way to make the
        # contract core relies on a property of THIS file rather than of the server version.
        hits.sort(key=lambda h: h.similarity, reverse=True)
        return hits

    def describe(self) -> VectorStoreInfo:
        where = f"folder {self._path}" if self._path else self._url
        try:
            client = self._connect()
            if not client.collection_exists(self._collection):
                return VectorStoreInfo(
                    backend="qdrant",
                    collection=self._collection,
                    reachable=True,
                    detail=f"connected to {where}; collection not created yet "
                    "(it is created on the first document ingested)",
                )
            info = client.get_collection(self._collection)
            count = client.count(self._collection, exact=False).count
            params = info.config.params.vectors
            dim = getattr(params, "size", None)
            return VectorStoreInfo(
                backend="qdrant",
                collection=self._collection,
                dimension=int(dim) if dim else None,
                count=int(count),
                reachable=True,
                detail=f"connected to {where}",
            )
        except Exception as exc:  # noqa: BLE001 - describe must never raise
            # The message is rendered in the UI and written to logs, so it names the endpoint
            # and the error but never the api key.
            return VectorStoreInfo(
                backend="qdrant",
                collection=self._collection,
                reachable=False,
                detail=sentence_with_detail(self._unreachable(exc), exc),
            )

    def _failed(self, exc: BaseException) -> QdrantStoreError:
        """What ``upsert``/``delete_item``/``query`` raise for a failed call: the same sentence
        ``describe`` gives, with the client's words after it."""
        return QdrantStoreError(sentence_with_detail(self._unreachable(exc), exc))

    def _unreachable(self, exc: BaseException) -> str:
        """What a failed connect or call means, and what to do — the client's own words
        ("[Errno 61] Connection refused", "Unexpected Response: 401 …") say neither."""
        causes = list(_causes(exc))
        if isinstance(exc, ImportError):
            return (
                "Qdrant Vector Store couldn't load qdrant-client, the Python package it talks to "
                "Qdrant through. Reinstall Qdrant Vector Store from the Store — that package "
                "ships with this app, not with PersonalClaw itself."
            )
        if any(
            isinstance(c, _SizeMismatch) or "vector dimension error" in str(c).lower()
            for c in causes
        ):
            # Found before the call (the collection's size, read up front), or in Qdrant's own
            # words for vectors of another size than the collection was made for.
            return (
                f"The collection {self._collection} holds vectors of a different size than the "
                f"embedding model in use now makes. Set Collection {_ON_CARD} to a new name, and "
                "one is created at the new size when the next document is ingested."
            )
        if self._path:
            folder = f"Local folder (no server) {_ON_CARD}"
            if "already accessed by another instance" in str(exc):
                return (
                    f"The local folder {self._path} is already open in another Qdrant client, "
                    "and only one may use it at a time. Close whatever else has it open, or set "
                    f"{folder} to another folder."
                )
            if any(isinstance(c, PermissionError) for c in causes):
                return (
                    f"Qdrant Vector Store isn't allowed to use the local folder {self._path}. Fix "
                    f"that folder's permissions, or set {folder} to one PersonalClaw can write to."
                )
            if self._client is not None:
                # The folder opened; what failed is a call made to the engine over it.
                return (
                    "Qdrant Vector Store's request to the Qdrant engine over its local folder "
                    f"{self._path} failed. Check that folder, or set {folder} to another one."
                )
            return (
                f"Qdrant Vector Store couldn't open its local folder {self._path}. Check that "
                f"folder, or set {folder} to another one."
            )
        url = _shown(self._url)
        status = getattr(exc, "status_code", None)
        if status in (401, 403):
            return (
                f"The Qdrant at {url} refused Qdrant Vector Store's request (HTTP {status}): the "
                f"API key it sent is missing or wrong. Set Qdrant API Key {_ON_CARD} to that "
                "server's key."
            )
        if isinstance(status, int):
            return (
                f"The Qdrant at {url} answered with an error (HTTP {status}). Check Qdrant URL "
                f"and Collection {_ON_CARD}, and that the server is healthy."
            )
        if any(isinstance(c, ConnectionRefusedError) for c in causes):
            return (
                f"Nothing is accepting connections at {url}. Check that Qdrant is running there, "
                f"and that Qdrant URL {_ON_CARD} has the right host and port."
            )
        if any(isinstance(c, socket.gaierror) for c in causes):
            return (
                f"The host in Qdrant URL, {urlsplit(url).hostname or url}, can't be found from "
                f"this machine. Check Qdrant URL {_ON_CARD}."
            )
        if any(isinstance(c, TimeoutError) or "Timeout" in type(c).__name__ for c in causes):
            return (
                f"The Qdrant at {url} didn't answer within {self._timeout} seconds. Check that it "
                f"is reachable from this machine, or raise Timeout (seconds) {_ON_CARD}."
            )
        if any(isinstance(c, ssl.SSLError) for c in causes):
            return (
                f"Qdrant Vector Store couldn't make a secure connection to {url}: the TLS "
                "handshake failed, or this machine doesn't trust the server's certificate. Check "
                f"that Qdrant URL {_ON_CARD} is right, and that the server's certificate is "
                "valid for that host."
            )
        return (
            f"Qdrant Vector Store couldn't talk to the Qdrant at {url}. Check that Qdrant is "
            f"running there, and Qdrant URL {_ON_CARD}."
        )


def create_provider(config: dict | None = None) -> QdrantVectorStore:
    """Factory named by ``app.json``'s ``provider.implementation``.

    *config* is the app's settings as ``ProviderSettings.load`` returns them: the ``api_key``
    field holds the key itself, resolved from the credential store.
    """
    cfg = config or {}
    return QdrantVectorStore(
        url=str(cfg.get("url", "http://localhost:6333")),
        collection=str(cfg.get("collection", "personalclaw_knowledge")),
        path=str(cfg.get("path", "")),
        timeout_secs=int(cfg.get("timeout_secs", 10) or 10),
        api_key=str(cfg.get("api_key", "") or ""),
    )
