#!/usr/bin/env python3
"""
load_to_mysql.py  --  Load scraped soccer_checklists JSON exports into MySQL.

Pass              What it does
-----------       ----------------------------------------------------------
sets              Upserts each set into the sets table (keyed on source_url)
cards             Upserts player rows, then inserts set_cards
images            Inserts image filename rows linked to the set
notes             Inserts the scraped description as a note row

Re-run safety:
    Sets are upserted via ON DUPLICATE KEY UPDATE on source_url.
    Cards/images/notes for a set are skipped if already present
    unless --force-reload is passed.

Inline data fixes (applied at load time, not modifying JSON on disk):
    year_start/year_end  - parsed from season_raw
    country              - junk values (card counts, publisher names, etc.)
                           are discarded; stored in country_raw unchanged
    player_name blobs    - entries longer than MAX_NAME_LEN are skipped

Usage:
    python load_to_mysql.py                      load all sets
    python load_to_mysql.py --dry-run            parse everything, no DB writes
    python load_to_mysql.py --limit 20           stop after first 20 sets
    python load_to_mysql.py --force-reload       overwrite cards/images/notes
    python load_to_mysql.py --set-filter "A&BC"  only folders matching string
    python load_to_mysql.py --stats-only         show DB counts and quit
    python load_to_mysql.py --password secret    override DB password

Connection overrides (--host, --port, --user, --password, --database) let you
point at different MySQL instances without editing this file.
"""

import os
import re
import json
import sys
import time
import argparse
from datetime import datetime


# ------------------------------------------------------------------
# TEE LOGGER  -- mirrors all print() output to a timestamped log file
# ------------------------------------------------------------------

class _Tee:
    """
    Replaces sys.stdout so that every print() call goes to both the
    console and an open log file at the same time.
    """
    def __init__(self, log_path):
        self._console = sys.stdout
        self._file    = open(log_path, "w", encoding="utf-8")

    def write(self, message):
        self._console.write(message)
        self._file.write(message)
        self._file.flush()

    def flush(self):
        self._console.flush()
        self._file.flush()

    def close(self):
        sys.stdout = self._console   # restore original stdout
        self._file.close()

# ------------------------------------------------------------------
# CONFIGURATION -- edit DB_CONFIG before first run
# ------------------------------------------------------------------

DB_CONFIG = {
    "host":     "127.0.0.1",
    "port":     3306,
    "user":     "sc_loader",
    "password": "Gator888",               # set your password here or pass --password
    "database": "soccer_checklist_db",
    "charset":  "utf8mb4",
}

# Path to the scraped data folder (relative to this script's directory)
BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "soccer_checklists")

# Log files are written here (created automatically if it doesn't exist)
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

# Player name entries longer than this are blob artefacts -- skip them
MAX_NAME_LEN = 200


# ------------------------------------------------------------------
# YEAR PARSING HELPERS
# ------------------------------------------------------------------

# Patterns applied in order -- first match wins
_MULTI_RANGE = re.compile(
    r'(1[89]\d{2}|20[012]\d)[/\-]\d{2,4}.*?\b(1[89]\d{2}|20[012]\d)\b'
)
_SEASON_RANGE = re.compile(
    r'(1[89]\d{2}|20[012]\d)[/\-](1[89]\d{2}|20[012]\d|\d{2})'
)
_SINGLE_YEAR = re.compile(r'\b(1[89]\d{2}|20[012]\d)\b')
_DECADE      = re.compile(r"(1[89]\d0)'?s?", re.IGNORECASE)


def _parse_season_pair(y1_str, y2_str):
    """
    Convert a matched season pair like ('1934', '35') or ('1934', '1935')
    into (year_start, year_end) integers.
    """
    y1 = int(y1_str)
    if len(y2_str) == 2:
        century = (y1 // 100) * 100
        y2 = century + int(y2_str)
        if y2 < y1:   # e.g. 1999-00 -> 2000
            y2 += 100
    else:
        y2 = int(y2_str)
    return y1, y2


def parse_year_range(season_raw):
    """
    Return (year_start, year_end) from a raw season string.
    Examples:
        "1934-35"           -> (1934, 1935)
        "1934-1935"         -> (1934, 1935)
        "1999-00"           -> (1999, 2000)
        "1906-07 - 1914-15" -> (1906, 1915)
        "1925-28"           -> (1925, 1928)
        "1953"              -> (1953, None)
        "1950's"            -> (1950, None)
        "("                 -> (None, None)
    """
    if not season_raw:
        return None, None
    s = str(season_raw).strip()

    # Multi-range: "1906-07 - 1914-15"
    # Find all season pairs; take start from first, end from last.
    all_pairs = _SEASON_RANGE.findall(s)
    if len(all_pairs) >= 2:
        y_start, _ = _parse_season_pair(*all_pairs[0])
        _, y_end   = _parse_season_pair(*all_pairs[-1])
        return y_start, y_end

    # Standard season: "1934-35" or "1934-1935"
    if len(all_pairs) == 1:
        return _parse_season_pair(*all_pairs[0])

    # Single year
    m = _SINGLE_YEAR.search(s)
    if m:
        return int(m.group(1)), None

    # Decade: "1950's"
    m = _DECADE.search(s)
    if m:
        return int(m.group(1)), None

    return None, None


# ------------------------------------------------------------------
# COUNTRY CLEANING HELPER
# ------------------------------------------------------------------

# Values that look like card counts, publisher names, or other junk
_JUNK_COUNTRY = re.compile(
    r'^\d'                      # starts with a digit  e.g. "92 cards", "12"
    r'|booklet'
    r'|wrapper'
    r'|album'
    r'|photos?'
    r'|series\b'
    r'|programme'
    r'|cards?\b'
    r'|stickers?\b'
    r'|cut.outs?'
    r'|\bbook\b'
    r'|unknown'
    r'|amalgamated'
    r'|barratt'
    r'|\bpress\b'
    r'|gazette'
    r'|postcards?'
    r'|cigarette'
    r'|instantaneo'
    r'|match action'
    r'|not issued'
    r'|\?{2,}'                  # multiple question marks
    ,
    re.IGNORECASE
)


def clean_country(raw):
    """
    Return a plausible country string, or None if the value looks like junk.
    The raw value is always preserved in country_raw regardless.
    """
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if len(s) > 100:
        return None
    if _JUNK_COUNTRY.search(s):
        return None
    return s


# ------------------------------------------------------------------
# JSON LOADER  (handles the occasional multi-object corruption)
# ------------------------------------------------------------------

def load_json_safe(path):
    """
    Load a JSON file. Handles files that accidentally contain multiple
    concatenated JSON objects (takes the first one).
    Returns (data_dict, warning_or_None).
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            content = f.read()
        return json.loads(content), None
    except json.JSONDecodeError:
        try:
            dec = json.JSONDecoder()
            obj, _ = dec.raw_decode(content.strip())
            return obj, "multi-object JSON -- only first object used"
        except Exception as e:
            return None, str(e)


# ------------------------------------------------------------------
# DATABASE CONNECTION  (tries pymysql first, then mysql-connector)
# ------------------------------------------------------------------

def get_connection(cfg):
    """
    Connect to MySQL.  Tries pymysql first (pure-Python, easier to install),
    then mysql.connector (official Oracle driver).

    Install with one of:
        pip install pymysql
        pip install mysql-connector-python
    """
    try:
        import pymysql
        conn = pymysql.connect(
            host=cfg["host"],
            port=cfg["port"],
            user=cfg["user"],
            password=cfg["password"],
            database=cfg["database"],
            charset=cfg["charset"],
            autocommit=False,
        )
        return conn, "pymysql"
    except ImportError:
        pass

    try:
        import mysql.connector
        conn = mysql.connector.connect(
            host=cfg["host"],
            port=cfg["port"],
            user=cfg["user"],
            password=cfg["password"],
            database=cfg["database"],
            charset=cfg["charset"],
            autocommit=False,
        )
        return conn, "mysql.connector"
    except ImportError:
        pass

    raise ImportError(
        "No MySQL driver found. Install one with:\n"
        "    pip install pymysql\n"
        "or: pip install mysql-connector-python"
    )


def print_db_counts(cur):
    """Print current row counts for all live tables."""
    for table in ["sets", "players", "set_cards", "images", "notes"]:
        cur.execute(f"SELECT COUNT(*) FROM `{table}`")
        print(f"  {table:15s}: {cur.fetchone()[0]:,}")


# ------------------------------------------------------------------
# SET UPSERT
# ------------------------------------------------------------------

def upsert_set(cur, data, dry_run):
    """
    INSERT or UPDATE a row in the sets table.
    Returns (set_id, action_str).
    action_str is one of: "INSERT", "UPDATE", "DRY"
    """
    season_raw = (data.get("season_raw") or "").strip()
    year_start, year_end = parse_year_range(season_raw)

    country_raw = str(data.get("country") or "").strip()
    country     = clean_country(country_raw)

    row = {
        "og_title":    (data.get("og_title") or "")[:500],
        "set_name":    ((data.get("set_name") or "")[:500]) or None,
        "publisher":   ((data.get("publisher") or "")[:255]) or None,
        "country_raw": country_raw[:500] or None,
        "country":     country,
        "season_raw":  season_raw[:100] or None,
        "year_start":  year_start,
        "year_end":    year_end,
        "total_cards": data.get("total_cards"),
        "cards_found": data.get("cards_found") or 0,
        "source_url":  ((data.get("source_url") or "")[:1000]) or None,
    }

    if dry_run:
        return None, "DRY"

    cur.execute(
        """
        INSERT INTO sets
            (og_title, set_name, publisher,
             country_raw, country,
             season_raw, year_start, year_end,
             total_cards, cards_found, source_url)
        VALUES
            (%(og_title)s, %(set_name)s, %(publisher)s,
             %(country_raw)s, %(country)s,
             %(season_raw)s, %(year_start)s, %(year_end)s,
             %(total_cards)s, %(cards_found)s, %(source_url)s)
        ON DUPLICATE KEY UPDATE
            og_title    = VALUES(og_title),
            set_name    = VALUES(set_name),
            publisher   = VALUES(publisher),
            country_raw = VALUES(country_raw),
            country     = VALUES(country),
            season_raw  = VALUES(season_raw),
            year_start  = VALUES(year_start),
            year_end    = VALUES(year_end),
            total_cards = VALUES(total_cards),
            cards_found = VALUES(cards_found)
        """,
        row
    )

    # ON DUPLICATE KEY UPDATE with lastrowid:
    #   - pure INSERT  -> lastrowid = new set_id
    #   - UPDATE fired -> lastrowid = 0 (pymysql) or existing id (connector)
    # So we always fetch by source_url to be safe.
    if row["source_url"]:
        cur.execute(
            "SELECT set_id FROM sets WHERE source_url = %s",
            (row["source_url"],)
        )
        result = cur.fetchone()
        set_id = result[0] if result else None
    else:
        set_id = cur.lastrowid or None

    # Distinguish insert from update for reporting
    action = "INSERT" if cur.lastrowid else "UPDATE"
    return set_id, action


# ------------------------------------------------------------------
# PLAYER UPSERT  (with in-memory cache to avoid repeated lookups)
# ------------------------------------------------------------------

_player_cache = {}   # lowercased name_raw -> player_id


def upsert_player(cur, name_raw, dry_run):
    """
    Find or create a player row keyed on name_raw.
    Returns player_id (int), or None in dry-run mode.

    Uses the MySQL trick:
        ON DUPLICATE KEY UPDATE player_id = LAST_INSERT_ID(player_id)
    which makes LAST_INSERT_ID() return the existing row's PK even when
    the insert is skipped due to the unique constraint.
    """
    key = name_raw.strip().lower()
    if key in _player_cache:
        return _player_cache[key]

    if dry_run:
        return None

    cur.execute(
        """
        INSERT INTO players (name_raw)
        VALUES (%s)
        ON DUPLICATE KEY UPDATE player_id = LAST_INSERT_ID(player_id)
        """,
        (name_raw[:500],)
    )

    pid = cur.lastrowid
    if not pid:
        # Fallback: shouldn't be needed with LAST_INSERT_ID trick, but be safe
        cur.execute(
            "SELECT player_id FROM players WHERE name_raw = %s",
            (name_raw[:500],)
        )
        row = cur.fetchone()
        pid = row[0] if row else None

    _player_cache[key] = pid
    return pid


# ------------------------------------------------------------------
# CARDS LOADER
# ------------------------------------------------------------------

def load_cards(cur, set_id, checklist, dry_run, force_reload):
    """
    Load checklist entries:  upsert players, then insert set_cards.

    Returns:
        (inserted, skipped)   -- normal case
        (-1, existing_count)  -- cards already present and force_reload=False
    """
    if not checklist:
        return 0, 0

    if not dry_run:
        cur.execute(
            "SELECT COUNT(*) FROM set_cards WHERE set_id = %s", (set_id,)
        )
        existing = cur.fetchone()[0]
        if existing > 0:
            if not force_reload:
                return -1, existing    # signal: already loaded, skip
            # Clear old cards for this set before reloading
            cur.execute("DELETE FROM set_cards WHERE set_id = %s", (set_id,))

    inserted = 0
    skipped  = 0

    for card in checklist:
        name_raw = (card.get("player_name") or "").strip()

        if not name_raw:
            skipped += 1
            continue
        if len(name_raw) > MAX_NAME_LEN:
            skipped += 1
            continue

        player_id = upsert_player(cur, name_raw, dry_run)
        if player_id is None and not dry_run:
            skipped += 1
            continue

        if not dry_run:
            cur.execute(
                """
                INSERT INTO set_cards
                    (set_id, player_id, card_number, name_in_set, confirmed)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    set_id,
                    player_id,
                    card.get("card_number"),          # NULL if not numbered
                    name_raw[:500],
                    1 if card.get("confirmed", True) else 0,
                )
            )
        inserted += 1

    return inserted, skipped


# ------------------------------------------------------------------
# IMAGES LOADER
# ------------------------------------------------------------------

def load_images(cur, set_id, images, dry_run, force_reload):
    """
    Load image filename records linked to a set.
    Returns count of rows inserted.
    """
    if not images:
        return 0

    if not dry_run:
        cur.execute(
            "SELECT COUNT(*) FROM images WHERE set_id = %s AND card_id IS NULL",
            (set_id,)
        )
        existing = cur.fetchone()[0]
        if existing > 0:
            if not force_reload:
                return 0
            cur.execute(
                "DELETE FROM images WHERE set_id = %s AND card_id IS NULL",
                (set_id,)
            )

    inserted = 0
    for sort_order, filename in enumerate(images):
        fn = str(filename).strip()[:500]
        if not fn:
            continue
        if not dry_run:
            cur.execute(
                """
                INSERT INTO images (set_id, filename, sort_order)
                VALUES (%s, %s, %s)
                """,
                (set_id, fn, sort_order)
            )
        inserted += 1

    return inserted


# ------------------------------------------------------------------
# NOTES LOADER  (description -> scraped note)
# ------------------------------------------------------------------

def load_description_note(cur, set_id, description, dry_run, force_reload):
    """
    Store the set's scraped description text as a note row.
    Returns 1 if a note was inserted, 0 otherwise.
    """
    text = (description or "").strip()
    if not text:
        return 0

    if not dry_run:
        cur.execute(
            "SELECT COUNT(*) FROM notes WHERE set_id = %s AND note_source = 'scraped'",
            (set_id,)
        )
        existing = cur.fetchone()[0]
        if existing > 0:
            if not force_reload:
                return 0
            cur.execute(
                "DELETE FROM notes WHERE set_id = %s AND note_source = 'scraped'",
                (set_id,)
            )
        cur.execute(
            """
            INSERT INTO notes (set_id, note_text, note_source)
            VALUES (%s, %s, 'scraped')
            """,
            (set_id, text)
        )

    return 1


# ------------------------------------------------------------------
# CLI ARGUMENT PARSER
# ------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="Load soccer_checklists JSON exports into MySQL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Parse all data and print what would happen; make no DB writes."
    )
    p.add_argument(
        "--limit", type=int, default=0, metavar="N",
        help="Stop after loading N sets (default: no limit)."
    )
    p.add_argument(
        "--force-reload", action="store_true",
        help="Re-load cards/images/notes even if already present for a set."
    )
    p.add_argument(
        "--set-filter", default="", metavar="TEXT",
        help="Only process folders whose name contains TEXT (case-insensitive)."
    )
    p.add_argument(
        "--stats-only", action="store_true",
        help="Print current DB row counts and exit."
    )
    # DB connection overrides
    p.add_argument("--host",     default=None)
    p.add_argument("--port",     type=int, default=None)
    p.add_argument("--user",     default=None)
    p.add_argument("--password", default=None)
    p.add_argument("--database", default=None)
    return p.parse_args()


# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------

def main():
    args = parse_args()

    # ---- Set up log file (tees all output to console + file) ----
    os.makedirs(LOG_DIR, exist_ok=True)
    timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix     = "_dryrun" if args.dry_run else ""
    log_path   = os.path.join(LOG_DIR, f"load_{timestamp}{suffix}.log")
    tee        = _Tee(log_path)
    sys.stdout = tee
    print(f"Log file: {log_path}\n")

    try:
        _main(args, log_path)
    finally:
        tee.close()


def _main(args, log_path):
    # Build effective DB config (CLI args override the config dict)
    cfg = dict(DB_CONFIG)
    if args.host     is not None: cfg["host"]     = args.host
    if args.port     is not None: cfg["port"]     = args.port
    if args.user     is not None: cfg["user"]     = args.user
    if args.password is not None: cfg["password"] = args.password
    if args.database is not None: cfg["database"] = args.database

    print("load_to_mysql.py")
    print(f"  DB:           {cfg['user']}@{cfg['host']}:{cfg['port']}/{cfg['database']}")
    print(f"  Source dir:   {BASE_DIR}")
    print(f"  Dry run:      {args.dry_run}")
    print(f"  Force reload: {args.force_reload}")
    if args.limit:
        print(f"  Limit:        {args.limit} sets")
    if args.set_filter:
        print(f"  Set filter:   '{args.set_filter}'")
    print()

    # Validate source dir
    if not os.path.isdir(BASE_DIR):
        print(f"ERROR: Source directory not found: {BASE_DIR}")
        sys.exit(1)

    # Connect
    try:
        conn, driver = get_connection(cfg)
    except ImportError as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Cannot connect to MySQL: {e}")
        sys.exit(1)

    print(f"  Connected via {driver}\n")
    cur = conn.cursor()

    # --stats-only mode
    if args.stats_only:
        print("=== Current DB row counts ===")
        print_db_counts(cur)
        cur.close()
        conn.close()
        return

    # Build folder list
    folders = sorted(
        f for f in os.listdir(BASE_DIR)
        if os.path.isdir(os.path.join(BASE_DIR, f))
    )

    # Apply set-filter
    if args.set_filter:
        folders = [f for f in folders if args.set_filter.lower() in f.lower()]

    # Skip hollowed children (no export.json)
    folders = [
        f for f in folders
        if os.path.exists(os.path.join(BASE_DIR, f, "export.json"))
    ]

    total = len(folders)
    print(f"Sets to process: {total}")
    if args.limit:
        print(f"(will stop after first {args.limit})")
    print()

    # Counters
    c = {
        "sets_inserted":   0,
        "sets_updated":    0,
        "sets_error":      0,
        "cards_inserted":  0,
        "cards_skipped":   0,   # quality skips (no name / blob)
        "sets_cards_skip": 0,   # sets whose cards already existed
        "imgs_inserted":   0,
        "notes_inserted":  0,
        "parse_errors":    0,
    }
    warnings = []
    t0 = time.time()

    for idx, folder in enumerate(folders, 1):
        if args.limit and idx > args.limit:
            print(f"\n[LIMIT] Stopping after {args.limit} sets.")
            break

        jpath = os.path.join(BASE_DIR, folder, "export.json")

        # ---- Parse JSON ----
        data, warn = load_json_safe(jpath)
        if data is None:
            print(f"[{idx:4d}/{total}] PARSE_ERR  {folder[:65]}")
            c["parse_errors"] += 1
            warnings.append(f"Parse error ({warn}): {folder}")
            continue
        if warn:
            warnings.append(f"JSON repaired ({warn}): {folder}")

        try:
            # ---- SET ----
            set_id, action = upsert_set(cur, data, args.dry_run)
            if action == "INSERT":
                c["sets_inserted"] += 1
            elif action == "UPDATE":
                c["sets_updated"] += 1

            checklist   = data.get("checklist") or []
            images      = data.get("images") or []
            description = data.get("description") or ""

            # ---- CARDS ----
            cards_in, cards_sk = load_cards(
                cur, set_id, checklist, args.dry_run, args.force_reload
            )
            if cards_in == -1:
                # Already loaded, not force-reloading
                c["sets_cards_skip"] += 1
                cards_label = f"cards=SKIP({cards_sk} exist)"
            else:
                c["cards_inserted"] += cards_in
                c["cards_skipped"]  += cards_sk
                cards_label = f"cards=+{cards_in} skip={cards_sk}"

            # ---- IMAGES ----
            imgs_in = load_images(
                cur, set_id, images, args.dry_run, args.force_reload
            )
            c["imgs_inserted"] += imgs_in

            # ---- NOTE ----
            note_in = load_description_note(
                cur, set_id, description, args.dry_run, args.force_reload
            )
            c["notes_inserted"] += note_in

            if not args.dry_run:
                conn.commit()

            # One-line status per set
            print(
                f"[{idx:4d}/{total}] {action:6s}  "
                f"{cards_label:28s}  "
                f"imgs=+{imgs_in:2d}  note={note_in}  "
                f"{folder[:55]}"
            )

        except Exception as e:
            conn.rollback()
            c["sets_error"] += 1
            msg = str(e)
            warnings.append(f"DB error on '{folder}': {msg}")
            print(f"[{idx:4d}/{total}] DB_ERR  {msg[:70]}  -- {folder[:40]}")

    # ---- SUMMARY ----
    elapsed = time.time() - t0
    print()
    print("=" * 70)
    print(f"Load complete{' [DRY RUN]' if args.dry_run else ''}  ({elapsed:.1f}s)")
    print(f"  Sets inserted:              {c['sets_inserted']}")
    print(f"  Sets updated:               {c['sets_updated']}")
    print(f"  Sets with DB errors:        {c['sets_error']}")
    print(f"  Cards inserted:             {c['cards_inserted']}")
    print(f"  Cards skipped (data issue): {c['cards_skipped']}")
    print(f"  Sets with cards pre-loaded: {c['sets_cards_skip']}")
    print(f"  Images inserted:            {c['imgs_inserted']}")
    print(f"  Notes inserted:             {c['notes_inserted']}")
    print(f"  JSON parse errors:          {c['parse_errors']}")

    if warnings:
        print(f"\nWarnings ({len(warnings)}):")
        for w in warnings[:30]:
            print(f"  ! {w}")
        if len(warnings) > 30:
            print(f"  ... and {len(warnings) - 30} more")

    if not args.dry_run:
        print()
        print("=== Final DB row counts ===")
        print_db_counts(cur)

    cur.close()
    conn.close()
    print(f"\nLog saved to: {log_path}")


if __name__ == "__main__":
    main()
