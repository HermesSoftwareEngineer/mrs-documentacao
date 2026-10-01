"""Servidor MCP (stdio) de RAG sobre os requisitos da MRS para fornecedores e subcontratadas.

Ferramentas:
  buscar_requisitos  – busca híbrida (semântica + palavra-chave) com citação de documento/página
  listar_documentos  – catálogo dos documentos indexados
  ler_paginas        – texto integral de páginas de um documento (para contexto completo)
"""
from __future__ import annotations

import json
import sys
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.mcpserver import MCPServer  # noqa: E402

from rag import config as C  # noqa: E402
from rag.core import hybrid_search  # noqa: E402

mcp = MCPServer(
    "mrs-fornecedores",
    instructions=(
        "Base de conhecimento com os documentos oficiais da MRS Logística para fornecedores e "
        "subcontratadas (requisitos de SMA, segurança, meio ambiente, acesso de terceiros, gestão de "
        "documentos, SAP Ariba, condições gerais de fornecimento, IDF etc.). Use buscar_requisitos para "
        "responder perguntas e sempre cite documento e página. Use ler_paginas quando precisar do "
        "contexto completo de uma seção."
    ),
)


@lru_cache(maxsize=1)
def catalog() -> dict[str, dict]:
    return {d["doc_id"]: d for d in json.loads((C.INDEX_DIR / "catalog.json").read_text(encoding="utf-8"))}


@lru_cache(maxsize=1)
def pages_by_sha() -> dict[str, list[dict]]:
    out = {}
    for line in C.TEXT_CACHE.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        out[rec["sha"]] = rec["pages"]
    return out


def _resolve(documento: str) -> dict | None:
    cat = catalog()
    if documento in cat:
        return cat[documento]
    q = documento.lower().replace(" ", "")
    for d in cat.values():
        if q in d["doc_id"].lower().replace("_", "").replace("-", "") or q in d["title"].lower().replace(" ", ""):
            return d
    return None


@mcp.tool()
def buscar_requisitos(pergunta: str, quantidade: int = 8, documento: str | None = None) -> str:
    """Busca trechos relevantes nos documentos da MRS para fornecedores e subcontratadas.

    Args:
        pergunta: pergunta ou termos em linguagem natural (ex.: "exigências de EPI para contratadas",
            "prazo para comunicar acidente", "MN-SMA-0013 trabalho em altura").
        quantidade: número de trechos a retornar (1-20).
        documento: opcional, restringe a um documento (doc_id de listar_documentos ou parte do nome,
            ex.: "MN-SMA-0013").
    """
    quantidade = max(1, min(int(quantidade), 20))
    doc_id = None
    if documento:
        d = _resolve(documento)
        if not d:
            return f"Documento '{documento}' não encontrado. Use listar_documentos."
        doc_id = d["doc_id"]
    hits = hybrid_search(pergunta, k=quantidade, doc_id=doc_id)
    if not hits:
        return "Nenhum trecho encontrado."
    parts = []
    for i, h in enumerate(hits, 1):
        body = h["text"].split("\n", 1)[1] if h["text"].startswith("[") else h["text"]
        parts.append(
            f"### [{i}] {h['title']} — página {h['page']}\n"
            f"doc_id: {h['doc_id']} | arquivo: {h['file']} | url: {h['url']}"
            + (" | (texto via OCR)" if h.get("extraction") == "ocr" else "")
            + f"\n\n{body}"
        )
    return "\n\n---\n\n".join(parts)


@mcp.tool()
def listar_documentos() -> str:
    """Lista os documentos indexados (doc_id, título, nº de páginas, link de origem)."""
    lines = [f"{len(catalog())} documentos indexados:\n"]
    for d in sorted(catalog().values(), key=lambda x: x["title"]):
        lines.append(f"- **{d['title']}** — doc_id: `{d['doc_id']}` — {d['pages']} pág. — {d['url']}")
    return "\n".join(lines)


@mcp.tool()
def ler_paginas(documento: str, pagina_inicial: int, pagina_final: int | None = None) -> str:
    """Retorna o texto integral de uma ou mais páginas de um documento (máx. 10 páginas por chamada).

    Args:
        documento: doc_id (de listar_documentos/buscar_requisitos) ou parte do nome, ex.: "PG-SMA-0037".
        pagina_inicial: primeira página (1 = primeira).
        pagina_final: última página (padrão = pagina_inicial).
    """
    d = _resolve(documento)
    if not d:
        return f"Documento '{documento}' não encontrado. Use listar_documentos."
    pages = pages_by_sha()[d["sha256"]]
    ini = max(1, int(pagina_inicial))
    fim = min(int(pagina_final or ini), ini + 9, len(pages))
    if ini > len(pages):
        return f"O documento tem {len(pages)} páginas."
    out = [f"# {d['title']} ({d['file']}) — páginas {ini}-{fim} de {len(pages)}"]
    for p in pages[ini - 1:fim]:
        out.append(f"\n## Página {p['page']}" + (" (OCR)" if p["method"] == "ocr" else "") + f"\n{p['text']}")
    return "\n".join(out)


if __name__ == "__main__":
    mcp.run()
