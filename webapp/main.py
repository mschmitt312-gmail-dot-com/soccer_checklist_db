# webapp/main.py
# Entry point for the Soccer Card Checklist web app.
#
# Run with:
#   cd webapp
#   uvicorn main:app --reload --port 8000
#
# Then open http://localhost:8000 in your browser.

import sys, os
sys.path.insert(0, os.path.dirname(__file__))   # so routers can import database/config

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from config import APP_CONFIG, SECRET_KEY
from database import get_site_stats
from routers import sets, players
from routers import auth, editor

# App setup
app = FastAPI(
    title=APP_CONFIG["title"],
    description=APP_CONFIG["description"],
)

# Session middleware uses a signed cookie, no server-side storage needed.
# User tracking for DB history (set_current_user / @current_user) is handled
# inside require_user() in auth.py, which every editor route already calls.
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, https_only=False)

app.mount("/static", StaticFiles(directory="static"), name="static")

# Local image serving - points at the soccer_checklists/ folder.
# Configure the path via checklists_dir in config.py.
# Only active when use_local_images=True. Switch off for Azure deployment.
if APP_CONFIG.get("use_local_images"):
    _checklists_dir = APP_CONFIG.get("checklists_dir") or os.path.join(
        os.path.dirname(__file__), "..", "soccer_checklists"
    )
    _checklists_dir = os.path.normpath(_checklists_dir)
    if os.path.isdir(_checklists_dir):
        app.mount("/local-images", StaticFiles(directory=_checklists_dir), name="local-images")
    else:
        print(f"WARNING: checklists_dir not found at '{_checklists_dir}' — local images disabled.")

templates = Jinja2Templates(directory="templates")


# Register routers
app.include_router(sets.router)
app.include_router(players.router)
app.include_router(auth.router)
app.include_router(editor.router)


# Home page
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    stats = get_site_stats()
    return templates.TemplateResponse("home.html", {
        "request": request,
        "stats": stats,
    })
