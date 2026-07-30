# Implementation Plan — Structured Diff Analysis

Build order, acceptance criteria, and the runnable check for each phase.
Read `architecture.md` for design, `decisions.md` for rationale.

**Phases 0 and 1 have no API dependency and may be most of the value.** Build them, look at the numbers, then decide how much model is actually needed.

---

## Phase 0 — Paired changes + anchors + a dump to look at

**Goal.** Produce the enriched change list and *measure* the A1/A2 agreement rate on real documents. This phase exists to generate evidence, not features.

### Tasks

0. **`diff_engine.align_pages(base_pages, comp_pages)`** (`architecture.md` §19, D-19)
   Returns `[(base_page|None, comp_page|None)]`. Body is `itertools.zip_longest` — page N with page N, `None` where one document is longer. `tokenize()` walks pairs; `compare()` builds one row per pair. This replaces the page-number union at `diff_engine.py:162`, not the diff itself: the diff stays a single stream over all pages, so opcodes, boxes, segments, and the change count are unchanged. **No new behaviour in this task** — it names what the code already does so later strategies have one place to live.

1. **`diff_engine.changes(base_tokens, edited_tokens)`**
   Walk the same `matcher.get_opcodes()` already used by `diff_sides()`. For each non-`equal` opcode emit one record per `architecture.md` §5.1, with `id` as `"{page}:{token_index}"` (§15/D-16) — never list position. `replace` stays **paired** — this is the whole point; the existing `diff_sides()` splits it into a left delete and a right insert and loses the pairing.

1b. **`diff_engine.normalize_ocr(words)`** (`architecture.md` §12, D-14)
   Runs between `_ocr_words()` and `clean()`, only when `page.ocr` is true. Whitespace collapse, digit-context confusable fixes, hyphen-break rejoin. Native text-layer pages untouched — add an assert to `_demo()` proving a non-OCR fixture is byte-identical before/after this stage exists.

2. **`_a1_context(tokens, i1, i2)`**
   Up to `A1_LEFT_WORDS = 8` tokens before, `A1_RIGHT_WORDS = 4` after, from the surrounding `equal` runs.

3. **`_a2_left(page_tokens, change_box)`**
   Nearest box on the same visual line, to the left. `SAME_LINE_TOL = 0.6`. Boxes are already page percentages from `_box()`.

4. **Wire into `compare()`** — add a `"changes"` key to the returned dict. `rows`, `changes` (the existing count), and both `*_source` keys keep their current meaning. **Do not touch `diff_sides()`.**

5. **Dump mode** — `python diff_engine.py --dump base.pdf edited.pdf > dump.json`.

6. **OCR quality logging** (`architecture.md` §16) — log average tesseract `conf` per OCR'd page and count of tokens dropped by `MIN_OCR_CONF`, alongside the dump.

### Runnable check

Extend the existing `_demo()` in `diff_engine.py`, matching its assert style:

```python
# index alignment: N pages in, N pairs out; ragged lengths pad with None
assert align_pages(base_pages, edited_pages) == list(zip(base_pages, edited_pages))
assert align_pages(base_pages, edited_pages[:1])[1][1] is None

# the 500 -> 550 fixture must produce ONE paired change, not two
c = changes(tokenize(base_pages), tokenize(edited_pages))
assert len(c) == 1, c
assert c[0]["before"] == ["500"] and c[0]["after"] == ["550"], c[0]
assert c[0]["tag"] == "replace"
assert "Total cost is" in c[0]["a1_left"], c[0]["a1_left"]
assert c[0]["a2_left"], "geometric neighbour must resolve on a single-line fixture"
```

Add a second fixture with two words on one line at known coordinates to pin `_a2_left` independently of the diff.

### Acceptance

- [ ] `python diff_engine.py` self-check passes, including the existing assertions
- [ ] `align_pages()` returns N pairs for two N-page documents, pads with `None` when lengths differ, and the existing `_demo()` assertions (boxes, segments, `changes == 2`) still pass unmodified
- [ ] `--dump` produces valid JSON on at least 3 real PDF pairs from different document families
- [ ] **Measured and written down:** A1/A2 agreement rate per document family
- [ ] OCR quality logged per OCR'd page (average `conf`, tokens dropped)
- [ ] `id` is `"{page}:{token_index}"` and survives being re-derived from a re-run of the same PDFs

### Decision gate

| Agreement rate | Read as | Do |
|---|---|---|
| ≥ 90% | Escalation set is tiny | Proceed; the model call will be near-free |
| 50–90% | Normal | Proceed as planned |
| < 50% | `SAME_LINE_TOL` is probably wrong | Tune it against the dump *before* Phase 2 |

Tuning `SAME_LINE_TOL` on real output is the single highest-leverage half hour in this plan.

---

## Phase 1 — Triage, still no model

**Goal.** Resolve everything the heuristic can, locally. Ship a working table with zero network calls.

### Tasks

1. **`analyze.normalise(s)`** — case, whitespace, punctuation, trailing colon, commas, currency symbols. One function, used by every rule and by verification.

2. **`analyze.triage(changes)` → `(dropped, resolved, escalated)`**
   - **Drop as noise:** `before` == `after` after normalisation (`1,000` vs `1000`)
   - **Colon fast path:** `a2_left` ends with `:` → anchor is that string minus the colon
   - **Agreement gate:** `tag == "replace"` and normalised `a1_left` == `a2_left` and candidate ≤ 6 words
   - **Escalate:** everything else

3. **`templates/analyze.html`** — table with a `source` badge column. `/analyze` returns the resolved set only, for now.

4. **`app.py`** — `POST /analyze`. Embed the JSON in `result.html` inside `<script type="application/json" id="changes">`; add the button.

5. **Payload caps at the route boundary** — max body bytes, max change count, max per-field length. Reject with 400 before any processing. (`decisions.md` D-12.)

6. **`analyze.confidence(change, resolution)`** (`architecture.md` §14, D-15) — the local half: `high` when A1/A2 agree, `low` when they disagree and the row is escalated with no verification yet to check against. Phase 2 fills in the `verified`-aware branches once escalation exists.

7. **Escalation-rate logging** (`architecture.md` §16) — log dropped/resolved/escalated counts per comparison automatically, replacing the by-hand tally in the acceptance check below.

### Runnable check

`analyze.py` gets an assert-based `_demo()` with a hand-built change list covering: a formatting-noise pair, a colon-label pair, an agreement pair, and a disagreement. Assert each lands in the right bucket. No fixtures, no framework.

### Acceptance

- [ ] `python analyze.py` self-check passes
- [ ] End-to-end: upload two PDFs → result page → Analyze → table renders
- [ ] Uvicorn bound to `127.0.0.1`
- [ ] Oversized POST body rejected with 400
- [ ] **Measured and written down:** what fraction is dropped as noise, resolved locally, escalated (now logged automatically, §16)
- [ ] Every resolved row carries a `confidence` value

### Decision gate

If the escalated set is empty or trivially small on your real documents, **stop here.** The feature is done and the spec's local-only promise is intact.

---

## Phase 2 — The model call

**Goal.** Resolve the escalated set.

### Tasks

1. **`requirements.txt`** — add `openai`. Client: `OpenAI(base_url="https://openrouter.ai/api/v1", api_key=os.environ["OPENROUTER_API_KEY"])`.

2. **`analyze.ask_model(escalated)`**
   - Build the payload per `architecture.md` §5.2 (one entry per change, `box` excluded)
   - `response_format` per §5.3, `strict: true`
   - `provider: {require_parameters: true, sort: "price", data_collection: "deny"}`
   - `usage: {include: true}`
   - Hard-fail with a readable message if `OPENROUTER_API_KEY` is unset

3. **System prompt.** State plainly: anchor is the *field name or label* the change belongs to; pick between the two supplied candidates when they differ; return values exactly as given; do not invent an anchor — return `null` if none is identifiable. Explicitly permit `null`; a forced guess is worse than an honest gap.

4. **`analyze.verify(returned, escalated)`** — per `architecture.md` §8. Drop unknown `id`s silently. Mark value mismatches `verified: false` and **render them, visibly flagged.** Do not hide them.

5. **Merge and order** by page, then position within page.

6. **Log spend** from the `usage` block for the Phase 3 bake-off.

7. **Finish `analyze.confidence()`** — add the `verified`-aware branches (§14): escalated-and-verified with A1/A2 disagreement → `medium`; unverified → `low` regardless of agreement.

8. **Verification-failure and latency logging** (`architecture.md` §16) — count of `verified: false` rows per comparison; wall time for extract+diff vs. wall time for the model call, logged separately.

### Runnable check

Add to `analyze._demo()`: feed `verify()` a fabricated model response containing (a) a good row, (b) a row with a hallucinated `base_value`, (c) a row with an unknown `id`. Assert accept / flag / drop respectively. No network in the self-check.

### Acceptance

- [ ] `python analyze.py` self-check passes, including the three verification cases
- [ ] Real comparison produces a merged table with correct `source` badges
- [ ] Hallucinated values are flagged, not silently dropped
- [ ] Missing API key gives a clear error, not a traceback
- [ ] Model output is rendered escaped and drives no action anywhere
- [ ] Verification-failure count and per-comparison latency (extract+diff vs. model) are logged

---

## Phase 3 — Model selection bake-off

**Goal.** Replace the placeholder model ID with a measured choice.

### Tasks

1. **Build the eval set** — take a Phase 0 dump, hand-correct the `anchor` for each escalated change. This file *is* the evaluation set; do not build a second one. (`decisions.md` D-11.)

2. **Shortlist** — query OpenRouter's models endpoint, keep only models whose `supported_parameters` include `structured_outputs`. Take 4–6 from the cheap tier.

3. **Run** the same prompt and schema across all candidates. Score exact-match on `anchor` after normalisation. Record real cost from `usage`.

4. **Pick** the cheapest that clears your accuracy bar. Record the choice, the numbers, and the date in `decisions.md`.

### Acceptance

- [ ] Results table (model, accuracy, cost per comparison) recorded in `decisions.md`
- [ ] Chosen model ID pinned in config or an env var, not hardcoded mid-function

### Note

Small models degrade on long structured lists. Include a high-change document in the bake-off — a model that scores well on 10 changes and collapses on 60 is a trap you want to find here, not in production.

---

## Deferred

See `decisions.md` → *Deferred backlog* for the full list with triggers. Nothing in it should be built pre-emptively.

---

## Open questions

| # | Question | Resolved by |
|---|---|---|
| 1 | Is `SAME_LINE_TOL = 0.6` right for your documents? | Phase 0 dump |
| 2 | Do table-shaped changes need `a2_above`? | Phase 0 dump (`decisions.md` D-07) |
| 3 | What fraction of raw changes is formatting noise? | Phase 1 measurement |
| 4 | Is the model needed at all for your document families? | Phase 1 decision gate |
| 5 | Which model, at what cost? | Phase 3 bake-off |
| 6 | Does the multi-column reading-order problem actually bite here? | Phase 0, on a two-column PDF specifically |

Question 6 deserves a deliberate test. The entire A1 method inherits PyMuPDF's reading order, and a structured layer sitting on a scrambled token stream produces confidently wrong anchors. A2 is the hedge — confirm it works before trusting the output.

---

## Known limitations (not bugs to fix)

- **Moved text is unsupported.** `SequenceMatcher` renders a relocated block as delete+insert, not "moved." Documented, not patched — see `decisions.md` D-17.
- **Page-level insertion and reordering is unsupported.** `align_pages()` pairs by index, so an inserted page shifts the whole tail and everything after it reads as changed. Semantic alignment is deferred behind that seam — see `decisions.md` D-19, `architecture.md` §19.
- **Multi-column reading order** relies on A2 as the hedge until question 6 above forces the issue. Layout-aware reading regions are deferred — see `decisions.md` D-18.
