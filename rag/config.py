from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
PDF_DIR = DATA_DIR / "pdfs"
MANIFEST = DATA_DIR / "manifest.json"
INDEX_DIR = DATA_DIR / "index"  # ChromaDB + SQLite FTS + texto extraído
CHROMA_DIR = INDEX_DIR / "chroma"
FTS_DB = INDEX_DIR / "fts.sqlite"
TEXT_CACHE = INDEX_DIR / "pages.jsonl"  # texto por página (inclui OCR), reaproveitado entre execuções
MODELS_DIR = ROOT / "models"

# Apenas documentação de fornecedores/subcontratadas é indexada (RI fica de fora).
INDEXED_SOURCES = {"fornecedores"}

COLLECTION = "mrs_fornecedores"
EMBED_MODEL = "intfloat/multilingual-e5-small"  # 384 dims, 512 tokens, multilíngue (pt-BR)
EMBED_DIM = 384

CHUNK_CHARS = 1200
CHUNK_OVERLAP = 200
OCR_MIN_CHARS = 80  # página com menos texto nativo que isso passa por OCR
OCR_DPI = 200
