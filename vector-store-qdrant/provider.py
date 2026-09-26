"""Qdrant as PersonalClaw's knowledge chunk-vector index (KBVS-1).

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
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Sequence

from personalclaw.sdk.vector_store import (
    VectorHit,
    VectorRecord,
    VectorStoreInfo,
    VectorStoreProvider,
)

logger = logging.getLogger("vector_store_qdrant")

#: The environment variable an empty ``api_key`` setting falls back to.
API_KEY_NAME = "QDRANT_API_KEY"

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
        #: Dimension of the collection as it exists. Learned from the first upsert (or from a
        #: describe of a collection that already exists), because the embedding model — and
        #: therefore the vector width — is the user's choice and can change.
        self._dim: int | None = None

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

    def _ensure_collection(self, dim: int) -> None:
        """Create the collection at *dim* if it is not there yet.

        The dimension comes from the vectors being written rather than from configuration:
        asking the user for it would be asking them to restate a property of the embedding
        model they already chose, and getting it wrong would produce a collection that silently
        rejects every write.
        """
        from qdrant_client.models import Distance, VectorParams

        client = self._connect()
        if client.collection_exists(self._collection):
            self._dim = dim
            return
        client.create_collection(
            collection_name=self._collection,
            vectors_config=VectorParams(size=dim, distance=Distance[_DISTANCE.upper()]),
        )
        self._dim = dim
        logger.info("created Qdrant collection %r at dimension %d", self._collection, dim)

    # ── the seam's four methods ──────────────────────────────────────────────────────

    def upsert(self, records: Sequence[VectorRecord]) -> int:
        if not records:
            return 0  # an item that produced no embedded chunks is not an error
        from qdrant_client.models import PointStruct

        dim = len(records[0].vector)
        self._ensure_collection(dim)
        points = [
            PointStruct(
                id=_point_id(r.chunk_id),
                vector=list(r.vector),
                payload={
                    # chunk_id verbatim: the point id is a re-spelled UUID, and core joins on
                    # the original.
                    "chunk_id": r.chunk_id,
                    "item_id": r.item_id,
                    "chunk_index": r.chunk_index,
                    "section": r.section,
                    "line_start": r.line_start,
                    "line_end": r.line_end,
                },
            )
            for r in records
            if len(r.vector) == dim
        ]
        if not points:
            return 0
        self._connect().upsert(collection_name=self._collection, points=points, wait=True)
        return len(points)

    def delete_item(self, item_id: str) -> int:
        """Delete by payload FILTER on ``item_id``, not by id list.

        Deleting by id would require knowing the ids, and the caller deletes precisely when it
        is about to mint new ones — a re-chunk. Filtering on the payload is what makes this
        idempotent for an item the store never held.
        """
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
        # Qdrant's delete reports an operation status, not a row count.
        return 0

    def query(self, vector: Sequence[float], *, k: int) -> list[VectorHit]:
        client = self._connect()
        if not client.collection_exists(self._collection):
            return []
        res = client.query_points(
            collection_name=self._collection,
            query=list(vector),
            limit=max(1, int(k)),
            with_payload=True,
        )
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
                detail=f"cannot reach {where}: {type(exc).__name__}: {exc}",
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
