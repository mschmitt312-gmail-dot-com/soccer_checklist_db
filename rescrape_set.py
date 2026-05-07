"""
rescrape_set.py
---------------
Re-fetches a set's source URL and loads card data that the original
scraper missed (e.g. posts using unnumbered span lists instead of
numbered checklists).

Tries two extraction strategies in order:
  Strategy 1 — Numbered checklist  e.g. "1. J. Smith (Arsenal)"
  Strategy 2 — Unnumbered Name (Club) spans  e.g. "J. Smith (Arsenal)"

Usage:
    python rescrape_set.py --set-id 1451            # dry-run (default)
    python rescrape_set.py --set-id 1451 --apply    # write to DB
    python rescrape_set.py --set-id 1451 --strategy numbered
    python rescrape_set.py --set-id 1451 --strategy unnumbered
"""

import argparse
import re
import sys
import mysql.connector
import requests
from bs4 import BeautifulSoup

# ── DB config ─────────────────────────────────────────────────────────────────

DB_CONFIG = dict(
    host="127.0.0.1",
    port=3306,
    user="sc_loader",
    password="Gator888",
    database="soccer_checklist_db",
    charset="utf8mb4",
)

# ── Text helpers ──────────────────────────────────────────────────────────────

def clean(text):
    if not text:
        return ""
    return text.replace('\xa0', ' ').replace('\u200b', '').strip()


# Editorial phrases — lines containing these are not card entries
SKIP_PHRASES = [
    'update', 'very rare', 'if anyone', 'date amended', 'has provided',
    'probably just', 'looking for cards', "i'm not sure", 'i do have',
    'this set', 'see the comment', 'note:', 'see below', 'copyright',
    'all rights', 'scraped', 'checklist', 'click here', 'available from',
    'thanks to', 'courtesy of', 'according to', 'believed to be',
]

EDITORIAL_SUFFIX = re.compile(
    r'\s*[-–]+\s*(updated|amended|team amended|away kit|home kit|'
    r'not confirmed|added|confirmed|see comment|\d{2}[-/]\d{2}[-/]\d{2,4})'
    r'.*$',
    re.IGNORECASE,
)


def is_editorial(text):
    t = text.strip().lower()
    if len(t) > 150:
        return True
    if 'http' in t or 'www.' in t:
        return True
    for phrase in SKIP_PHRASES:
        if phrase in t:
            return True
    return False


def strip_editorial_suffix(text):
    text = EDITORIAL_SUFFIX.sub('', text).strip()
    text = re.sub(r'\s*[-–]+\s*$', '', text).strip()
    return text


# ── Card text parsing ─────────────────────────────────────────────────────────

def parse_card_text(raw_text):
    """
    Split "J. Ashcroft (Woolwich Arsenal)" into (name_in_set, club_raw).
    Returns (raw_text, None) if no club parenthesis found.
    """
    raw_text = strip_editorial_suffix(raw_text)
    m = re.match(r'^(.+?)\s*\(([^)]+)\)\s*$', raw_text)
    if m:
        name_in_set = m.group(1).strip()
        club_raw = m.group(2).strip()
        # Strip editorial notes that sneak inside the parens
        club_raw = re.sub(
            r'\s*[-–]\s*(away kit|home kit|reserve|not confirmed).*$',
            '', club_raw, flags=re.IGNORECASE
        ).strip()
        return name_in_set, club_raw or None
    return raw_text, None


def parse_player_name(name_in_set):
    """
    Split a display name into (first_name, last_name).
    "J. Ashcroft" -> ("J.", "Ashcroft")
    "Bache"       -> (None, "Bache")
    "Tom Finney"  -> ("Tom", "Finney")
    """
    parts = name_in_set.strip().split()
    if not parts:
        return None, name_in_set
    if len(parts) == 1:
        return None, parts[0]
    first = parts[0].rstrip('.')
    if len(first) == 1 and first.isalpha():
        return parts[0], ' '.join(parts[1:])
    return parts[0], ' '.join(parts[1:])


# ── Extraction strategies ─────────────────────────────────────────────────────

def strategy_numbered(block):
    """
    Original scraper logic: find spans matching '123. Player Name'.
    Returns list of dicts with keys: card_number, raw_text, confirmed.
    """
    pattern = re.compile(r'^(\d+)\.\s*(.*)')
    not_confirmed_pat = re.compile(r'\s*-+\s*not confirmed\s*$', re.IGNORECASE)

    cards = []
    seen = set()

    for span in block.find_all('span'):
        txt = clean(span.get_text())
        m = pattern.match(txt)
        if not m:
            continue

        card_num  = int(m.group(1))
        card_text = clean(m.group(2))
        confirmed = True

        if not_confirmed_pat.search(card_text):
            confirmed = False
            card_text = not_confirmed_pat.sub('', card_text).strip()

        card_text = re.sub(r'\s*-+\s*$', '', card_text).strip()
        key = f"{card_num}_{card_text}"
        if key not in seen:
            cards.append({"card_number": card_num, "raw_text": card_text, "confirmed": confirmed})
            seen.add(key)

    return sorted(cards, key=lambda x: x["card_number"])


# Pattern: one or more capitalised name tokens, optionally followed by (Club)
_CARD_LINE_PAT = re.compile(
    r"^[A-Z][A-Za-z'.\-]{0,29}"           # first token, starts uppercase
    r"(\s+[A-Za-z][A-Za-z'.\-]{0,29})*"   # additional name tokens
    r"(\s*\([^)]{2,60}\))?$"              # optional (Club Name)
)


def _span_is_colored(span):
    """
    Return True if a span has an explicit color style (red = contributor names,
    blue = editorial update notices). These are never card entries.
    """
    style = span.get("style", "")
    if "color:" in style.lower():
        # Allow black / default — only skip explicitly coloured spans
        if re.search(r'color\s*:\s*(blue|red|green|purple|orange)', style, re.IGNORECASE):
            return True
    return False


def _direct_text(span):
    """
    Return only the direct text nodes of a span, ignoring any child elements.
    This handles cases like:
        <span>McIvor (Blackburn Rovers)<span style="color:blue;">- updated</span></span>
    where the card text is the direct text and the editorial note is a child span.
    """
    from bs4 import NavigableString
    parts = []
    for node in span.children:
        if isinstance(node, NavigableString):
            parts.append(str(node))
    return clean("".join(parts))


def strategy_unnumbered(block):
    """
    Handle posts using plain unnumbered span lines like 'Name (Club)'.
    Works at the individual span level so coloured editorial spans
    (contributor names in red, update notices in blue) are skipped.
    For spans that contain coloured child spans, only the direct text
    content is used (the child editorial note is ignored).
    """
    cards = []
    seen  = set()

    for span in block.find_all('span'):
        # Skip coloured spans — editorial highlights (red=contributor, blue=note)
        if _span_is_colored(span):
            continue
        # Skip bold spans (header metadata fields)
        if span.find('b') or (span.parent and span.parent.name == 'b'):
            continue

        # If this span has child spans, use only its direct text nodes
        # (the child spans are editorial suffixes like "- updated 11-12-2019")
        if span.find('span'):
            txt = _direct_text(span)
        else:
            txt = clean(span.get_text())

        if not txt or len(txt) < 2:
            continue
        if is_editorial(txt):
            continue
        txt_stripped = strip_editorial_suffix(txt)
        if not txt_stripped:
            continue
        if not _CARD_LINE_PAT.match(txt_stripped):
            continue
        if txt_stripped not in seen:
            cards.append({"card_number": None, "raw_text": txt_stripped, "confirmed": True})
            seen.add(txt_stripped)

    return cards


# ── Player lookup / creation ──────────────────────────────────────────────────

def find_or_create_player(cur, cur2, conn, first_name, last_name, name_in_set, dry_run):
    """
    Find an existing canonical player (first_name, last_name) or create one.
    Returns player_id (or a fake negative id in dry-run mode).
    """
    # Try exact match first
    if first_name:
        cur.execute("""
            SELECT player_id FROM players
            WHERE first_name = %s AND last_name = %s
              AND is_non_player = 0 AND canonical_player_id IS NULL
            ORDER BY player_id ASC LIMIT 1
        """, (first_name, last_name))
    else:
        cur.execute("""
            SELECT player_id FROM players
            WHERE (first_name IS NULL OR first_name = '') AND last_name = %s
              AND is_non_player = 0 AND canonical_player_id IS NULL
            ORDER BY player_id ASC LIMIT 1
        """, (last_name,))

    row = cur.fetchone()
    if row:
        return row["player_id"], False  # (id, was_created)

    if dry_run:
        return None, True  # would create

    # Create new player
    cur2.execute("""
        INSERT INTO players (name_raw, first_name, last_name, is_non_player)
        VALUES (%s, %s, %s, 0)
    """, (name_in_set, first_name or None, last_name))
    conn.commit()
    return cur2.lastrowid, True


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Re-scrape a set and load missing cards.")
    parser.add_argument("--set-id",   type=int, required=True, help="set_id to rescrape")
    parser.add_argument("--apply",    action="store_true",     help="Write to DB (default: dry-run)")
    parser.add_argument("--strategy", choices=["auto", "numbered", "unnumbered"],
                        default="auto", help="Extraction strategy (default: auto)")
    args = parser.parse_args()

    dry_run = not args.apply

    # ── Connect ────────────────────────────────────────────────────────────────
    conn  = mysql.connector.connect(**DB_CONFIG)
    cur   = conn.cursor(dictionary=True)
    cur2  = conn.cursor()

    # ── Look up the set ────────────────────────────────────────────────────────
    cur.execute("""
        SELECT set_id, og_title, set_name, publisher, year_start,
               total_cards, cards_found, source_url
        FROM sets WHERE set_id = %s
    """, (args.set_id,))
    the_set = cur.fetchone()
    if not the_set:
        print(f"ERROR: set_id {args.set_id} not found.")
        sys.exit(1)

    print("=" * 70)
    print(f"Set      : {the_set['set_id']}  {the_set['publisher'] or '?'}")
    print(f"Set Name : {the_set['set_name'] or the_set['og_title']}")
    print(f"Year     : {the_set['year_start']}")
    print(f"URL      : {the_set['source_url']}")
    print(f"DB says  : total_cards={the_set['total_cards']}  cards_found={the_set['cards_found']}")

    # ── Check existing cards ───────────────────────────────────────────────────
    cur.execute("SELECT COUNT(*) AS cnt FROM set_cards WHERE set_id = %s", (args.set_id,))
    existing_count = cur.fetchone()["cnt"]
    print(f"Cards in DB now: {existing_count}")

    if existing_count > 0 and not args.apply:
        print("\nThis set already has cards in the DB.")
        print("If you want to ADD more, run with --apply and the script will skip duplicates.")

    # ── Fetch the page ─────────────────────────────────────────────────────────
    print(f"\nFetching {the_set['source_url']} ...")
    try:
        resp = requests.get(the_set['source_url'], timeout=30,
                            headers={"User-Agent": "Mozilla/5.0 (compatible; SoccerChecklist/1.0)"})
        resp.raise_for_status()
    except Exception as e:
        print(f"ERROR fetching page: {e}")
        sys.exit(1)

    soup  = BeautifulSoup(resp.text, 'html.parser')
    block = soup.find('div', class_='post-body entry-content')
    if not block:
        block = soup.find('div', class_='post-body')
    if not block:
        block = soup.find('article')
    if not block:
        print("ERROR: Could not find post body in the fetched page.")
        sys.exit(1)

    # ── Extract cards using chosen strategy ────────────────────────────────────
    raw_cards = []

    if args.strategy in ("auto", "numbered"):
        raw_cards = strategy_numbered(block)
        if raw_cards:
            print(f"\nStrategy: NUMBERED — found {len(raw_cards)} entries")
        elif args.strategy == "numbered":
            print("\nStrategy: NUMBERED — found 0 entries.")

    if not raw_cards and args.strategy in ("auto", "unnumbered"):
        raw_cards = strategy_unnumbered(block)
        if raw_cards:
            print(f"\nStrategy: UNNUMBERED — found {len(raw_cards)} entries")
        else:
            print("\nStrategy: UNNUMBERED — found 0 entries.")

    if not raw_cards:
        print("\nNo card entries found. The page layout may need a custom parser.")
        print("Tip: open the source URL in a browser and check how the names are formatted.")
        sys.exit(0)

    # ── Parse and preview ──────────────────────────────────────────────────────
    print(f"\n{'─'*70}")
    print(f"{'#':>4}  {'Name in Set':<30}  {'Club':<25}  {'Player action'}")
    print(f"{'─'*70}")

    parsed = []
    for i, entry in enumerate(raw_cards, start=1):
        name_in_set, club_raw = parse_card_text(entry["raw_text"])
        first_name, last_name = parse_player_name(name_in_set)

        # Quick lookup for preview (don't create yet)
        if first_name:
            cur.execute("""
                SELECT player_id FROM players
                WHERE first_name = %s AND last_name = %s
                  AND is_non_player = 0 AND canonical_player_id IS NULL
                LIMIT 1
            """, (first_name, last_name))
        else:
            cur.execute("""
                SELECT player_id FROM players
                WHERE (first_name IS NULL OR first_name = '') AND last_name = %s
                  AND is_non_player = 0 AND canonical_player_id IS NULL
                LIMIT 1
            """, (last_name,))
        player_row = cur.fetchone()
        player_action = f"match id={player_row['player_id']}" if player_row else "CREATE new"

        card_num_display = entry["card_number"] if entry["card_number"] is not None else "-"
        conf_flag = "" if entry["confirmed"] else " [?]"
        print(f"{card_num_display:>4}  {name_in_set:<30}  {(club_raw or ''):.<25}  {player_action}{conf_flag}")

        parsed.append({
            "card_number":  entry["card_number"],
            "name_in_set":  name_in_set,
            "club_raw":     club_raw,
            "first_name":   first_name,
            "last_name":    last_name,
            "confirmed":    entry["confirmed"],
            "player_id":    player_row["player_id"] if player_row else None,
        })

    print(f"{'─'*70}")
    creates = sum(1 for p in parsed if p["player_id"] is None)
    matches = len(parsed) - creates
    print(f"Total: {len(parsed)} cards  |  {matches} player matches  |  {creates} new players")

    if dry_run:
        print(f"\n[DRY RUN] No changes written. Run with --apply to load into DB.")
        cur.close(); cur2.close(); conn.close()
        return

    # ── Get already-existing name_in_set values for this set ──────────────────
    cur.execute("""
        SELECT LOWER(name_in_set) AS n FROM set_cards WHERE set_id = %s
    """, (args.set_id,))
    existing_names = {row["n"] for row in cur.fetchall() if row["n"]}

    # ── Insert ────────────────────────────────────────────────────────────────
    inserted = 0
    skipped  = 0
    players_created = 0

    for p in parsed:
        # Skip if we already have a card with this name for this set
        if p["name_in_set"] and p["name_in_set"].lower() in existing_names:
            skipped += 1
            continue

        # Find or create player
        player_id = p["player_id"]
        if player_id is None:
            player_id, was_created = find_or_create_player(
                cur, cur2, conn,
                p["first_name"], p["last_name"], p["name_in_set"],
                dry_run=False,
            )
            if was_created:
                players_created += 1

        cur2.execute("""
            INSERT INTO set_cards (set_id, player_id, card_number, name_in_set, club_raw, confirmed)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            args.set_id,
            player_id,
            p["card_number"],
            p["name_in_set"],
            p["club_raw"],
            1 if p["confirmed"] else 0,
        ))
        existing_names.add(p["name_in_set"].lower())
        inserted += 1

    # Update cards_found on the set
    cur2.execute("""
        UPDATE sets
        SET cards_found = (SELECT COUNT(*) FROM set_cards WHERE set_id = %s)
        WHERE set_id = %s
    """, (args.set_id, args.set_id))

    conn.commit()

    print(f"\n✓ Done.")
    print(f"  Inserted  : {inserted} cards")
    print(f"  Skipped   : {skipped} (already in DB)")
    print(f"  Players created: {players_created}")

    cur.close(); cur2.close(); conn.close()


if __name__ == "__main__":
    main()
