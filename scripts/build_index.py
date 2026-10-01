"""Extrai texto (com OCR quando necessário), divide em trechos e indexa no ChromaDB + FTS5.

Uso:  .venv/Scripts/python scripts/build_index.py
Indexa somente as fontes em rag.config.INDEXED_SOURCES (fornecedores/subcontratadas).
Recria o índice do zero a cada execução; o texto extraído/OCR fica em cache.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from pathlib import Path

import numpy as np
import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rag import config as C  # noqa: E402
from rag.core import collection, embed_passages, fts_conn  # noqa: E402

_ocr = None


def ocr_page(page: pymupdf.Page) -> str:
    global _ocr
    if _ocr is None:
        from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR
        _ocr = RapidOCR(params={
            "Rec.lang_type": LangRec.LATIN,
            "Rec.ocr_version": OCRVersion.PPOCRV5,
            "Rec.model_type": ModelType.MOBILE,
        })
    pix = page.get_pixmap(dpi=C.OCR_DPI)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)[:, :, :3]
    res = _ocr(img)
    return "\n".join(res.txts) if res.txts else ""


def clean(text: str) -> str:
    text = text.replace("­", "").replace("\xa0", " ")
    text = re.sub(r"-\n(?=[a-zà-ú])", "", text)  # hifenização de fim de linha
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_pages(path: Path, sha: str, cache: dict) -> list[dict]:
    if sha in cache:
        return cache[sha]
    pages = []
    with pymupdf.open(path) as doc:
        for i, page in enumerate(doc, 1):
            text = page.get_text("text", sort=True)
            method = "texto"
            if len(text.strip()) < C.OCR_MIN_CHARS:
                ocr = ocr_page(page)
                if len(ocr.strip()) > len(text.strip()):
                    text, method = ocr, "ocr"
            pages.append({"page": i, "text": clean(text), "method": method})
    cache[sha] = pages
    return pages


def chunk_page(text: str) -> list[str]:
    if len(text) <= C.CHUNK_CHARS:
        return [text] if text else []
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n(?=\s*(?:\d+(?:\.\d+)*[.)]?\s|[a-z]\)\s|[•\-–]\s))", text) if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        while len(p) > C.CHUNK_CHARS:  # parágrafo gigante: corta em limite de frase/palavra
            cut = p.rfind(". ", 0, C.CHUNK_CHARS)
            cut = cut + 1 if cut > C.CHUNK_CHARS // 2 else p.rfind(" ", 0, C.CHUNK_CHARS)
            cut = cut if cut > 0 else C.CHUNK_CHARS
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(p[:cut].strip())
            p = p[cut:].strip()
        if cur and len(cur) + len(p) + 1 > C.CHUNK_CHARS:
            chunks.append(cur)
            tail = cur[-C.CHUNK_OVERLAP:]
            tail = tail[tail.find(" ") + 1:] if " " in tail else tail
            cur = f"{tail}\n{p}"
        else:
            cur = f"{cur}\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks


def doc_id_for(relpath: str) -> str:
    return re.sub(r"[^\w-]+", "_", Path(relpath).stem).strip("_")


def main() -> None:
    t0 = time.time()
    manifest = json.loads(C.MANIFEST.read_text(encoding="utf-8"))
    items = [m for m in manifest if m["source"] in C.INDEXED_SOURCES and m["status"] in ("ok", "cached")]

    C.INDEX_DIR.mkdir(parents=True, exist_ok=True)
    cache: dict = {}
    if C.TEXT_CACHE.exists():
        for line in C.TEXT_CACHE.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            cache[rec["sha"]] = rec["pages"]

    docs, seen_sha = [], {}
    for it in items:
        path = C.PDF_DIR / it["relpath"]
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        if sha in seen_sha:
            print(f"  duplicado: {it['relpath']} == {seen_sha[sha]}")
            continue
        seen_sha[sha] = it["relpath"]
        pages = extract_pages(path, sha, cache)
        n_ocr = sum(p["method"] == "ocr" for p in pages)
        print(f"  {len(pages):>3} pág ({n_ocr} OCR)  {it['relpath']}")
        docs.append((it, sha, pages))

    C.TEXT_CACHE.write_text(
        "\n".join(json.dumps({"sha": s, "pages": p}, ensure_ascii=False) for s, p in cache.items()),
        encoding="utf-8",
    )

    ids, texts, metas = [], [], []
    catalog = []
    for it, sha, pages in docs:
        did = doc_id_for(it["relpath"])
        fname = Path(it["relpath"]).name
        n_chunks = 0
        for p in pages:
            for j, ch in enumerate(chunk_page(p["text"])):
                if len(ch) < 30:
                    continue
                ids.append(f"{did}:p{p['page']}:c{j}")
                texts.append(f"[{it['title']} | {fname} | página {p['page']}]\n{ch}")
                metas.append({
                    "doc_id": did, "title": it["title"], "file": fname, "page": p["page"],
                    "source": it["source"], "category": it["category"], "url": it["url"],
                    "path": it["relpath"], "extraction": p["method"],
                })
                n_chunks += 1
        catalog.append({
            "doc_id": did, "title": it["title"], "file": fname, "path": it["relpath"],
            "url": it["url"], "pages": len(pages), "chunks": n_chunks, "sha256": sha,
            "ocr_pages": sum(p["method"] == "ocr" for p in pages),
        })

    print(f"{len(ids)} trechos; gerando embeddings...", flush=True)
    col = collection(reset=True)
    con = fts_conn()
    con.execute("DELETE FROM chunks")
    B = 64
    for i in range(0, len(ids), B):
        embs = embed_passages(texts[i:i + B])
        col.add(ids=ids[i:i + B], documents=texts[i:i + B], metadatas=metas[i:i + B], embeddings=embs)
        con.executemany(
            "INSERT INTO chunks(id, doc_id, title, text) VALUES (?,?,?,?)",
            [(ids[k], metas[k]["doc_id"], metas[k]["title"], texts[k]) for k in range(i, min(i + B, len(ids)))],
        )
        print(f"  {min(i + B, len(ids))}/{len(ids)}", flush=True)
    con.commit()
    con.close()
    (C.INDEX_DIR / "catalog.json").write_text(json.dumps(catalog, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Pronto: {len(catalog)} documentos, {len(ids)} trechos em {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
