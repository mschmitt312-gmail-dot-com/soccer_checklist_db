# webapp/routers/sets.py
# Routes for browsing card sets.

import re
import unicodedata

from fastapi import APIRouter, Request, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from database import query
from config import APP_CONFIG


def _local_image_url(og_title: str, filename: str) -> str:
    """Reconstruct the local static URL for a scraped image file.

    Mirrors the scraper's safe_folder_name() logic so the folder path matches
    what was written to disk.  Returns a URL rooted at /local-images/.
    """
    name = (og_title or "").replace('\xa0', ' ').replace('\u200b', '').strip()
    name = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode('ascii')
    name = re.sub(r'[\*?:"<>|]', '', name)
    name = re.sub(r'\s+', '_', name)
    folder = name[:120]
    return f"/local-images/{folder}/{filename}"

router = APIRouter(prefix="/sets", tags=["sets"])
templates = Jinja2Templates(directory="templates")

PER_PAGE = APP_CONFIG["items_per_page"]


# ── Sets list ─────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def sets_list(
    request: Request,
    q: str = Query(default="", description="Search set name or publisher"),
    decade: str = Query(default="", description="Filter by decade e.g. 1950"),
    country: str = Query(default="", description="Filter by publishing country"),
    page: int = Query(default=1, ge=1),
):
    offset = (page - 1) * PER_PAGE
    like   = f"%{q}%"

    # Build WHERE clause (JOIN must come before WHERE in SQL)
    where_parts  = ["(s.set_name LIKE %s OR s.publisher LIKE %s)"]
    where_params = [like, like]

    if decade.isdigit():
        decade_start = int(decade)
        decade_end   = decade_start + 9
        where_parts.append(
            "(s.year_start BETWEEN %s AND %s OR s.year_end BETWEEN %s AND %s)"
        )
        where_params += [decade_start, decade_end, decade_start, decade_end]

    if country:
        where_parts.append("s.country = %s")
        where_params.append(country)

    where_sql = "WHERE " + " AND ".join(where_parts)

    total_row = query(
        f"SELECT COUNT(*) AS cnt FROM sets s {where_sql}",
        tuple(where_params),
        one=True,
    )
    total = total_row["cnt"] if total_row else 0

    sets = query(f"""
        SELECT
            s.set_id,
            COALESCE(s.set_name, s.og_title, '(Untitled)') AS set_name,
            s.og_title,
            s.publisher,
            s.year_start,
            s.year_end,
            s.season_raw,
            s.country,
            s.total_cards,
            s.cards_found,
            COUNT(DISTINCT sc.card_id) AS card_count,
            COUNT(DISTINCT i.image_id)  AS image_count
        FROM sets s
        LEFT JOIN set_cards sc ON s.set_id = sc.set_id
        LEFT JOIN images    i  ON s.set_id = i.set_id AND i.card_id IS NULL
        {where_sql}
        GROUP BY s.set_id
        ORDER BY (s.category = 'other'), (COUNT(DISTINCT sc.card_id) = 0), (s.year_start IS NULL), s.year_start ASC, s.set_name
        LIMIT %s OFFSET %s
    """, tuple(where_params) + (PER_PAGE, offset))

    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)

    # Dropdown options
    decades = query("""
        SELECT DISTINCT (FLOOR(year_start / 10) * 10) AS decade
        FROM sets
        WHERE year_start IS NOT NULL
        ORDER BY decade DESC
    """)

    countries = query("""
        SELECT DISTINCT country
        FROM sets
        WHERE country IS NOT NULL AND country != ''
        ORDER BY country
    """)

    context = dict(
        request=request,
        sets=sets,
        q=q,
        decade=decade,
        country=country,
        decades=decades,
        countries=countries,
        page=page,
        total_pages=total_pages,
        total=total,
    )

    # HTMX partial: return only the results fragment
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse("sets/list_rows.html", context)

    return templates.TemplateResponse("sets/list.html", context)


# ── Set detail ────────────────────────────────────────────────────────────────

@router.get("/{set_id}", response_class=HTMLResponse)
async def set_detail(
    request: Request,
    set_id: int,
    q: str = Query(default="", description="Search cards in this set"),
    page: int = Query(default=1, ge=1),
):
    # Fetch set metadata
    the_set = query("""
        SELECT
            s.set_id,
            COALESCE(s.set_name, s.og_title, '(Untitled)') AS set_name,
            s.og_title,
            s.publisher,
            s.year_start,
            s.year_end,
            s.season_raw,
            s.country,
            s.category,
            s.total_cards,
            s.cards_found,
            s.source_url,
            COUNT(sc.card_id) AS card_count
        FROM sets s
        LEFT JOIN set_cards sc ON s.set_id = sc.set_id
        WHERE s.set_id = %s
        GROUP BY s.set_id
    """, (set_id,), one=True)

    if not the_set:
        return HTMLResponse("<h2>Set not found</h2>", status_code=404)

    offset   = (page - 1) * PER_PAGE
    like     = f"%{q}%"

    total_row = query("""
        SELECT COUNT(*) AS cnt
        FROM set_cards sc
        WHERE sc.set_id = %s AND sc.name_in_set LIKE %s
    """, (set_id, like), one=True)
    total = total_row["cnt"] if total_row else 0

    cards = query("""
        SELECT
            sc.card_id,
            sc.card_number,
            sc.name_in_set,
            sc.club_raw,
            sc.player_id,
            p.first_name,
            p.last_name,
            p.is_non_player,
            c.country_name,
            COUNT(i.image_id) AS image_count,
            (SELECT COALESCE(ii.storage_url, ii.filename)
             FROM images ii WHERE ii.card_id = sc.card_id LIMIT 1) AS card_image_url
        FROM set_cards sc
        LEFT JOIN players   p ON sc.player_id  = p.player_id
        LEFT JOIN countries c ON sc.country_id = c.country_id
        LEFT JOIN images    i ON sc.card_id    = i.card_id
        WHERE sc.set_id = %s AND sc.name_in_set LIKE %s
        GROUP BY sc.card_id
        ORDER BY
            CASE WHEN sc.card_number REGEXP '^[0-9]+$'
                 THEN CAST(sc.card_number AS UNSIGNED)
                 ELSE 9999 END,
            sc.card_number,
            sc.name_in_set
        LIMIT %s OFFSET %s
    """, (set_id, like, PER_PAGE, offset))

    # Fetch set-level images only (card_id IS NULL keeps card-specific images out of the gallery)
    images = query("""
        SELECT image_id, filename, storage_url, sort_order
        FROM images
        WHERE set_id = %s AND card_id IS NULL
        ORDER BY sort_order
    """, (set_id,))

    # If running locally with scraped images, fill in local URLs where storage_url is NULL
    if APP_CONFIG.get("use_local_images"):
        og_title = the_set.get("og_title") or the_set.get("set_name") or ""
        for img in images:
            if not img.get("storage_url") and img.get("filename"):
                img["storage_url"] = _local_image_url(og_title, img["filename"])
        # Fix card-level image URLs — the subquery returns a bare filename when storage_url is NULL
        for card in cards:
            url = card.get("card_image_url") or ""
            if card.get("image_count") and not url.startswith(("http", "/")):
                card["card_image_url"] = _local_image_url(og_title, url) if url else None

    # Fetch notes
    notes = query("""
        SELECT note_text, note_source, source_url
        FROM notes
        WHERE set_id = %s
        ORDER BY created_at
    """, (set_id,))

    # Fetch links
    links = query("""
        SELECT link_id, link_name, link_url
        FROM set_links
        WHERE set_id = %s
        ORDER BY created_at
    """, (set_id,))

    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)

    context = dict(
        request=request,
        the_set=the_set,
        cards=cards,
        images=images,
        notes=notes,
        links=links,
        q=q,
        page=page,        total_pages=total_pages,
        total=total,
    )

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse("sets/detail_rows.html", context)

    return templates.TemplateResponse("sets/detail.html", context)
