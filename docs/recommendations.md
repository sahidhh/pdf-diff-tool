# Recommendations Under Review — Parser Chain + Model Fallback Chain

Not decisions yet. Proposed externally, verified here against public docs/pricing (2026-07-30), and checked against existing entries in `decisions.md`. One item **conflicts with an existing decision (D-08)** — flagged below, not silently overridden.

---

## R-01 — Parser chain: OpenDataLoader PDF → Docling/MinerU → PyMuPDF+OCR

**Proposed.**
```
Primary:  OpenDataLoader PDF
Fallback: Docling or MinerU
Last:     PyMuPDF + OCR   (current pipeline)
```

**Verified:**

| Tool | Real? | License | Local/offline | Notes |
|---|---|---|---|---|
| OpenDataLoader PDF | Yes — Apache PDFBox-based, from Hancom, Apache 2.0 | Apache 2.0 | Fully local, CPU-only, no network | Core is Java (~73% of codebase); Python SDK wraps it — **adds a JVM dependency** the current pipeline doesn't have. Claims #1 reading-order accuracy (0.91) at ~1,200 pages/min on CPU. |
| Docling | Yes — IBM Research / LF AI & Data Foundation | Apache 2.0 | Local; ships a compact VLM (Granite-Docling-258M) | Best fit for general structured extraction; broad ecosystem integration (LlamaIndex/LangChain). |
| MinerU | Yes — OpenDataLab / Shanghai AI Lab | Apache 2.0 (verify per release) | Local; PaddleOCR-based | Strongest on CJK and complex academic layouts. Broadest GPU vendor support if GPU used. |
| PyMuPDF + OCR (pytesseract) | Already in `diff_engine.py` | — | Local | Current implementation. Reasonable as last-resort fallback — it's the known-working baseline, not a downgrade in trust, just in reading-order/table quality. |

**Assessment.** All three are real, actively maintained, Apache-licensed, and — critically — **all run fully local**, which is actually a *better* fit to `pdf-diff-tool-spec.md`'s "no external services" line than the model-analysis feature already accepted as an exception in D-01. No conflict with existing decisions.

**Open questions before adopting:**
- OpenDataLoader's JVM dependency is a real install-cost line item, same category reasoning that ruled out ollama as primary in D-01 ("adds an install the user must manage") — weigh it the same way.
- Docling vs. MinerU as *the* fallback (proposal says "or") needs one pick, not both — MinerU wins if CJK/complex-academic docs are in the corpus (see the open multi-column question in `implementation-plan.md` #6); Docling otherwise.
- None of this has been benchmarked against this tool's actual token/box contract (`diff_engine.Token`, `_box()` percentages) — swapping the extraction layer means re-validating `changes()`, A1/A2 anchoring, and the OCR-normalization stage (`architecture.md` §12) against whatever these parsers emit.

**Suggested placement if adopted:** new decision in `decisions.md` (e.g. D-19), only after a Phase-0-style dump comparison against the current PyMuPDF path on the same real document families already used for A1/A2 measurement.

---

## R-02 — Model fallback chain: Qwen3-8B → DeepSeek V3.1 → GLM-4.5-Air → Gemini Flash → Mistral Small

**Proposed.** A 5-model auto-failover chain for the `/analyze` model call.

**Conflicts with D-08.** `decisions.md` D-08 explicitly decided *against* this:

> **Decided.** One model ID. On error, surface a clear message.
> **Rejected.** OpenRouter's `models: [a, b, c]` auto-failover array.
> **Why.** Single-user local tool. A failed analyze is an annoyance, not an outage.
> **Revisit when.** The tool becomes shared or unattended.

Adopting R-02 as-is means reversing D-08. That reversal should be a deliberate call tied to the stated trigger (tool becoming shared/unattended), not something added incidentally while researching model options. If the tool is still single-user/local, D-08's reasoning still holds — a fallback chain adds real failure-mode complexity (silent quality degradation from model 1 to model 5 is worse than a clear error, per D-08's own logic) for a problem (occasional call failure) that a retry already solves.

**Verified, if it proceeds anyway:**

| Model | On OpenRouter? | Structured outputs | Price in / out (per 1M tok) | Notes |
|---|---|---|---|---|
| Qwen3-8B | Yes | Not confirmed from public page — FAQ question present, no answer text surfaced. **Must verify via `supported_parameters=structured_outputs` filter** (the method D-11 already prescribes) before relying on it. | $0.117 / $0.455 | 32K context (131K w/ YaRN). Smallest model in the chain — highest risk of the "small models degrade on long lists" caveat already in `architecture.md` §10 and `implementation-plan.md` Phase 3. |
| DeepSeek V3.1 | Yes | Supports structured tool calling per model card | $0.27 / $0.95 | 671B MoE, 37B active. |
| GLM-4.5-Air | Yes | Supports tool calling and structured outputs per model card | $0.13 / $0.85 | **Cheaper than DeepSeek V3.1** on both axes — its position after DeepSeek V3.1 in the chain isn't price-ordered. If chain order is meant to be cost-ascending, these two are swapped; if it's meant to be capability-descending, that's a separate claim to verify, not assume. |
| Gemini Flash | Ambiguous — "Gemini Flash" doesn't name a version. 2.5 Flash: $0.15 / $0.60 (non-thinking); thinking-mode output jumps to $3.50/M. | Yes (2.5 Flash) | See above | Pin an exact version string (`google/gemini-2.5-flash` or newer) before use — "Gemini Flash" alone is not a valid OpenRouter model ID. |
| Mistral Small | Yes, exists on OpenRouter | Not confirmed in this pass | Not confirmed in this pass | Needs its own pricing/schema check before inclusion. |

**Assessment.** Every named model plausibly exists on OpenRouter, but:
1. Structured-outputs support isn't confirmed for 2 of 5 (Qwen3-8B, Mistral Small) from public pages alone — exactly the risk `decisions.md` D-13 already calls out (`require_parameters: true` prevents silent fallback to prose, but only for models that actually support the parameter).
2. The chain's ordering logic (cost? capability? latency?) isn't self-evident from the numbers — GLM-4.5-Air undercuts DeepSeek V3.1 on price while sitting later in the chain.
3. "Gemini Flash" needs a pinned version string.

**Suggested path if a fallback chain is wanted despite D-08:**
- Treat this as **reversing D-08**, not amending it — write a new decision (e.g. D-20) that explicitly supersedes D-08, states the new trigger condition that justified reversing it, and only then feed the models above into the Phase 3 bake-off (`implementation-plan.md` Phase 3, `decisions.md` D-11) where accuracy/cost are actually measured against the hand-corrected eval set — not adopted on documentation/reputation, which D-11 already rejected as a selection method.
- Filter candidates through `supported_parameters=structured_outputs` (D-11's prescribed method) before building the chain, not after.
