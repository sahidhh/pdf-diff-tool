# Structured Diff Analysis — Architecture

Design for a second page that turns the word-level diff into structured records:

```json
{"anchor": "Total cost", "base_value": "500", "comp_value": "550"}
```

Status: **design only. No code written yet.** See `implementation-plan.md` for the build order.

---

## 1. Constraint conflict (read this first)

`pdf-diff-tool-spec.md` line 6: *"Runs locally, single machine, no external services."*

This feature calls a hosted model through OpenRouter. That is a deliberate, scoped exception to the original spec, not an oversight. Mitigations are built into the design:

- Only the **smallest useful slice** leaves the machine — a few words per unresolved change, never whole pages or whole documents.
- The **hybrid tier resolves most changes locally**, so many comparisons make no network call at all.
- `provider.data_collection: "deny"` excludes inference providers that train on prompts.
- The analyze action is **opt-in per comparison** (a button on the result page), never automatic.

If this trade is unacceptable for a given document set, the local-only path is Phase 0 + Phase 1 alone, which produce anchors without any API call. See `decisions.md` D-01.

---

## 2. System diagram

Stages:

```
 Parser              extract_pages → OCR → clean → boxes
     ↓
 Page Alignment      align_pages() → matched page pairs   (§19)
     ↓
 Diff Engine         opcodes over the aligned-pair token stream
     ↓
 Structured Changes  paired records + A1/A2 anchors
     ↓
 Analyze             triage → model → verify
```

The diff engine consumes **aligned page pairs**, never raw page numbers. Today alignment is by index, so the token stream is byte-identical to a straight page-order concatenation. See §19.

In full:

```
 base.pdf ─┐
           ├──▶ diff_engine.py ─────────────────────────────────────────┐
 comp.pdf ─┘    extract_pages → normalize (OCR pages only)              │
                        │                                               │
                        ▼                                               │
                align_pages()  ── page alignment stage, §19 ──          │
                (index: N → N today; semantic later)                    │
                        │  [(base_page, comp_page), ...]                │
                        ▼                                               │
                tokenize(pairs) → SequenceMatcher.get_opcodes()         │
                                    │                                    │
                    ┌───────────────┴────────────────┐                   │
                    ▼                                ▼                   │
             diff_sides()                      changes()   ◀── NEW       │
        (boxes + segments for render,      (paired changes, enriched     │
         UNCHANGED behaviour)               with A1 + A2 anchors)        │
                    │                                │                   │
                    ▼                                ▼                   │
              result.html ◀──────── embeds ──── change JSON ─────────────┘
                    │              <script type="application/json" id="changes">
                    │
         [user clicks "Analyze differences"]
                    │   fetch POST /analyze   (body = the embedded blob)
                    ▼
 ┌────────────────────────────────────────────────────────────────────┐
 │                            analyze.py                               │
 │                                                                     │
 │  1. validate_payload()  size / count / field-length caps            │
 │                                                                     │
 │  2. triage()   ┌── formatting-only ─────────────────▶ DROPPED       │
 │                ├── A1 == A2 and label-shaped ───────▶ RESOLVED      │
 │                └── everything else ─────────────────▶ ESCALATED     │
 │                                                                     │
 │  3. ask_model()   one OpenRouter call for ESCALATED only            │
 │                   response_format: json_schema, strict: true        │
 │                                                                     │
 │  4. verify()      base/comp values must match the original opcode   │
 │                                                                     │
 │  5. merge RESOLVED + verified ESCALATED, order by page then position│
 └───────────────────────────────┬─────────────────────────────────────┘
                                 ▼
                          analyze.html
                 table + source badge (local / model / unverified)
                 + "download JSON"
```

---

## 3. Why enrichment happens at compare time

The client roundtrip means `/analyze` receives **only the JSON that was embedded in `result.html`**. It has no tokens, no boxes, no PDFs.

Therefore A1 and A2 anchor extraction **must run inside `compare()`**, while tokens and boxes are still in memory. `changes()` emits fully-enriched records. `analyze.py` is a pure function of that JSON plus one network call.

Consequence: `/analyze` is stateless and cheap to re-run, and the same JSON can be replayed offline for testing.

---

## 4. Component responsibilities

| Component | Owns | Must not |
|---|---|---|
| `diff_engine.align_pages()` | Deciding which base page corresponds to which comp page | Know about tokens, opcodes, anchors, models, or render data |
| `diff_engine.changes()` | Pairing opcodes, A1 window, A2 geometry, emitting JSON-serialisable records | Know about models, HTTP, or triage policy |
| `diff_engine.diff_sides()` | Existing render data. **Unchanged.** | — |
| `app.py` | Routes, embedding the JSON, size caps | Contain analysis logic |
| `analyze.py` | Triage rules, the OpenRouter call, verification, merge | Touch PDFs or tokens |
| `templates/analyze.html` | Display | Contain business rules |

---

## 5. Data contracts

### 5.1 Change record — output of `changes()`

Plain dicts (not namedtuples) because these serialise straight into the page.

```jsonc
{
  "id": "2:14",                       // stable join key: "{page}:{token_index}", NOT list position
  "page": 2,
  "tag": "replace",                   // replace | insert | delete
  "before": ["500"],                  // [] for insert
  "after":  ["550"],                  // [] for delete
  "a1_left":  "Total cost is",        // up to 8 equal tokens before, reading order
  "a1_right": "dollars per unit",     // up to 4 equal tokens after
  "a2_left":  "Total cost is"         // nearest box on same visual line, to the left
}
```

`box` is deliberately **not** included. The model cannot use coordinates, and `a2_left` already encodes what the geometry meant. Keeping it out shrinks the embedded blob and the POST body.

`id` must survive re-runs, filtering (dropped-as-noise rows disappear), and reordering by page/position (§8). A list index does not survive any of those. See §16.

### 5.2 Escalation payload — one entry per unresolved change

```jsonc
{"id": 12, "before": "Smith", "after": "Smyth",
 "before_ctx": "42 Oak Ave Name", "after_ctx": "of applicant is",
 "label_left": "Name:"}
```

~70 tokens each. `before`/`after` are joined to strings here; the array form is only needed for verification, which happens server-side against the original record.

### 5.3 Model response schema

Strict JSON-schema mode requires `additionalProperties: false` **and every property listed in `required`**. Optional fields are expressed as nullable, not omitted.

```jsonc
{
  "type": "json_schema",
  "json_schema": {
    "name": "diff_analysis",
    "strict": true,
    "schema": {
      "type": "object",
      "additionalProperties": false,
      "required": ["changes"],
      "properties": {
        "changes": {
          "type": "array",
          "items": {
            "type": "object",
            "additionalProperties": false,
            "required": ["id", "anchor", "base_value", "comp_value"],
            "properties": {
              "id":          {"type": "integer"},
              "anchor":      {"type": ["string", "null"]},
              "base_value":  {"type": ["string", "null"]},
              "comp_value":  {"type": ["string", "null"]},
              "kind":        {"type": "string", "enum": ["value", "wording"]}
            }
          }
        }
      }
    }
  }
}
```

`kind` is the only classification the model performs. `addition`/`deletion` derive from `tag` locally; `formatting` never reaches the model because the local filter removes it first.

### 5.4 Final row (what the table renders)

```jsonc
{"id": "2:14", "page": 2, "anchor": "Total cost", "base_value": "500",
 "comp_value": "550", "kind": "value", "source": "local|model",
 "verified": true, "confidence": "high"}
```

`confidence` is computed locally, deterministically — see §15. It is not the model's opinion of itself.

---

## 6. Anchor extraction

### A1 — reading-order context window

Walk backwards from the change through the preceding `equal` run in the token list; take up to `A1_LEFT_WORDS` (8). Forward for `A1_RIGHT_WORDS` (4).

Cheap and correct on single-column prose. **Known weakness:** `page.get_text("words")` flattens multi-column layouts into interleaved word order, so A1 can pull text from the wrong column.

### A2 — geometric left neighbour

Uses `Token.box`, already computed as page percentages in `diff_engine._box()`.

```
candidates where:
    abs(candidate.top - change.top) < SAME_LINE_TOL * change.height
    candidate.left < change.left
take the nearest by horizontal gap
```

`SAME_LINE_TOL = 0.6` is **the calibration knob**. Line-height tolerance is the one value that genuinely varies between document families — forms, contracts, scanned pages all sit differently. It must be tuned against real PDFs; no amount of clean code substitutes for looking at output. Phase 0's dump exists specifically to tune it.

### Why both

A1 and A2 are independent methods. Their **agreement is a free confidence signal**:

```
agree     → the two methods converged → resolve locally, no model call
disagree  → this is exactly where judgment is needed → escalate, send both
```

The disagreement rate self-tunes per document. There is no threshold to guess, and the escalation set is naturally small on clean documents.

---

## 7. Triage rules (tier 0 — no model)

Applied in order. **Superseded in part by D-22** — see the note at the end.

**Drop as noise** — never sent, never charged:
- `before` and `after` are equal after normalising commas, whitespace, and currency symbols (`1,000` vs `1000`). On financial documents this typically removes a large fraction of raw changes before anything else runs.

**Resolve locally** — run `a2_left` through `analyze._label()`. If a field name comes out, that is the anchor. `_label()` strips what geometry drags in alongside the label: leading line numbers and item letters (`22 `, `13 a `), trailing cross-references (` 13a`, ` 204`), `SEE STATEMENT n` pointers, and dangling words where the walk overran (`of the`, `(see`). It returns `None` when what is left is all figures, over `ANCHOR_MAX_WORDS`, or carries a 5-digit run dragged in from a neighbouring column.

**Confidence, not resolution, is where A1 is still used:** if normalised `a1_left` and `a2_left` agree, the row is `high`; A2 alone is `medium`. Two independent methods converging is still the strongest signal available (D-03) — it just no longer gates *whether* a row resolves, because A1 is unusable on grid layouts (D-21).

**Leave unanchored** when `_label()` returns `None`. The row still renders with its values and `confidence: low`. Nothing is hidden.

> **D-22 note.** This section originally escalated the unresolved set to a model and capped anchors at 6 words. The Phase 3 bake-off measured the model returning `a2_left` verbatim on 17 of 19 changes, so the model tier is now opt-in (`PDF_DIFF_USE_MODEL=1`) and the cap is 8 words. §8's verification and §10's request shape still apply whenever it is switched on.

---

## 8. Verification

Ground truth lives in the opcodes. The model classifies; it never supplies facts.

```
for each returned row:
    id present in the escalated set?         no → drop silently
    base_value ⊆ join(before), normalised?   no → verified = false
    comp_value ⊆ join(after),  normalised?   no → verified = false
    anchor appeared in the payload we sent?  no → verified = false
```

Unverified rows are **rendered, visibly marked** — not hidden. Silently dropping them hides model drift.

---

## 9. Security boundaries

- **Bind to `127.0.0.1`.** `/analyze` is unauthenticated and spends money. It must not be reachable from the network.
- **Validate the POST body.** The browser returns JSON this app generated, but nothing enforces that. Cap total body size, change count, and per-field string length *before* anything reaches the model. Without caps, a malformed body is an unbounded bill.
- **Treat PDF text as hostile.** Document content enters the prompt, so prompt injection is possible in principle. Blast radius is already small — output is display-only and every value is verified against the opcodes. Keep it that way: no returned field may drive a file operation, redirect, or further API call. Render as escaped text.
- **Key handling.** `OPENROUTER_API_KEY` from the environment only. Never in a template, never echoed in a response, never logged.

---

## 10. OpenRouter request shape

OpenAI-compatible HTTP, not the Anthropic SDK. Client is the `openai` package pointed at `https://openrouter.ai/api/v1`.

```jsonc
{
  "model": "<chosen in Phase 3>",
  "response_format": { /* §5.3 */ },
  "provider": {
    "require_parameters": true,     // only route to providers honouring json_schema
    "sort": "price",                // cheapest qualifying provider
    "data_collection": "deny"       // exclude providers that train on prompts
  },
  "usage": {"include": true},       // real spend per call, for the bake-off
  "messages": [...]
}
```

`require_parameters: true` is load-bearing: without it OpenRouter may route to a provider that ignores the schema and returns prose, which fails at parse time with a confusing error.

**Small-model caveat:** cheap models degrade on long structured lists. Cap escalated changes at ~60 per call. Chunking beyond that is deferred (see `decisions.md` D-09).

---

## 11. Cost model

```
input  ≈ 600 (system + schema) + 85 × escalated_count
output ≈ 40 × escalated_count
```

Hybrid typically escalates 20–40% of surviving changes. A 50-change comparison → ~15 escalated → ~1.9k in / ~0.6k out. At cheap-tier OpenRouter pricing this is fractions of a cent per comparison.

Implication: **do not build caching, batching, or streaming.** At this scale they solve problems that do not exist here.

---

## 12. OCR normalization (pre-tokenize)

Scanned pages carry OCR-specific noise that native-text pages never see: confusable characters (`O`/`0`, `l`/`1`/`I`), split or joined words, stray punctuation from speckle that survived `MIN_OCR_CONF`. Left alone, this noise surfaces as diff changes indistinguishable from real edits.

`normalize_ocr(words)` runs between `_ocr_words()` and `clean()` — **only on pages where `page.ocr` is true.** Native text-layer pages pass through untouched; normalization must never edit ground-truth text. Deterministic rules only, no model:

- collapse repeated whitespace from rasterization artifacts
- fix digit-context confusables (`O`→`0` etc.) only inside runs that are otherwise all-digit — never inside a word
- rejoin hyphenated line-break splits

Per-token OCR confidence (already captured as tesseract's `conf`, currently only used for the `MIN_OCR_CONF` drop threshold) becomes an input to pipeline confidence, §15.

## 13. OCR provider interface

`_ocr_words(page)` calls pytesseract directly today. Abstract it behind `OCRProvider.words(page) -> [(text, box, conf)]` so `extract_pages()` and the normalization stage (§12b) don't change if a second OCR engine is ever added.

Not built until a second provider is actually needed — see `decisions.md` D-14. The interface exists in this document as a seam to cut along, not as code to write speculatively.

## 14. Pipeline confidence (deterministic, not model-reported)

D-06 rejected a model-*reported* confidence field as uncalibrated. This is a different signal, computed locally before or instead of any model call, from three things already known:

- A1/A2 agreement (§6)
- opcode verification (§8), when a row was escalated
- OCR token confidence (§12b), when the change touches an OCR'd page

```
high   → A1/A2 agree, no OCR involved (or OCR conf comfortably above MIN_OCR_CONF), and verified if escalated
medium → single mismatch: OCR borderline conf, OR escalated-and-verified but A1/A2 disagreed
low    → unverified escalated row, OR OCR conf below threshold on a changed token
```

No new inputs beyond what §6, §8, and OCR extraction already produce — no added cost, no added latency.

## 15. Stable internal identifiers

`id` in §5.1 is `"{page}:{token_index}"` — page number plus the reading-order index into that page's cleaned token list at the start of the changed span. `page` is the **base** page of the aligned pair (§19); for a change with no base side, it is the comp page. Under index alignment these are the same number, so the convention costs nothing today and stays well-defined if alignment ever stops being 1:1. Not list position: the change list gets filtered (noise dropped, §7) and reordered (by page then position, §8), and a list index survives neither. Anchors point at text; `id` must point at position, independent of anchor text ever changing.

## 16. Metrics / instrumentation

Logged (stdout or a log file), not user-facing. Plain logging — no metrics backend until real usage says one is needed.

- **OCR quality** — average tesseract `conf` per OCR'd page; count of tokens dropped by `MIN_OCR_CONF`.
- **Escalation rate** — dropped / resolved / escalated counts per comparison. Phase 1 measures this by hand once; this makes it automatic, per run.
- **Verification failures** — count of `verified: false` rows per comparison. A rising rate is the drift signal that should trigger revisiting the Phase 3 model choice (`decisions.md` D-11).
- **Latency** — wall time for extract+diff and wall time for the model call, logged separately so slow OCR isn't blamed on the model or vice versa.
- **Cost** — real spend from `usage.include` (§10), logged per comparison rather than only estimated per §11.

## 17. Known limitations (documented, not built around)

- **Moved text.** `SequenceMatcher` matches by position. Content moved from one place in the document to another renders as a delete-here + insert-there pair, not a "moved" change. This is a property of the algorithm, not a bug — documented so nobody goes hunting for a moved-text case inside the anchor logic. See `decisions.md` D-17.
- **Page-level insertion and reordering.** Alignment is by index (§19), so a page inserted in the middle of one document shifts every following page against its counterpart, and the whole tail reads as changed. Distinct from moved text above: that is content moving *within* an aligned pair; this is the pairing itself being wrong. The `align_pages()` seam exists so fixing it later touches one function. See `decisions.md` D-19.
- **Multi-column reading order.** Already flagged in §6 as A1's known weakness — `page.get_text("words")` interleaves columns. Layout-aware reading regions (bucket tokens by column before building the reading-order stream) are the fix, deferred until a real two-column document is actually part of the target corpus. See `decisions.md` D-18.

## 18. Files touched

| File | Change |
|---|---|
| `diff_engine.py` | Add `align_pages()` (§19, index pairing), `changes()`, `_a1_context()`, `_a2_left()`, `normalize_ocr()`, constants. Extend `_demo()` with asserts. Add a dump mode. **`compare()` and `diff_sides()` behaviour unchanged.** |
| `analyze.py` | New. Triage, confidence, model call, verification, merge, metrics logging. |
| `app.py` | New `POST /analyze` route. Embed change JSON in the `/compare` response context. |
| `templates/result.html` | Embed the JSON blob; add the "Analyze differences" button. |
| `templates/analyze.html` | New. Results table. |
| `requirements.txt` | `openai` |

Two new files. No new concepts in the existing render path.

## 19. Page alignment

A named stage between parsing and diffing. It answers one question — *which base page corresponds to which comp page* — and nothing else.

### Contract

```python
def align_pages(base_pages, comp_pages) -> list[tuple[Page | None, Page | None]]:
    """Matched page pairs, in output order. None means the page has no counterpart."""
```

Everything downstream consumes pairs. `tokenize()` builds its stream by walking pairs in order; `compare()` builds one row per pair. Neither reads a page number to decide what lines up with what — that decision belongs to this function alone.

### Current strategy: index alignment

Page N pairs with page N. Where one document is longer, the extra pages pair with `None`.

This is exactly what the code already does — `compare()` iterates the union of page numbers and `side()` already returns `None` for a page that only one document has. The stage is a name for existing behaviour, not new behaviour. Concatenating index-aligned pairs yields the same token stream as concatenating pages in order, so opcodes, boxes, segments, anchors, and the change count are unchanged.

### Why the seam exists now

Without it, page correspondence is an assumption spread across `tokenize()`, `compare()`, `changes()`, and the `id` convention. Any future change to it would touch all four, plus anything reading `page` off a record. With it, a different strategy is a different function body. See `decisions.md` D-19.

### Future strategies (names only, not designs)

If index alignment stops holding (trigger in `decisions.md` deferred backlog), candidates worth measuring — none specified further here, deliberately:

- page fingerprints
- label similarity
- heading similarity
- layout similarity
- token similarity

Whichever is chosen replaces the body of `align_pages()`. The diff engine, `changes()`, `analyze.py`, and the templates do not change: they already speak in pairs.
