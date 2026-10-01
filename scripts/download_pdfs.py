"""Baixa os PDFs da MRS (Central de Downloads de RI + Documentações de Fornecedores).

Uso:  .venv/Scripts/python scripts/download_pdfs.py
Idempotente: arquivos já baixados (mesmo tamanho) são pulados.
Gera data/manifest.json com metadados de cada arquivo (usado na indexação).
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests
import truststore
from bs4 import BeautifulSoup

truststore.inject_into_ssl()  # usa o repositório de certificados do Windows

ROOT = Path(__file__).resolve().parents[1]
PDF_DIR = ROOT / "data" / "pdfs"
MANIFEST = ROOT / "data" / "manifest.json"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"
S = requests.Session()
S.headers["User-Agent"] = UA

MZ_COMPANY = "e14f1680-635b-4328-8a06-b799b1574b43"
MZ_BASE = f"https://api.mziq.com/mzfilemanager/company/{MZ_COMPANY}"
MZ_CATEGORY = "central_de_downloads_central_de_downloads"

FORNECEDORES_URL = "https://www.mrs.com.br/fornecedores/documentacoes/"
EXTRA_URLS = [
    "https://www.mrs.com.br/wp-content/uploads/2025/12/MN-SMA-0013-%E2%80%93-13.00-%E2%80%93-Manual-de-Requisitos-de-SMA-para-Contratadas.pdf",
]


def slug(text: str, maxlen: int = 120) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^\w\s.-]", " ", text)
    text = re.sub(r"[\s_]+", "_", text).strip("._ ")
    return text[:maxlen].rstrip("._") or "documento"


def ri_items() -> list[dict]:
    r = S.post(
        f"{MZ_BASE}/filter/categories/meta",
        json={"categoryInternalNames": [MZ_CATEGORY], "language": "pt_BR", "published": True},
        timeout=60,
    )
    r.raise_for_status()
    metas = r.json()["data"]["document_metas"]
    items, seen = [], set()
    for m in metas:
        url = m.get("file_url") or m.get("permalink")
        if not url or url in seen:
            continue
        seen.add(url)
        year = m.get("file_year") or "sem_ano"
        date = (m.get("file_published_date") or m.get("file_date") or "")[:10]
        title = (m.get("file_title") or m.get("file_name_original") or "documento").strip()
        items.append({
            "source": "ri",
            "category": "Central de Downloads RI",
            "title": title,
            "year": year,
            "date": date,
            "url": url,
            "page": "https://ri.mrs.com.br/servicos-aos-investidores/central-de-downloads/",
            "size": int(m.get("file_size") or 0),
            "relpath": f"ri/{year}/{date}_{slug(title)}_{m['download_link_id'][:8]}.pdf",
        })
    return items


def fornecedores_items() -> list[dict]:
    html = S.get(FORNECEDORES_URL, timeout=60).text
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        href = unquote(urljoin(FORNECEDORES_URL, a["href"]))
        if urlparse(href).path.lower().endswith(".pdf"):
            label = " ".join(a.get_text(" ").split())
            found.setdefault(href, label)
    for u in EXTRA_URLS:
        found.setdefault(unquote(u), "")
    items = []
    for url, label in found.items():
        fname = unquote(Path(urlparse(url).path).name)
        generic = len(label) <= 3 or re.search(r"clique|baixar|download|acesse", label, re.I)
        title = Path(fname).stem.replace("-", " ") if generic else label
        items.append({
            "source": "fornecedores",
            "category": "Documentações para Fornecedores",
            "title": title,
            "year": int(m.group(1)) if (m := re.search(r"/uploads/(\d{4})/", url)) else None,
            "date": "",
            "url": url,
            "page": FORNECEDORES_URL,
            "size": 0,
            "relpath": f"fornecedores/{slug(Path(fname).stem)}.pdf",
        })
    return items


def download(item: dict) -> dict:
    dest = PDF_DIR / item["relpath"]
    if dest.exists() and dest.stat().st_size > 0 and (not item["size"] or dest.stat().st_size == item["size"]):
        item["status"] = "cached"
        return item
    dest.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(3):
        try:
            r = S.get(item["url"], timeout=180)
            r.raise_for_status()
            data = r.content
            break
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                item["status"] = f"erro: {e}"
                return item
    if not data.startswith(b"%PDF"):
        # não é PDF (ex.: planilha/zip): guarda em "outros" com a extensão original
        ext = Path(urlparse(r.url).path).suffix or ".bin"
        cd = r.headers.get("content-disposition", "")
        if m := re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd):
            ext = Path(unquote(m.group(1))).suffix or ext
        dest = PDF_DIR.parent / "outros" / Path(item["relpath"]).with_suffix(ext)
        dest.parent.mkdir(parents=True, exist_ok=True)
        item["relpath"] = str(dest.relative_to(PDF_DIR.parent / "outros")).replace("\\", "/")
        item["status"] = "nao_pdf"
    else:
        item["status"] = "ok"
    dest.write_bytes(data)
    item["size"] = len(data)
    return item


def main() -> None:
    items = fornecedores_items() + ri_items()
    print(f"{len(items)} arquivos encontrados", flush=True)
    results = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(download, it) for it in items]
        for i, f in enumerate(as_completed(futs), 1):
            res = f.result()
            results.append(res)
            if res["status"].startswith("erro") or i % 50 == 0:
                print(f"[{i}/{len(items)}] {res['status']} {res['relpath']}", flush=True)
    results.sort(key=lambda x: (x["source"], str(x["year"]), x["relpath"]))
    MANIFEST.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    from collections import Counter
    print(Counter(r["status"].split(":")[0] for r in results))


if __name__ == "__main__":
    sys.exit(main())
