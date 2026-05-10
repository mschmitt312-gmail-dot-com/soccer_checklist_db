# webapp/routers/editor.py
# Editor routes — all protected by login.
# Every route must call require_user(request) and return early if it gets back
# a RedirectResponse (meaning the user is not logged in).

import os
import re
import unicodedata
import shutil

from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from typing import Optional
from routers.auth import require_user
from database import query, execute
from config import APP_CONFIG

router    = APIRouter(prefix="/editor", tags=["editor"])
templates = Jinja2Templates(directory="templates")


# ── Editor dashboard ──────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def editor_dashboard(request: Request):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    return templates.TemplateResponse("editor/dashboard.html", {
        "request": request,
        "user":    user,
    })



@router.get("/changelog", response_class=HTMLResponse)
async def editor_changelog(
    request:  Request,
    q_player: str = "",
    q_set:    str = "",
    limit:    int = 10,
):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    where_sets    = []
    where_cards   = []
    where_players = []
    params_sets:    list = []
    params_cards:   list = []
    params_players: list = []

    if q_set.strip():
        like = f"%{q_set.strip()}%"
        where_sets.append(
            "(COALESCE(h.set_name, h.og_title) LIKE %s OR h.publisher LIKE %s)"
        )
        params_sets += [like, like]
        where_cards.append(
            "(COALESCE(s.set_name, s.og_title) LIKE %s OR s.publisher LIKE %s)"
        )
        params_cards += [like, like]

    if q_player.strip():
        like = f"%{q_player.strip()}%"
        where_players.append(
            "(COALESCE(h.first_name,'') LIKE %s OR h.last_name LIKE %s)"
        )
        params_players += [like, like]
        where_cards.append(
            "(h.name_in_set LIKE %s)"
        )
        params_cards.append(like)

    # When filtering by player only, skip sets entirely (and vice versa)
    include_sets    = not q_player.strip()
    include_players = not q_set.strip()

    def _where(parts):
        return ("WHERE " + " AND ".join(parts)) if parts else ""

    rows = []

    if include_sets:
        rows += query(f"""
            SELECT 'set' AS entity_type, h.action, h.changed_at, h.changed_by,
                   h.history_id,
                   h.set_id,
                   COALESCE(h.set_name, h.og_title, '(Untitled)') AS label,
                   h.publisher, h.year_start,
                   NULL AS sub_label,
                   NULL AS entity_id2
            FROM sets_history h
            {_where(where_sets)}
            ORDER BY h.changed_at DESC
            LIMIT %s
        """, tuple(params_sets) + (limit,))

    rows += query(f"""
        SELECT 'card' AS entity_type, h.action, h.changed_at, h.changed_by,
               h.history_id,
               h.set_id,
               COALESCE(s.set_name, s.og_title, '(Untitled)') AS label,
               s.publisher, s.year_start,
               h.name_in_set AS sub_label,
               h.card_id AS entity_id2
        FROM set_cards_history h
        LEFT JOIN sets s ON h.set_id = s.set_id
        {_where(where_cards)}
        ORDER BY h.changed_at DESC
        LIMIT %s
    """, tuple(params_cards) + (limit,))

    if include_players:
        rows += query(f"""
            SELECT 'player' AS entity_type, h.action, h.changed_at, h.changed_by,
                   h.history_id,
                   NULL AS set_id,
                   TRIM(CONCAT(COALESCE(h.first_name,''), ' ', COALESCE(h.last_name,''))) AS label,
                   NULL AS publisher, NULL AS year_start,
                   NULL AS sub_label,
                   h.player_id AS entity_id2
            FROM players_history h
            {_where(where_players)}
            ORDER BY h.changed_at DESC
            LIMIT %s
        """, tuple(params_players) + (limit,))

    # Sort merged results and take the top N
    rows.sort(key=lambda r: r["changed_at"], reverse=True)
    rows = rows[:limit]

    return templates.TemplateResponse("editor/partials/changelog.html", {
        "request":  request,
        "rows":     rows,
        "q_player": q_player,
        "q_set":    q_set,
        "limit":    limit,
    })


@router.get("/changelog/detail", response_class=HTMLResponse)
async def changelog_detail(
    request:     Request,
    entity_type: str,
    history_id:  int,
):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    old = None
    cur = None
    fields = []

    if entity_type == "player":
        old = query(
            "SELECT * FROM players_history WHERE history_id = %s",
            (history_id,), one=True,
        )
        if old:
            cur = query(
                "SELECT * FROM players WHERE player_id = %s",
                (old["player_id"],), one=True,
            )
        fields = [
            ("First name",   "first_name"),
            ("Last name",    "last_name"),
            ("Display name", "display_name"),
            ("Nationality",  "nationality"),
            ("Date of birth","date_of_birth"),
            ("Birth place",  "birth_place"),
            ("Photo URL",    "photo_url"),
        ]

    elif entity_type == "set":
        old = query(
            "SELECT * FROM sets_history WHERE history_id = %s",
            (history_id,), one=True,
        )
        if old:
            cur = query(
                "SELECT * FROM sets WHERE set_id = %s",
                (old["set_id"],), one=True,
            )
        fields = [
            ("Set name",    "set_name"),
            ("Publisher",   "publisher"),
            ("Country",     "country"),
            ("Year start",  "year_start"),
            ("Year end",    "year_end"),
            ("Total cards", "total_cards"),
            ("Season raw",  "season_raw"),
        ]

    elif entity_type == "card":
        old = query(
            "SELECT * FROM set_cards_history WHERE history_id = %s",
            (history_id,), one=True,
        )
        if old:
            cur = query(
                "SELECT * FROM set_cards WHERE card_id = %s",
                (old["card_id"],), one=True,
            )
        fields = [
            ("Card number",  "card_number"),
            ("Name in set",  "name_in_set"),
            ("Player ID",    "player_id"),
            ("Confirmed",    "confirmed"),
        ]

    # Build diff: list of (label, old_val, new_val, changed)
    diff = []
    for label, key in fields:
        old_val = str(old[key]) if old and old.get(key) is not None else "—"
        new_val = str(cur[key]) if cur and cur.get(key) is not None else ("deleted" if old and old.get("action") == "DELETE" else "—")
        diff.append({
            "label":   label,
            "old":     old_val,
            "new":     new_val,
            "changed": old_val != new_val,
        })

    return templates.TemplateResponse("editor/partials/changelog_detail.html", {
        "request":     request,
        "diff":        diff,
        "entity_type": entity_type,
        "history_id":  history_id,
        "action":      old["action"] if old else "?",
        "changed_at":  old["changed_at"] if old else None,
    })


# ═══════════════════════════════════════════════════════════════════════════════
# PLAYER EDITOR
# ═══════════════════════════════════════════════════════════════════════════════

def _fetch_player(player_id: int):
    return query("""
        SELECT player_id, first_name, last_name, name_raw, display_name,
               nationality, date_of_birth, birth_year, birth_place,
               photo_url, photo_position
        FROM players WHERE player_id = %s
    """, (player_id,), one=True)


@router.get("/players/{player_id}/view-panel", response_class=HTMLResponse)
async def view_player_panel(request: Request, player_id: int):
    """Return the read-only info panel (used by Cancel in the edit form)."""
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    player = _fetch_player(player_id)
    if not player:
        return HTMLResponse("<p class='text-danger'>Player not found.</p>", status_code=404)

    links = query(
        "SELECT link_id, player_id, link_name, link_url FROM player_links WHERE player_id = %s ORDER BY link_id",
        (player_id,),
    )

    return templates.TemplateResponse("editor/partials/player_info_panel.html", {
        "request": request,
        "player":  player,
        "links":   links,
        "saved":   False,
    })


@router.get("/players/{player_id}/edit-panel", response_class=HTMLResponse)
async def edit_player_panel(request: Request, player_id: int):
    """Return the inline edit form for a player's metadata."""
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    player = _fetch_player(player_id)
    if not player:
        return HTMLResponse("<p class='text-danger'>Player not found.</p>", status_code=404)

    links = query(
        "SELECT link_id, player_id, link_name, link_url FROM player_links WHERE player_id = %s ORDER BY link_id",
        (player_id,),
    )

    return templates.TemplateResponse("editor/partials/player_edit_panel.html", {
        "request": request,
        "player":  player,
        "links":   links,
    })


@router.post("/players/{player_id}", response_class=HTMLResponse)
async def save_player(
    request:      Request,
    player_id:    int,
    first_name:   str = Form(default=""),
    last_name:    str = Form(default=""),
    nationality:  str = Form(default=""),
    date_of_birth: str = Form(default=""),
    birth_place:  str = Form(default=""),
):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    dob = date_of_birth.strip() or None

    execute("""
        UPDATE players
        SET first_name    = %s,
            last_name     = %s,
            nationality   = %s,
            date_of_birth = %s,
            birth_place   = %s
        WHERE player_id   = %s
    """, (
        first_name.strip() or None,
        last_name.strip() or None,
        nationality.strip() or None,
        dob,
        birth_place.strip() or None,
        player_id,
    ))

    return HTMLResponse(
        "",
        headers={"HX-Redirect": f"/players/{player_id}"},
    )


# ── Player merge page ─────────────────────────────────────────────────────────

@router.get("/players/{player_id}/merge", response_class=HTMLResponse)
async def merge_player_page(request: Request, player_id: int):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    source = _fetch_player(player_id)
    if not source:
        return HTMLResponse("<h2>Player not found</h2>", status_code=404)

    if source.get("canonical_player_id"):
        return HTMLResponse(
            "<h2>This player is already an alias and cannot be used as a merge source.</h2>",
            status_code=400,
        )

    return templates.TemplateResponse("editor/player_merge.html", {
        "request": request,
        "user":    user,
        "source":  source,
    })


# ── Player merge search (typeahead) ──────────────────────────────────────────

@router.get("/players/merge-search", response_class=HTMLResponse)
async def merge_player_search(
    request:   Request,
    q:         str = "",
    exclude_id: int = 0,
):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    q = q.strip()
    if len(q) < 2:
        return HTMLResponse("")

    players = query("""
        SELECT p.player_id, p.first_name, p.last_name,
               COUNT(DISTINCT sc.set_id) AS set_count
        FROM players p
        JOIN set_cards sc ON sc.player_id = p.player_id
        WHERE p.canonical_player_id IS NULL
          AND p.is_non_player = 0
          AND p.player_id != %s
          AND (
              p.last_name LIKE %s
              OR CONCAT(COALESCE(p.first_name, ''), ' ', p.last_name) LIKE %s
          )
        GROUP BY p.player_id
        ORDER BY p.last_name, p.first_name
        LIMIT 20
    """, (exclude_id, f"%{q}%", f"%{q}%"))

    if not players:
        return HTMLResponse(
            "<div style='padding:6px 10px; font-size:.85rem; color:var(--grey-mid);'>"
            "No players found</div>"
        )

    rows = []
    for p in players:
        name      = f"{p['first_name'] or ''} {p['last_name'] or ''}".strip()
        set_label = f"{p['set_count']} set{'s' if p['set_count'] != 1 else ''}"
        pid       = p['player_id']
        rows.append(
            f'<div class="player-result-item" '
            f'onclick="selectMergeTarget({pid}, \'{name.replace(chr(39), chr(92)+chr(39))}\', {exclude_id})">'
            f'{name} <span style="color:var(--grey-mid); font-size:.8rem;">({set_label})</span>'
            f'</div>'
        )
    return HTMLResponse("\n".join(rows))


# ── Player merge preview ──────────────────────────────────────────────────────

@router.get("/players/{player_id}/merge-preview/{target_id}", response_class=HTMLResponse)
async def merge_preview(request: Request, player_id: int, target_id: int):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    source = query("""
        SELECT p.player_id, p.first_name, p.last_name, p.name_raw,
               p.nationality, p.date_of_birth, p.birth_year, p.birth_place, p.photo_url,
               COUNT(DISTINCT sc.card_id) AS card_count,
               COUNT(DISTINCT sc.set_id)  AS set_count,
               GROUP_CONCAT(DISTINCT s.set_id ORDER BY s.year_start SEPARATOR ',') AS set_ids
        FROM players p
        LEFT JOIN set_cards sc ON sc.player_id = p.player_id
        LEFT JOIN sets s       ON sc.set_id    = s.set_id
        WHERE p.player_id = %s
        GROUP BY p.player_id
    """, (player_id,), one=True)

    target = query("""
        SELECT p.player_id, p.first_name, p.last_name, p.name_raw,
               p.nationality, p.date_of_birth, p.birth_year, p.birth_place, p.photo_url,
               COUNT(DISTINCT sc.card_id) AS card_count,
               COUNT(DISTINCT sc.set_id)  AS set_count,
               GROUP_CONCAT(DISTINCT s.set_id ORDER BY s.year_start SEPARATOR ',') AS set_ids
        FROM players p
        LEFT JOIN set_cards sc ON sc.player_id = p.player_id
        LEFT JOIN sets s       ON sc.set_id    = s.set_id
        WHERE p.player_id = %s
        GROUP BY p.player_id
    """, (target_id,), one=True)

    if not source or not target:
        return HTMLResponse("<p class='text-danger'>Player not found.</p>", status_code=404)

    # Detect overlapping sets (same set_id in both)
    source_sets = set((source.get("set_ids") or "").split(",")) - {""}
    target_sets = set((target.get("set_ids") or "").split(",")) - {""}
    shared_sets = source_sets & target_sets

    return templates.TemplateResponse("editor/partials/merge_preview.html", {
        "request":     request,
        "source":      source,
        "target":      target,
        "shared_sets": len(shared_sets),
    })


# ── Player merge — confirm ────────────────────────────────────────────────────

@router.post("/players/{player_id}/merge/{target_id}", response_class=HTMLResponse)
async def confirm_merge(request: Request, player_id: int, target_id: int):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    if player_id == target_id:
        return HTMLResponse("<p class='text-danger'>Cannot merge a player into themselves.</p>",
                            status_code=400)

    source = _fetch_player(player_id)
    target = _fetch_player(target_id)

    if not source or not target:
        return HTMLResponse("<p class='text-danger'>Player not found.</p>", status_code=404)

    if source.get("canonical_player_id") or target.get("canonical_player_id"):
        return HTMLResponse(
            "<p class='text-danger'>One of these players is already an alias. "
            "Merge into the canonical record instead.</p>",
            status_code=400,
        )

    # 1. Re-point all cards from source to target
    execute(
        "UPDATE set_cards SET player_id = %s WHERE player_id = %s",
        (target_id, player_id),
    )

    # 2. Copy any metadata the target is missing
    execute("""
        UPDATE players
        SET nationality   = COALESCE(nationality,   %s),
            date_of_birth = COALESCE(date_of_birth, %s),
            birth_place   = COALESCE(birth_place,   %s),
            photo_url     = COALESCE(photo_url,     %s)
        WHERE player_id = %s
    """, (
        source.get("nationality"),
        source.get("date_of_birth"),
        source.get("birth_place"),
        source.get("photo_url"),
        target_id,
    ))

    # 3. Mark source as alias (retired)
    execute(
        "UPDATE players SET canonical_player_id = %s WHERE player_id = %s",
        (target_id, player_id),
    )

    return HTMLResponse("", headers={"HX-Redirect": f"/players/{target_id}"})


# ═══════════════════════════════════════════════════════════════════════════════
# SET EDITOR
# ═══════════════════════════════════════════════════════════════════════════════

def _get_countries():
    """Distinct normalized country values used across the sets table."""
    rows = query("""
        SELECT DISTINCT country FROM sets
        WHERE country IS NOT NULL AND country != ''
        ORDER BY country
    """)
    return [r["country"] for r in rows]


@router.get("/sets/new", response_class=HTMLResponse)
async def new_set_form(request: Request):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    return templates.TemplateResponse("editor/set_new.html", {
        "request":   request,
        "user":      user,
        "countries": _get_countries(),
    })


@router.post("/sets/new", response_class=HTMLResponse)
async def create_set(
    request:     Request,
    publisher:   str = Form(default=""),
    set_name:    str = Form(default=""),
    year_start:  str = Form(default=""),
    year_end:    str = Form(default=""),
    season_raw:  str = Form(default=""),
    country:     str = Form(default=""),
    category:    str = Form(default="football"),
    total_cards: str = Form(default=""),
    source_url:  str = Form(default=""),
):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    def _int_or_none(v):
        try:
            return int(v.strip()) if v.strip() else None
        except ValueError:
            return None

    if category not in ("football", "other"):
        category = "football"

    pub  = publisher.strip() or None
    name = set_name.strip() or None
    # og_title is NOT NULL with no default — use set_name or publisher as fallback
    og   = name or pub or ""

    set_id = execute("""
        INSERT INTO sets
            (og_title, publisher, set_name, year_start, year_end, season_raw,
             country, category, total_cards, source_url)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        og,
        pub,
        name,
        _int_or_none(year_start),
        _int_or_none(year_end),
        season_raw.strip() or None,
        country.strip() or None,
        category,
        _int_or_none(total_cards),
        source_url.strip() or None,
    ))

    return RedirectResponse(url=f"/editor/sets/{set_id}", status_code=303)


@router.get("/sets/{set_id}", response_class=HTMLResponse)
async def edit_set(request: Request, set_id: int):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    the_set = query("""
        SELECT set_id, og_title, set_name, publisher, year_start, year_end,
               season_raw, country, category, total_cards, cards_found, source_url
        FROM sets WHERE set_id = %s
    """, (set_id,), one=True)

    if not the_set:
        return HTMLResponse("<h2>Set not found</h2>", status_code=404)

    cards = query("""
        SELECT sc.card_id, sc.card_number, sc.name_in_set, sc.club_raw, sc.confirmed,
               p.player_id, p.first_name, p.last_name,
               (SELECT COUNT(DISTINCT sc2.set_id)
                FROM set_cards sc2
                WHERE sc2.player_id = p.player_id) AS player_set_count,
               EXISTS (SELECT 1 FROM images i WHERE i.card_id = sc.card_id) AS has_image
        FROM set_cards sc
        LEFT JOIN players p ON sc.player_id = p.player_id
        WHERE sc.set_id = %s
        ORDER BY sc.card_number IS NULL, sc.card_number, sc.name_in_set
    """, (set_id,))

    image_count = (query(
        "SELECT COUNT(*) AS cnt FROM images WHERE set_id = %s AND card_id IS NULL", (set_id,), one=True
    ) or {}).get("cnt", 0)

    notes = query("""
        SELECT note_id, note_text, note_source
        FROM notes
        WHERE set_id = %s
        ORDER BY created_at
    """, (set_id,))

    links = query("""
        SELECT link_id, link_name, link_url
        FROM set_links
        WHERE set_id = %s
        ORDER BY created_at
    """, (set_id,))

    return templates.TemplateResponse("editor/set_edit.html", {
        "request":     request,
        "user":        user,
        "the_set":     the_set,
        "set_id":      set_id,
        "cards":       cards,
        "countries":   _get_countries(),
        "image_count": image_count,
        "notes":       notes,
        "links":       links,
    })


# ── Delete set ────────────────────────────────────────────────────────────────

@router.delete("/sets/{set_id}", response_class=HTMLResponse)
async def delete_set(request: Request, set_id: int):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    # ON DELETE CASCADE handles set_cards, images, notes automatically
    execute("DELETE FROM sets WHERE set_id = %s", (set_id,))

    return HTMLResponse("", headers={"HX-Redirect": "/sets"})


# ── Save set metadata ─────────────────────────────────────────────────────────

@router.post("/sets/{set_id}/metadata", response_class=HTMLResponse)
async def save_set_metadata(
    request:     Request,
    set_id:      int,
    publisher:   str = Form(default=""),
    set_name:    str = Form(default=""),
    year_start:  str = Form(default=""),
    year_end:    str = Form(default=""),
    season_raw:  str = Form(default=""),
    country:     str = Form(default=""),
    category:    str = Form(default="football"),
    total_cards: str = Form(default=""),
    source_url:  str = Form(default=""),
):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    def _int_or_none(v):
        try:
            return int(v.strip()) if v.strip() else None
        except ValueError:
            return None

    category_val = category if category in ("football", "other") else "football"

    execute("""
        UPDATE sets
        SET publisher   = %s,
            set_name    = %s,
            year_start  = %s,
            year_end    = %s,
            season_raw  = %s,
            country     = %s,
            category    = %s,
            total_cards = %s,
            source_url  = %s
        WHERE set_id = %s
    """, (
        publisher.strip() or None,
        set_name.strip() or None,
        _int_or_none(year_start),
        _int_or_none(year_end),
        season_raw.strip() or None,
        country.strip() or None,
        category_val,
        _int_or_none(total_cards),
        source_url.strip() or None,
        set_id,
    ))

    the_set = query("""
        SELECT set_id, og_title, set_name, publisher, year_start, year_end,
               season_raw, country, category, total_cards, cards_found, source_url
        FROM sets WHERE set_id = %s
    """, (set_id,), one=True)

    return templates.TemplateResponse("editor/partials/metadata_form.html", {
        "request":   request,
        "the_set":   the_set,
        "saved":     True,
        "countries": _get_countries(),
    })


# ── Card row — switch to edit form ────────────────────────────────────────────

@router.get("/sets/{set_id}/cards/{card_id}/edit-row", response_class=HTMLResponse)
async def card_edit_row(request: Request, set_id: int, card_id: int):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    card = query("""
        SELECT sc.card_id, sc.card_number, sc.name_in_set, sc.club_raw, sc.confirmed,
               p.player_id, p.first_name, p.last_name
        FROM set_cards sc
        LEFT JOIN players p ON sc.player_id = p.player_id
        WHERE sc.card_id = %s AND sc.set_id = %s
    """, (card_id, set_id), one=True)

    if not card:
        return HTMLResponse("", status_code=404)

    return templates.TemplateResponse("editor/partials/card_row_edit.html", {
        "request": request,
        "card":    card,
        "set_id":  set_id,
    })


# ── Card row — cancel edit (restore read view) ────────────────────────────────

@router.get("/sets/{set_id}/cards/{card_id}/view-row", response_class=HTMLResponse)
async def card_view_row(request: Request, set_id: int, card_id: int):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    card = query("""
        SELECT sc.card_id, sc.card_number, sc.name_in_set, sc.club_raw, sc.confirmed,
               p.player_id, p.first_name, p.last_name,
               (SELECT COUNT(DISTINCT sc2.set_id)
                FROM set_cards sc2
                WHERE sc2.player_id = p.player_id) AS player_set_count,
               EXISTS (SELECT 1 FROM images i WHERE i.card_id = sc.card_id) AS has_image
        FROM set_cards sc
        LEFT JOIN players p ON sc.player_id = p.player_id
        WHERE sc.card_id = %s AND sc.set_id = %s
    """, (card_id, set_id), one=True)

    if not card:
        return HTMLResponse("", status_code=404)

    return templates.TemplateResponse("editor/partials/card_row.html", {
        "request": request,
        "card":    card,
        "set_id":  set_id,
    })


# ── Player live search ────────────────────────────────────────────────────────

@router.get("/player-search", response_class=HTMLResponse)
async def player_search(request: Request, q: str = "", card_id: int = 0):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    q = q.strip()
    if len(q) < 2:
        return HTMLResponse("")

    players = query("""
        SELECT p.player_id, p.first_name, p.last_name,
               COUNT(DISTINCT sc.set_id) AS set_count
        FROM players p
        JOIN set_cards sc ON sc.player_id = p.player_id
        WHERE p.canonical_player_id IS NULL
          AND p.is_non_player = 0
          AND (
              p.last_name LIKE %s
              OR CONCAT(COALESCE(p.first_name, ''), ' ', p.last_name) LIKE %s
          )
        GROUP BY p.player_id
        ORDER BY p.last_name, p.first_name
        LIMIT 25
    """, (f"%{q}%", f"%{q}%"))

    if not players:
        return HTMLResponse(
            "<div style='padding:6px 10px; font-size:.85rem; color:var(--grey-mid);'>"
            "No players found</div>"
        )

    rows = []
    for p in players:
        name      = f"{p['first_name'] or ''} {p['last_name'] or ''}".strip()
        name_js   = name.replace("\\", "\\\\").replace("'", "\\'")
        pid       = p['player_id']
        set_count = p['set_count']
        set_label = f"{set_count} set{'s' if set_count != 1 else ''}"
        rows.append(
            f'<div class="player-result-item" '
            f"onclick=\"selectPlayer({card_id},{pid},'{name_js}')\">"
            f'{name} <span style="color:var(--grey-mid); font-size:.8rem;">({set_label})</span>'
            f'</div>'
        )
    return HTMLResponse("\n".join(rows))


# ── Player quick-create ───────────────────────────────────────────────────────

@router.post("/player-quick-create")
async def player_quick_create(request: Request, name: str = Form(default="")):
    """Create a new player record from a raw name string.

    Parses first/last name, checks for an exact-match duplicate first,
    then inserts if none found. Returns JSON {player_id, display_name}.
    """
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return JSONResponse({"error": "Not logged in"}, status_code=401)

    name = name.strip()
    if not name:
        return JSONResponse({"error": "Name is required"}, status_code=400)

    parts = name.split()
    if len(parts) >= 2:
        first_name = parts[0]
        last_name  = " ".join(parts[1:])
    else:
        first_name = None
        last_name  = name

    # Check for existing exact match first to avoid creating duplicates
    if first_name:
        existing = query("""
            SELECT player_id, first_name, last_name FROM players
            WHERE first_name = %s AND last_name = %s
              AND is_non_player = 0 AND canonical_player_id IS NULL
            LIMIT 1
        """, (first_name, last_name), one=True)
    else:
        existing = query("""
            SELECT player_id, first_name, last_name FROM players
            WHERE (first_name IS NULL OR first_name = '') AND last_name = %s
              AND is_non_player = 0 AND canonical_player_id IS NULL
            LIMIT 1
        """, (last_name,), one=True)

    if existing:
        pid          = existing["player_id"]
        display_name = f"{existing['first_name'] or ''} {existing['last_name'] or ''}".strip()
        return JSONResponse({"player_id": pid, "display_name": display_name, "created": False})

    pid = execute("""
        INSERT INTO players (name_raw, first_name, last_name, is_non_player)
        VALUES (%s, %s, %s, 0)
    """, (name, first_name or None, last_name))

    display_name = f"{first_name or ''} {last_name}".strip()
    return JSONResponse({"player_id": pid, "display_name": display_name, "created": True})


# ── Card row — save edit ──────────────────────────────────────────────────────

@router.post("/sets/{set_id}/cards/{card_id}", response_class=HTMLResponse)
async def save_card(
    request:     Request,
    set_id:      int,
    card_id:     int,
    card_number: str = Form(default=""),
    name_in_set: str = Form(default=""),
    club_raw:    str = Form(default=""),
    player_id:   str = Form(default=""),
):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    def _int_or_none(v):
        try:
            return int(v.strip()) if v.strip() else None
        except ValueError:
            return None

    new_player_id = _int_or_none(player_id)

    execute("""
        UPDATE set_cards
        SET card_number = %s, name_in_set = %s, club_raw = %s, player_id = %s
        WHERE card_id = %s AND set_id = %s
    """, (
        _int_or_none(card_number),
        name_in_set.strip() or None,
        club_raw.strip() or None,
        new_player_id,
        card_id, set_id,
    ))

    card = query("""
        SELECT sc.card_id, sc.card_number, sc.name_in_set, sc.club_raw, sc.confirmed,
               p.player_id, p.first_name, p.last_name,
               (SELECT COUNT(DISTINCT sc2.set_id)
                FROM set_cards sc2
                WHERE sc2.player_id = p.player_id) AS player_set_count,
               EXISTS (SELECT 1 FROM images i WHERE i.card_id = sc.card_id) AS has_image
        FROM set_cards sc
        LEFT JOIN players p ON sc.player_id = p.player_id
        WHERE sc.card_id = %s
    """, (card_id,), one=True)

    return templates.TemplateResponse("editor/partials/card_row.html", {
        "request": request,
        "card":    card,
        "set_id":  set_id,
        "saved":   True,
    })


# ── Card row — delete ─────────────────────────────────────────────────────────

@router.delete("/sets/{set_id}/cards/{card_id}", response_class=HTMLResponse)
async def delete_card(request: Request, set_id: int, card_id: int):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    execute("DELETE FROM set_cards WHERE card_id = %s AND set_id = %s", (card_id, set_id))
    execute("""
        UPDATE sets SET cards_found = (SELECT COUNT(*) FROM set_cards WHERE set_id = %s)
        WHERE set_id = %s
    """, (set_id, set_id))

    return HTMLResponse("")


# ── New card row form ─────────────────────────────────────────────────────────

@router.get("/sets/{set_id}/cards/new-row", response_class=HTMLResponse)
async def new_card_row(request: Request, set_id: int):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    return templates.TemplateResponse("editor/partials/card_row_new.html", {
        "request": request,
        "set_id":  set_id,
    })


# ── New card — save ───────────────────────────────────────────────────────────

@router.post("/sets/{set_id}/cards", response_class=HTMLResponse)
async def add_card(
    request:     Request,
    set_id:      int,
    card_number: str = Form(default=""),
    name_in_set: str = Form(default=""),
    club_raw:    str = Form(default=""),
    player_id:   str = Form(default=""),
):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    name = name_in_set.strip()
    if not name:
        return HTMLResponse("<tr><td colspan='5' class='text-danger small p-2'>Name on card is required.</td></tr>")

    def _int_or_none(v):
        try:
            return int(v.strip()) if v.strip() else None
        except ValueError:
            return None

    # Use the explicitly chosen player if provided; otherwise leave unlinked
    player_id = _int_or_none(player_id)

    card_id = execute("""
        INSERT INTO set_cards (set_id, player_id, card_number, name_in_set, club_raw, confirmed)
        VALUES (%s, %s, %s, %s, %s, 1)
    """, (set_id, player_id, _int_or_none(card_number), name or None, club_raw.strip() or None))

    execute("""
        UPDATE sets SET cards_found = (SELECT COUNT(*) FROM set_cards WHERE set_id = %s)
        WHERE set_id = %s
    """, (set_id, set_id))

    card = query("""
        SELECT sc.card_id, sc.card_number, sc.name_in_set, sc.club_raw, sc.confirmed,
               p.player_id, p.first_name, p.last_name,
               (SELECT COUNT(DISTINCT sc2.set_id)
                FROM set_cards sc2
                WHERE sc2.player_id = p.player_id) AS player_set_count,
               EXISTS (SELECT 1 FROM images i WHERE i.card_id = sc.card_id) AS has_image
        FROM set_cards sc
        LEFT JOIN players p ON sc.player_id = p.player_id
        WHERE sc.card_id = %s
    """, (card_id,), one=True)

    return templates.TemplateResponse("editor/partials/card_row.html", {
        "request": request,
        "card":    card,
        "set_id":  set_id,
        "saved":   True,
    })


# ═══════════════════════════════════════════════════════════════════════════════
# IMAGE EDITOR
# ═══════════════════════════════════════════════════════════════════════════════

def _safe_folder_name(name: str) -> str:
    name = (name or "").replace('\xa0', ' ').replace('​', '').strip()
    name = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode('ascii')
    name = re.sub(r'[\*?:"<>|]', '', name)
    name = re.sub(r'\s+', '_', name)
    return name[:120]


def _local_image_url(og_title: str, filename: str) -> str:
    return f"/local-images/{_safe_folder_name(og_title)}/{filename}"


def _checklists_dir() -> str:
    return os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..", "soccer_checklists")
    )


def _resolve_display_url(img: dict, og_title: str) -> str:
    if img.get("storage_url"):
        return img["storage_url"]
    if img.get("filename") and APP_CONFIG.get("use_local_images"):
        return _local_image_url(og_title, img["filename"])
    return ""


# ── GET /editor/sets/{set_id}/images ─────────────────────────────────────────

@router.get("/sets/{set_id}/images", response_class=HTMLResponse)
async def edit_set_images(request: Request, set_id: int):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    the_set = query(
        "SELECT set_id, og_title, set_name, publisher FROM sets WHERE set_id = %s",
        (set_id,), one=True,
    )
    if not the_set:
        return HTMLResponse("<h2>Set not found</h2>", status_code=404)

    images = query("""
        SELECT image_id, filename, storage_url, card_id, sort_order
        FROM images
        WHERE set_id = %s
        ORDER BY card_id IS NOT NULL, sort_order, image_id
    """, (set_id,))

    for img in images:
        img["display_url"] = _resolve_display_url(img, the_set["og_title"])

    return templates.TemplateResponse("editor/set_images.html", {
        "request": request,
        "user":    user,
        "the_set": the_set,
        "images":  images,
    })


# ── POST /editor/sets/{set_id}/images/upload ─────────────────────────────────

@router.post("/sets/{set_id}/images/upload", response_class=HTMLResponse)
async def upload_image(
    request: Request,
    set_id:  int,
    file:    UploadFile = File(...),
):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    the_set = query(
        "SELECT set_id, og_title FROM sets WHERE set_id = %s", (set_id,), one=True
    )
    if not the_set:
        return HTMLResponse("<p class='text-danger small'>Set not found.</p>", status_code=404)

    allowed = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    _, ext = os.path.splitext(file.filename or "")
    if ext.lower() not in allowed:
        return templates.TemplateResponse("editor/partials/image_upload_result.html", {
            "request": request,
            "error":   f"File type '{ext or 'unknown'}' not allowed. Use JPG, PNG, GIF, or WebP.",
        })

    folder_name = _safe_folder_name(the_set["og_title"])
    dest_dir    = os.path.join(_checklists_dir(), folder_name)
    os.makedirs(dest_dir, exist_ok=True)

    safe_name = re.sub(r'[^\w.\-]', '_', os.path.basename(file.filename or "upload"))
    if not safe_name.lower().endswith(tuple(allowed)):
        safe_name += ext.lower()

    dest_path = os.path.join(dest_dir, safe_name)
    if os.path.exists(dest_path):
        base, ext2 = os.path.splitext(safe_name)
        counter = 1
        while os.path.exists(dest_path):
            safe_name = f"{base}_{counter}{ext2}"
            dest_path = os.path.join(dest_dir, safe_name)
            counter += 1

    with open(dest_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    image_id = execute(
        "INSERT INTO images (set_id, card_id, filename) VALUES (%s, NULL, %s)",
        (set_id, safe_name),
    )

    img = {
        "image_id":    image_id,
        "filename":    safe_name,
        "storage_url": None,
        "card_id":     None,
        "sort_order":  None,
    }
    img["display_url"] = _resolve_display_url(img, the_set["og_title"])

    return templates.TemplateResponse("editor/partials/image_tile.html", {
        "request": request,
        "img":     img,
        "set_id":  set_id,
        "new":     True,
    })


# ── DELETE /editor/sets/{set_id}/images/{image_id} ───────────────────────────

@router.delete("/sets/{set_id}/images/{image_id}", response_class=HTMLResponse)
async def delete_image(request: Request, set_id: int, image_id: int):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    img = query(
        "SELECT image_id, filename, storage_url, card_id FROM images WHERE image_id = %s AND set_id = %s",
        (image_id, set_id), one=True,
    )
    if not img:
        return HTMLResponse("", status_code=404)

    if not img.get("storage_url") and img.get("filename") and APP_CONFIG.get("use_local_images"):
        the_set = query("SELECT og_title FROM sets WHERE set_id = %s", (set_id,), one=True)
        if the_set:
            file_path = os.path.join(
                _checklists_dir(), _safe_folder_name(the_set["og_title"]), img["filename"]
            )
            if os.path.exists(file_path):
                os.remove(file_path)

    execute("DELETE FROM images WHERE image_id = %s AND set_id = %s", (image_id, set_id))
    return HTMLResponse("")


# ═══════════════════════════════════════════════════════════════════════════════
# CARD IMAGE EDITOR
# ═══════════════════════════════════════════════════════════════════════════════

def _get_card_with_set(card_id: int, set_id: int):
    """Return card + set info, or None if not found."""
    return query("""
        SELECT sc.card_id, sc.card_number, sc.name_in_set, sc.club_raw,
               s.set_id, s.og_title, s.set_name, s.publisher
        FROM set_cards sc
        JOIN sets s ON sc.set_id = s.set_id
        WHERE sc.card_id = %s AND sc.set_id = %s
    """, (card_id, set_id), one=True)


# ── GET /editor/sets/{set_id}/cards/{card_id}/images ─────────────────────────

@router.get("/sets/{set_id}/cards/{card_id}/images", response_class=HTMLResponse)
async def edit_card_images(request: Request, set_id: int, card_id: int):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    row = _get_card_with_set(card_id, set_id)
    if not row:
        return HTMLResponse("<h2>Card not found</h2>", status_code=404)

    # Images linked to this card
    card_images = query("""
        SELECT image_id, filename, storage_url, card_id, sort_order
        FROM images
        WHERE set_id = %s AND card_id = %s
        ORDER BY sort_order, image_id
    """, (set_id, card_id))

    for img in card_images:
        img["display_url"] = _resolve_display_url(img, row["og_title"])

    # All set-level images (card_id IS NULL) — available to link
    set_images = query("""
        SELECT image_id, filename, storage_url, card_id, sort_order
        FROM images
        WHERE set_id = %s AND card_id IS NULL
        ORDER BY sort_order, image_id
    """, (set_id,))

    for img in set_images:
        img["display_url"] = _resolve_display_url(img, row["og_title"])

    return templates.TemplateResponse("editor/card_images.html", {
        "request":     request,
        "user":        user,
        "the_set":     row,
        "card":        row,
        "card_images": card_images,
        "set_images":  set_images,
    })


# ── POST /editor/sets/{set_id}/cards/{card_id}/images/upload ─────────────────

@router.post("/sets/{set_id}/cards/{card_id}/images/upload", response_class=HTMLResponse)
async def upload_card_image(
    request: Request,
    set_id:  int,
    card_id: int,
    file:    UploadFile = File(...),
):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    row = _get_card_with_set(card_id, set_id)
    if not row:
        return HTMLResponse("<p class='text-danger small'>Card not found.</p>", status_code=404)

    allowed = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    _, ext = os.path.splitext(file.filename or "")
    if ext.lower() not in allowed:
        return HTMLResponse(
            f"<span class='text-danger small'>File type '{ext or 'unknown'}' not allowed.</span>"
        )

    folder_name = _safe_folder_name(row["og_title"])
    dest_dir    = os.path.join(_checklists_dir(), folder_name)
    os.makedirs(dest_dir, exist_ok=True)

    safe_name = re.sub(r'[^\w.\-]', '_', os.path.basename(file.filename or "upload"))
    if not safe_name.lower().endswith(tuple(allowed)):
        safe_name += ext.lower()

    dest_path = os.path.join(dest_dir, safe_name)
    if os.path.exists(dest_path):
        base, ext2 = os.path.splitext(safe_name)
        counter = 1
        while os.path.exists(dest_path):
            safe_name = f"{base}_{counter}{ext2}"
            dest_path = os.path.join(dest_dir, safe_name)
            counter += 1

    with open(dest_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    image_id = execute(
        "INSERT INTO images (set_id, card_id, filename) VALUES (%s, %s, %s)",
        (set_id, card_id, safe_name),
    )

    img = {
        "image_id":    image_id,
        "filename":    safe_name,
        "storage_url": None,
        "card_id":     card_id,
        "sort_order":  None,
    }
    img["display_url"] = _resolve_display_url(img, row["og_title"])

    return templates.TemplateResponse("editor/partials/card_image_tile.html", {
        "request": request,
        "img":     img,
        "set_id":  set_id,
        "card_id": card_id,
        "new":     True,
    })


# ── POST /editor/sets/{set_id}/cards/{card_id}/images/link/{image_id} ────────
# Links an existing set-level image to this card by creating a new images row
# that shares the same filename (and physical file) but has card_id set.

@router.post("/sets/{set_id}/cards/{card_id}/images/link/{image_id}", response_class=HTMLResponse)
async def link_image_to_card(request: Request, set_id: int, card_id: int, image_id: int):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    row = _get_card_with_set(card_id, set_id)
    if not row:
        return HTMLResponse("<p class='text-danger small'>Card not found.</p>", status_code=404)

    # Source image lookup is no longer restricted to the destination set —
    # editors can search the whole image library and link any image to a card.
    src = query("""
        SELECT i.image_id, i.filename, i.storage_url, i.set_id,
               s.og_title AS src_og_title
        FROM images i
        JOIN sets s ON i.set_id = s.set_id
        WHERE i.image_id = %s
    """, (image_id,), one=True)
    if not src:
        return HTMLResponse("<p class='text-danger small'>Image not found.</p>", status_code=404)

    # Check if this exact file is already linked to the card
    existing = query(
        "SELECT image_id FROM images WHERE set_id = %s AND card_id = %s AND filename = %s",
        (set_id, card_id, src["filename"]), one=True,
    )
    if existing:
        return HTMLResponse(
            "<span class='text-warning small'><i class='bi bi-exclamation-triangle me-1'></i>Already linked to this card.</span>"
        )

    # Resolve the storage_url to write on the new row.
    #   - If the source already has an absolute storage_url (Azure mode), copy it.
    #   - If we're cross-set in local mode, bake the source's resolved local
    #     path into storage_url so the file resolves to the source set's folder
    #     instead of the destination's (which wouldn't contain the file).
    #   - Same-set in local mode keeps storage_url NULL (existing behaviour).
    final_storage_url = src.get("storage_url")
    if not final_storage_url and src["set_id"] != set_id:
        if APP_CONFIG.get("use_local_images"):
            final_storage_url = _local_image_url(src["src_og_title"], src["filename"])

    new_id = execute(
        "INSERT INTO images (set_id, card_id, filename, storage_url) VALUES (%s, %s, %s, %s)",
        (set_id, card_id, src["filename"], final_storage_url),
    )

    img = {
        "image_id":    new_id,
        "filename":    src["filename"],
        "storage_url": final_storage_url,
        "card_id":     card_id,
        "sort_order":  None,
    }
    img["display_url"] = _resolve_display_url(img, row["og_title"])

    return templates.TemplateResponse("editor/partials/card_image_tile.html", {
        "request": request,
        "img":     img,
        "set_id":  set_id,
        "card_id": card_id,
        "new":     True,
    })


# ── DELETE /editor/sets/{set_id}/cards/{card_id}/images/{image_id} ────────────
# Removes the link between this image record and the card.
# Only deletes the physical file if the image was directly uploaded to the card
# (i.e. no other images record shares the same filename for this set).

@router.delete("/sets/{set_id}/cards/{card_id}/images/{image_id}", response_class=HTMLResponse)
async def delete_card_image(request: Request, set_id: int, card_id: int, image_id: int):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    img = query(
        "SELECT image_id, filename, storage_url, card_id FROM images WHERE image_id = %s AND set_id = %s AND card_id = %s",
        (image_id, set_id, card_id), one=True,
    )
    if not img:
        return HTMLResponse("", status_code=404)

    # Only remove the file if no other images record for this set references the same filename
    other_refs = query(
        "SELECT COUNT(*) AS cnt FROM images WHERE set_id = %s AND filename = %s AND image_id != %s",
        (set_id, img["filename"], image_id), one=True,
    )
    if (other_refs or {}).get("cnt", 1) == 0:
        if not img.get("storage_url") and img.get("filename") and APP_CONFIG.get("use_local_images"):
            the_set = query("SELECT og_title FROM sets WHERE set_id = %s", (set_id,), one=True)
            if the_set:
                file_path = os.path.join(
                    _checklists_dir(), _safe_folder_name(the_set["og_title"]), img["filename"]
                )
                if os.path.exists(file_path):
                    os.remove(file_path)

    execute("DELETE FROM images WHERE image_id = %s AND set_id = %s AND card_id = %s",
            (image_id, set_id, card_id))
    return HTMLResponse("")


# ═══════════════════════════════════════════════════════════════════════════════
# NOTES EDITOR
# ═══════════════════════════════════════════════════════════════════════════════

def _get_note(note_id: int, set_id: int):
    return query(
        "SELECT note_id, note_text, note_source, source_url FROM notes WHERE note_id = %s AND set_id = %s",
        (note_id, set_id), one=True,
    )


def _is_first_note(note_id: int, set_id: int) -> bool:
    """Return True if this note is the first (oldest) note for the set."""
    first = query(
        "SELECT note_id FROM notes WHERE set_id = %s ORDER BY created_at, note_id LIMIT 1",
        (set_id,), one=True,
    )
    return bool(first and first["note_id"] == note_id)


# ── GET  /editor/sets/{set_id}/notes/{note_id}/edit-row ──────────────────────

@router.get("/sets/{set_id}/notes/{note_id}/edit-row", response_class=HTMLResponse)
async def note_edit_row(request: Request, set_id: int, note_id: int):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    note = _get_note(note_id, set_id)
    if not note:
        return HTMLResponse("", status_code=404)

    return templates.TemplateResponse("editor/partials/note_row_edit.html", {
        "request": request,
        "note":    note,
        "set_id":  set_id,
    })


# ── GET  /editor/sets/{set_id}/notes/{note_id}/view-row ──────────────────────

@router.get("/sets/{set_id}/notes/{note_id}/view-row", response_class=HTMLResponse)
async def note_view_row(request: Request, set_id: int, note_id: int):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    note = _get_note(note_id, set_id)
    if not note:
        return HTMLResponse("", status_code=404)

    return templates.TemplateResponse("editor/partials/note_row.html", {
        "request":     request,
        "note":        note,
        "set_id":      set_id,
        "is_overview": _is_first_note(note_id, set_id),
    })


# ── POST /editor/sets/{set_id}/notes/{note_id} ────────────────────────────────

@router.post("/sets/{set_id}/notes/{note_id}", response_class=HTMLResponse)
async def save_note(
    request:     Request,
    set_id:      int,
    note_id:     int,
    note_text:   str = Form(default=""),
    note_source: str = Form(default="manual"),
    source_url:  str = Form(default=""),
):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    text = note_text.strip()
    if not text:
        return HTMLResponse(
            "<div class='text-danger small p-2'>Note text cannot be empty.</div>",
            status_code=422,
        )

    source = note_source if note_source in ("scraped", "manual") else "manual"

    execute(
        "UPDATE notes SET note_text = %s, note_source = %s, source_url = %s WHERE note_id = %s AND set_id = %s",
        (text, source, source_url.strip() or None, note_id, set_id),
    )

    note = _get_note(note_id, set_id)
    return templates.TemplateResponse("editor/partials/note_row.html", {
        "request":     request,
        "note":        note,
        "set_id":      set_id,
        "saved":       True,
        "is_overview": _is_first_note(note_id, set_id),
    })


# ── DELETE /editor/sets/{set_id}/notes/{note_id} ─────────────────────────────

@router.delete("/sets/{set_id}/notes/{note_id}", response_class=HTMLResponse)
async def delete_note(request: Request, set_id: int, note_id: int):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    execute("DELETE FROM notes WHERE note_id = %s AND set_id = %s", (note_id, set_id))
    return HTMLResponse("")


# ── GET  /editor/sets/{set_id}/notes/new-row ─────────────────────────────────

@router.get("/sets/{set_id}/notes/new-row", response_class=HTMLResponse)
async def new_note_row(request: Request, set_id: int):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    # Pre-fill with set's source_url as a convenience starting point
    prefill_url = (query(
        "SELECT source_url FROM sets WHERE set_id = %s", (set_id,), one=True
    ) or {}).get("source_url") or ""

    return templates.TemplateResponse("editor/partials/note_row_new.html", {
        "request":    request,
        "set_id":     set_id,
        "source_url": prefill_url,
    })


# ── POST /editor/sets/{set_id}/notes ─────────────────────────────────────────

@router.post("/sets/{set_id}/notes", response_class=HTMLResponse)
async def add_note(
    request:     Request,
    set_id:      int,
    note_text:   str = Form(default=""),
    note_source: str = Form(default="manual"),
    source_url:  str = Form(default=""),
):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    text = note_text.strip()
    if not text:
        return HTMLResponse(
            "<div class='text-danger small p-2'>Note text cannot be empty.</div>",
            status_code=422,
        )

    source = note_source if note_source in ("scraped", "manual") else "manual"

    note_id = execute(
        "INSERT INTO notes (set_id, note_text, note_source, source_url) VALUES (%s, %s, %s, %s)",
        (set_id, text, source, source_url.strip() or None),
    )

    note = _get_note(note_id, set_id)
    return templates.TemplateResponse("editor/partials/note_row.html", {
        "request":     request,
        "note":        note,
        "set_id":      set_id,
        "saved":       True,
        "is_overview": _is_first_note(note_id, set_id),
    })


# ═══════════════════════════════════════════════════════════════════════════════
# SET LINKS
# ═══════════════════════════════════════════════════════════════════════════════

def _get_link(link_id: int, set_id: int):
    return query(
        "SELECT link_id, set_id, link_name, link_url FROM set_links WHERE link_id = %s AND set_id = %s",
        (link_id, set_id), one=True,
    )


@router.get("/sets/{set_id}/links/new-row", response_class=HTMLResponse)
async def new_link_row(request: Request, set_id: int):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse("editor/partials/link_row_new.html", {
        "request": request,
        "set_id":  set_id,
    })


@router.post("/sets/{set_id}/links", response_class=HTMLResponse)
async def add_link(
    request:   Request,
    set_id:    int,
    link_name: str = Form(default=""),
    link_url:  str = Form(default=""),
):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    name = link_name.strip()
    url  = link_url.strip()
    if not name or not url:
        return HTMLResponse(
            "<div class='text-danger small p-2'>Name and URL are both required.</div>",
            status_code=422,
        )

    link_id = execute(
        "INSERT INTO set_links (set_id, link_name, link_url) VALUES (%s, %s, %s)",
        (set_id, name, url),
    )

    link = _get_link(link_id, set_id)
    return templates.TemplateResponse("editor/partials/link_row.html", {
        "request": request,
        "link":    link,
        "set_id":  set_id,
        "saved":   True,
    })


@router.get("/sets/{set_id}/links/{link_id}/view-row", response_class=HTMLResponse)
async def link_view_row(request: Request, set_id: int, link_id: int):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    link = _get_link(link_id, set_id)
    if not link:
        return HTMLResponse("", status_code=404)
    return templates.TemplateResponse("editor/partials/link_row.html", {
        "request": request,
        "link":    link,
        "set_id":  set_id,
    })


@router.get("/sets/{set_id}/links/{link_id}/edit-row", response_class=HTMLResponse)
async def link_edit_row(request: Request, set_id: int, link_id: int):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    link = _get_link(link_id, set_id)
    if not link:
        return HTMLResponse("", status_code=404)
    return templates.TemplateResponse("editor/partials/link_row_edit.html", {
        "request": request,
        "link":    link,
        "set_id":  set_id,
    })


@router.post("/sets/{set_id}/links/{link_id}", response_class=HTMLResponse)
async def save_link(
    request:   Request,
    set_id:    int,
    link_id:   int,
    link_name: str = Form(default=""),
    link_url:  str = Form(default=""),
):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    name = link_name.strip()
    url  = link_url.strip()
    if not name or not url:
        return HTMLResponse(
            "<div class='text-danger small p-2'>Name and URL are both required.</div>",
            status_code=422,
        )

    execute(
        "UPDATE set_links SET link_name = %s, link_url = %s WHERE link_id = %s AND set_id = %s",
        (name, url, link_id, set_id),
    )

    link = _get_link(link_id, set_id)
    return templates.TemplateResponse("editor/partials/link_row.html", {
        "request": request,
        "link":    link,
        "set_id":  set_id,
        "saved":   True,
    })


@router.delete("/sets/{set_id}/links/{link_id}", response_class=HTMLResponse)
async def delete_link(request: Request, set_id: int, link_id: int):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    execute("DELETE FROM set_links WHERE link_id = %s AND set_id = %s", (link_id, set_id))
    return HTMLResponse("")


# ═══════════════════════════════════════════════════════════════════════════════
# PLAYER LINKS
# ═══════════════════════════════════════════════════════════════════════════════

def _get_player_link(link_id: int, player_id: int):
    return query(
        "SELECT link_id, player_id, link_name, link_url FROM player_links WHERE link_id = %s AND player_id = %s",
        (link_id, player_id), one=True,
    )


@router.get("/players/{player_id}/links/new-row", response_class=HTMLResponse)
async def new_player_link_row(request: Request, player_id: int):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse("editor/partials/player_link_row_new.html", {
        "request":   request,
        "player_id": player_id,
    })


@router.post("/players/{player_id}/links", response_class=HTMLResponse)
async def add_player_link(
    request:   Request,
    player_id: int,
    link_name: str = Form(default=""),
    link_url:  str = Form(default=""),
):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    name = link_name.strip()
    url  = link_url.strip()
    if not name or not url:
        return HTMLResponse(
            "<div class='text-danger small p-2'>Name and URL are both required.</div>",
            status_code=422,
        )

    link_id = execute(
        "INSERT INTO player_links (player_id, link_name, link_url) VALUES (%s, %s, %s)",
        (player_id, name, url),
    )

    link = _get_player_link(link_id, player_id)
    return templates.TemplateResponse("editor/partials/player_link_row.html", {
        "request":   request,
        "link":      link,
        "player_id": player_id,
        "saved":     True,
    })


@router.get("/players/{player_id}/links/{link_id}/view-row", response_class=HTMLResponse)
async def player_link_view_row(request: Request, player_id: int, link_id: int):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    link = _get_player_link(link_id, player_id)
    if not link:
        return HTMLResponse("", status_code=404)
    return templates.TemplateResponse("editor/partials/player_link_row.html", {
        "request":   request,
        "link":      link,
        "player_id": player_id,
    })


@router.get("/players/{player_id}/links/{link_id}/edit-row", response_class=HTMLResponse)
async def player_link_edit_row(request: Request, player_id: int, link_id: int):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    link = _get_player_link(link_id, player_id)
    if not link:
        return HTMLResponse("", status_code=404)
    return templates.TemplateResponse("editor/partials/player_link_row_edit.html", {
        "request":   request,
        "link":      link,
        "player_id": player_id,
    })


@router.post("/players/{player_id}/links/{link_id}", response_class=HTMLResponse)
async def save_player_link(
    request:   Request,
    player_id: int,
    link_id:   int,
    link_name: str = Form(default=""),
    link_url:  str = Form(default=""),
):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    name = link_name.strip()
    url  = link_url.strip()
    if not name or not url:
        return HTMLResponse(
            "<div class='text-danger small p-2'>Name and URL are both required.</div>",
            status_code=422,
        )

    execute(
        "UPDATE player_links SET link_name = %s, link_url = %s WHERE link_id = %s AND player_id = %s",
        (name, url, link_id, player_id),
    )

    link = _get_player_link(link_id, player_id)
    return templates.TemplateResponse("editor/partials/player_link_row.html", {
        "request":   request,
        "link":      link,
        "player_id": player_id,
        "saved":     True,
    })


@router.delete("/players/{player_id}/links/{link_id}", response_class=HTMLResponse)
async def delete_player_link(request: Request, player_id: int, link_id: int):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    execute("DELETE FROM player_links WHERE link_id = %s AND player_id = %s", (link_id, player_id))
    return HTMLResponse("")


# ═══════════════════════════════════════════════════════════════════════════════
# PLAYER PHOTO UPLOAD
# ═══════════════════════════════════════════════════════════════════════════════

def _player_photos_dir() -> str:
    """Subfolder inside soccer_checklists/ dedicated to player portrait photos."""
    return os.path.join(_checklists_dir(), "_player_photos")


@router.get("/players/{player_id}/photo", response_class=HTMLResponse)
async def player_photo_page(request: Request, player_id: int):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    player = _fetch_player(player_id)
    if not player:
        return HTMLResponse("<h2>Player not found</h2>", status_code=404)

    # Parse stored position into x/y integers for the crop UI
    pos_str = (player.get("photo_position") or "50% 50%").split()
    try:
        pos_x = int(float(pos_str[0].rstrip('%')))
        pos_y = int(float(pos_str[1].rstrip('%')))
    except (IndexError, ValueError):
        pos_x, pos_y = 50, 50

    return templates.TemplateResponse("editor/player_photo.html", {
        "request": request,
        "user":    user,
        "player":  player,
        "pos_x":   pos_x,
        "pos_y":   pos_y,
    })


@router.post("/players/{player_id}/photo/upload", response_class=HTMLResponse)
async def upload_player_photo(
    request:   Request,
    player_id: int,
    file:      UploadFile = File(...),
):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    player = _fetch_player(player_id)
    if not player:
        return HTMLResponse("<p class='text-danger'>Player not found.</p>", status_code=404)

    allowed = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    _, ext = os.path.splitext(file.filename or "")
    if ext.lower() not in allowed:
        return HTMLResponse(
            f"<div class='alert alert-danger py-2'>File type '{ext or 'unknown'}' not allowed. "
            f"Use JPG, PNG, GIF, or WebP.</div>",
            status_code=422,
        )

    dest_dir = _player_photos_dir()
    os.makedirs(dest_dir, exist_ok=True)

    safe_name = re.sub(r'[^\w.\-]', '_', os.path.basename(file.filename or "upload"))
    if not safe_name.lower().endswith(tuple(allowed)):
        safe_name += ext.lower()

    # Prefix with player_id to avoid collisions
    safe_name = f"p{player_id}_{safe_name}"
    dest_path = os.path.join(dest_dir, safe_name)

    # Deduplicate filename if needed
    if os.path.exists(dest_path):
        base, ext2 = os.path.splitext(safe_name)
        counter = 1
        while os.path.exists(dest_path):
            safe_name = f"{base}_{counter}{ext2}"
            dest_path = os.path.join(dest_dir, safe_name)
            counter += 1

    with open(dest_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    photo_url = f"/local-images/_player_photos/{safe_name}"
    execute(
        "UPDATE players SET photo_url = %s WHERE player_id = %s",
        (photo_url, player_id),
    )

    return HTMLResponse(
        "",
        headers={"HX-Redirect": f"/editor/players/{player_id}/photo"},
    )


# ── POST /editor/players/{player_id}/photo/use-existing/{image_id} ───────────
# Sets the player's photo to point at an image that already exists in the
# system (any card or set image). No file copy — we just resolve the source
# image's display URL and write it to players.photo_url. photo_position is
# reset to NULL because the new image's framing is unlikely to match the old.

@router.post("/players/{player_id}/photo/use-existing/{image_id}", response_class=HTMLResponse)
async def use_existing_player_photo(request: Request, player_id: int, image_id: int):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    player = _fetch_player(player_id)
    if not player:
        return HTMLResponse("<p class='text-danger small'>Player not found.</p>", status_code=404)

    src = query("""
        SELECT i.image_id, i.filename, i.storage_url, i.set_id,
               s.og_title
        FROM images i
        JOIN sets s ON i.set_id = s.set_id
        WHERE i.image_id = %s
    """, (image_id,), one=True)
    if not src:
        return HTMLResponse("<p class='text-danger small'>Image not found.</p>", status_code=404)

    photo_url = _resolve_display_url(src, src["og_title"])
    if not photo_url:
        return HTMLResponse(
            "<div class='alert alert-danger py-2'>Cannot resolve a usable URL for this image.</div>",
            status_code=400,
        )

    execute(
        "UPDATE players SET photo_url = %s, photo_position = NULL WHERE player_id = %s",
        (photo_url, player_id),
    )

    return HTMLResponse(
        "",
        headers={"HX-Redirect": f"/editor/players/{player_id}/photo"},
    )


@router.post("/players/{player_id}/photo/remove", response_class=HTMLResponse)
async def remove_player_photo(request: Request, player_id: int):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    execute(
        "UPDATE players SET photo_url = NULL, photo_position = NULL WHERE player_id = %s",
        (player_id,),
    )
    return HTMLResponse(
        "",
        headers={"HX-Redirect": f"/editor/players/{player_id}/photo"},
    )


@router.post("/players/{player_id}/photo/position", response_class=HTMLResponse)
async def save_player_photo_position(
    request:   Request,
    player_id: int,
    pos_x:     int = Form(default=50),
    pos_y:     int = Form(default=50),
):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    x = max(0, min(100, pos_x))
    y = max(0, min(100, pos_y))
    position = f"{x}% {y}%"

    execute(
        "UPDATE players SET photo_position = %s WHERE player_id = %s",
        (position, player_id),
    )
    return HTMLResponse(
        '<div class="alert alert-success py-2 small mb-0">'
        '<i class="bi bi-check-circle me-1"></i>Position saved.</div>'
    )


# ═══════════════════════════════════════════════════════════════════════════════
# IMAGE MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

def _local_image_url_mgr(og_title: str, filename: str) -> str:
    import re as _re, unicodedata as _ud
    name = (og_title or "").replace('\xa0', ' ').replace('​', '').strip()
    name = _ud.normalize('NFKD', name).encode('ascii', 'ignore').decode('ascii')
    name = _re.sub(r'[\*?:"<>|]', '', name)
    name = _re.sub(r'\s+', '_', name)
    return f"/local-images/{name[:120]}/{filename}"


def _fix_img_url(img: dict, use_local: bool):
    url = img.get("storage_url") or ""
    if url:
        return url
    filename = img.get("filename") or ""
    if not filename:
        return None
    if use_local:
        og = img.get("og_title") or img.get("set_name") or ""
        return _local_image_url_mgr(og, filename)
    return None


IMG_PER_PAGE = 48


@router.get("/images", response_class=HTMLResponse)
async def manage_images_page(request: Request):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    missing_count = (query("""
        SELECT COUNT(*) AS cnt FROM set_cards sc
        WHERE NOT EXISTS (SELECT 1 FROM images i WHERE i.card_id = sc.card_id)
    """, one=True) or {}).get("cnt", 0)

    unlinked_count = (query(
        "SELECT COUNT(*) AS cnt FROM images WHERE card_id IS NULL", one=True
    ) or {}).get("cnt", 0)

    library_count = (query(
        "SELECT COUNT(*) AS cnt FROM images", one=True
    ) or {}).get("cnt", 0)

    return templates.TemplateResponse("editor/images_manage.html", {
        "request":        request,
        "user":           user,
        "missing_count":  missing_count,
        "unlinked_count": unlinked_count,
        "library_count":  library_count,
    })


@router.get("/images/missing-cards", response_class=HTMLResponse)
async def images_missing_cards(
    request: Request,
    page:    int = 1,
    set_id:  int = 0,
    q:       str = "",
):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    # Always load the set dropdown (cheap)
    sets = query("""
        SELECT DISTINCT s.set_id,
               COALESCE(s.set_name, s.og_title, '(Untitled)') AS set_name,
               s.publisher, s.year_start
        FROM set_cards sc
        JOIN sets s ON sc.set_id = s.set_id
        WHERE NOT EXISTS (SELECT 1 FROM images i WHERE i.card_id = sc.card_id)
        ORDER BY s.year_start IS NULL, s.year_start, set_name
    """)

    q = q.strip()

    # Require at least one filter before running the heavy query
    if not set_id and not q:
        return templates.TemplateResponse("editor/partials/images_missing_cards.html", {
            "request":     request,
            "cards":       [],
            "sets":        sets,
            "total":       0,
            "page":        1,
            "total_pages": 1,
            "set_id":      set_id,
            "q":           "",
            "no_filter":   True,
        })

    offset = (page - 1) * IMG_PER_PAGE
    where_parts = ["NOT EXISTS (SELECT 1 FROM images i WHERE i.card_id = sc.card_id)"]
    params: list = []

    if set_id:
        where_parts.append("sc.set_id = %s")
        params.append(set_id)

    if q:
        like = f"%{q}%"
        where_parts.append(
            "(sc.name_in_set LIKE %s OR p.last_name LIKE %s OR p.first_name LIKE %s)"
        )
        params += [like, like, like]

    where = "WHERE " + " AND ".join(where_parts)

    # When filtering by player name we need the JOIN in the count too
    join_player = "LEFT JOIN players p ON sc.player_id = p.player_id"

    total = (query(
        f"SELECT COUNT(*) AS cnt FROM set_cards sc {join_player} {where}",
        tuple(params), one=True
    ) or {}).get("cnt", 0)

    cards = query(f"""
        SELECT sc.card_id, sc.card_number, sc.name_in_set, sc.club_raw,
               s.set_id, COALESCE(s.set_name, s.og_title, '(Untitled)') AS set_name,
               s.publisher, s.year_start,
               p.first_name, p.last_name
        FROM set_cards sc
        JOIN sets s ON sc.set_id = s.set_id
        {join_player}
        {where}
        ORDER BY s.year_start IS NULL, s.year_start, set_name,
                 sc.card_number IS NULL, sc.card_number
        LIMIT %s OFFSET %s
    """, tuple(params) + (IMG_PER_PAGE, offset))

    total_pages = max(1, (total + IMG_PER_PAGE - 1) // IMG_PER_PAGE)

    return templates.TemplateResponse("editor/partials/images_missing_cards.html", {
        "request":     request,
        "cards":       cards,
        "sets":        sets,
        "total":       total,
        "page":        page,
        "total_pages": total_pages,
        "set_id":      set_id,
        "q":           q,
        "no_filter":   False,
    })


@router.get("/images/unlinked", response_class=HTMLResponse)
async def images_unlinked(
    request: Request,
    page:    int = 1,
    set_id:  int = 0,
):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    use_local = APP_CONFIG.get("use_local_images", False)

    # Always load set dropdown (cheap)
    sets = query("""
        SELECT DISTINCT s.set_id,
               COALESCE(s.set_name, s.og_title, '(Untitled)') AS set_name,
               s.publisher, s.year_start
        FROM images i
        JOIN sets s ON i.set_id = s.set_id
        WHERE i.card_id IS NULL
        ORDER BY s.year_start IS NULL, s.year_start, set_name
    """)

    # Require a set filter before running the heavy query
    if not set_id:
        return templates.TemplateResponse("editor/partials/images_unlinked.html", {
            "request":     request,
            "images":      [],
            "sets":        sets,
            "total":       0,
            "page":        1,
            "total_pages": 1,
            "set_id":      0,
            "no_filter":   True,
        })

    offset = (page - 1) * IMG_PER_PAGE
    where_parts = ["i.card_id IS NULL", "i.set_id = %s"]
    params: list = [set_id]

    where = "WHERE " + " AND ".join(where_parts)

    total = (query(
        f"SELECT COUNT(*) AS cnt FROM images i {where}", tuple(params), one=True
    ) or {}).get("cnt", 0)

    images = query(f"""
        SELECT i.image_id, i.filename, i.storage_url, i.set_id, i.sort_order,
               s.og_title, COALESCE(s.set_name, s.og_title, '(Untitled)') AS set_name,
               s.publisher, s.year_start
        FROM images i
        JOIN sets s ON i.set_id = s.set_id
        {where}
        ORDER BY s.year_start IS NULL, s.year_start, set_name, i.sort_order
        LIMIT %s OFFSET %s
    """, tuple(params) + (IMG_PER_PAGE, offset))


    for img in images:
        img["display_url"] = _fix_img_url(img, use_local)

    total_pages = max(1, (total + IMG_PER_PAGE - 1) // IMG_PER_PAGE)

    return templates.TemplateResponse("editor/partials/images_unlinked.html", {
        "request":     request,
        "images":      images,
        "sets":        sets,
        "total":       total,
        "page":        page,
        "total_pages": total_pages,
        "set_id":      set_id,
        "no_filter":   False,
    })


@router.get("/images/library", response_class=HTMLResponse)
async def images_library(
    request: Request,
    page:    int = 1,
    q:       str = "",
    set_id:  int = 0,
):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    use_local = APP_CONFIG.get("use_local_images", False)
    q = q.strip()

    sets = query("""
        SELECT DISTINCT s.set_id,
               COALESCE(s.set_name, s.og_title, '(Untitled)') AS set_name,
               s.publisher, s.year_start
        FROM images i
        JOIN sets s ON i.set_id = s.set_id
        ORDER BY s.year_start IS NULL, s.year_start, set_name
    """)

    if not set_id and not q:
        return templates.TemplateResponse(
            "editor/partials/images_library.html",
            {
                "request":     request,
                "images":      [],
                "sets":        sets,
                "total":       0,
                "page":        1,
                "total_pages": 1,
                "set_id":      0,
                "q":           "",
                "no_filter":   True,
            }
        )

    offset       = (page - 1) * IMG_PER_PAGE
    where_parts: list = []
    params:      list = []

    if set_id:
        where_parts.append("i.set_id = %s")
        params.append(set_id)

    if q:
        where_parts.append(
            "(s.publisher LIKE %s OR s.set_name LIKE %s OR sc.name_in_set LIKE %s)"
        )
        like = f"%{q}%"
        params += [like, like, like]

    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    total = (query(
        f"""SELECT COUNT(*) AS cnt
            FROM images i
            JOIN sets s ON i.set_id = s.set_id
            LEFT JOIN set_cards sc ON i.card_id = sc.card_id
            {where}""",
        tuple(params), one=True,
    ) or {}).get("cnt", 0)

    images = query(f"""
        SELECT i.image_id, i.filename, i.storage_url, i.set_id, i.card_id,
               s.og_title, COALESCE(s.set_name, s.og_title, '(Untitled)') AS set_name,
               s.publisher, s.year_start,
               sc.name_in_set, sc.card_number
        FROM images i
        JOIN sets s ON i.set_id = s.set_id
        LEFT JOIN set_cards sc ON i.card_id = sc.card_id
        {where}
        ORDER BY s.year_start IS NULL, s.year_start, set_name,
                 i.card_id IS NULL DESC, i.sort_order
        LIMIT %s OFFSET %s
    """, tuple(params) + (IMG_PER_PAGE, offset))

    for img in images:
        img["display_url"] = _fix_img_url(img, use_local)

    total_pages = max(1, (total + IMG_PER_PAGE - 1) // IMG_PER_PAGE)

    return templates.TemplateResponse("editor/partials/images_library.html", {
        "request":     request,
        "images":      images,
        "sets":        sets,
        "total":       total,
        "page":        page,
        "total_pages": total_pages,
        "set_id":      set_id,
        "q":           q,
        "no_filter":   False,
    })


# ── GET /editor/sets/{set_id}/cards/{card_id}/images/search ───────────────────
# Card-scoped image-library search. Same query shape as /editor/images/library
# but the partial it renders has "Link to this card" buttons that hook into
# card_images.html's handleLinkClick() staging.
#
# `filter_set_id` (not `set_id`) is the dropdown filter — `set_id` and `card_id`
# in the path identify the destination card the editor is linking *to*.

@router.get("/sets/{set_id}/cards/{card_id}/images/search", response_class=HTMLResponse)
async def card_image_search(
    request:        Request,
    set_id:         int,
    card_id:        int,
    q:              str = "",
    filter_set_id:  int = 0,
    page:           int = 1,
):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    # Confirm the destination card exists — gives a clean 404 instead of an
    # empty results card with a malformed link button.
    dest = _get_card_with_set(card_id, set_id)
    if not dest:
        return HTMLResponse("<p class='text-danger small'>Card not found.</p>", status_code=404)

    use_local = APP_CONFIG.get("use_local_images", False)
    q = q.strip()

    # Set dropdown — every set that has at least one image.
    sets = query("""
        SELECT DISTINCT s.set_id,
               COALESCE(s.set_name, s.og_title, '(Untitled)') AS set_name,
               s.publisher, s.year_start
        FROM images i
        JOIN sets s ON i.set_id = s.set_id
        ORDER BY s.year_start IS NULL, s.year_start, set_name
    """)

    # Empty-state prompt before the editor types anything (mirrors Library tab).
    if not filter_set_id and not q:
        return templates.TemplateResponse(
            "editor/partials/card_image_search.html",
            {
                "request":       request,
                "images":        [],
                "sets":          sets,
                "total":         0,
                "page":          1,
                "total_pages":   1,
                "filter_set_id": 0,
                "q":             "",
                "no_filter":     True,
                "set_id":        set_id,
                "card_id":       card_id,
            }
        )

    offset       = (page - 1) * IMG_PER_PAGE
    where_parts: list = []
    params:      list = []

    # Hide images that are already linked to *this* card so the editor can't
    # double-link the same row. Other-card and set-level images are still
    # shown — same image can legitimately apply to multiple cards (team
    # photos, sticker album spreads, etc.).
    where_parts.append("NOT (i.set_id = %s AND i.card_id = %s)")
    params += [set_id, card_id]

    if filter_set_id:
        where_parts.append("i.set_id = %s")
        params.append(filter_set_id)

    if q:
        where_parts.append(
            "(s.publisher LIKE %s OR s.set_name LIKE %s OR sc.name_in_set LIKE %s)"
        )
        like = f"%{q}%"
        params += [like, like, like]

    where = "WHERE " + " AND ".join(where_parts)

    total = (query(
        f"""SELECT COUNT(*) AS cnt
            FROM images i
            JOIN sets s ON i.set_id = s.set_id
            LEFT JOIN set_cards sc ON i.card_id = sc.card_id
            {where}""",
        tuple(params), one=True,
    ) or {}).get("cnt", 0)

    images = query(f"""
        SELECT i.image_id, i.filename, i.storage_url, i.set_id, i.card_id,
               s.og_title, COALESCE(s.set_name, s.og_title, '(Untitled)') AS set_name,
               s.publisher, s.year_start,
               sc.name_in_set, sc.card_number
        FROM images i
        JOIN sets s ON i.set_id = s.set_id
        LEFT JOIN set_cards sc ON i.card_id = sc.card_id
        {where}
        ORDER BY s.year_start IS NULL, s.year_start, set_name,
                 i.card_id IS NULL DESC, i.sort_order
        LIMIT %s OFFSET %s
    """, tuple(params) + (IMG_PER_PAGE, offset))

    for img in images:
        img["display_url"] = _fix_img_url(img, use_local)

    total_pages = max(1, (total + IMG_PER_PAGE - 1) // IMG_PER_PAGE)

    return templates.TemplateResponse("editor/partials/card_image_search.html", {
        "request":       request,
        "images":        images,
        "sets":          sets,
        "total":         total,
        "page":          page,
        "total_pages":   total_pages,
        "filter_set_id": filter_set_id,
        "q":             q,
        "no_filter":     False,
        "set_id":        set_id,
        "card_id":       card_id,
    })


# ── GET /editor/players/{player_id}/photo/search ──────────────────────────────
# Player-photo image-library search. Defaults to the sets this player has cards
# in (filter_set_id = -1 sentinel), so the dropdown lands on the most relevant
# images on first load. Falls back to "All sets" if the player has no cards.
#
# filter_set_id values:
#    -1  →  player's sets only (default, when the player has any cards)
#     0  →  all sets
#    >0  →  single specific set

@router.get("/players/{player_id}/photo/search", response_class=HTMLResponse)
async def player_photo_search(
    request:        Request,
    player_id:      int,
    q:              str = "",
    filter_set_id:  int = -1,
    page:           int = 1,
):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    player = _fetch_player(player_id)
    if not player:
        return HTMLResponse("<p class='text-danger small'>Player not found.</p>", status_code=404)

    use_local = APP_CONFIG.get("use_local_images", False)
    q = q.strip()

    # Sets this player has cards in — drives both the default filter and the
    # "(player)" markers in the dropdown.
    player_set_ids = [
        r["set_id"] for r in query(
            "SELECT DISTINCT set_id FROM set_cards WHERE player_id = %s",
            (player_id,),
        )
    ]

    # Player has no cards yet → "Player's sets" is meaningless. Fall through
    # to "All sets" so the editor still sees results.
    if filter_set_id == -1 and not player_set_ids:
        filter_set_id = 0

    sets = query("""
        SELECT DISTINCT s.set_id,
               COALESCE(s.set_name, s.og_title, '(Untitled)') AS set_name,
               s.publisher, s.year_start
        FROM images i
        JOIN sets s ON i.set_id = s.set_id
        ORDER BY s.year_start IS NULL, s.year_start, set_name
    """)

    offset       = (page - 1) * IMG_PER_PAGE
    where_parts: list = []
    params:      list = []

    if filter_set_id == -1:
        # Player's sets — use IN (...) over the precomputed list. We've already
        # ruled out the empty-list case above.
        placeholders = ",".join(["%s"] * len(player_set_ids))
        where_parts.append(f"i.set_id IN ({placeholders})")
        params += player_set_ids
    elif filter_set_id > 0:
        where_parts.append("i.set_id = %s")
        params.append(filter_set_id)
    # filter_set_id == 0 → all sets, no filter clause

    if q:
        where_parts.append(
            "(s.publisher LIKE %s OR s.set_name LIKE %s OR sc.name_in_set LIKE %s)"
        )
        like = f"%{q}%"
        params += [like, like, like]

    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    total = (query(
        f"""SELECT COUNT(*) AS cnt
            FROM images i
            JOIN sets s ON i.set_id = s.set_id
            LEFT JOIN set_cards sc ON i.card_id = sc.card_id
            {where}""",
        tuple(params), one=True,
    ) or {}).get("cnt", 0)

    images = query(f"""
        SELECT i.image_id, i.filename, i.storage_url, i.set_id, i.card_id,
               s.og_title, COALESCE(s.set_name, s.og_title, '(Untitled)') AS set_name,
               s.publisher, s.year_start,
               sc.name_in_set, sc.card_number
        FROM images i
        JOIN sets s ON i.set_id = s.set_id
        LEFT JOIN set_cards sc ON i.card_id = sc.card_id
        {where}
        ORDER BY s.year_start IS NULL, s.year_start, set_name,
                 i.card_id IS NULL DESC, i.sort_order
        LIMIT %s OFFSET %s
    """, tuple(params) + (IMG_PER_PAGE, offset))

    for img in images:
        img["display_url"] = _fix_img_url(img, use_local)

    total_pages = max(1, (total + IMG_PER_PAGE - 1) // IMG_PER_PAGE)

    return templates.TemplateResponse("editor/partials/player_photo_search.html", {
        "request":         request,
        "images":          images,
        "sets":            sets,
        "player_set_ids":  player_set_ids,
        "total":           total,
        "page":            page,
        "total_pages":     total_pages,
        "filter_set_id":   filter_set_id,
        "q":               q,
        "no_filter":       False,
        "player_id":       player_id,
        "has_existing":    bool(player.get("photo_url")),
    })
