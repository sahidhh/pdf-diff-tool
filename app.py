import json
import logging

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

import analyze as analysis
import diff_engine

logging.basicConfig(level=logging.INFO, format="%(message)s")

app = FastAPI(title="PDF Diff")
templates = Jinja2Templates(directory="templates")

# The browser posts back JSON this app generated, but nothing enforces that, and
# /analyze is unauthenticated. Cap everything at the boundary (decisions.md D-12).
MAX_BODY_BYTES = 1_000_000
MAX_CHANGES = 2_000
MAX_FIELD_CHARS = 2_000
REQUIRED_FIELDS = {"id", "page", "tag", "before", "after", "a1_left", "a1_right", "a2_left"}
ANCHOR_MODES = {"auto", "local", "model"}


def _form_context():
    """What the upload form needs to describe the machine it is running on."""
    return {
        "models": analysis.ALLOWED_MODELS,
        "default_model": analysis.MODEL,
        "model_available": analysis.model_available(),
        "tesseract_available": diff_engine.tesseract_available(),
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", _form_context())


@app.post("/compare", response_class=HTMLResponse)
async def compare(
    request: Request,
    base: UploadFile = File(...),
    edited: UploadFile = File(...),
    ocr_mode: str = Form("auto"),
    anchor_mode: str = Form("auto"),
    model: str = Form(""),
):
    if ocr_mode not in diff_engine.OCR_MODES:
        raise HTTPException(400, f"ocr_mode must be one of {diff_engine.OCR_MODES}")
    # anchor_mode and model are not used here — they ride through to /analyze — but they
    # are validated now so a bad value fails on the form, not two clicks later.
    settings = _validated_settings({"anchor_mode": anchor_mode, "model": model})
    try:
        result = diff_engine.compare(await base.read(), await edited.read(), ocr_mode)
    except Exception as exc:  # unreadable / non-PDF upload — back to the form
        return templates.TemplateResponse(
            request,
            "index.html",
            {"error": f"Could not read the PDFs: {exc}", **_form_context()},
            status_code=400,
        )
    return templates.TemplateResponse(
        request,
        "result.html",
        {
            "base_name": base.filename,
            "edited_name": edited.filename,
            "ocr_mode": ocr_mode,
            "settings": settings,
            **_form_context(),
            **result,
        },
    )


def _validated_settings(settings):
    """Bound the runtime settings arriving from the browser.

    The model identifier is the one that matters: /analyze is unauthenticated and spends
    money, so a name off the wire is only ever forwarded to OpenRouter if the server
    already knows it. Without the allowlist this endpoint is an open proxy to any model
    on the account (decisions.md D-12).
    """
    if not isinstance(settings, dict):
        raise HTTPException(400, "settings must be an object")
    anchor_mode = settings.get("anchor_mode") or "auto"
    model = settings.get("model") or ""
    if anchor_mode not in ANCHOR_MODES:
        raise HTTPException(400, f"anchor_mode must be one of {sorted(ANCHOR_MODES)}")
    if model and model not in analysis.ALLOWED_MODELS:
        raise HTTPException(400, "Unknown model")
    return {"anchor_mode": anchor_mode, "model": model}


def _validated(body):
    """Parse and bound the POST body. Raises 400 before anything else runs.

    Accepts either a bare list of change records or {"changes": [...], "settings": {...}}.
    The bare list is the original contract and still means default settings.
    """
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(400, "Payload too large")
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(400, "Body is not valid JSON")
    if isinstance(payload, dict):
        changes = payload.get("changes")
        # Absent means defaults; present-but-malformed is refused rather than coerced.
        raw = payload.get("settings")
        settings = _validated_settings({} if raw is None else raw)
    else:
        changes, settings = payload, _validated_settings({})
    if not isinstance(changes, list):
        raise HTTPException(400, "Expected a list of change records")
    if len(changes) > MAX_CHANGES:
        raise HTTPException(400, f"Too many changes (max {MAX_CHANGES})")
    for change in changes:
        if not isinstance(change, dict) or not REQUIRED_FIELDS <= change.keys():
            raise HTTPException(400, "Change record is missing required fields")
        sides = [change["before"], change["after"]]
        if not all(isinstance(s, list) for s in sides) or not all(
            isinstance(w, str) for s in sides for w in s
        ):
            raise HTTPException(400, "before/after must be lists of strings")
        text_fields = [change["id"], change["tag"], change["a1_left"], change["a1_right"],
                       change["a2_left"]]
        if not all(isinstance(v, str) for v in text_fields):
            raise HTTPException(400, "id/tag/anchor context fields must be strings")
        if any(len(v) > MAX_FIELD_CHARS for v in text_fields + [w for s in sides for w in s]):
            raise HTTPException(400, "Change record field too long")
    return changes, settings


# "auto" defers to analyze(), which turns the model tier on when a key is present.
_USE_MODEL = {"auto": None, "local": False, "model": True}


@app.post("/analyze", response_class=HTMLResponse)
async def analyze(request: Request):
    changes, settings = _validated(await request.body())
    try:
        rows = analysis.analyze(
            changes,
            use_model=_USE_MODEL[settings["anchor_mode"]],
            model=settings["model"] or None,
        )
    except RuntimeError as exc:  # missing key — a clear message, not a traceback
        raise HTTPException(400, str(exc))
    except Exception as exc:  # one model, no fallback chain: surface it and let them retry
        raise HTTPException(502, f"The model call failed: {exc}")
    # Every row renders — including unverified ones, visibly flagged (architecture.md §8).
    return templates.TemplateResponse(request, "analyze.html", {"rows": rows})
