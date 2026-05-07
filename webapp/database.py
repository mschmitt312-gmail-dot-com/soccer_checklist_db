# webapp/database.py
# Database connection pool and reusable query helpers.

from contextlib import contextmanager
from contextvars import ContextVar
from mysql.connector import pooling
from config import DB_CONFIG

# Holds the current editor's email for the duration of a request.
# Set by require_user() in auth.py; read by execute() to populate @current_user
# so MySQL history triggers can record who made each change.
_current_user: ContextVar[str] = ContextVar("current_user", default=None)


def set_current_user(email: str):
    """Call this at the start of each editor request to record who is writing."""
    _current_user.set(email)


# Connection pool (created once on first use)
_pool = None

def _get_pool():
    global _pool
    if _pool is None:
        _pool = pooling.MySQLConnectionPool(
            pool_name="webapp",
            pool_size=5,
            **DB_CONFIG,
        )
    return _pool


@contextmanager
def get_db():
    """Yield a connection from the pool; always return it when done."""
    conn = _get_pool().get_connection()
    try:
        yield conn
    finally:
        conn.close()


def query(sql: str, params: tuple = (), one: bool = False):
    """
    Run a SELECT and return results as a list of dicts (or a single dict).
    one=True returns the first row or None.
    """
    with get_db() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.close()
    return rows[0] if (one and rows) else (None if one else rows)


def execute(sql: str, params: tuple = ()):
    """Run an INSERT / UPDATE / DELETE and commit.
    Automatically sets @current_user on the connection so history triggers
    can record who made the change."""
    with get_db() as conn:
        cur = conn.cursor()
        user = _current_user.get()
        if user:
            cur.execute("SET @current_user = %s", (user,))
        cur.execute(sql, params)
        conn.commit()
        last_id = cur.lastrowid
        cur.close()
    return last_id


# Shared stats (used on the home page)
def get_site_stats() -> dict:
    row = query("""
        SELECT
            (SELECT COUNT(*) FROM sets)                              AS total_sets,
            (SELECT COUNT(*) FROM set_cards)                         AS total_cards,
            (SELECT COUNT(DISTINCT p.player_id) FROM players p
              JOIN set_cards sc ON sc.player_id = p.player_id
              WHERE p.is_non_player = 0
                AND p.canonical_player_id IS NULL)                    AS total_players,
            (SELECT COUNT(DISTINCT club_raw)
               FROM set_cards WHERE club_raw IS NOT NULL)            AS total_clubs
    """, one=True)
    return row or {}
