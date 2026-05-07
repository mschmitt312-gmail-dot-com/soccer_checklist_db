# webapp/routers/auth.py
# Login / logout routes.
#
# Currently uses a hardcoded test account from config.TEST_USERS.
# To add Google OAuth later:
#   1. Add a GET /auth/google  route that redirects to Google's auth URL
#   2. Add a GET /auth/google/callback route that exchanges the code for a token,
#      then calls _set_session_user(request, email, name, "google") and redirects.
#   The rest of the app just reads request.session["user"] and doesn't care how
#   it was populated.

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from config import TEST_USERS
from database import set_current_user

router    = APIRouter(prefix="/auth", tags=["auth"])
templates = Jinja2Templates(directory="templates")


def _set_session_user(request: Request, email: str, name: str, auth_method: str):
    request.session["user"] = {
        "email":       email,
        "name":        name,
        "auth_method": auth_method,
    }


def get_current_user(request: Request) -> dict | None:
    """Return the session user dict, or None if not logged in."""
    return request.session.get("user")


def require_user(request: Request) -> dict:
    """
    Call from editor routes: returns the user dict or redirects to login.
    Usage:  user = require_user(request)
            if isinstance(user, RedirectResponse): return user

    Also sets the ContextVar used by database.execute() to stamp @current_user
    on every DB write, so history triggers can record who made each change.
    Done here (not in middleware) because BaseHTTPMiddleware breaks ContextVar
    propagation across async task boundaries.
    """
    user = get_current_user(request)
    if not user:
        next_path = str(request.url.path)
        return RedirectResponse(f"/auth/login?next={next_path}", status_code=302)
    set_current_user(user.get("email", ""))
    return user


# GET /auth/login
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/editor"):
    if get_current_user(request):
        return RedirectResponse(next, status_code=302)
    return templates.TemplateResponse("auth/login.html", {
        "request": request,
        "next":    next,
        "error":   None,
    })


# POST /auth/login
@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    email:    str = Form(...),
    password: str = Form(...),
    next:     str = Form(default="/editor"),
):
    user = TEST_USERS.get(email.strip().lower())
    if user and user["password"] == password:
        _set_session_user(request, email.strip().lower(), user["name"], "local")
        return RedirectResponse(next, status_code=302)

    return templates.TemplateResponse("auth/login.html", {
        "request": request,
        "next":    next,
        "error":   "Invalid email or password.",
    }, status_code=401)


# GET /auth/logout
@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=302)
