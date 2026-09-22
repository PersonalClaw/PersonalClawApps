"""End-to-end: core's HybridRetriever answering out of a REAL Qdrant.

`test_provider.py` proves this bundle against Qdrant's real engine, and core's
`tests/test_knowledge_external_vector_store.py` proves the seam with an in-repo test double.
Neither composes the two, and the atom's clause is about the composition: *"after binding it,
ingesting a doc indexes its chunk vectors into that external store and HybridRetriever fuses the
external store's hits via RRF alongside FTS5 + graph — proven by a fixture that ingests a doc,
runs a query, and asserts a vector-arm hit came from the EXTERNAL backend, not the
sqlite-vec/vec0 path."* So that fixture lives here, where both halves are importable.

This file reaches past `personalclaw.sdk.*` into `personalclaw.knowledge.*` deliberately, and
only because it is a TEST: `tests/test_apps_import_boundary.py` exempts `test_*.py` explicitly
("they legitimately import core test helpers + patch core module paths; they run in the dev tree,
not as an installed app"). `provider.py` itself imports nothing but the SDK — that is the rail,
and this file does not weaken it.

Qdrant runs in-process over a `tmp_path` folder, so there is no server and nothing to clean up.
"""

from __future__ import annotations

import importlib.util
import struct
import uuid

import pytest
from provider import QdrantVectorStore

from personalclaw.knowledge.chunking import Chunk
from personalclaw.knowledge.retrieval import HybridRetriever
from personalclaw.knowledge.store import KnowledgeStore
from personalclaw.vector_stores import registry as vs_registry

DIM = 8

#: See `test_provider.py`'s note: `provider.py` imports `qdrant_client` lazily, so this file
#: collects without the dependency and then fails at the first engine call instead of skipping.
HAVE_QDRANT = importlib.util.find_spec("qdrant_client") is not None


def _vec(*vals: float) -> list[float]:
    return list(vals) + [0.0] * (DIM - len(vals))


def _blob(vals) -> bytes:
    return struct.pack(f"{DIM}f", *vals)


@pytest.fixture()
def wired(tmp_path):
    """A knowledge store whose chunk-vector arm is a real Qdrant.

    Registering the provider is what core's type handler does when the app is enabled, so this
    fixture is the composition a user gets by turning the app on — not a special test path.
    """
    if not HAVE_QDRANT:
        pytest.skip("qdrant-client not installed")
    qdrant = QdrantVectorStore(path=str(tmp_path / "qdrant"), collection="kb")
    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    vs_registry.register_provider(qdrant.name, qdrant)
    try:
        yield store, qdrant
    finally:
        vs_registry.unregister_provider(qdrant.name)
        store.close()


@pytest.fixture()
def wired_unbound(tmp_path):
    """Both halves built, NOTHING registered — the state a user is in before enabling the app.

    Separate from `wired` rather than a flag on it, because the ordering is the whole point of
    the backfill: a corpus ingested while nothing is bound is a corpus the per-item
    write-through never saw.
    """
    if not HAVE_QDRANT:
        pytest.skip("qdrant-client not installed")
    qdrant = QdrantVectorStore(path=str(tmp_path / "qdrant"), collection="kb")
    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    try:
        yield store, qdrant
    finally:
        vs_registry.unregister_provider(qdrant.name)
        store.close()


def _ingest(store, title: str, body: str, vectors) -> str:
    """Ingest a document whose chunk line spans are REAL.

    The body is padded to cover every chunk's declared span, because core's locator validates the
    span against the document it cites and suppresses `line_range` when it does not fit — a
    1-line body with a chunk claiming lines 3-4 silently loses its locator, which would make a
    locator assertion look like a code defect rather than a bad fixture. (Found exactly that way.)
    """
    needed = max((i * 2 + 2 for i in range(len(vectors))), default=1)
    lines = body.split("\n")
    lines += [f"line {n}" for n in range(len(lines) + 1, needed + 1)]
    iid = store.create_typed_item(item_type="note", title=title, content="\n".join(lines))
    store.replace_chunks(
        iid,
        [
            Chunk(
                text=f"passage {i}",
                section=f"sec{i}",
                line_start=i * 2 + 1,
                line_end=i * 2 + 2,
                chunk_index=i,
                embedding=_blob(v),
            )
            for i, v in enumerate(vectors)
        ],
    )
    return iid


def test_ingesting_a_doc_indexes_its_chunk_vectors_into_qdrant(wired):
    store, qdrant = wired
    iid = _ingest(store, "Ingested", "alpha bravo", [_vec(1.0), _vec(0.0, 1.0)])

    # Read them back out of Qdrant itself, not out of a counter this test kept.
    info = qdrant.describe()
    assert info.reachable is True
    assert info.count == 2
    assert info.dimension == DIM

    got = {h.item_id for h in qdrant.query(_vec(1.0), k=10)}
    assert got == {iid}


def test_a_vector_arm_hit_comes_from_qdrant_and_not_from_vec0(wired):
    """The atom's clause, as one executable claim.

    Two documents are ingested, so both are in the LOCAL chunk table and both are an exact
    match for the query. Then document A's vectors are deleted **from Qdrant only** — the local
    `chunks` rows and the local `vec0` index still hold them, untouched.

    A vector-arm result of exactly [B] therefore proves the hit was served by Qdrant: had the
    sqlite-vec/vec0 path run, A would be there too, tied at cosine 1.0. That negative is the
    whole proof; asserting B alone would pass on either backend.
    """
    store, qdrant = wired
    q = _vec(1.0)
    a_id = _ingest(store, "Alpha", "aaa", [q])
    b_id = _ingest(store, "Bravo", "bbb", [q])

    # A is still fully present locally...
    assert len(store.get_chunks(a_id)) == 1
    local_chunk_ids = {c["id"] for c in store.get_chunks(a_id)}
    assert local_chunk_ids
    # ...but no longer in the external store.
    qdrant.delete_item(a_id)
    assert {h.item_id for h in qdrant.query(q, k=10)} == {b_id}

    retriever = HybridRetriever(store, embedder=lambda _q: q)
    ranked = [iid for iid, _ in (retriever._vector_search("zzz-no-keyword-match", limit=10) or [])]

    assert b_id in ranked, "Qdrant's hit reached the vector arm"
    assert a_id not in ranked, (
        "Alpha is present in the LOCAL chunk table and vec0 index and is an exact match for the "
        "query — its absence is what proves the external store served this arm"
    )


def test_the_external_hit_fuses_through_rrf_with_a_local_locator(wired):
    """The full `search()` path: RRF fusion, `match_type`, and the cited passage.

    `match_type == "vector"` (not `keyword+vector`) is what shows the hit arrived through the
    vector arm rather than FTS5 finding the query terms, and the section proves the locator was
    resolved from the local chunk row rather than trusted from Qdrant's payload.
    """
    store, _ = wired
    q = _vec(0.0, 0.0, 1.0)
    iid = _ingest(store, "Fused", "zulu yankee", [_vec(0.0, 1.0), q])

    retriever = HybridRetriever(store, embedder=lambda _q: q)
    hits = retriever.search("nothing here matches these words", limit=5)

    assert [h["id"] for h in hits] == [iid]
    assert hits[0]["match_type"] == "vector"
    assert hits[0]["section"] == "sec1", "the winning chunk's own section, from the local row"
    assert hits[0]["line_range"] == [3, 4]


def test_fts5_and_graph_arms_still_answer_alongside_qdrant(wired):
    """"alongside FTS5 + graph" — the other arms are untouched by binding a store.

    A query that matches on TERMS returns the document through the keyword arm, and the
    attribution names both arms, so fusion really is combining an external vector hit with a
    local keyword hit.
    """
    store, _ = wired
    q = _vec(1.0)
    iid = _ingest(store, "Distinctivetitleword", "distinctivebodyword here", [q])

    retriever = HybridRetriever(store, embedder=lambda _q: q)
    hits = retriever.search("distinctivebodyword", limit=5)

    assert [h["id"] for h in hits] == [iid]
    assert "keyword" in hits[0]["match_type"]
    assert "vector" in hits[0]["match_type"], "both arms contributed to the fused hit"


def test_deleting_a_document_removes_it_from_qdrant_too(wired):
    store, qdrant = wired
    iid = _ingest(store, "Doomed", "ddd", [_vec(1.0)])
    assert qdrant.describe().count == 1
    store.delete_item(iid)
    assert qdrant.query(_vec(1.0), k=10) == []


def test_a_re_poll_that_adds_one_doc_indexes_only_that_doc(wired):
    """Clause 3's incremental half, end to end against the real store: the first document's
    points are still the same points afterwards — not deleted and rewritten."""
    store, qdrant = wired
    first = _ingest(store, "First", "one", [_vec(1.0)])
    before = {h.chunk_id for h in qdrant.query(_vec(1.0), k=10)}
    assert len(before) == 1

    second = _ingest(store, "Second", "two", [_vec(0.0, 1.0), _vec(0.0, 0.0, 1.0)])

    assert qdrant.describe().count == 3, "two added, one untouched — not a full re-index"
    still_there = {h.chunk_id for h in qdrant.query(_vec(1.0), k=10)}
    assert before <= still_there
    assert {h.item_id for h in qdrant.query(_vec(0.0, 1.0), k=10)} >= {second}
    assert {h.item_id for h in qdrant.query(_vec(1.0), k=10)} >= {first}


def test_a_rechunk_leaves_no_orphan_vectors_in_qdrant(wired):
    store, qdrant = wired
    iid = _ingest(store, "Rechunked", "v1", [_vec(1.0), _vec(0.0, 1.0)])
    assert qdrant.describe().count == 2

    store.replace_chunks(
        iid,
        [
            Chunk(
                text="v2",
                section="only",
                line_start=1,
                line_end=1,
                chunk_index=0,
                embedding=_blob(_vec(0.0, 0.0, 1.0)),
            )
        ],
    )
    assert qdrant.describe().count == 1, "the previous generation's vectors are gone"
    assert {h.item_id for h in qdrant.query(_vec(0.0, 0.0, 1.0), k=10)} == {iid}


def test_chunk_ids_in_qdrant_are_the_stores_own_chunk_ids(wired):
    """The join core relies on: Qdrant's payload `chunk_id` must be the id in the local `chunks`
    table, or every external candidate would be dropped by the liveness join and the arm would
    silently return nothing."""
    store, qdrant = wired
    iid = _ingest(store, "Joined", "jjj", [_vec(1.0)])
    local = {c["id"] for c in store.get_chunks(iid)}
    remote = {h.chunk_id for h in qdrant.query(_vec(1.0), k=10)}
    assert remote == local
    for cid in local:
        uuid.UUID(hex=cid)  # core mints uuid4().hex; the point-id mapping depends on it


def test_an_unreachable_store_degrades_without_substituting(tmp_path):
    """Bound but broken: the keyword arm still answers and the vector arm contributes nothing —
    it does NOT quietly answer from the local vec0 index, which still holds the vectors.

    Driven with a real provider pointed at a dead port rather than a raising stub, so the failure
    is the vendor client's own (a connection error out of qdrant-client), not a shape invented
    here.
    """
    store = KnowledgeStore(str(tmp_path / "k.db"))
    dead = QdrantVectorStore(url="http://127.0.0.1:1/nope", collection="kb", timeout_secs=1)
    try:
        q = _vec(1.0)
        # Ingest BEFORE binding, so the local chunk rows and vec0 index are fully populated.
        _ingest(store, "Keywordfindable", "sentinelsearchword", [q])
        assert dead.describe().reachable is False

        vs_registry.register_provider(dead.name, dead)
        retriever = HybridRetriever(store, embedder=lambda _q: q)
        hits = retriever.search("sentinelsearchword", limit=5)

        assert hits, "FTS5 still answers — a dead index must never fail a search"
        assert "vector" not in hits[0]["match_type"], "no silent fallback to the local vec0 index"
    finally:
        vs_registry.unregister_provider(dead.name)
        store.close()


# ── #3139: the same-dimension model switch, against the real engine ───────────────────
#
# The acceptance proof for the fourth chunk-vector write site. `reembed_stale_chunks` is the
# only route that rewrites vectors under UNCHANGED chunk ids, and a same-dimension switch is
# the only shape in which that is invisible: the row COUNTS match, so the local index's
# reconciliation sees nothing wrong, and RET-4's freshness join reads the LOCAL row — which
# was just re-stamped with the new model. An external store left alone therefore keeps the
# PREVIOUS model's numbers under ids core now certifies as fresh, and answers with them.
#
# A dimension change would be caught by row-count/dimension reconciliation and would not test
# this seam, which is why both models below are 8-dim.

#: Two different embedding models at the SAME dimension.
_MODEL_A = ("all-minilm-l6-v2", "native")
_MODEL_B = ("bge-small-en", "native")


def _bind_embedding_model(monkeypatch, spec) -> None:
    """Bind the active embedding selection every fingerprint read resolves through.

    `active_fingerprint()` reads `_active_embedding_spec()`, so this is the one place a model
    switch happens as far as staleness is concerned — the same accessor the product uses.
    """
    provider_model = None if spec is None else (spec[1], spec[0])
    monkeypatch.setattr(
        "personalclaw.embedding_providers.registry._active_embedding_spec",
        lambda: provider_model,
    )


class _ModelEmbedder:
    """An embedding provider that always answers with one model's vector."""

    def __init__(self, vector) -> None:
        self._vector = list(vector)

    def embed(self, text):
        return list(self._vector)

    def embed_for_item(self, title, summary, content=None):
        return list(self._vector)

    def is_available(self):
        return True


def test_a_same_dimension_model_switch_moves_qdrants_vectors_and_the_ranking(
    wired, monkeypatch, caplog
):
    """Bind Qdrant, switch embedding model at the same dimension, re-embed — and Qdrant serves
    the NEW model's vectors under the same chunk ids, with the ranking moved to match.

    Asserted by querying Qdrant itself in both directions rather than by reading bytes back:
    after the switch the re-embedded chunk must score ~1.0 against model B's direction and ~0.0
    against model A's. A store that was never mirrored would score exactly the other way round
    while every local row claims to be fresh.

    `caplog` is load-bearing, not decoration. `_external_replace_item` wraps its whole body in a
    blanket `except Exception` logged at WARNING, so a mirror that raises — on a wrong row
    width, or on the real vendor client — writes nothing and still leaves this suite's
    ingestion-time assertions green. A warning here means the proof below is vacuous.
    """
    store, qdrant = wired
    a_dir, b_dir = _vec(1.0), _vec(0.0, 1.0)

    _bind_embedding_model(monkeypatch, _MODEL_A)
    stale = _ingest(store, "Reembedded", "aaa", [a_dir])
    # A second document already on model B, so the pass has exactly one item in scope and the
    # ranking assertion has something to outrank.
    other = _ingest(store, "Untouched", "bbb", [_vec(0.0, 0.0, 1.0)])
    store.db.execute(
        "UPDATE chunks SET embedding_model_id = ?, embedding_provider = ? WHERE item_id = ?",
        (_MODEL_B[0], _MODEL_B[1], other),
    )
    store.db.commit()

    (a_chunk,) = [c["id"] for c in store.get_chunks(stale)]
    before = qdrant.query(b_dir, k=10)
    assert all(h.similarity < 0.5 for h in before), "nothing points at model B's direction yet"
    assert qdrant.describe().count == 2

    _bind_embedding_model(monkeypatch, _MODEL_B)
    assert store.count_stale_chunk_vectors() == 1, "exactly one chunk is on the old model"

    with caplog.at_level("WARNING"):
        result = store.reembed_stale_chunks(_ModelEmbedder(b_dir))

    swallowed = [
        r.getMessage() for r in caplog.records if "external vector store" in r.getMessage()
    ]
    assert not swallowed, f"the mirror swallowed a failure, so this proof is vacuous: {swallowed}"
    assert result["reembedded"] == 1 and result["stale_remaining"] == 0

    # 1. The vectors moved, read out of Qdrant in both directions.
    assert qdrant.describe().count == 2, "an in-place rewrite, not a re-index"
    assert [c["id"] for c in store.get_chunks(stale)] == [a_chunk], "the chunk id is preserved"
    hit = next(h for h in qdrant.query(b_dir, k=10) if h.chunk_id == a_chunk)
    assert hit.similarity > 0.99, "Qdrant now holds model B's vector for that chunk"
    gone = next(h for h in qdrant.query(a_dir, k=10) if h.chunk_id == a_chunk)
    assert gone.similarity < 0.01, (
        "model A's vector is no longer what Qdrant serves for this chunk — this is the "
        "assertion an unmirrored external store fails while every local row reads as fresh"
    )

    # 2. The answer moved with them.
    retriever = HybridRetriever(store, embedder=lambda _q: b_dir)
    ranked = [iid for iid, _ in (retriever._vector_search("zzz-no-keyword-match", limit=10) or [])]
    assert ranked[:1] == [stale], f"the external ranking did not move: {ranked}"
    assert other not in ranked[:1]


# ── binding over an EXISTING library: the backfill, against the real engine ───────────
#
# The write-through mirrors one item at a time as it is ingested, so a store bound AFTER a
# corpus exists has seen none of it. That state is not loud: a freshly-created Qdrant is
# perfectly REACHABLE, so the seam's fail-soft WARNING never fires — the chunk arm simply
# answers nothing, which reads identically to "no matches". Only a backfill closes it, and
# only a real engine proves the backfill wrote points a query can find.


def test_binding_over_an_existing_library_backfills_qdrant(wired_unbound):
    """Enabling the app over a library that already exists fills the store.

    Asserted against the real engine in both directions: empty-and-reachable before the bind
    (so the silence is the defect, not an error), and answering a search after it.
    """
    store, qdrant = wired_unbound
    q = _vec(1.0)
    iid = _ingest(store, "Ingested before binding", "alpha", [q, _vec(0.0, 1.0)])
    # MEASURED: a never-written Qdrant reports `count=None`, not 0 — it creates the collection
    # on first write, and describe() says "collection not created yet". That is exactly why a
    # backfill's cheap skip must treat an unreportable count as "must walk" rather than "in
    # sync": against this real provider `None == local_chunks` never holds, but a backend that
    # DID report 0 here would be skipped into permanent emptiness by an `is not None` slip.
    assert qdrant.describe().count in (0, None), "nothing was mirrored while nothing was bound"
    assert qdrant.query(q, k=10) == [], "and nothing is retrievable from it"

    vs_registry.register_provider(qdrant.name, qdrant)
    try:
        result = store.reindex_external_vector_store()
        assert result["items"] == 1 and result["chunks"] == 2, result
        assert qdrant.describe().count == 2
        assert {h.item_id for h in qdrant.query(q, k=10)} == {iid}

        hits = HybridRetriever(store, embedder=lambda _q: q).search("zzqqxx nothing", limit=5)
        assert [h["id"] for h in hits] == [iid], "and the backfilled vectors answer a search"
    finally:
        vs_registry.unregister_provider(qdrant.name)


def test_qdrant_is_installed_so_the_engine_suite_is_not_vacuous():
    """A missing `qdrant-client` must read as a RED, not as a quiet row of skips — every test
    above is gated on the `wired` fixture, which skips without it, and a file of skips is
    indistinguishable from a file of passes in a CI summary. Install the bundle's declared
    dependencies (`./scripts/test-bundles` does) rather than trusting a green run without them.
    """
    assert HAVE_QDRANT, (
        "qdrant-client is not installed, so every engine assertion in this file was skipped — "
        "run this bundle through ./scripts/test-bundles, which installs app.json's declared "
        "pythonDependencies, instead of invoking pytest against a bare environment"
    )
