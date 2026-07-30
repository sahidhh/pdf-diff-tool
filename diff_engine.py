"""PDF text extraction (with OCR fallback), tokenizing, and word-level diffing.

The diff itself is text-based; word boxes are carried along only so the result
view can highlight the changed words on top of the rendered page.

Pure functions — no FastAPI dependency, importable and testable standalone.
Run `python diff_engine.py` for a self-check.
"""

import base64
import io
from collections import namedtuple
from difflib import SequenceMatcher

import fitz  # PyMuPDF

RENDER_DPI = 120  # page images shown in the browser
OCR_DPI = 300  # OCR needs more resolution than the screen does
MIN_OCR_CONF = 40  # tesseract confidence below this is speckle, not text

# ponytail: page images ride along in the HTML as data URIs — no server-side state,
# but ~200KB per page. Serve them from an in-memory store if long PDFs get slow.

Page = namedtuple("Page", "number tokens ocr png")
Token = namedtuple("Token", "page word box")


def _box(rect, width, height):
    """PDF-point rect -> percentages of the page, so the overlay scales with the image."""
    x0, y0, x1, y1 = rect
    return {
        "left": 100 * x0 / width,
        "top": 100 * y0 / height,
        "width": 100 * (x1 - x0) / width,
        "height": 100 * (y1 - y0) / height,
    }


def _ocr_words(page):
    """Rasterize a scanned page and read words + boxes back out with tesseract."""
    # ponytail: imported lazily so native-text PDFs work without the tesseract binary
    import pytesseract
    from PIL import Image

    pix = page.get_pixmap(dpi=OCR_DPI)
    data = pytesseract.image_to_data(
        Image.open(io.BytesIO(pix.tobytes("png"))), output_type=pytesseract.Output.DICT
    )
    scale = OCR_DPI / 72  # image pixels -> PDF points
    words = []
    for text, conf, x, y, w, h in zip(
        data["text"], data["conf"], data["left"], data["top"], data["width"], data["height"]
    ):
        # Low-confidence reads are speckle and rule lines, not words.
        if text.strip() and float(conf) >= MIN_OCR_CONF:
            words.append((text, (x / scale, y / scale, (x + w) / scale, (y + h) / scale)))
    return words


def clean(words):
    """Drop layout filler so it can't drown out real changes.

    Forms are full of leader dots, rules and box glyphs that extract as words:
    "......", "____", and long runs of a single repeated letter (IRS forms draw
    dotted leaders as rows of "m"). Both are noise in a word diff.
    """
    kept = [(text, rect) for text, rect in words if any(c.isalnum() for c in text)]

    out, i = [], 0
    while i < len(kept):
        run = i
        while run + 1 < len(kept) and kept[run + 1][0] == kept[i][0]:
            run += 1
        length = run - i + 1
        # A lone "a"/"b" is a real line label; three-plus in a row is a drawn rule.
        if len(kept[i][0]) == 1 and length >= 3:
            i = run + 1
            continue
        out.extend(kept[i : run + 1])
        i = run + 1
    return out


def extract_pages(pdf_bytes):
    """Return [Page] — words with boxes, an OCR flag, and a rendered page image."""
    pages = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for number, page in enumerate(doc, start=1):
            words = [(w[4], w[:4]) for w in page.get_text("words")]
            ocr = not words
            if ocr:
                words = _ocr_words(page)
            width, height = page.rect.width, page.rect.height
            tokens = [Token(number, text, _box(rect, width, height)) for text, rect in clean(words)]
            png = page.get_pixmap(dpi=RENDER_DPI).tobytes("png")
            pages.append(Page(number, tokens, ocr, base64.b64encode(png).decode()))
    return pages


def tokenize(pages):
    """Flatten pages into a single token list, in reading order."""
    return [token for page in pages for token in page.tokens]


def diff_sides(base_tokens, edited_tokens):
    """Diff two token lists, returning (left, right) dicts of page -> view data.

    Each page gets both renderings from the same opcodes: `boxes` (changed words
    only, for the highlight overlay) and `segments` (the full text, runs merged by
    tag, for the text view). A `replace` becomes a delete on the left and an insert
    on the right, so a one-word edit stays one word per side.
    """
    matcher = SequenceMatcher(
        a=[t.word for t in base_tokens],
        b=[t.word for t in edited_tokens],
        autojunk=False,  # autojunk drops common words as "popular" and ruins word diffs
    )

    def add(bucket, tokens, tag):
        for token in tokens:
            side = bucket.setdefault(token.page, {"boxes": [], "segments": []})
            if tag != "equal":
                side["boxes"].append({**token.box, "tag": tag, "word": token.word})
            segments = side["segments"]
            if segments and segments[-1]["tag"] == tag:
                segments[-1]["words"].append(token.word)
            else:
                segments.append({"tag": tag, "words": [token.word]})

    left, right = {}, {}
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "insert":
            add(left, base_tokens[i1:i2], "equal" if tag == "equal" else "delete")
        if tag != "delete":
            add(right, edited_tokens[j1:j2], "equal" if tag == "equal" else "insert")
    return left, right


def source_label(pages):
    """How this document's text was obtained, for the header."""
    ocr = [p.number for p in pages if p.ocr]
    if not ocr:
        return "text layer (PyMuPDF)"
    if len(ocr) == len(pages):
        return "OCR (tesseract)"
    return "mixed — OCR on page %s" % ", ".join(str(n) for n in ocr)


def compare(base_bytes, edited_bytes):
    """Full pipeline: two PDFs in, render-ready page rows out."""
    base_pages = extract_pages(base_bytes)
    edited_pages = extract_pages(edited_bytes)
    left, right = diff_sides(tokenize(base_pages), tokenize(edited_pages))
    empty = {"boxes": [], "segments": []}

    def side(pages, diffs, number):
        page = next((p for p in pages if p.number == number), None)
        if page is None:
            return None
        return {"png": page.png, "ocr": page.ocr, **diffs.get(number, empty)}

    numbers = sorted({p.number for p in base_pages} | {p.number for p in edited_pages})
    rows = [
        {"page": n, "base": side(base_pages, left, n), "edited": side(edited_pages, right, n)}
        for n in numbers
    ]
    changes = sum(len(v["boxes"]) for v in left.values()) + sum(len(v["boxes"]) for v in right.values())
    return {
        "rows": rows,
        "changes": changes,
        "base_source": source_label(base_pages),
        "edited_source": source_label(edited_pages),
    }


def _pdf(*page_texts):
    """Build a tiny native-text PDF in memory (test helper)."""
    doc = fitz.open()
    for text in page_texts:
        doc.new_page().insert_text((72, 72), text, fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


def _demo():
    base = _pdf("Total cost is 500 dollars per unit", "Second page unchanged")
    edited = _pdf("Total cost is 550 dollars per unit", "Second page unchanged")
    result = compare(base, edited)

    page1 = result["rows"][0]
    assert [b["word"] for b in page1["base"]["boxes"]] == ["500"], page1["base"]["boxes"]
    assert [b["word"] for b in page1["edited"]["boxes"]] == ["550"], page1["edited"]["boxes"]

    box = page1["base"]["boxes"][0]
    assert box["tag"] == "delete"
    assert 0 < box["left"] < 100 and 0 < box["top"] < 100, box
    assert 0 < box["width"] < 20 and 0 < box["height"] < 20, box  # one word, not a block

    # Same diff, text view: equal / delete / equal around the one changed word.
    tags = [s["tag"] for s in page1["base"]["segments"]]
    assert tags == ["equal", "delete", "equal"], page1["base"]["segments"]
    assert page1["edited"]["segments"][1] == {"tag": "insert", "words": ["550"]}

    page2 = result["rows"][1]
    assert page2["base"]["boxes"] == [] and page2["edited"]["boxes"] == []
    assert [s["tag"] for s in page2["base"]["segments"]] == ["equal"]
    assert page1["base"]["png"].startswith("iVBOR")  # base64 PNG
    assert result["base_source"] == "text layer (PyMuPDF)"
    assert result["changes"] == 2

    # Noise cleanup: leader dots and drawn rules go, real one-letter labels stay.
    noisy = [(w, (0, 0, 1, 1)) for w in
             ". . . ___ a 5 m m m m b 12".split()]
    assert [w for w, _ in clean(noisy)] == ["a", "5", "b", "12"], clean(noisy)

    print("diff_engine self-check OK")


if __name__ == "__main__":
    _demo()
