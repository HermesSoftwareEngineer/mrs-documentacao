"""Componentes compartilhados: embeddings, ChromaDB, FTS5 (BM25) e busca híbrida."""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from functools import lru_cache

import truststore

truststore.inject_into_ssl()  # downloads do HuggingFace atrás de inspeção SSL

import chromadb  # noqa: E402
from fastembed import TextEmbedding  # noqa: E402
from fastembed.common.model_description import ModelSource, PoolingType  # noqa: E402

from . import config as C  # noqa: E402


@lru_cache(maxsize=1)
def embedder() -> TextEmbedding:
    try:
        TextEmbedding.add_custom_model(
            model=C.EMBED_MODEL,
            pooling=PoolingType.MEAN,
            normalization=True,
            sources=ModelSource(hf=C.EMBED_MODEL),
            dim=C.EMBED_DIM,
            model_file="onnx/model.onnx",
        )
    except ValueError:
        pass  # já registrado
    return TextEmbedding(C.EMBED_MODEL, cache_dir=str(C.MODELS_DIR))


def embed_passages(texts: list[str]) -> list[list[float]]:
    return [v.tolist() for v in embedder().embed([f"passage: {t}" for t in texts], batch_size=16)]


def embed_query(text: str) -> list[float]:
    return next(iter(embedder().embed([f"query: {text}"]))).tolist()


def collection(reset: bool = False):
    client = chromadb.PersistentClient(path=str(C.CHROMA_DIR))
    if reset:
        try:
            client.delete_collection(C.COLLECTION)
        except Exception:  # noqa: BLE001
            pass
    return client.get_or_create_collection(C.COLLECTION, metadata={"hnsw:space": "cosine"})


def fts_conn() -> sqlite3.Connection:
    C.INDEX_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(C.FTS_DB)
    con.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5("
        "id UNINDEXED, doc_id UNINDEXED, title, text, tokenize='unicode61 remove_diacritics 2')"
    )
    return con


def _fts_query(q: str) -> str:
    q = unicodedata.normalize("NFKC", q)
    terms = [t for t in re.findall(r"[\w-]+", q) if len(t) > 1]
    # termos com hífen (ex.: MN-SMA-0013) viram frase exata; demais em OR
    return " OR ".join(f'"{t}"' for t in terms)


def hybrid_search(query: str, k: int = 8, doc_id: str | None = None) -> list[dict]:
    """Busca vetorial (e5) + BM25 (FTS5) combinadas por Reciprocal Rank Fusion."""
    col = collection()
    n = max(k * 4, 20)
    where = {"doc_id": doc_id} if doc_id else None
    vec = col.query(query_embeddings=[embed_query(query)], n_results=n, where=where)
    scores: dict[str, float] = {}
    for rank, cid in enumerate(vec["ids"][0]):
        scores[cid] = scores.get(cid, 0) + 1 / (60 + rank)

    fq = _fts_query(query)
    if fq:
        con = fts_conn()
        sql = "SELECT id FROM chunks WHERE chunks MATCH ?" + (" AND doc_id = ?" if doc_id else "") + " ORDER BY bm25(chunks) LIMIT ?"
        args = [fq] + ([doc_id] if doc_id else []) + [n]
        try:
            for rank, (cid,) in enumerate(con.execute(sql, args)):
                scores[cid] = scores.get(cid, 0) + 1 / (60 + rank)
        except sqlite3.OperationalError:
            pass
        con.close()

    top = sorted(scores, key=scores.get, reverse=True)[:k]
    if not top:
        return []
    got = col.get(ids=top, include=["documents", "metadatas"])
    by_id = {i: (d, m) for i, d, m in zip(got["ids"], got["documents"], got["metadatas"])}
    out = []
    for cid in top:
        doc, meta = by_id[cid]
        out.append({"id": cid, "score": round(scores[cid], 5), "text": doc, **meta})
    return out
