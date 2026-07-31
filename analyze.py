"""Triage of `diff_engine.changes()` records into the rows the analyze table renders.

Local heuristic first; only what it cannot resolve reaches a model, and what the model
returns is verified against the opcodes before it is rendered (architecture.md §7–§8,
§14; D-02, D-04, D-05). No PDFs, no tokens — a pure function of the JSON the result page
embedded plus at most one network call, so it replays offline.

Run `python analyze.py` for a self-check. It makes no network call.
"""

import json
import logging
import os
import re
import time

log = logging.getLogger("pdf_diff.analyze")

ANCHOR_MAX_WORDS = 8  # longer than this is a sentence fragment, not a field name
# §7 proposed 6. Raised to 8 on evidence: "notes, bonds payable in 1 year or more" is a
# real 1065 balance-sheet label at 8 words, and 6 threw it away. Sentence fragments in
# the same dump ran to 10+, so the two populations still separate cleanly.

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# The Phase 3 bake-off winner (D-20). Env var, not hardcoded mid-function, so swapping it
# needs no edit. One model, no fallback chain (D-08).
MODEL = os.environ.get("PDF_DIFF_MODEL", "qwen/qwen3-30b-a3b-instruct-2507")

# The models /analyze will accept from a client. This is a security boundary, not a
# convenience list: /analyze is unauthenticated and spends money, so a model identifier
# arriving over HTTP is never forwarded to OpenRouter unless it appears here. Entries are
# the bake-off candidates that completed (D-20); PDF_DIFF_MODEL is included so the env
# override still works.
ALLOWED_MODELS = tuple(
    dict.fromkeys(
        [
            MODEL,
            "qwen/qwen3-30b-a3b-instruct-2507",
            "mistralai/mistral-nemo",
            "openai/gpt-oss-20b",
            "openai/gpt-5-nano",
            "meta-llama/llama-3.1-8b-instruct",
        ]
    )
)
MAX_ESCALATED = 60  # cheap models degrade on long structured lists (§10); no chunking (D-09)

# Thousands separators and currency marks vanish; other punctuation becomes a gap.
_DROPPED_CHARS = str.maketrans("", "", ",$£€¥")


def model_available():
    """Whether the model tier can run at all.

    Returns a boolean and nothing else — the key is never returned, rendered or logged,
    so the form can say "no key set" without the key reaching a template (D-12).
    """
    return bool(os.environ.get("OPENROUTER_API_KEY"))


def normalise(s):
    """Case-, punctuation- and separator-insensitive form. Used by every rule."""
    return " ".join(re.sub(r"[^\w\s]", " ", (s or "").lower().translate(_DROPPED_CHARS)).split())


# Words a label never ends on — what is left when the geometric walk overruns the label.
_DANGLING = {"the", "of", "or", "and", "by", "in", "to", "a", "an", "see", "for", "is"}


def _label(candidate):
    """A field name from a raw A2 string, or None if there isn't one in there.

    A2 reads geometry, so it returns whatever sits to the left on the line — which on a
    printed form includes the line number, the trailing cross-reference, and any
    "SEE STATEMENT n" pointer. Every rule below was written against strings the 1065
    dump actually produced:

        "22 Total liabilities and capital"            -> "Total liabilities and capital"
        "13 a Cash contributions SEE STATEMENT 3 13a" -> "Cash contributions"
        "3 Net income (loss) (see"                    -> "Net income (loss)"
        "1a 80,580,999. b c 1c"                       -> None, it is all value

    ponytail: form-shaped rules tuned on one document family. A prose corpus will want
    different ones — check a dump before adding more, the way these were.
    """
    s = candidate.strip()
    s = re.sub(r"\s+\d+[a-z]?$", "", s)  # trailing line reference: "13a", "1c", "204"
    s = re.sub(r"\s+SEE STATEMENTS?\s*\d*$", "", s, flags=re.I)  # statement pointer
    s = re.sub(r"^(\d+\s*)?([a-z]\s+)?", "", s)  # leading line number and/or item letter

    words = s.split()
    def dangling(w):
        # "(see" is the walk running into the next column; "(loss)" is part of the label.
        return (w.startswith("(") and ")" not in w) or w.lower().strip(":") in _DANGLING

    while words and dangling(words[-1]):
        words.pop()  # the walk overran the label; drop what it picked up
    s = " ".join(words).strip().rstrip(":").strip()

    if not s or len(words) > ANCHOR_MAX_WORDS:
        return None
    if not any(len(w.strip(".,:()")) >= 3 and w.strip(".,:()").isalpha() for w in words):
        return None  # all figures and single letters — a value, not a label
    if re.search(r"\d{5}", s):
        return None  # an account or EIN fragment dragged in from a neighbouring column
    return s


def _kind(change):
    """The only classification made locally; the model's `kind` covers escalated rows."""
    joined = " ".join(change["before"] + change["after"])
    return "value" if any(c.isdigit() for c in joined) else "wording"


def confidence(change, resolution, verified=True):
    """Deterministic pipeline confidence (architecture.md §14, D-15).

    Computed from A1/A2 agreement and opcode verification — never self-reported by a
    model, which D-06 rejected as uncalibrated.

    ponytail: §14's third input, OCR token confidence, is not wired in — change records
    carry no per-token `conf`. Plumb it through `changes()` if OCR'd documents show
    confident-looking anchors built on bad reads.
    """
    if resolution == "escalated" and not verified:
        return "low"  # the model's answer failed the opcode check, or never arrived
    agree = normalise(change["a1_left"]) == normalise(change["a2_left"])
    return "high" if agree else "medium"


def _row(change, anchor, kind=None, source="local", verified=True):
    """A final table row (architecture.md §5.4).

    `base_value`/`comp_value` always come from the opcodes, never from the model — the
    model classifies, it does not supply facts (§8).
    """
    before, after = " ".join(change["before"]), " ".join(change["after"])
    return {
        "id": change["id"],
        "page": change["page"],
        "anchor": anchor,
        "base_value": before or None,
        "comp_value": after or None,
        "kind": kind or _kind(change),
        "source": source,
        "verified": verified,
        # An honest null anchor passes verification but resolved nothing, so it never
        # reads as confident.
        "confidence": "low" if anchor is None else confidence(
            change, "resolved" if source == "local" else "escalated", verified
        ),
    }


def triage(changes, resolve_locally=True):
    """(dropped, resolved, escalated) — architecture.md §7, rules applied in order.

    Noise is always dropped locally: it is free to detect and there is nothing to judge.

    `resolve_locally=False` sends everything that survives to the caller's escalated
    bucket. That is the mode used when the model tier is on — A2 resolves a change like
    "JAMES -> BEN JERRY" to the anchor "BEN JERRY", the changed value itself, so letting
    it pre-empt the model would keep the worst anchors and only forward the hard ones.
    """
    dropped, resolved, escalated = [], [], []
    for change in changes:
        before, after = " ".join(change["before"]), " ".join(change["after"])
        if normalise(before) == normalise(after):
            dropped.append(change)  # formatting noise: 1,000 vs 1000. Never sent, never charged.
            continue

        anchor = _label(change["a2_left"]) if resolve_locally else None
        if anchor is None:
            escalated.append(change)
        else:
            resolved.append(_row(change, anchor))

    # Escalation rate, architecture.md §16 — the number the Phase 1 decision gate reads.
    log.info(
        "triage: %d dropped as noise, %d resolved locally, %d unresolved (of %d)",
        len(dropped), len(resolved), len(escalated), len(changes),
    )
    return dropped, resolved, escalated


SYSTEM_PROMPT = """\
You label differences found between two versions of a printed document.

For each change you get the text that changed, and `region`: the lines of the document \
around it, in the order they appear on the page. The changed text appears somewhere in \
that region.

Name the field the change belongs to — what a reader would say changed. On a form that \
is the printed label of the box; in prose it is the subject of the sentence. Examples \
of good anchors: "Total liabilities and capital", "Preparer's name", \
"Principal product or service".

Rules:
- Read the whole region. The label is often ABOVE the value, or continues onto the next \
line. It is not always to the left.
- The anchor is never the changed value itself, and never a whole sentence.
- Prefer the printed label verbatim over a paraphrase, and drop line numbers and \
cross-references around it ("22 Total liabilities and capital" -> "Total liabilities \
and capital").
- If the region contains no identifiable label — a bare checkbox, a lone figure — return \
null. An honest null beats a guess; guesses are checked and will be flagged anyway.
- Return one entry per input id, using the ids exactly as given.
- kind is "value" when a quantity, date, amount or identifier changed, and "wording" \
when the phrasing changed.\
"""

# Strict mode requires additionalProperties: false AND every property in `required`;
# optional fields are expressed as nullable rather than omitted (architecture.md §5.3).
RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "diff_analysis",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["changes"],
            "properties": {
                "changes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["id", "anchor", "base_value", "comp_value", "kind"],
                        "properties": {
                            "id": {"type": "integer"},
                            "anchor": {"type": ["string", "null"]},
                            "base_value": {"type": ["string", "null"]},
                            "comp_value": {"type": ["string", "null"]},
                            "kind": {"type": "string", "enum": ["value", "wording"]},
                        },
                    },
                }
            },
        },
    },
}


def _payload(index, change):
    """One escalation entry, ~150 tokens.

    `id` is the position in the escalated list, not the record's stable "{page}:{index}"
    id — it is a short integer for the model to echo, and `verify()` maps it straight
    back. `box` stays absent: the model cannot use coordinates, and `region` is what the
    geometry was for.

    `region` replaced the old before_ctx/after_ctx/label_left trio. Those were three
    guesses at where the label lived; on grid forms all three were wrong and the model
    could only pick the least bad (D-20). The region makes no guess.
    """
    return {
        "id": index,
        "before": " ".join(change["before"]),
        "after": " ".join(change["after"]),
        "region": change.get("window") or [change["a1_left"], change["a2_left"]],
    }


def ask_model(escalated, model=None):
    """One OpenRouter call for the escalated set. Returns the raw `changes` array."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set. Export it, or use the locally-resolved "
            "results only — Phases 0 and 1 need no key."
        )
    from openai import OpenAI  # imported lazily so the local-only path needs no install

    sent = escalated[:MAX_ESCALATED]
    if len(escalated) > MAX_ESCALATED:
        log.warning(
            "escalated %d changes, sending %d — the rest render unresolved (D-09)",
            len(escalated), MAX_ESCALATED,
        )

    client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=key)
    started = time.perf_counter()
    response = client.chat.completions.create(
        model=model or MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps([_payload(i, c) for i, c in enumerate(sent)]),
            },
        ],
        response_format=RESPONSE_FORMAT,
        extra_body={
            "provider": {
                "require_parameters": True,  # only providers that honour json_schema (D-13)
                "sort": "price",
                "data_collection": "deny",  # exclude providers that train on prompts (D-12)
            },
            "usage": {"include": True},
        },
    )
    # Latency and real spend, architecture.md §16. The key itself is never logged.
    usage = getattr(response, "usage", None)
    log.info(
        "model call: %s, %d escalated, %.2fs, %s prompt / %s completion tokens, cost %s",
        model or MODEL, len(sent), time.perf_counter() - started,
        getattr(usage, "prompt_tokens", "?"), getattr(usage, "completion_tokens", "?"),
        getattr(usage, "cost", "?"),
    )
    return json.loads(response.choices[0].message.content).get("changes", [])


def _echoes(source, claim):
    """Did the model's string actually come from what we sent it?"""
    if claim is None:
        return True  # an honest null is permitted, and is not a verification failure
    claim = normalise(claim)
    return claim == "" or claim in normalise(source)


def verify(returned, escalated):
    """Model rows checked against the opcodes (architecture.md §8).

    Ground truth lives in the opcodes; the model only classifies. Unknown ids are
    dropped silently, mismatches are marked `verified: false` and still rendered —
    hiding them would hide model drift.
    """
    rows, seen, failures = [], set(), 0
    for item in returned:
        index = item.get("id")
        if not isinstance(index, int) or not 0 <= index < len(escalated) or index in seen:
            continue  # an id we never sent: drop, no row, no noise
        seen.add(index)
        change = escalated[index]
        before, after = " ".join(change["before"]), " ".join(change["after"])
        # The region we sent includes the changed line, so "anchor appears in context" is
        # necessary but not sufficient: qwen3-30b once answered "DEVELOPMENT" for a
        # "DEPLOYEMENT -> DEVELOPMENT" change and passed on that test alone. A label is
        # never the span that changed, so require both.
        context = " ".join(
            [change["a1_left"], change["a1_right"], change["a2_left"], *change.get("window", [])]
        )
        anchor = item.get("anchor")
        verified = (
            _echoes(before, item.get("base_value"))
            and _echoes(after, item.get("comp_value"))
            and _echoes(context, anchor)
            and not (anchor and (_echoes(before, anchor) or _echoes(after, anchor)))
        )
        failures += not verified
        kind = item.get("kind") if item.get("kind") in ("value", "wording") else None
        rows.append(_row(change, anchor, kind, "model", verified))

    for index, change in enumerate(escalated):
        if index not in seen:  # over the cap, or the model just skipped it
            rows.append(_row(change, None, None, "model", False))

    log.info(
        "verify: %d model rows, %d failed the opcode check, %d unanswered",
        len(rows), failures, len(escalated) - len(seen),
    )
    return rows


def _order(row):
    """Page, then position within the page — parsed back out of the stable id (§15)."""
    page, _, index = row["id"].partition(":")
    return int(page), int(index.lstrip("+"))  # "+" marks a comp-stream index


def analyze(changes, use_model=None, model=None):
    """Full pipeline: triage, resolve anchors, verify, merge, order.

    `use_model` defaults to whether a key is configured. The model reads the layout
    region (D-23) rather than choosing between two pre-chewed candidates, which is what
    made it a pass-through in the D-20 bake-off. Without a key the local A2 tier still
    runs — worse anchors, but free and offline, and the page says which one ran.
    """
    if use_model is None:
        use_model = model_available()
    _, resolved, escalated = triage(changes, resolve_locally=not use_model)
    if escalated and use_model:
        rows = resolved + verify(ask_model(escalated, model), escalated)
    else:
        # Nothing is hidden: an unanchored change still renders, with its values.
        rows = resolved + [_row(c, None, None, "local", True) for c in escalated]
    rows.sort(key=_order)
    return rows


def _demo():
    def change(**kw):
        base = {"id": "1:0", "page": 1, "tag": "replace", "before": [], "after": [],
                "a1_left": "", "a1_right": "", "a2_left": ""}
        return {**base, **kw}

    noise = change(before=["1,000"], after=["1000"], a2_left="Total:")
    colon = change(id="1:5", before=["500"], after=["550"],
                   a1_left="of applicant is stated Amount:", a2_left="Amount:")
    agree = change(id="2:9", before=["Smith"], after=["Smyth"],
                   a1_left="Full name", a2_left="Full name")
    disagree = change(id="3:2", tag="insert", after=["indemnified"],
                      a1_left="party shall be", a2_left="The other")
    # A2 found only a figure to the left — no label exists to be had.
    unanchored = change(id="4:1", before=["12"], after=["15"],
                        a1_left="3001330238 ABC", a2_left="1a 80,580,999. b c 1c")

    dropped, resolved, escalated = triage([noise, colon, agree, disagree, unanchored])

    assert [c["id"] for c in dropped] == ["1:0"], dropped
    assert [r["id"] for r in resolved] == ["1:5", "2:9", "3:2"], resolved
    assert [c["id"] for c in escalated] == ["4:1"], escalated

    # A2 alone resolves it; without A1 to corroborate, it is medium, never high.
    assert resolved[2]["anchor"] == "The other" and resolved[2]["confidence"] == "medium"

    assert resolved[0]["anchor"] == "Amount", resolved[0]  # colon stripped
    assert resolved[0]["base_value"] == "500" and resolved[0]["comp_value"] == "550"
    assert resolved[0]["kind"] == "value"
    assert resolved[0]["confidence"] == "medium", resolved[0]  # A2 alone, A1 disagreed

    assert resolved[1]["anchor"] == "Full name"
    assert resolved[1]["kind"] == "wording"
    assert resolved[1]["confidence"] == "high", resolved[1]  # A1 and A2 agreed
    assert all(r["source"] == "local" for r in resolved)

    # A sentence fragment is not a field name, even with a colon on the end.
    long_label = change(before=["a"], after=["b"],
                        a2_left="and in the event of any such failure to deliver:")
    assert triage([long_label])[2], "over-long anchor candidate must not resolve"

    # _label(), against strings the 1065 dump actually produced.
    assert _label("22 Total liabilities and capital") == "Total liabilities and capital"
    assert _label("13 a Cash contributions SEE STATEMENT 3 13a") == "Cash contributions"
    assert _label("20 a Investment income SEE STATEMENT 4 20a") == "Investment income"
    assert _label("3 Net income (loss) (see") == "Net income (loss)"
    assert _label("b Under the covered surrogate foreign corporation") == \
        "Under the covered surrogate foreign corporation"
    assert _label("U.S. address 204") == "U.S. address"
    assert _label("effective date of the") == "effective date"
    assert _label("notes, bonds payable in 1 year or more") == \
        "notes, bonds payable in 1 year or more"
    assert _label("Form 1125-A") == "Form 1125-A"  # a real label that contains digits
    assert _label("1a 80,580,999. b c 1c") is None  # all value, no label
    assert _label("") is None
    assert _label("ASPEN FINANCIAL INSURANCE SERV 81-1079414PARTNERSHI OC") is None
    assert _label("of PR SWANNANOA, NC 28778 number of PR") is None

    # Local-only pipeline: no model, and nothing silently disappears. use_model is pinned
    # False so this self-check never touches the network, whatever is in the environment.
    rows = analyze([noise, colon, agree, disagree, unanchored], use_model=False)
    assert len(rows) == 4, rows  # noise dropped, the other four all present
    assert all(r["source"] == "local" for r in rows)
    assert any(r["anchor"] is None and r["confidence"] == "low" for r in rows)

    assert normalise("$1,000.00") == normalise("1000.00")
    assert normalise("Total cost:") == "total cost"

    # verify(): accept a good row, flag a hallucinated value, drop an id we never sent.
    # No network — a fabricated response stands in for the model.
    sent = [
        change(id="4:7", before=["500"], after=["550"],
               a1_left="of the Total cost", a1_right="per unit", a2_left="is"),
        change(id="4:9", before=["Smith"], after=["Smyth"],
               a1_left="signed by", a1_right="on behalf", a2_left="by"),
    ]
    verified = verify(
        [
            {"id": 0, "anchor": "Total cost", "base_value": "500", "comp_value": "550",
             "kind": "value"},
            {"id": 1, "anchor": "Signatory", "base_value": "Jones", "comp_value": "Smyth",
             "kind": "wording"},
            {"id": 99, "anchor": "Ghost", "base_value": "x", "comp_value": "y",
             "kind": "value"},
        ],
        sent,
    )
    assert len(verified) == 2, verified  # the unknown id left no trace

    good, bad = verified
    assert good["verified"] is True and good["anchor"] == "Total cost", good
    assert good["kind"] == "value" and good["source"] == "model"

    assert bad["verified"] is False, bad  # "Jones" is nowhere in the opcodes
    assert bad["confidence"] == "low", bad
    # Values on the row are ours, not the model's — a hallucination never reaches the table.
    assert bad["base_value"] == "Smith", bad

    # A change the model never answered for is still rendered, flagged, not dropped.
    silent = verify([], sent)
    assert len(silent) == 2 and not any(r["verified"] for r in silent), silent
    assert all(r["anchor"] is None for r in silent)

    # A verified row whose two anchor methods disagreed is medium, not high (§14).
    assert good["confidence"] == "medium", good

    # The changed value is not an anchor, even though we did send it — and it stays
    # rejected when the region we sent legitimately contains that value.
    windowed = [dict(sent[0], window=["Total cost 500 per unit", "Total cost 550 per unit"])]
    parroted = verify([{"id": 0, "anchor": "550", "base_value": "500",
                        "comp_value": "550", "kind": "value"}], windowed)
    assert parroted[0]["verified"] is False, parroted
    # A real label out of that same region still verifies.
    good_from_window = verify([{"id": 0, "anchor": "Total cost", "base_value": "500",
                                "comp_value": "550", "kind": "value"}], windowed)
    assert good_from_window[0]["verified"] is True, good_from_window

    # Model mode does not let A2 pre-empt it: everything non-noise escalates.
    _, local_rows, to_model = triage([noise, colon, agree], resolve_locally=False)
    assert local_rows == [] and len(to_model) == 2, (local_rows, to_model)
    assert _payload(0, sent[0])["region"], "payload must carry a region"

    # An honest null anchor verifies, but resolved nothing — never a confident row.
    declined = verify([{"id": 0, "anchor": None, "base_value": "500",
                        "comp_value": "550", "kind": "value"}], sent[:1])
    assert declined[0]["verified"] is True and declined[0]["confidence"] == "low", declined

    # Merge order is page then position within page, read off the stable id.
    assert [_order(r) for r in verified] == [(4, 7), (4, 9)]

    print("analyze self-check OK")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    _demo()
