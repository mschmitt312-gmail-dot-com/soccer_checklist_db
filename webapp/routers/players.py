# webapp/routers/players.py
# Routes for browsing players.

import re
import unicodedata

from fastapi import APIRouter, Request, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from database import query
from config import APP_CONFIG


def _local_image_url(og_title: str, filename: str) -> str:
    """Mirrors scraper safe_folder_name() to reconstruct a local static image URL."""
    name = (og_title or "").replace('\xa0', ' ').replace('​', '').strip()
    name = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode('ascii')
    name = re.sub(r'[\*?:"<>|]', '', name)
    name = re.sub(r'\s+', '_', name)
    folder = name[:120]
    return f"/local-images/{folder}/{filename}"

router = APIRouter(prefix="/players", tags=["players"])
templates = Jinja2Templates(directory="templates")

PER_PAGE = APP_CONFIG["items_per_page"]


# ── Players list / search ─────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def players_list(
    request: Request,
    q: str = Query(default="", description="Search player name"),
    club: str = Query(default="", description="Filter by club name"),
    country: str = Query(default="", description="Filter by country name"),
    page: int = Query(default=1, ge=1),
):
    has_filters   = bool(q or club or country)
    players       = []
    total         = 0
    total_pages   = 1
    total_players = (query("""
        SELECT COUNT(DISTINCT p.player_id) AS cnt
        FROM players p
        JOIN set_cards sc ON sc.player_id = p.player_id
        WHERE p.is_non_player = 0 AND p.canonical_player_id IS NULL
    """, one=True) or {}).get("cnt", 0)

    if has_filters:
        offset       = (page - 1) * PER_PAGE
        like_q       = f"%{q}%"
        like_club    = f"%{club}%"
        like_country = f"%{country}%"

        where_parts  = ["p.is_non_player = 0", "p.canonical_player_id IS NULL"]
        params: list = []

        if q:
            where_parts.append("(p.name_raw LIKE %s OR p.first_name LIKE %s OR p.last_name LIKE %s)")
            params += [like_q, like_q, like_q]

        if club:
            where_parts.append("""
                p.player_id IN (
                    SELECT DISTINCT player_id FROM set_cards
                    WHERE club_raw LIKE %s
                )
            """)
            params.append(like_club)

        if country:
            where_parts.append("""
                p.player_id IN (
                    SELECT DISTINCT sc.player_id FROM set_cards sc
                    JOIN countries c ON sc.country_id = c.country_id
                    WHERE c.country_name LIKE %s
                )
            """)
            params.append(like_country)

        where_sql = "WHERE " + " AND ".join(where_parts)

        total_row = query(
            f"""SELECT COUNT(DISTINCT p.player_id) AS cnt
                FROM players p
                JOIN set_cards sc ON sc.player_id = p.player_id
                {where_sql}""",
            tuple(params),
            one=True,
        )
        total = total_row["cnt"] if total_row else 0

        players = query(f"""
            SELECT
                p.player_id,
                p.name_raw,
                p.first_name,
                p.last_name,
                COUNT(DISTINCT sc.set_id) AS set_count
            FROM players p
            JOIN set_cards sc ON sc.player_id = p.player_id
            {where_sql}
            GROUP BY p.player_id
            ORDER BY p.last_name, p.first_name
            LIMIT %s OFFSET %s
        """, tuple(params) + (PER_PAGE, offset))

        total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)

    context = dict(
        request=request,
        players=players,
        q=q,
        club=club,
        country=country,
        page=page,
        total_pages=total_pages,
        total=total,
        has_filters=has_filters,
        total_players=total_players,
    )

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse("players/list_rows.html", context)

    return templates.TemplateResponse("players/list.html", context)


# ── Player detail ─────────────────────────────────────────────────────────────

@router.get("/{player_id}", response_class=HTMLResponse)
async def player_detail(request: Request, player_id: int):
    player = query("""
        SELECT player_id, name_raw, first_name, last_name, display_name,
               nationality, date_of_birth, birth_year, birth_place,
               photo_url, photo_position
        FROM players
        WHERE player_id = %s
    """, (player_id,), one=True)

    if not player:
        return HTMLResponse("<h2>Player not found</h2>", status_code=404)

    # All cards for this player across all sets
    appearances = query("""
        SELECT
            sc.card_id,
            sc.card_number,
            sc.name_in_set,
            sc.club_raw,
            s.set_id,
            s.og_title,
            COALESCE(s.set_name, '(Untitled)') AS set_name,
            s.year_start,
            s.year_end,
            s.season_raw,
            s.publisher,
            s.country  AS set_country,
            c.country_name,
            (SELECT COALESCE(ii.storage_url, ii.filename)
             FROM images ii WHERE ii.card_id = sc.card_id LIMIT 1) AS card_image_url
        FROM set_cards sc
        JOIN sets       s  ON sc.set_id    = s.set_id
        LEFT JOIN countries c ON sc.country_id = c.country_id
        WHERE sc.player_id = %s
        ORDER BY s.year_start, s.set_name, sc.card_number
    """, (player_id,))

    # Reconstruct local image paths where storage_url is absent
    if APP_CONFIG.get("use_local_images"):
        for a in appearances:
            url = a.get("card_image_url") or ""
            if url and not url.startswith(("http", "/")):
                og = a.get("og_title") or a.get("set_name") or ""
                a["card_image_url"] = _local_image_url(og, url)

    # Clubs this player is associated with (via set_cards)
    clubs = query("""
        SELECT DISTINCT club_raw
        FROM set_cards
        WHERE player_id = %s AND club_raw IS NOT NULL
        ORDER BY club_raw
    """, (player_id,))

    links = query(
        "SELECT link_id, link_name, link_url FROM player_links WHERE player_id = %s ORDER BY link_id",
        (player_id,),
    )

    context = dict(
        request=request,
        player=player,
        appearances=appearances,
        clubs=clubs,
        links=links,
    )
    return templates.TemplateResponse("players/detail.html", context)
