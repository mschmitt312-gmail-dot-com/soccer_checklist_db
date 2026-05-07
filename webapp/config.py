# webapp/config.py
# Central configuration for the Soccer Card Checklist web app.
# Update DB_CONFIG to match your local MySQL credentials.

DB_CONFIG = dict(
    host="127.0.0.1",
    port=3306,
    user="sc_loader",
    password="Gator888",
    database="soccer_checklist_db",
    charset="utf8mb4",
)

APP_CONFIG = dict(
    title="Soccer Card Checklist",
    description="Browse historical soccer card and sticker sets",
    items_per_page=24,
    # Set True locally to serve images from the scraped soccer_checklists/ folder.
    # Set False (or remove) when deploying to Azure — images will use storage_url instead.
    use_local_images=True,
)

# ── Auth config ───────────────────────────────────────────────────────────────
# Secret key for signing session cookies. Change this to a long random string
# in production, and never commit the real value to source control.
SECRET_KEY = "change-me-before-going-live-use-a-long-random-string"

# Test account for local development.
# Replace with Google OAuth in production — see routers/auth.py for the hook.
TEST_USERS = {
    "admin@soccerchecklist.com": {
        "password": "Admin1234!",
        "name":     "Admin",
        "role":     "admin",
    },
}
