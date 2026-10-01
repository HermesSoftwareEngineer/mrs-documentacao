# MRS – Documentação + RAG (MCP)

## Estrutura

```
data/
  pdfs/fornecedores/      22 PDFs de https://www.mrs.com.br/fornecedores/documentacoes/ (inclui MN-SMA-0013)
  pdfs/ri/<ano>/          1.252 PDFs da Central de Downloads de RI (baixados, NÃO indexados e fora do git: rode scripts/download_pdfs.py)
  outros/ri/...           12 arquivos de RI que não são PDF (.xls/.zip)
  manifest.json           metadados de cada arquivo (título, ano, URL de origem, caminho)
  index/                  banco vetorial (ChromaDB) + BM25 (SQLite FTS5) + texto extraído
models/                   modelo de embeddings e5-small (baixado automaticamente na 1ª execução; fora do git)
scripts/download_pdfs.py  baixa/atualiza os PDFs
scripts/build_index.py    extrai texto (OCR quando necessário), divide em trechos e indexa
mcp_server.py             servidor MCP (stdio)
```

## RAG

- Escopo: só os documentos para **fornecedores e subcontratadas** (`INDEXED_SOURCES` em `rag/config.py`).
- Extração com PyMuPDF; páginas escaneadas passam por OCR (RapidOCR, PP-OCRv5 latino).
- Trechos de ~1.200 caracteres por página, com sobreposição; cada trecho guarda documento, página e URL.
- Busca híbrida: vetorial (e5-small, multilíngue) + palavra-chave BM25 (bom para códigos como `PG-SMA-0037`), combinadas por RRF.
- Tudo roda localmente, sem API key.

## Ferramentas MCP

| Ferramenta | Uso |
|---|---|
| `buscar_requisitos(pergunta, quantidade=8, documento=None)` | trechos relevantes com documento/página/URL |
| `listar_documentos()` | catálogo indexado |
| `ler_paginas(documento, pagina_inicial, pagina_final)` | texto integral de páginas (até 10) |

## Conectar

- **Claude Code**: `.mcp.json` na raiz do projeto (já criado).
- **Claude Desktop**: entrada `mrs-fornecedores` em `%APPDATA%\Claude\claude_desktop_config.json` (já adicionada; reinicie o app).
- Outros clientes MCP: comando `.venv\Scripts\python.exe mcp_server.py`, env `PYTHONIOENCODING=utf-8`, `HF_HUB_OFFLINE=1`.

## Atualizar

```bash
.venv/Scripts/python scripts/download_pdfs.py
.venv/Scripts/python scripts/build_index.py
```

Obs.: esta máquina tem inspeção SSL; o pip usa `truststore` (`.venv/pip.ini`) e os scripts chamam `truststore.inject_into_ssl()`.
