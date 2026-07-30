import json
import logging
import os

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
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


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.post("/compare", response_class=HTMLResponse)
async def compare(
    request: Request,
    base: UploadFile = File(...),
    edited: UploadFile = File(...),
):
    try:
        result = diff_engine.compare(await base.read(), await edited.read())
    except Exception as exc:  # unreadable / non-PDF upload — back to the form
        return templates.TemplateResponse(
            request, "index.html", {"error": f"Could not read the PDFs: {exc}"}, status_code=400
        )
    return templates.TemplateResponse(
        request,
        "result.html",
        {"base_name": base.filename, "edited_name": edited.filename, **result},
    )


def _validated(body):
    """Parse and bound the POST body. Raises 400 before anything else runs."""
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(400, "Payload too large")
    try:
        changes = json.loads(body)
    except ValueError:
        raise HTTPException(400, "Body is not valid JSON")
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
    return changes


@app.post("/analyze", response_class=HTMLResponse)
async def analyze(request: Request):
    changes = _validated(await request.body())
    # Local by default — nothing leaves the machine. PDF_DIFF_USE_MODEL=1 re-enables the
    # model tier for a corpus where A1 works and it has something to add (D-22).
    use_model = os.environ.get("PDF_DIFF_USE_MODEL") == "1"
    try:
        rows = analysis.analyze(changes, use_model=use_model)
    except RuntimeError as exc:  # missing key — a clear message, not a traceback
        raise HTTPException(400, str(exc))
    except Exception as exc:  # one model, no fallback chain: surface it and let them retry
        raise HTTPException(502, f"The model call failed: {exc}")
    # Every row renders — including unverified ones, visibly flagged (architecture.md §8).
    return templates.TemplateResponse(request, "analyze.html", {"rows": rows})
