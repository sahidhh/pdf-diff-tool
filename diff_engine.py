"""PDF text extraction (with OCR fallback), tokenizing, and word-level diffing.

The diff itself is text-based; word boxes are carried along only so the result
view can highlight the changed words on top of the rendered page.

Pure functions — no FastAPI dependency, importable and testable standalone.
Run `python diff_engine.py` for a self-check.
"""

import base64
import io
import json
import logging
import sys
import time
from collections import namedtuple
from difflib import SequenceMatcher
from itertools import zip_longest

import fitz  # PyMuPDF

log = logging.getLogger("pdf_diff")

RENDER_DPI = 120  # page images shown in the browser
OCR_DPI = 300  # OCR needs more resolution than the screen does
MIN_OCR_CONF = 40  # tesseract confidence below this is speckle, not text

A1_LEFT_WORDS = 8  # reading-order context taken before a change
A1_RIGHT_WORDS = 4  # ...and after
SAME_LINE_TOL = 0.4  # share of a word's height that still counts as the same visual line
# SAME_LINE_TOL is THE calibration knob (architecture.md §6). Line spacing is the one
# value that genuinely varies between forms, contracts and scans — tune it on a --dump
# of real PDFs, not on the fixtures below.
#
# Tuned 2026-07-30 on a 1065 tax-form pair: 0.6 merged adjacent table rows, turning the
# label "22 Total liabilities and capital" into "22 Partners' Total liabilities capital
# and accounts capital". It held to 0.5; 0.4 leaves margin. Within-line jitter measured
# at ~0.03 of token height, so there is a wide gap between "too tight" and "too loose".

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
    words, confs, dropped = [], [], 0
    for text, conf, x, y, w, h in zip(
        data["text"], data["conf"], data["left"], data["top"], data["width"], data["height"]
    ):
        if not text.strip():
            continue
        confs.append(float(conf))
        # Low-confidence reads are speckle and rule lines, not words.
        if float(conf) < MIN_OCR_CONF:
            dropped += 1
            continue
        words.append((text, (x / scale, y / scale, (x + w) / scale, (y + h) / scale)))
    if confs:  # OCR quality metric, architecture.md §16
        log.info(
            "ocr page %d: avg conf %.1f, %d/%d tokens dropped below %d",
            page.number + 1, sum(confs) / len(confs), dropped, len(confs), MIN_OCR_CONF,
        )
    return words


# Only characters that are plausibly a misread digit — never applied inside a word.
_CONFUSABLE = str.maketrans("OoIl", "0011")
_DIGITISH = set("0123456789OoIl.,:-/")


def normalize_ocr(words):
    """OCR-only noise cleanup, run between `_ocr_words()` and `clean()`.

    Native text layers are ground truth and never come through here — running
    confusable fixes on them would corrupt real text (architecture.md §12, D-14).
    """
    out = []
    for text, rect in words:
        text = " ".join(text.split())
        if any(c.isdigit() for c in text) and all(c in _DIGITISH for c in text):
            text = text.translate(_CONFUSABLE)
        if out and out[-1][0].endswith("-"):
            # ponytail: line-break rejoin keeps the first fragment's box, so the
            # highlight covers the first half only. Merge the rects if that shows.
            previous, rect_of_previous = out.pop()
            out.append((previous[:-1] + text, rect_of_previous))
            continue
        out.append((text, rect))
    return out


def clean(words):
    """Drop layout filler so it can't drown out real changes.

    Forms are full of leader dots, rules and box glyphs that extract as words:
    "......", "____", and long runs of a single repeated letter (IRS forms draw
    dotted leaders as rows of "m"). Both are noise in a word diff.

    A rule glyph is one character repeated — "m" or "mmm", it makes no difference —
    so the run test is on distinct characters, not on token length. A 1065 pair drew
    its leaders as 23 consecutive "mmm" tokens, which a length-1 test walked straight
    past and reported as a real change.
    """
    kept = [(text, rect) for text, rect in words if any(c.isalnum() for c in text)]

    out, i = [], 0
    while i < len(kept):
        run = i
        while run + 1 < len(kept) and kept[run + 1][0] == kept[i][0]:
            run += 1
        length = run - i + 1
        # A lone "a"/"b" is a real line label; three-plus in a row is a drawn rule.
        if len(set(kept[i][0])) == 1 and length >= 3:
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
                words = normalize_ocr(_ocr_words(page))
            width, height = page.rect.width, page.rect.height
            tokens = [Token(number, text, _box(rect, width, height)) for text, rect in clean(words)]
            png = page.get_pixmap(dpi=RENDER_DPI).tobytes("png")
            pages.append(Page(number, tokens, ocr, base64.b64encode(png).decode()))
    return pages


def align_pages(base_pages, comp_pages):
    """Matched page pairs, in output order. None means the page has no counterpart.

    Index alignment: base page N pairs with comp page N (architecture.md §19, D-19).
    This is what the code already did — naming the stage is the whole point, so a
    semantic strategy later is a new body here and no change anywhere downstream.
    """
    return list(zip_longest(base_pages, comp_pages))


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


def _a1_context(tokens, i1, i2):
    """(left, right) reading-order words around a changed span — anchor method A1.

    `get_opcodes()` never emits two non-equal runs back to back, so these slices sit
    inside the neighbouring `equal` runs unless a run is shorter than the window.
    """
    left = tokens[max(0, i1 - A1_LEFT_WORDS) : i1]
    right = tokens[i2 : i2 + A1_RIGHT_WORDS]
    return " ".join(t.word for t in left), " ".join(t.word for t in right)


def _a2_left(page_tokens, box):
    """Words on the same visual line, left of `box`, in reading order — anchor method A2.

    Independent of A1: A1 trusts PyMuPDF's reading order, A2 trusts coordinates. Where
    they agree the anchor needs no model (architecture.md §6).

    The walk left stops at the first word ending in ":" — on a line holding several
    fields ("Amount: 500 Name: Smith") that keeps the previous field's value out of
    this field's anchor. ponytail: it also truncates multi-word colon labels to their
    last word ("Total cost:" -> "cost:"). Truncation, never invention; check the Phase 0
    dump before making this rule cleverer.
    """
    same_line = sorted(
        (
            t
            for t in page_tokens
            if abs(t.box["top"] - box["top"]) < SAME_LINE_TOL * box["height"]
            and t.box["left"] < box["left"]
        ),
        key=lambda t: t.box["left"],
    )
    words = []
    for token in reversed(same_line[-A1_LEFT_WORDS:]):
        words.append(token.word)
        if token.word.endswith(":"):
            break
    return " ".join(reversed(words))


def _by_page(tokens):
    """(page -> [token], id(token) -> index within its page) for `id` and A2 lookups."""
    pages, index = {}, {}
    for token in tokens:
        bucket = pages.setdefault(token.page, [])
        index[id(token)] = len(bucket)
        bucket.append(token)
    return pages, index


def changes(base_tokens, comp_tokens):
    """Paired, anchor-enriched change records (architecture.md §5.1).

    Same opcodes `diff_sides()` walks, read differently: a `replace` stays ONE record
    with both sides, where the render path splits it into a delete and an insert.
    """
    matcher = SequenceMatcher(
        a=[t.word for t in base_tokens], b=[t.word for t in comp_tokens], autojunk=False
    )
    base_by_page, base_index = _by_page(base_tokens)
    comp_by_page, comp_index = _by_page(comp_tokens)

    records = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        before, after = base_tokens[i1:i2], comp_tokens[j1:j2]
        head = (before or after)[0]
        by_page, index = (base_by_page, base_index) if before else (comp_by_page, comp_index)
        a1_left, a1_right = _a1_context(base_tokens, i1, i2)
        records.append(
            {
                # "{page}:{token_index}" — must survive noise-dropping and reordering,
                # which a list position does not (architecture.md §15, D-16).
                #
                # An insert has no base side, so its index counts into the COMP stream
                # while every other change counts into the base one. Two streams, two
                # independent numberings, and they collide: a real 1065 pair produced
                # "1:678" twice. "+" marks the comp-stream ones and makes the join key
                # do the one job it exists for.
                "id": "%d:%s%d" % (head.page, "" if before else "+", index[id(head)]),
                "page": head.page,
                "tag": tag,
                "before": [t.word for t in before],
                "after": [t.word for t in after],
                "a1_left": a1_left,
                "a1_right": a1_right,
                "a2_left": _a2_left(by_page[head.page], head.box),
            }
        )
    return records


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
    started = time.perf_counter()
    base_pages = extract_pages(base_bytes)
    edited_pages = extract_pages(edited_bytes)
    pairs = align_pages(base_pages, edited_pages)
    base_tokens = tokenize(p for p, _ in pairs if p)
    edited_tokens = tokenize(c for _, c in pairs if c)
    left, right = diff_sides(base_tokens, edited_tokens)
    empty = {"boxes": [], "segments": []}

    def side(page, diffs):
        if page is None:
            return None
        return {"png": page.png, "ocr": page.ocr, **diffs.get(page.number, empty)}

    rows = [
        {"page": (base or edited).number, "base": side(base, left), "edited": side(edited, right)}
        for base, edited in pairs
    ]
    changed_words = sum(len(v["boxes"]) for v in left.values()) + sum(
        len(v["boxes"]) for v in right.values()
    )
    records = changes(base_tokens, edited_tokens)
    # Logged apart from the model call so slow OCR isn't blamed on the model (§16).
    log.info(
        "extract+diff: %d pages, %d changes, %.2fs",
        len(pairs), len(records), time.perf_counter() - started,
    )
    return {
        "rows": rows,
        "changes": changed_words,  # changed-word count, as before — not the records below
        "change_records": records,
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

    # Same rule, multi-character glyphs: a run of "mmm" is a drawn leader, "12" is not.
    rule = [(w, (0, 0, 1, 1)) for w in "7 mmm mmm mmm mmm 12 12 12".split()]
    assert [w for w, _ in clean(rule)] == ["7", "12", "12", "12"], clean(rule)

    # Index alignment: N pages in, N pairs out; ragged lengths pad with None.
    base_pages, edited_pages = extract_pages(base), extract_pages(edited)
    assert align_pages(base_pages, edited_pages) == list(zip(base_pages, edited_pages))
    assert align_pages(base_pages, edited_pages[:1])[1][1] is None

    # OCR normalization must leave a native text layer byte-identical (D-14).
    native = [(w, (0, 0, 1, 1)) for w in "Total cost is 500 dollars".split()]
    assert normalize_ocr(native) == native, normalize_ocr(native)
    ocr_noise = [("l0O", (0, 0, 1, 1)), ("bro-", (0, 0, 1, 1)), ("ken", (0, 0, 1, 1))]
    assert [w for w, _ in normalize_ocr(ocr_noise)] == ["100", "broken"], normalize_ocr(ocr_noise)

    # The 500 -> 550 fixture must produce ONE paired change, not a delete plus an insert.
    c = changes(tokenize(base_pages), tokenize(edited_pages))
    assert len(c) == 1, c
    assert c[0]["before"] == ["500"] and c[0]["after"] == ["550"], c[0]
    assert c[0]["tag"] == "replace"
    assert c[0]["id"] == "1:3", c[0]["id"]  # page 1, 4th token — not list position
    assert "Total cost is" in c[0]["a1_left"], c[0]["a1_left"]
    # 4 words, and the equal run legitimately spans the page break (D-19).
    assert c[0]["a1_right"] == "dollars per unit Second", c[0]["a1_right"]
    assert c[0]["a2_left"], "geometric neighbour must resolve on a single-line fixture"

    # A2 pinned independently of the diff: same line to the left wins, other lines lose.
    line = [
        Token(1, "Name", {"left": 10, "top": 20, "width": 8, "height": 2}),
        Token(1, "Smith", {"left": 30, "top": 20, "width": 8, "height": 2}),
        Token(1, "Address", {"left": 10, "top": 40, "width": 8, "height": 2}),
    ]
    assert _a2_left(line, line[1].box) == "Name", _a2_left(line, line[1].box)
    assert _a2_left(line, line[0].box) == ""

    # Two fields on one line: the walk stops at "Name:", not at the start of the line.
    form = [
        Token(1, "Amount:", {"left": 10, "top": 20, "width": 8, "height": 2}),
        Token(1, "500", {"left": 25, "top": 20, "width": 5, "height": 2}),
        Token(1, "Name:", {"left": 40, "top": 20, "width": 8, "height": 2}),
        Token(1, "Smith", {"left": 55, "top": 20, "width": 8, "height": 2}),
    ]
    assert _a2_left(form, form[3].box) == "Name:", _a2_left(form, form[3].box)
    assert _a2_left(form, form[1].box) == "Amount:", _a2_left(form, form[1].box)

    print("diff_engine self-check OK")


def _dump(base_path, comp_path):
    """`python diff_engine.py --dump base.pdf comp.pdf > dump.json` — Phase 0 evidence."""
    with open(base_path, "rb") as f:
        base_bytes = f.read()
    with open(comp_path, "rb") as f:
        comp_bytes = f.read()
    json.dump(compare(base_bytes, comp_bytes)["change_records"], sys.stdout, indent=2)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    if sys.argv[1:2] == ["--dump"]:
        _dump(*sys.argv[2:4])
    else:
        _demo()
