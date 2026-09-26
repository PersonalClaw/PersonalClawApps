# Qdrant Vector Store

Point PersonalClaw's knowledge vector search at **your own Qdrant** instead of the built-in
`sqlite-vec` index.

## What changes when you enable it

Hybrid retrieval has three arms — FTS5 keyword search, graph traversal, and vector search —
fused with RRF. This app replaces the **chunk-vector half of the vector arm** and nothing else:

| | Where it lives with this app enabled |
|---|---|
| Your documents, their text, sections, line numbers | PersonalClaw's own database, unchanged |
| Chunk **vectors** | Qdrant **and** PersonalClaw's database |
| Chunk-vector **search** | Qdrant |
| Whole-document (title + summary) vector search | PersonalClaw, unchanged |
| Keyword (FTS5) and graph arms, RRF fusion, the similarity floor, ranking | PersonalClaw, unchanged |

Qdrant holds vectors plus chunk ids and locators — not your document text. PersonalClaw remains
the record store; Qdrant is an index. That is deliberate: if your Qdrant is unreachable, you
lose vector recall for a query, not data.

**Enabling the app is the binding.** There is no separate setting to point knowledge at it.
Disable the app and retrieval returns to the built-in index immediately. Enable *two* external
vector stores and PersonalClaw refuses to pick between them and uses the built-in index,
logging a warning that names both — answering out of a store you didn't choose is worse than
not using either.

## Setup

### Option A — a Qdrant you run (recommended)

```bash
docker run -p 6333:6333 -v "$PWD/qdrant-data:/qdrant/storage" qdrant/qdrant
```

Then install this app, leave **Qdrant URL** at `http://localhost:6333`, pick a **Collection**
name, and enable it.

### Option B — no server at all

Set **Local folder (no server)** under Advanced to a path such as `~/qdrant-knowledge`. Qdrant's
engine then runs in-process over that folder — nothing to install. The folder is locked to a
single process, so the gateway must be its only reader; use Option A if you also want to query
the store from your own scripts.

### Authentication

If your Qdrant requires an api key (a Qdrant Cloud cluster, or a server started with one), put it
in **Qdrant API Key**. The settings file never holds it: PersonalClaw keeps the key in its
credential store under a name this app owns, the file keeps only a reference to it, and
uninstalling the app removes it. Leave the field empty to fall back to the `QDRANT_API_KEY`
environment variable. No key is fine and normal for a local Qdrant.

The app reads its settings when it is enabled, so a changed key or URL takes effect the next time
it is: disable and re-enable the app, or restart the gateway.

### Collections

Use a **fresh collection name**. This app owns the points it writes and deletes them by an
`item_id` payload filter when a document is re-ingested or removed, so pointing it at a
collection holding other data risks deleting that data. The collection is created on first use
at the dimension of whatever embedding model you have bound; changing embedding models means
using a new collection name (vectors from two models are not comparable).

## What is proven, and what is not

Honesty about coverage, because "supports Qdrant/pgvector/Chroma" is easy to claim and hard to
verify:

- **Qdrant — proven end to end.** `test_provider.py` drives Qdrant's real engine in-process
  (`QdrantClient(path=...)`): real collection DDL, real upsert, real filtered delete, real KNN
  search, and an assertion that Qdrant's returned score equals the cosine similarity
  PersonalClaw would have computed itself to within 1e-6 — which is what lets PersonalClaw keep
  applying its own calibrated similarity floor.
- **Qdrant over HTTP to a separate server — the same code, proven against a fake.** Both modes
  share every method; only the client constructor differs. `test_api_key.py` drives the server
  mode over a real socket against a fake Qdrant that refuses any request without the right api
  key: a key saved on Configure reaches every call, and a missing or wrong one is refused. No
  test in this bundle has talked to a real networked Qdrant.
- **pgvector and Chroma — not implemented.** The seam they would use
  (`personalclaw.sdk.vector_store`) is published and vendor-neutral, and a pgvector or Chroma
  app is four methods: `upsert`, `delete_item`, `query`, `describe`. They are not in this
  bundle and nothing in PersonalClaw pretends otherwise.

## Writing your own backend

```python
from personalclaw.sdk.vector_store import (
    VectorHit, VectorRecord, VectorStoreInfo, VectorStoreProvider,
)

class MyStore(VectorStoreProvider):
    name = "my-store"

    def upsert(self, records: list[VectorRecord]) -> int: ...
    def delete_item(self, item_id: str) -> int: ...
    def query(self, vector, *, k: int) -> list[VectorHit]: ...
    def describe(self) -> VectorStoreInfo: ...
```

Three rules the seam depends on:

1. **`query` returns hits sorted by descending cosine similarity.** PersonalClaw stops walking
   at the first hit below its floor, on the argument that every later hit is below it too. Out
   of order, you silently truncate your own recall.
2. **`similarity` is cosine similarity in `[-1, 1]`, not a distance and not a vendor-scaled
   score.** Convert if your engine returns a distance.
3. **`delete_item` is idempotent**, including for an item the store never held — PersonalClaw
   calls it before every re-chunk.

Declare `"type": "vector_store"` in your `app.json` provider block. Everything vendor-specific
— the client library, the endpoint, the auth, the schema — belongs in your bundle; core carries
no vector-store client at all.

## Licence

MIT. See `LICENSE`.
