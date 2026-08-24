"""Extract the candidate data pack into machine-readable form.

- Each PDF -> data/corpus/<name>.txt (with page markers preserved)
- Each workbook sheet -> data/structured/<sheet>.json (list of row dicts)

Run from the repo root: .venv/bin/python scripts/extract_data.py
"""

import json
import re
from pathlib import Path

import openpyxl
import pdfplumber

ROOT = Path(__file__).resolve().parent.parent
CORPUS_DIR = ROOT / "data" / "corpus"
STRUCTURED_DIR = ROOT / "data" / "structured"

PDFS = sorted(ROOT.glob("*.pdf"))
XLSX = ROOT / "ParcelPilot_Assessment_Data.xlsx"


def extract_pdfs() -> None:
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    for pdf_path in PDFS:
        out_path = CORPUS_DIR / (pdf_path.stem + ".txt")
        pages = []
        with pdfplumber.open(pdf_path) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                text = page.extract_text() or ""
                pages.append(f"[page {i}]\n{text.strip()}")
        out_path.write_text("\n\n".join(pages), encoding="utf-8")
        print(f"{pdf_path.name}: {len(pages)} pages -> {out_path.relative_to(ROOT)}")


def cell_value(v):
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def extract_workbook() -> None:
    STRUCTURED_DIR.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.load_workbook(XLSX, data_only=True)
    for ws in wb.worksheets:
        rows = [[cell_value(c) for c in row] for row in ws.iter_rows(values_only=True)]
        # Drop fully empty trailing rows/columns
        while rows and all(v is None for v in rows[-1]):
            rows.pop()
        out_path = STRUCTURED_DIR / f"{slug(ws.title)}.json"
        if not rows:
            out_path.write_text("[]", encoding="utf-8")
            print(f"{ws.title}: empty")
            continue
        header = [str(h) if h is not None else f"col{i}" for i, h in enumerate(rows[0])]
        records = [dict(zip(header, r)) for r in rows[1:]]
        out_path.write_text(json.dumps(records, indent=2, default=str), encoding="utf-8")
        print(f"{ws.title}: {len(records)} rows, cols={header} -> {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    extract_pdfs()
    extract_workbook()
