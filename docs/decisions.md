# Decision Log — Structured Diff Analysis

Each entry: what was decided, what was rejected, why, and when to revisit.
Companion to `architecture.md`. Date: 2026-07-30.

---

## D-01 — Hosted model, accepted as a scoped spec exception

**Context.** `pdf-diff-tool-spec.md` promises local-only operation. Structured anchoring needs judgment that heuristics alone cannot always supply.

**Decided.** Call a hosted model via OpenRouter, opt-in per comparison, sending the smallest useful slice.

**Rejected:**
- *Local model (ollama)* — keeps the promise intact, but weaker at layout judgment and less reliable at strict JSON output. Adds an install the user must manage.
- *No model at all* — see D-02; still viable as a fallback, and Phases 0–1 deliver it.

**Why.** The hybrid design means many comparisons never call out at all, and what does leave is a handful of words per change, not documents.

**Revisit when.** Documents contain regulated PII, or the phase-0 measurement shows the heuristic alone is sufficient.

---

## D-02 — Hybrid: local heuristic first, model only for the remainder

**Decided.** Three tiers — drop as noise, resolve locally, escalate to model.

**Rejected:**
- *Model for every change* — 3–5× the cost and latency for no accuracy gain on the easy majority.
- *Heuristic only* — cannot handle inserts/deletions or column-bleed cases.

**Why.** The easy cases really are easy (`Label: value`), and the hard cases are exactly the ones a model is good at.

**Revisit when.** Escalation rate measured in Phase 0 is either near 0% (drop the model) or near 100% (drop the heuristic).

---

## D-03 — Two anchor methods (A1 reading-order + A2 geometric), used as an agreement gate

**Decided.** Compute both. Agreement → resolve locally. Disagreement → escalate and send both candidates to the model.

**Rejected:**
- *A1 only* — breaks on multi-column PDFs, where PyMuPDF interleaves word order.
- *A2 only* — breaks on flowing prose, where the nearest left box is not a label.
- *A confidence threshold on a single method* — requires guessing a number that varies per document.

**Why.** Two independent methods agreeing is a stronger and cheaper signal than any single-method score, and it needs no tuning. The escalation rate becomes self-adjusting per document.

**This is the load-bearing idea of the design.**

---

## D-04 — Client roundtrip for state (option 6a)

**Decided.** `/compare` embeds the change JSON in the page. The browser POSTs it to `/analyze`.

**Rejected:**
- *Server-side dict keyed by UUID with TTL* — introduces state to an app whose spec says "process and discard"; dies on restart.
- *Re-upload both PDFs on the analyze page* — re-runs extraction and OCR for data already computed.

**Why.** Zero new concepts. The JSON is already in the page; sending it back costs nothing.

**Consequence (important).** `/analyze` receives *only* what was embedded. Anchor enrichment must therefore run inside `compare()`, while tokens and boxes still exist. This is a hard constraint, not a preference.

---

## D-05 — Enrichment lives in `diff_engine.changes()`, not in `analyze.py`

**Decided.** `changes()` emits fully-enriched records (A1 + A2 already resolved).

**Why.** Forced by D-04. Also makes `analyze.py` a pure function of JSON — replayable offline, trivially testable, no PDF dependency.

---

## D-06 — Model schema cut to four required fields

**Decided.** `id`, `anchor`, `base_value`, `comp_value`, plus a two-value `kind`.

**Rejected / removed from an earlier draft:**

| Field | Why cut |
|---|---|
| `confidence` (float from model) | Self-reported LLM confidence is uncalibrated. A real signal already exists: A1/A2 agreement plus opcode verification. A number nobody can act on is worse than no number. |
| `change_type` with 5 values | `addition`/`deletion` derive from `tag` deterministically. `formatting` never reaches the model — the local noise filter removes it first. Only `value` vs `wording` needs judgment, so the enum is 2 values. |
| `significance` (material/minor/noise) | Scope creep. The requested output was anchor + base + comp. |

**Revisit `significance` when** the table is in use and triage-by-importance is actually wanted. It is the highest-value addition to make first.

---

## D-07 — `a2_above` (table-header anchoring) deferred

**Decided.** Only the same-line-left neighbour is computed initially.

**Why.** Speculative. Whether table changes lack a left label is an empirical question, and Phase 0's dump answers it directly.

**Revisit when.** The dump shows a meaningful cluster of changes with an empty or nonsense `a2_left` inside table regions.

---

## D-08 — Single model, no fallback chain

**Decided.** One model ID. On error, surface a clear message.

**Rejected.** OpenRouter's `models: [a, b, c]` auto-failover array.

**Why.** Single-user local tool. A failed analyze is an annoyance, not an outage, and the user can retry.

**Revisit when.** The tool becomes shared or unattended.

---

## D-09 — No chunking, no caching, no batching, no streaming

**Decided.** One request per analyze action.

**Why.** Cost model (`architecture.md` §11) puts a typical comparison at fractions of a cent and a few seconds. These are optimisations for problems that do not exist at this scale.

**Revisit when.** Escalated change count regularly exceeds ~60 (chunk, page-aligned, concurrent, joined by `id`), or bulk document-set processing appears (batch endpoints, 50% cheaper).

---

## D-10 — JSON export only

**Decided.** One download format.

**Rejected.** CSV alongside it.

**Why.** The payload is already JSON. A second serialiser for a format nobody asked for is pure surface area.

---

## D-11 — Model selection by bake-off against Phase 0's dump

**Decided.** Hand-correct anchors in the Phase 0 dump; that file *is* the evaluation set. Run 4–6 cheap-tier candidates against it, score exact-match on `anchor`, read real spend from `usage`, pick the cheapest that clears the bar.

**Rejected.** Picking a model from documentation or reputation; building a separate hand-labelled golden set.

**Why.** This task is narrow — bounded classification over pre-extracted strings with a strict schema, no reasoning depth needed. The cheapest model that holds the schema very likely wins, but that is a claim to measure rather than assume. The dump already exists; hand-correcting it is thirty minutes, and building a second artifact would be duplicated work.

**Note.** Model IDs and prices on OpenRouter change frequently. Filter by `supported_parameters` containing `structured_outputs` rather than working from any cached list.

---

## D-12 — Security posture

**Decided, not negotiable:**
- Bind uvicorn to `127.0.0.1`. `/analyze` is unauthenticated and spends money.
- Cap POST body size, change count, and per-field string length at the route boundary.
- Model output is display-only; it may never drive a file operation, redirect, or further call.
- `provider.data_collection: "deny"` on every request.
- `OPENROUTER_API_KEY` from the environment only — never in a template, response, or log.

**Why.** These are trust-boundary and privacy controls. The rest of this document is full of things that were cut for being speculative; none of these are.

---

## D-13 — `require_parameters: true` on every request

**Decided.** Always set it.

**Why.** Without it, OpenRouter may route to a provider that silently ignores `response_format` and returns prose. The failure then surfaces as a confusing parse error far from its cause.

---

## D-14 — OCR provider abstracted behind an interface; normalization stage added ahead of tokenize

**Decided.** Add `normalize_ocr(words)` between `_ocr_words()` and `clean()`, applied only to pages where `page.ocr` is true. Define `OCRProvider.words(page) -> [(text, box, conf)]` as the seam pytesseract sits behind.

**Rejected.**
- *Normalize all pages, OCR or not* — native text layers are ground truth; running confusable-character fixes on them risks corrupting real text for no benefit.
- *Build a second provider now* — no second provider exists to justify it. The interface is documented as a seam, not implemented as an abstract base class, until pytesseract actually needs a partner.

**Why.** OCR noise (confusable characters, split/joined words) reads as false diff changes on scanned pages specifically. Scoping normalization to OCR-only pages keeps native-text behavior byte-identical to today.

**Revisit when.** A second OCR engine is actually needed (cloud OCR fallback, different local engine) — build the interface then, not before.

---

## D-15 — Pipeline confidence: a deterministic field, distinct from the model-confidence rejected in D-06

**Decided.** Add `confidence: high|medium|low` to the final row (`architecture.md` §5.4 / §14), computed from A1/A2 agreement, opcode verification, and OCR token confidence. No model involvement.

**Rejected.** Reviving self-reported model confidence — D-06's reasoning stands: uncalibrated, unactionable.

**Why.** D-06 correctly rejected asking the model to grade itself. It did not reject having *a* confidence signal — the inputs for one (A1/A2 agreement, verification, OCR conf) already exist and cost nothing extra to combine.

---

## D-16 — Stable id is `"{page}:{token_index}"`, not list position

**Decided.** `id` in the change record is derived from page number and reading-order token index, not the row's position in the change list.

**Rejected.** Keeping list-index `id` — breaks the moment a row is dropped as noise (§7) or the list is reordered by page/position (§8), both of which happen before the id is ever used for verification.

**Why.** The id's whole job is to survive filtering and reordering so `verify()` can join a model response back to its source record. A value that doesn't survive the pipeline's own steps fails at the one thing it exists to do.

---

## D-17 — Moved text is intentionally unsupported

**Decided.** Document the limitation; do not build move-detection.

**Rejected.**
- *Myers diff with move detection* / *content-addressed matching (hash blocks, detect relocation)* — real algorithms, real complexity, no evidence yet that moved text appears in the target document set.

**Why.** `SequenceMatcher` matches by position; a moved block renders as delete-here + insert-there. That's a property of the chosen algorithm, not a defect — worth writing down so nobody mistakes it for a bug later, and worth deferring past unless real documents show reordering.

**Revisit when.** Real usage shows document reordering is common enough that delete+insert pairs for moved (not edited) content are actively misleading in the anchor output.

---

## D-18 — Multi-column layout-aware reading regions deferred

**Decided.** Ship with `page.get_text("words")`'s native (possibly interleaved) reading order for A1; rely on A2 (geometric) as the hedge on multi-column pages, per §6/§17.

**Rejected.** Building column-bucketed reading regions now — no two-column document has yet been confirmed as part of the target corpus (Phase 0's deliberate two-column test, `implementation-plan.md` open question 6, answers this empirically).

**Why.** Speculative work against a corpus assumption not yet verified. A2 already exists as the hedge; building a second reading-order system before knowing it's needed is the exact kind of premature generality this design otherwise avoids.

**Revisit when.** Phase 0's two-column test shows A1 producing wrong anchors on a document family that's actually in scope.

---

## D-19 — Page alignment is a named stage now; semantic alignment deferred

**Context.** Page correspondence is currently implicit: `tokenize()` concatenates pages in order, `compare()` iterates the union of page numbers, and `id` embeds a page number. Nothing states that base page N corresponds to comp page N — it is assumed in four places at once.

**Decided.** Introduce `align_pages(base_pages, comp_pages) -> [(base_page|None, comp_page|None)]` as an explicit stage between parsing and diffing (`architecture.md` §19). Its current body pairs by index, which is what the code already does — same token stream, same opcodes, same output. Downstream consumes pairs, never page numbers.

**Rejected:**
- *No seam — leave the assumption implicit.* Changing page correspondence later would then touch `tokenize()`, `compare()`, `changes()`, and the `id` convention together, plus every consumer reading `page` off a record. The cost of naming it now is one function and one paragraph.
- *Build semantic alignment now.* No document set has yet shown inserted or reordered pages. Fingerprints, heading similarity, and layout similarity are all plausible and all unmeasured — exactly the speculative infrastructure the rest of this log rejects.
- *Diff per page pair instead of over one flat stream.* That would be a real behaviour change: `equal` runs and A1 context windows currently span page boundaries, and cutting them at pages changes anchors and the change count. The seam is about *pairing*, not about where the diff runs.

**Why.** The abstraction is free — it renames behaviour that exists. What it buys is that the day semantic alignment is needed, the diff engine, `changes()`, `analyze.py`, and the templates do not change, because they already speak in pairs. Deferring the *strategy* while adopting the *shape* is the cheap half of the work.

**Consequence.** `id`'s `page` component is defined as the base page of the pair (`architecture.md` §15). Identical to today under index alignment; well-defined if alignment stops being 1:1.

**Related.** D-17 defers moved text *within* a document; this defers *page-level* insertion and reordering. Different failure, different fix, neither covers the other.

**Revisit when.** Documents regularly contain inserted, reordered, or merged pages — the symptom is a single inserted page making every following page read as fully changed.

---

## D-20 — Phase 3 bake-off run; the model tier did not earn its cost on 1065 forms

**Date.** 2026-07-30. **Corpus.** One Form 1065 pair (`pdf_a.pdf` / `pdf_b.pdf`), 6 base
pages vs 10 comp pages, native text layer, 20 raw changes → 1 dropped, 0 resolved
locally, 19 escalated.

**Shortlist method.** D-11's, followed literally: OpenRouter `/models` filtered to
`supported_parameters` containing `structured_outputs` (285 models), then the cheap tier
by estimated cost for this payload. Not chosen from reputation or documentation.

| Model | Latency | Answered | Verified | Verbatim A2 echo |
|---|---|---|---|---|
| `qwen/qwen3-30b-a3b-instruct-2507` | 41.0s | 19/19 | 19/19 | **17/19** |
| `mistralai/mistral-nemo` | 60.8s | 18/19 | 16/19 | 16/19 |
| `openai/gpt-5-nano` | 108.8s | 17/19 | 16/19 | — |
| `openai/gpt-oss-20b` | 13.8s | 14/19 | 14/19 | 14/19 |
| `meta-llama/llama-3.1-8b-instruct` | 175.2s | 10/19 | 9/19 | — |
| `inclusionai/ling-2.6-flash` | 8.5s | 1/19 | 1/19 | — |
| `mistralai/mistral-small-24b-instruct-2501` | — | provider 429 | — | — |

Cost for the winner ≈ **$0.0004 per comparison** (~5.2k in / ~0.8k out at $0.048/$0.193
per M). `z-ai/glm-4.5-air`, the pre-bake-off placeholder, **404s** under
`require_parameters: true` + `data_collection: "deny"` — no provider satisfies both.
D-13's guard behaving exactly as designed, and a disqualification rather than a reason to
relax D-12.

**Finding.** The winning model returned `a2_left` verbatim on 17 of 19 changes. It is a
pass-through of the geometric anchor, not a source of judgement. This is not a model
failure — the system prompt says to pick between two candidates, and A1 is unusable on
this document family (see below), so A2 is always the correct pick. The design assumed
A1 would be right often enough for the choice to be meaningful.

Of the two non-echoes, one was **wrong**: qwen3-30b answered `"DEVELOPMENT"` — the
changed value itself — as the anchor for a `DEPLOYEMENT -> DEVELOPMENT` change, and it
**passed verification**, because §8's anchor check ran against a haystack that included
`before`/`after`. Fixed: the anchor is now checked against the context fields only. A
label never lives inside the span that changed.

**Not decided here.** Whether to keep the model tier at all for this corpus. Evidence
says A2 alone reproduces ~90% of the model's output for $0. Recorded so the choice is
made against numbers.

**Revisit when.** A document family arrives where A1 is sound, making the A1/A2 choice
real work again — that is the condition under which this bake-off's premise holds.

---

## D-21 — A1 confirmed unusable on grid-layout tax forms (answers open question 6)

**Context.** `implementation-plan.md` open question 6 asked whether multi-column reading
order actually bites. D-18 deferred layout-aware regions until a real in-scope document
proved it. The 1065 pair proves it.

**Measured.** A1/A2 agreement **0/20 = 0%**. A1 returns interleaved header, footer and
cross-column text: the context for a preparer-name change came back as
`'SD8480 A84Y 10/10/2025 10:09:32 V24-7.1F 3001330238 BEN JERRY'`. The colon fast path
never fires either — 1065 forms label by line number (`13a`, `22`), not `Name:`.

Consequence: the local tier resolved **0%** and everything escalated. The agreement gate
(D-03, "the load-bearing idea of the design") cannot fire when one of its two methods is
structurally broken on the corpus.

**A2, by contrast, works** once tuned — `"22 Total liabilities and capital"`,
`"notes, bonds payable in 1 year or more"`, `"13 a Cash contributions SEE STATEMENT 3 13a"`
are correct labels pulled purely from geometry.

**Options, none chosen yet:**
- *Layout-aware column bucketing* — fixes A1 properly, revives the agreement gate, and is
  what D-18 deferred. Largest job.
- *A2-only local tier* — resolve on A2 alone with a local cleanup pass, drop the model for
  this family. Cheapest, and the bake-off says it loses almost nothing.
- *Status quo* — pay ~$0.0004 per comparison for what is largely an A2 pass-through.

**Revisit.** Immediately — this blocks the Phase 1 decision gate, which cannot be
evaluated honestly while one anchor method is broken. **Resolved by D-22.**

---

## D-22 — A2-only local tier; the model becomes opt-in

**Decided.** Resolve anchors from A2 alone, cleaned by `analyze._label()`. The model tier
stays in the codebase but is off unless `PDF_DIFF_USE_MODEL=1`. Default behaviour makes
no network call, so the tool is local-only again as `pdf-diff-tool-spec.md` line 6 always
wanted.

**Superseded.** `PDF_DIFF_USE_MODEL` is gone. The upload form and the result page now
carry an explicit "Anchor source" choice (`auto` / `local` / `model`) per request,
validated server-side (`app.py:_validated_settings`, `analyze.ALLOWED_MODELS`) — "auto"
still defers to the model only when `OPENROUTER_API_KEY` is set
(`analyze.model_available()`), which is this decision's default preserved, just moved
from an env flag to a request-scoped setting the user can see and change.

**Measured, same 1065 pair, 20 raw changes:**

| | Before (model tier) | After (A2-only) |
|---|---|---|
| Resolved locally | 0 | **14** |
| Unanchored | — | 5 |
| Model calls | 1 | **0** |
| Cost per comparison | ~$0.0004 | **$0** |
| Latency added | 41s | **0s** |

The local anchors are also *better* than the model's, because the model echoed A2 raw:
it answered `"13 a Cash contributions SEE STATEMENT 3 13a"`; `_label()` gives
`"Cash contributions"`. Same for `"3 Net income (loss) (see"` → `"Net income (loss)"` and
`"22 Total liabilities and capital"` → `"Total liabilities and capital"`.

The 5 unanchored changes are honest gaps — A2 found either nothing or nothing but figures
to the left. They render with their values and `confidence: low`, never hidden.

**Rejected:**
- *Column-bucketed A1 (D-18's fix)* — the correct long-term fix and still open, but it is
  a second reading-order system built to revive an agreement gate whose practical output
  A2 already supplies. Not worth it until a corpus needs A1 for its own sake.
- *Deleting the model tier* — it is written, tested, and costs nothing switched off. D-20
  records the condition under which it earns its place again.

**Consequences.**
- `confidence` is now `medium` for most rows: A2 alone is one method with no
  corroboration. `high` still requires A1/A2 agreement, which fires on prose corpora.
- `ANCHOR_MAX_WORDS` raised 6 → 8. `"notes, bonds payable in 1 year or more"` is a real
  balance-sheet label at 8 words; the §7 cap of 6 discarded it.
- `_label()`'s rules are tuned on **one** document family. They are form-shaped — line
  numbers, `SEE STATEMENT` pointers, trailing cross-references. A prose corpus will want
  different ones, and should get a dump first, the way these did.

**Revisit when.** A second document family is measured. If `_label()` mangles it, the
rules need splitting per family rather than extending in place.

---

## Under review (not decided)

See `recommendations.md` — an externally-proposed parser chain (OpenDataLoader PDF / Docling / MinerU) and model fallback chain (Qwen3-8B → DeepSeek V3.1 → GLM-4.5-Air → Gemini Flash → Mistral Small), verified against public pricing/docs. The model fallback chain **conflicts with D-08** (single model, no failover) — flagged there, not merged in here, until that conflict is deliberately resolved.

---

## Deferred backlog (with triggers)

| Item | Trigger to build it |
|---|---|
| `significance` field | Table in use, triage-by-importance wanted |
| `a2_above` table-header anchor | Phase 0 dump shows table changes with no left label |
| Model fallback chain | Tool becomes shared or unattended |
| Chunking | Escalated count regularly > 60 |
| Prompt caching / batch API | Bulk document-set processing |
| Streaming results into the table | Analyze latency becomes a complaint |
| CSV export | Someone asks |
| Local model (ollama) path | A document set where hosted inference is unacceptable |
| Semantic diff categories (`kind` beyond value/wording: numeric, date, currency, identifier) | Real usage shows `value` is too coarse to be useful in the table |
| Parallelizing analysis for large batches | Real usage shows batch comparisons, not one-at-a-time |
| Alternative diff algorithm (move-aware, see D-17) | Document reordering shows up as a real, common case |
| Second OCR provider / `OCRProvider` interface built out (see D-14) | pytesseract needs a partner or fallback |
| Layout-aware multi-column reading regions (see D-18) | ~~Two-column documents confirmed in the target corpus~~ **TRIGGERED 2026-07-30 — see D-21** |
| Semantic page alignment — replace index pairing in `align_pages()` with fingerprint / label / heading / layout / token similarity (see D-19, `architecture.md` §19) | Documents regularly contain inserted, reordered, or merged pages |
