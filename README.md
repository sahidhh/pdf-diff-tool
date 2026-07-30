# PDF Diff Tool

Upload two PDFs, get a side-by-side word-level diff in the browser. Runs locally,
in memory — nothing is written to disk, nothing leaves the machine.

## Install

Tesseract is an OS-level binary; `pytesseract` is only a wrapper around it and
will not install it for you.

```bash
# Ubuntu/Debian
sudo apt install tesseract-ocr
# macOS
brew install tesseract

pip install -r requirements.txt
```

Windows: `winget install UB-Mannheim.TesseractOCR`. It lands in
`%LOCALAPPDATA%\Programs\Tesseract-OCR` and is *not* added to `PATH` — either add
that folder to `PATH` or set `pytesseract.pytesseract.tesseract_cmd` to the exe.

## Run

```bash
uvicorn app:app --reload
```

Open <http://localhost:8000>, pick a base PDF and an edited PDF, hit Compare.

## How it works

1. **Extract** — `page.get_text("words")` per page (PyMuPDF): each word with its
   bounding box. A page with no text layer is scanned, so it is rasterized at
   300dpi and read with `pytesseract.image_to_data`, which returns words *and*
   boxes. Pages read via OCR are badged `OCR` in the result. Each page is also
   rendered to a PNG at 120dpi for display.
2. **Tokenize** — words in reading order as `(page_number, word, box)`, where the
   box is stored as percentages of the page so it scales with the image.
   `clean()` then drops layout filler that would otherwise swamp the diff: tokens
   with no alphanumeric character (`......`, `____`, rules) and runs of three or
   more identical single-character tokens (IRS forms draw dotted leaders as rows
   of `m`). A lone `a`/`b` line label survives — only runs are dropped. OCR words
   below `MIN_OCR_CONF` (40) are discarded as speckle. On a 3-page Schedule M-3
   this removes ~27% of tokens, all of it noise.
3. **Diff** — `difflib.SequenceMatcher` over the word sequences (`autojunk=False`,
   otherwise common words are discarded as "popular" and the diff degrades).
   Boxes ride along; they never affect matching.
4. **Render** — two-column table, one row per page, in two switchable views:
   - **PDF view** (default) — the rendered page image with absolutely-positioned
     highlight boxes over the changed words. Hovering a box shows the word.
   - **Text view** — the extracted text with `<del>`/`<ins>` spans.

   Red + strikethrough for deletions (left), green for insertions (right), a
   `replace` is both at once. Both views are built from the same opcodes and both
   ship in the HTML; the toggle is one CSS class on `<body>`. Each column header
   states how that document's text was obtained — `text layer (PyMuPDF)`,
   `OCR (tesseract)`, or `mixed` — with per-page `OCR` badges.

The diff is text-based throughout — pages are never compared as pixels. The
rendered page is only a backdrop for the highlights.

## Test

```bash
python diff_engine.py    # self-check: one-word highlight box on a "500" -> "550" edit
```

`diff_engine.py` has no FastAPI dependency — import and use `compare(base_bytes,
edited_bytes)` directly.

## Not included

No pixel-comparison diffing, no accounts, no database, no batch mode, no
deployment config.
