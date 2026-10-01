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

## Deploy com Docker (HTTP + OAuth)

No container o servidor roda em **Streamable HTTP** (`/mcp`) com **OAuth 2.1** embutido: o próprio
servidor é o authorization server e o login é uma página com uma senha única.

```bash
cp .env.example .env        # defina MCP_PASSWORD e MCP_PUBLIC_URL
docker compose up -d --build
```

- A imagem já contém o índice (`data/index`) e o modelo de embeddings; não precisa de rede em runtime.
- Coloque atrás de um proxy com HTTPS (Caddy, Nginx, Traefik, Cloudflare Tunnel) e use essa URL em `MCP_PUBLIC_URL`.
- Clientes MCP remotos (ex.: Claude → Configurações → Conectores → *Adicionar conector personalizado*)
  recebem só a URL `https://<seu-domínio>/mcp`; o cliente se registra sozinho, abre a página de senha e obtém o token.
- Endpoints: `/mcp`, `/health`, `/login`, `/authorize`, `/token`, `/register`, `/revoke`,
  `/.well-known/oauth-authorization-server`, `/.well-known/oauth-protected-resource/mcp`.
- Tokens: acesso 1 h, refresh 30 dias (com rotação); guardados como hash em SQLite no volume `oauth-state`.
- Sem `MCP_PASSWORD` (ou com `AUTH_MODE=none`) o servidor fica aberto, útil só para testes locais.
- Para trocar a senha, altere o `.env` e reinicie; para derrubar todas as sessões, apague o volume `oauth-state`.

## Atualizar

```bash
.venv/Scripts/pip install -r requirements-index.txt
.venv/Scripts/python scripts/download_pdfs.py
.venv/Scripts/python scripts/build_index.py
```

Obs.: esta máquina tem inspeção SSL; o pip usa `truststore` (`.venv/pip.ini`) e os scripts chamam `truststore.inject_into_ssl()`.
