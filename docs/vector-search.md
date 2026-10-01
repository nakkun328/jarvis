# Phase 2 vector search decision

Reviewed 2026-09-29 against the vendors' documentation. The local Chroma adapter is
implemented behind the vector contract. Embedding generation, canonical indexing,
and live retrieval quality checks remain later work.

## Comparison

| Criterion | Chroma | Qdrant |
| --- | --- | --- |
| Local setup | Python `PersistentClient` stores data in a local directory without a separate service. The same product also has an HTTP server mode. [Clients](https://docs.trychroma.com/docs/run-chroma/cloud-client?lang=typescript), [client/server mode](https://docs.trychroma.com/production/chroma-server/client-server-mode) | The standard local server quickstart uses Docker and a mounted storage directory. Qdrant Edge runs in-process with local disk, but its API is currently beta. [Quickstart](https://qdrant.tech/documentation/quickstart/), [Edge](https://qdrant.tech/documentation/edge/) |
| Portability | Local persistence and HTTP server modes exist. Chroma has documented on-disk migration changes, so copying an index directory is not our cross-engine export format. [Clients](https://docs.trychroma.com/docs/run-chroma/cloud-client?lang=typescript), [migration](https://docs.trychroma.com/updates/migration) | Server collection snapshots contain vectors, payloads and index configuration, but are Qdrant-specific and have distributed restore constraints. Edge can synchronize with a server. [Snapshots](https://qdrant.tech/documentation/operations/snapshots/), [Edge synchronization](https://qdrant.tech/documentation/edge/edge-synchronization-guide/) |
| Indexing and filters | Chroma supports vector search and metadata filters; its local HNSW index occupies RAM. This is adequate for the initial small personal corpus, subject to measurement. [Collection API](https://docs.trychroma.com/reference/js-collection), [performance](https://docs.trychroma.com/guides/deploy/performance) | Qdrant has dense HNSW, sparse indexes, payload indexes and configurable storage tiers. These are useful when filtered search or corpus size becomes demanding. [Indexing](https://qdrant.tech/documentation/manage-data/indexing/), [storage](https://qdrant.tech/documentation/manage-data/storage/) |
| Operational cost | A local client avoids a service, port, and server lifecycle for one user. A shared deployment adds a Chroma server to maintain. [Clients](https://docs.trychroma.com/reference/python), [client/server mode](https://docs.trychroma.com/production/chroma-server/client-server-mode) | The server adds a process/container, storage, backup and network configuration. Edge avoids the server for local use, with beta API risk. Self-hosted server security needs explicit setup. [Quickstart](https://qdrant.tech/documentation/quickstart/), [Edge](https://qdrant.tech/documentation/edge/), [security](https://qdrant.tech/documentation/security/) |
| Cross-device | One central Chroma server can be reached through HTTP clients; the embedded local directory alone does not synchronize devices. [Client/server mode](https://docs.trychroma.com/production/chroma-server/client-server-mode) | A central Qdrant server supports network clients and distributed deployment. Edge has documented, but application-managed, synchronization patterns. [Quickstart](https://qdrant.tech/documentation/quickstart/), [distributed deployment](https://qdrant.tech/documentation/scaling/distributed_deployment/), [Edge synchronization](https://qdrant.tech/documentation/edge/edge-synchronization-guide/) |

## Decision and boundaries

For the first single-device JARVIS vector implementation, **use Chroma's local
persistent client** behind `backend.memory.vector.VectorIndex`. The adapter accepts
caller-generated embeddings so the embedding model can be chosen separately. This
minimizes operations while the corpus is small. Reassess Qdrant server when multiple
devices must query one index, filtered
retrieval or index size exceeds measured Chroma performance, or centralized operations
become necessary. Qdrant Edge is another local option, but its beta status makes it
less suitable as the initial dependency today. The choice is an inference from the
linked capabilities and this project's current single-user scope, not a benchmark.

SQLite memory IDs, content, provenance, importance and confidence, plus editable
Obsidian notes, remain authoritative. The vector index is a derived cache. Each
record carries only a memory ID, an embedding and a `space` identifying the exact
model/version. Retrieval resolves returned IDs through the canonical memory store
and applies its access, freshness and conflict rules there. Scores are only ranked
within one space; they are not confidence values or probabilities.

## Migration path

1. Choose a versioned embedding space and dimension. Persist the model identity and
   source-to-memory ID mapping in canonical metadata before indexing approved memories.
2. Build the chosen index by replaying canonical memory records. Keep a rebuild
   command and verify count, IDs and representative search results against the source.
3. For a new engine, create a parallel index from those records. If the embedding
   model is unchanged, reuse stored embeddings where available; if it changes,
   generate new embeddings into a separate space. Never compare scores across spaces.
4. Switch the adapter after validation; retain the old index for rollback until the
   new one is accepted. Vendor snapshots can back up an index but are not the
   cross-vendor migration format.

## Local adapter

Install the optional dependency with `pip install -e '.[vector]'`. The adapter uses
Chroma's [PersistentClient](https://docs.trychroma.com/reference/python/client) and
passes precomputed vectors to the [collection upsert/query API](https://docs.trychroma.com/reference/python/collection).
It does not call an embedding model or store note text. Each exact `space` gets a
separate collection named from a hash, with its space and vector dimension in
collection metadata. Dimension mismatches are rejected. `delete` removes a memory ID
from every JARVIS space. The adapter creates the index root with private directory
permissions and rejects a symlinked or publicly readable root on POSIX systems.

```python
from pathlib import Path
from backend.memory.chroma import ChromaVectorIndex
from backend.memory.vector import VectorQuery, VectorRecord

index = ChromaVectorIndex(Path("data/vectors"))
await index.upsert([VectorRecord("memory-uuid", "embedding-model-v1", (0.1, 0.2))])
matches = await index.search(VectorQuery("embedding-model-v1", (0.1, 0.2)))
```

The returned score is the negative Chroma distance, so higher means nearer within
one space. It is neither confidence nor a probability. The Chroma collection is a
rebuildable cache; query results still need canonical SQLite/Obsidian resolution.
Local persistence, space isolation, replacement, deletion, and dimension validation
are covered by tests against Chroma itself. No OpenAI API key is required. Live
embeddings, retrieval quality, latency, and a source-to-index rebuild command remain
future validation.
