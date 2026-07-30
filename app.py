from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

import diff_engine

app = FastAPI(title="PDF Diff")
templates = Jinja2Templates(directory="templates")


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
