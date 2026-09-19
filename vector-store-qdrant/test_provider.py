"""Qdrant vector-store provider — driven against a REAL Qdrant engine.

Every test here runs Qdrant's own engine in-process over a ``tmp_path`` folder
(``QdrantClient(path=...)``), so the vendor's real collection DDL, real upsert, real filtered
delete and real KNN search are exercised — not a mock of them. Nothing listens on a socket and
nothing survives the test, which is what makes it runnable in CI and on a loaded laptop.

What the server (HTTP) mode shares with this and what it does not: every method below is
byte-identical in both modes — only ``_connect`` branches — so what these tests prove about
upsert/delete/query/describe semantics holds for a server too.
``test_a_url_config_builds_a_networked_client`` covers the branch itself without connecting.
What is NOT proven here is a real Qdrant server over the network; see the README.

The claim that matters most for retrieval correctness is
``test_qdrant_score_is_the_same_cosine_core_would_have_computed``: core applies its own
calibrated similarity floor to whatever number this provider returns, so if Qdrant's score
were on a different scale, binding this app would silently re-tune that threshold.
"""

from __future__ import annotations

import math

import pytest
from provider import (
    API_KEY_NAME,
    QdrantVectorStore,
    _point_id,
    create_provider,
)

from personalclaw.sdk.vector_store import VectorHit, VectorRecord, VectorStoreProvider

DIM = 8


def _vec(*vals: float) -> list[float]:
    """An 8-dim vector from the leading values given, zero-padded."""
    out = list(vals) + [0.0] * (DIM - len(vals))
    assert len(out) == DIM
    return out


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return 0.0 if not na or not nb else dot / (na * nb)


def _rec(chunk_id: str, item_id: str, vec, idx: int = 0, section: str = "s0") -> VectorRecord:
    return VectorRecord(
        chunk_id=chunk_id,
        item_id=item_id,
        chunk_index=idx,
        vector=vec,
        section=section,
        line_start=idx * 2 + 1,
        line_end=idx * 2 + 2,
    )


@pytest.fixture()
def store(tmp_path):
    """A provider over an embedded Qdrant in ``tmp_path``.

    32-char hex chunk ids throughout, because that is what core mints (``uuid4().hex``) and
    Qdrant rejects a point id that is neither an int nor a UUID — a test using ``"c1"`` would
    pass through :func:`_point_id`'s UUID5 fallback and never exercise the real path.
    """
    p = QdrantVectorStore(path=str(tmp_path / "q"), collection="test_chunks")
    yield p


# stable 32-hex chunk ids
C1 = "0" * 31 + "1"
C2 = "0" * 31 + "2"
C3 = "0" * 31 + "3"


# ── the contract, against the real engine ────────────────────────────────────────────


def test_it_implements_the_sdk_contract():
    assert issubclass(QdrantVectorStore, VectorStoreProvider)
    assert not QdrantVectorStore.__abstractmethods__


def test_upsert_then_query_returns_the_nearest_chunk(store):
    assert store.upsert([_rec(C1, "item-a", _vec(1.0)), _rec(C2, "item-b", _vec(0.0, 1.0))]) == 2
    hits = store.query(_vec(1.0), k=5)
    assert isinstance(hits[0], VectorHit)
    assert hits[0].chunk_id == C1
    assert hits[0].item_id == "item-a"
    assert hits[0].similarity == pytest.approx(1.0, abs=1e-6)


def test_hits_come_back_in_descending_similarity(store):
    """The ordering core's stop rule depends on. Out of order, core truncates its own recall
    at the first hit it wrongly believes proves the rest are below the floor."""
    store.upsert(
        [
            _rec(C1, "item-a", _vec(1.0)),
            _rec(C2, "item-b", _vec(0.7, 0.7)),
            _rec(C3, "item-c", _vec(0.0, 1.0)),
        ]
    )
    sims = [h.similarity for h in store.query(_vec(1.0), k=5)]
    assert sims == sorted(sims, reverse=True)
    assert sims[0] > sims[-1], "the corpus must actually spread, or the ordering claim is vacuous"


def test_qdrant_score_is_the_same_cosine_core_would_have_computed(store):
    """The scale claim. Core applies ``_VECTOR_MIN_SIMILARITY`` — calibrated on cosine
    similarity — to whatever this provider returns, so a vendor-scaled score or a distance
    would silently move that threshold for every query."""
    q = _vec(0.3, 0.9, 0.1)
    rows = {C1: _vec(1.0), C2: _vec(0.0, 1.0), C3: _vec(0.5, 0.5, 0.5)}
    store.upsert([_rec(cid, f"item-{cid[-1]}", v) for cid, v in rows.items()])
    for hit in store.query(q, k=5):
        assert hit.similarity == pytest.approx(_cosine(q, rows[hit.chunk_id]), abs=1e-6)


def test_k_bounds_the_result_set(store):
    store.upsert([_rec(c, f"item-{i}", _vec(1.0, i * 0.1)) for i, c in enumerate((C1, C2, C3))])
    assert len(store.query(_vec(1.0), k=2)) == 2
    assert len(store.query(_vec(1.0), k=99)) == 3


def test_upsert_is_idempotent_on_chunk_id(store):
    """Same chunk id twice is one point, and the second write wins — so a re-embed of the same
    chunk updates it rather than doubling it."""
    store.upsert([_rec(C1, "item-a", _vec(1.0))])
    store.upsert([_rec(C1, "item-a", _vec(0.0, 1.0))])
    hits = store.query(_vec(0.0, 1.0), k=5)
    assert len(hits) == 1
    assert hits[0].similarity == pytest.approx(1.0, abs=1e-6)


def test_delete_item_removes_only_that_items_vectors(store):
    store.upsert([_rec(C1, "item-a", _vec(1.0)), _rec(C2, "item-a", _vec(0.9, 0.1), idx=1)])
    store.upsert([_rec(C3, "item-b", _vec(0.0, 1.0))])
    store.delete_item("item-a")
    remaining = store.query(_vec(1.0), k=10)
    assert [h.chunk_id for h in remaining] == [C3]


def test_delete_item_is_idempotent_for_an_unknown_item(store):
    """The knowledge store deletes before every re-chunk, whether or not anything was ever
    indexed, so a raise here would fail an ordinary ingest."""
    store.upsert([_rec(C1, "item-a", _vec(1.0))])
    store.delete_item("never-indexed")
    store.delete_item("never-indexed")
    assert len(store.query(_vec(1.0), k=5)) == 1


def test_delete_before_the_collection_exists_is_a_noop(store):
    """A brand-new install deletes before it has ever written — no collection yet."""
    store.delete_item("item-a")


def test_query_before_the_collection_exists_returns_nothing(store):
    assert store.query(_vec(1.0), k=5) == []


def test_a_rechunk_replaces_rather_than_orphans(store):
    """Core deletes-then-upserts because a re-chunk mints fresh ids. Proven end to end: the old
    generation is unreachable afterwards."""
    store.upsert([_rec(C1, "item-a", _vec(1.0))])
    store.delete_item("item-a")
    store.upsert([_rec(C2, "item-a", _vec(0.0, 1.0))])
    assert [h.chunk_id for h in store.query(_vec(1.0), k=10)] == [C2]


def test_an_empty_upsert_is_a_noop_not_an_error(store):
    """An item whose chunks are all un-embedded (mid-backfill) arrives as an empty sequence."""
    assert store.upsert([]) == 0


def test_the_payload_carries_the_locator_fields(store):
    """Core prefers its own local row for the locator, but the payload is what makes the store
    independently readable — the point of BYO is that it is YOUR data."""
    store.upsert([_rec(C1, "item-a", _vec(1.0), idx=3, section="Chapter 4")])
    client = store._connect()
    pts = client.query_points("test_chunks", query=_vec(1.0), limit=1, with_payload=True).points
    payload = pts[0].payload
    assert payload["chunk_id"] == C1
    assert payload["item_id"] == "item-a"
    assert payload["chunk_index"] == 3
    assert payload["section"] == "Chapter 4"
    assert payload["line_start"] == 7 and payload["line_end"] == 8


def test_a_mismatched_dimension_is_skipped_not_written(store):
    """A half-re-embedded library holds two vector widths. Writing the odd one out would make
    Qdrant reject the whole batch, losing the chunks that were fine."""
    store.upsert([_rec(C1, "item-a", _vec(1.0)), _rec(C2, "item-b", [1.0, 0.0])])
    assert [h.chunk_id for h in store.query(_vec(1.0), k=10)] == [C1]


# ── dimension + collection lifecycle ────────────────────────────────────────────────


def test_the_collection_is_created_at_the_embedding_models_dimension(store):
    store.upsert([_rec(C1, "item-a", _vec(1.0))])
    info = store.describe()
    assert info.dimension == DIM, "asking the user to restate the model's width invites a typo"
    assert info.count == 1
    assert info.reachable is True


def test_describe_before_any_write_reports_reachable_without_a_collection(store):
    info = store.describe()
    assert info.reachable is True
    assert "not created yet" in info.detail
    assert info.backend == "qdrant" and info.collection == "test_chunks"


def test_describe_never_raises_and_never_leaks_the_key(monkeypatch, tmp_path):
    """Unreachable is a report, not an exception — it is rendered in the UI and logged."""
    monkeypatch.setenv(API_KEY_NAME, "super-secret-value")
    p = QdrantVectorStore(url="http://127.0.0.1:1/unreachable", collection="c", timeout_secs=1)
    info = p.describe()
    assert info.reachable is False
    assert "super-secret-value" not in info.detail
    assert "cannot reach" in info.detail


# ── clause 3: config round-trip, and the secret that must NOT be in it ──────────────


def test_every_settings_schema_property_round_trips_into_the_provider(tmp_path):
    """The config contract: each declared property reaches the object, and the defaults in
    ``app.json`` are the defaults the factory applies.

    Read from ``app.json`` rather than hand-listed, so a property added to the schema and not
    wired into ``create_provider`` reds here instead of silently doing nothing.
    """
    import json
    from pathlib import Path

    schema = json.loads((Path(__file__).parent / "app.json").read_text())["provider"][
        "settingsSchema"
    ]
    props = set(schema["properties"])
    assert props == {"url", "collection", "path", "timeout_secs"}

    p = create_provider(
        {
            "url": "http://example.invalid:6333",
            "collection": "mine",
            "path": str(tmp_path / "folder"),
            "timeout_secs": 42,
        }
    )
    assert p._url == "http://example.invalid:6333"
    assert p._collection == "mine"
    assert p._path == str(tmp_path / "folder")
    assert p._timeout == 42

    d = create_provider({})
    assert d._url == schema["properties"]["url"]["default"]
    assert d._collection == schema["properties"]["collection"]["default"]
    assert d._timeout == schema["properties"]["timeout_secs"]["default"]


def test_the_api_key_is_not_a_settings_field_and_is_ignored_in_config():
    """Clause 3, in its falsifiable form.

    The schema declares no key field, so the Configure form cannot write one — and if a config
    file acquired one anyway (hand-edited, or migrated from another app), the factory must not
    pick it up. Otherwise a secret would sit in cleartext in
    ``~/.personalclaw/apps/vector-store-qdrant/data/config.json``.
    """
    import json
    from pathlib import Path

    schema = json.loads((Path(__file__).parent / "app.json").read_text())["provider"][
        "settingsSchema"
    ]
    for field in schema["properties"]:
        assert "key" not in field and "secret" not in field and "password" not in field

    p = create_provider({"api_key": "leaked-into-config", "url": "http://x:1", "collection": "c"})
    assert not hasattr(p, "_api_key")
    assert "leaked-into-config" not in repr(vars(p))


def test_the_api_key_comes_from_the_environment_when_no_credential_is_registered(monkeypatch):
    """The documented fallback: credential store first, env var second, config never."""
    from provider import _api_key

    monkeypatch.setenv(API_KEY_NAME, "from-env")
    assert _api_key() == "from-env"
    monkeypatch.delenv(API_KEY_NAME)
    assert _api_key() == ""


def test_a_url_config_builds_a_networked_client(monkeypatch, tmp_path):
    """The server branch of ``_connect``, without a server.

    Captures the constructor kwargs instead of connecting, so the branch that carries the api
    key and the timeout to a real Qdrant is exercised on a host with no Qdrant running.
    """
    import provider as mod

    seen = {}

    class FakeClient:
        def __init__(self, **kw):
            seen.update(kw)

    monkeypatch.setenv(API_KEY_NAME, "k-123")
    # `_connect` imports QdrantClient from the package at call time, so patching the package
    # attribute is what the branch actually resolves.
    import qdrant_client

    monkeypatch.setattr(qdrant_client, "QdrantClient", FakeClient)

    p = mod.QdrantVectorStore(url="http://qdrant.internal:6333", collection="c", timeout_secs=7)
    p._connect()
    assert seen == {"url": "http://qdrant.internal:6333", "timeout": 7, "api_key": "k-123"}

    # and with no key set, the kwarg is absent rather than an empty string — Qdrant treats an
    # empty api key as a key and sends the header.
    seen.clear()
    monkeypatch.delenv(API_KEY_NAME)
    q = mod.QdrantVectorStore(url="http://qdrant.internal:6333", collection="c")
    q._connect()
    assert "api_key" not in seen


def test_the_local_folder_wins_over_the_url(tmp_path):
    p = create_provider({"url": "http://should-not-be-used:6333", "path": str(tmp_path / "q")})
    p.upsert([_rec(C1, "item-a", _vec(1.0))])
    assert p.query(_vec(1.0), k=1)[0].chunk_id == C1


# ── point ids ────────────────────────────────────────────────────────────────────────


def test_a_chunk_id_maps_to_a_stable_injective_point_id():
    """Two chunks must never collide onto one point, and the same chunk must always land on the
    same point or an upsert would stop being idempotent."""
    assert _point_id(C1) == _point_id(C1)
    assert len({_point_id(C1), _point_id(C2), _point_id(C3)}) == 3


def test_a_non_hex_chunk_id_is_hashed_rather_than_raising():
    """Nothing in core produces one, but an app must not crash on a shape it did not choose."""
    import uuid as _uuid

    got = _point_id("not-hex-at-all")
    assert _uuid.UUID(got)
    assert got == _point_id("not-hex-at-all")
    assert got != _point_id("also-not-hex")
