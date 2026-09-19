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

import struct
import uuid

import pytest
from provider import QdrantVectorStore

from personalclaw.knowledge.chunking import Chunk
from personalclaw.knowledge.retrieval import HybridRetriever
from personalclaw.knowledge.store import KnowledgeStore
from personalclaw.vector_stores import registry as vs_registry

DIM = 8


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
    qdrant = QdrantVectorStore(path=str(tmp_path / "qdrant"), collection="kb")
    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    vs_registry.register_provider(qdrant.name, qdrant)
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
