# PDF Diff Tool — Spec for Claude Code
 
## What it does
Upload two PDFs (`base.pdf`, `edited_base.pdf`) in a browser. Get back a side-by-side
HTML view with word-level differences highlighted (inserted = green, deleted =
red/strikethrough). Runs locally, single machine, no external services.
 
## Explicitly out of scope (don't build these)
- No image/pixel-overlay diffing on rendered pages
- No user accounts, no persistence/DB — process and discard
- No multi-file batch mode
- No cloud deployment config
---
 
## Architecture
 
```
pdf-diff-tool/
├── app.py              # FastAPI app: routes only
├── diff_engine.py       # extraction + OCR fallback + diff logic
├── templates/
│   ├── index.html       # upload form (2 file inputs)
│   └── result.html       # side-by-side diff render
├── requirements.txt
└── README.md
```
 
**Stack**: Python, FastAPI, Jinja2 templates, PyMuPDF, pytesseract. No JS framework,
no build step. `uvicorn app:app --reload`, open `localhost:8000`.
 
---
 
## Pipeline
 
1. **Extract text per page**
   - Use `page.get_text("text")` (PyMuPDF) per page.
   - If a page returns empty/whitespace-only text → it's scanned → rasterize page
     at 300dpi (`page.get_pixmap(dpi=300)`) and run `pytesseract.image_to_string`.
   - Keep a page boundary marker so the diff view can show "Page 3" headers.
2. **Tokenize**
   - Split each page's text into words (`str.split()` is fine to start).
   - Keep two parallel token lists: `base_tokens`, `edited_tokens`, each list of
     `(page_number, word)` tuples.
3. **Diff**
   - Use `difflib.SequenceMatcher` on the word sequences (ignore page number for
     matching, use it only for display grouping).
   - Walk `get_opcodes()`: `equal` / `insert` / `delete` / `replace` spans.
4. **Render**
   - Two-column table, one row group per page.
   - Left column = base tokens for that page, right column = edited tokens.
   - `equal` spans: plain text, no highlight.
   - `delete` spans: red background + strikethrough, shown only in left column.
   - `insert` spans: green background, shown only in right column.
   - `replace` spans: render as delete (left) + insert (right) side by side.
5. **Serve**
   - `GET /` → upload form.
   - `POST /compare` → accepts two `UploadFile`s, runs pipeline in-memory
     (no disk writes needed), renders `result.html`.
---
 
## Requirements.txt
```
fastapi
uvicorn[standard]
python-multipart
pymupdf
pytesseract
pillow
```
Also needs `tesseract-ocr` installed at the OS level (not pip) — note this in
README with the apt/brew install line.
 
---
 
## Acceptance criteria
- Upload two native-text PDFs → diff renders correctly, no OCR triggered.
- Upload two scanned PDFs → OCR triggers automatically per page, diff still renders.
- A single numeric value changed (e.g. "500" → "550") shows as a `replace` span,
  not a full-page block of red/green.
- Page headers in the result view make it obvious which page each diff is on.
- Works with `uvicorn app:app --reload` and nothing else — no config files to edit.
---