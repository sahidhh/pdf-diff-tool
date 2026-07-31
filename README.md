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

bash / macOS / Linux:

```bash
uvicorn app:app --reload --host 127.0.0.1
```

Windows cmd.exe:

```bat
uvicorn app:app --reload --host 127.0.0.1
```

Windows PowerShell:

```powershell
uvicorn app:app --reload --host 127.0.0.1
```

(the command itself is shell-agnostic — only how you set environment variables,
below, differs between them)

Open <http://localhost:8000>, pick a base PDF and an edited PDF, choose an anchor
source and OCR mode if the defaults don't fit, hit Compare.

`127.0.0.1` is uvicorn's default, stated explicitly because `/analyze` is
unauthenticated and must not be reachable from the network (`docs/decisions.md` D-12).

To generate a Phase 0 dump for tuning `SAME_LINE_TOL`:

```bash
python diff_engine.py --dump base.pdf edited.pdf > dump.json
```

## Structured changes

The result page has three views, toggled with buttons — **PDF view**, **Text view**,
and **Structured changes** — and switching between them never re-runs anything; it's
a plain view change.

**Structured changes** turns the word diff into `{anchor, base_value, comp_value}`
rows — the field each change belongs to, and what it went from and to. It only runs
when you click **Re-evaluate**, next to the "Anchor source" dropdown — never on tab
switch, and never just because you changed a setting (changing a setting shows a
"settings changed" note instead, so you know to click Re-evaluate again).

Once it has run, hover a highlighted change in **PDF view** to see more than the raw
word: a **"Hover shows"** dropdown lets you pick Word, Anchor, or Anchor + values for
the tooltip.

**Local anchors run entirely on your machine.** Nothing is uploaded, no key is needed.
Anchors come from the nearest label to the left on the printed line (or above it, on
grid-shaped forms), cleaned up locally. Changes with no identifiable field still
appear, with their values and a low confidence marker — nothing is hidden because it
couldn't be labelled.

### The optional model tier

A hosted-model tier exists for corpora where the local heuristic struggles. Pick it
per comparison from the "Anchor source" control (upload form or result page) — `Auto`
uses it only when a key is set, `Local only` never calls out, `Model` always does.

Set the key before starting the server:

```bash
# bash / macOS / Linux
export OPENROUTER_API_KEY=...                      # environment only, never a file
export PDF_DIFF_MODEL=qwen/qwen3-30b-a3b-instruct-2507   # optional, overrides the default model
```

```bat
:: Windows cmd.exe
set OPENROUTER_API_KEY=...
set PDF_DIFF_MODEL=qwen/qwen3-30b-a3b-instruct-2507
```

```powershell
# Windows PowerShell
$env:OPENROUTER_API_KEY = "..."
$env:PDF_DIFF_MODEL = "qwen/qwen3-30b-a3b-instruct-2507"
```

Without a key, `Model` is disabled in the UI and `Auto` behaves like `Local only`.
`PDF_DIFF_MODEL` must be one of `analyze.ALLOWED_MODELS` — the result page's Model
dropdown lists the same set — since `/analyze` is unauthenticated and only ever
forwards an allow-listed model name to OpenRouter (`docs/decisions.md` D-12).

It sends a few words of context per unresolved change — never whole pages — and every
value it returns is checked against the diff, with anything unbacked rendered flagged
rather than silently dropped (`docs/decisions.md` D-20, D-22).

## How it works

1. **Extract** — `page.get_text("words")` per page (PyMuPDF): each word with its
   bounding box. Whether a page also gets OCR'd depends on the "OCR" setting picked
   on the upload form — `auto` (only pages with no text layer, the default), `force`
   (every page, ignoring any text layer), or `off` (never; such a page reads as
   blank). OCR rasterizes at 300dpi and reads it back with
   `pytesseract.image_to_data`, which returns words *and* boxes. Pages read via OCR
   are badged `OCR` in the result. Each page is also rendered to a PNG at 120dpi for
   display.
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
   Boxes ride along; they never affect matching. Each box also carries the same
   `change_id` as its Structured-changes row, which is how hover can show the
   anchor once analysis has run.
4. **Render** — two-column table, one row per page, in three switchable views:
   - **PDF view** (default) — the rendered page image with absolutely-positioned
     highlight boxes over the changed words. Hovering a box shows the word, or —
     once Structured changes has run — whatever the "Hover shows" dropdown picks.
   - **Text view** — the extracted text with `<del>`/`<ins>` spans.
   - **Structured changes** — see above; only populated after Re-evaluate.

   Red + strikethrough for deletions (left), green for insertions (right), a
   `replace` is both at once. All views are built from the same opcodes and all
   ship in the HTML; the toggle is one CSS class on `<body>`. Each column header
   states how that document's text was obtained — `text layer (PyMuPDF)`,
   `OCR (tesseract)`, or `mixed` — with per-page `OCR` badges.

The diff is text-based throughout — pages are never compared as pixels. The
rendered page is only a backdrop for the highlights.

## Test

```bash
python diff_engine.py    # self-check: one-word highlight box on a "500" -> "550" edit
python analyze.py        # self-check: triage, confidence, and model-response verification
```

Neither module has a FastAPI dependency — import and use `diff_engine.compare(base_bytes,
edited_bytes)` or `analyze.analyze(changes)` directly.

## Not included

No pixel-comparison diffing, no accounts, no database, no batch mode, no
deployment config.
