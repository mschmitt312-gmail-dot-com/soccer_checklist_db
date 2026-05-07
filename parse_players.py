#!/usr/bin/env python3
"""
parse_players.py  —  Phase 1 name parsing
==========================================
Pass 1  (set_cards)  :  parse name_in_set  →  club_raw, country_id
Pass 2  (players)    :  parse name_raw     →  first_name, last_name, is_non_player

Run from PowerShell (not WSL) so mysql.connector can reach Windows MySQL.

Usage:
    python parse_players.py [--dry-run] [--limit N] [--pass {cards,players,both}]

Examples:
    python parse_players.py --dry-run --limit 200   # preview first 200 rows of each pass
    python parse_players.py --dry-run               # preview full run (no DB writes)
    python parse_players.py                         # run everything
    python parse_players.py --pass cards            # only update set_cards
    python parse_players.py --pass players          # only update players
"""

import re
import sys
import argparse
from dataclasses import dataclass
from typing import Optional

import mysql.connector

# ── DB config ─────────────────────────────────────────────────────────────────
DB_CONFIG = dict(
    host="127.0.0.1",
    port=3306,
    user="sc_loader",
    password="Gator888",
    database="soccer_checklist_db",
    charset="utf8mb4",
)

# ── Batch size for DB writes ──────────────────────────────────────────────────
BATCH = 500

# ── Sport-category keywords (case-insensitive) ────────────────────────────────
# Parenthetical text matching one of these = sport label, not a club name.
# Individual athletes (e.g. "Bob Fitzimmons (Boxning)") are still players;
# we use these keywords only when the BASE NAME also looks non-personal.
SPORT_KEYWORDS = {
    # English
    "athletics", "boxing", "tennis", "cricket", "rugby", "cycling",
    "swimming", "speedway", "wrestling", "gymnastics", "horse racing",
    "motorsport", "motor sport", "snooker", "golf", "hockey", "lacrosse",
    "baseball", "basketball", "american football", "ice hockey", "polo",
    "rowing", "sailing", "shooting", "fencing", "judo", "weightlifting",
    "water polo", "diving", "triathlon", "skiing", "bobsled",
    "football", "soccer", "rugby league", "rugby union",
    # Swedish
    "fotboll", "boxning", "löpning", "simning", "cykling",
    "friidrott", "brottning",
    # German
    "leichtathletik", "turnen", "schwimmen", "boxen", "radsport",
    "eishockey", "handball",
    # Dutch
    "voetbal", "zwemmen",
    # Italian
    "calcio", "ciclismo", "nuoto",
    # French
    "athlétisme", "natation", "cyclisme",
    # Portuguese
    "atletismo",
}

# ── Regex patterns ────────────────────────────────────────────────────────────

# Leading card number:  "(492)." or "02." or "492." or "50-51."
_RE_CARD_PREFIX = re.compile(r'^\s*(?:\(\d+\)\.?|\d+(?:-\d+)?\.)\s+')

# All parenthetical groups (non-greedy contents)
_RE_PAREN = re.compile(r'\(([^)]*)\)')

# Double-space dash separator: "  -  anything"  (2+ spaces each side)
_RE_DDASH = re.compile(r'\s{2,}-\s{2,}(.+)$')

# Team-name suffixes  (marks a string as a club/team, not a person)
_RE_TEAM_SUFFIX = re.compile(
    r'\b(?:'
    r'F\.?C\.?|A\.?F\.?C\.?|R\.?F\.?C\.?|R\.?F\.?L\.?|'
    r'United|City|Town|Wanderers|Rovers|'
    r'Athletic(?!s)|Albion|County|Rangers|Celtic|Villa|'
    r'Wednesday|Thursday|Saturday'
    r')\b',
    re.IGNORECASE
)

# Score pattern  e.g. "2-1", "3 - 2"
# Deliberately narrow: both sides must be 1-2 digits to avoid matching
# catalog numbers like "1951-12" or card-range prefixes like "50-51."
_RE_SCORE = re.compile(r'\b([1-9]\d?)\s*-\s*(\d{1,2})\b')

# "Last, First" comma split  — only short strings so we don't catch notes
_RE_LAST_FIRST = re.compile(r'^([^,]{2,50}),\s*(.{1,40})$')

# Initial(s) followed by a surname:  "J." / "J.S." / "J. S." + word(s)
_RE_INITIAL_LAST = re.compile(r'^((?:[A-ZÀÁÂÃÄÅÆÇÈÉÊËÌÍÎÏÐÑÒÓÔÕÖØÙÚÛÜÝÞ]\.\s*)+)(.+)$')

# Purely digits / punctuation (no real alphabetic content)
_RE_DIGITS_ONLY = re.compile(r'^[\d\s\-–—.:;,!?()/|]+$')


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class CardResult:
    club_raw:   Optional[str] = None
    country_id: Optional[int] = None

@dataclass
class PlayerResult:
    first_name:    Optional[str] = None
    last_name:     Optional[str] = None
    is_non_player: int           = 0


# ════════════════════════════════════════════════════════════════════════════════
# Country lookup
# ════════════════════════════════════════════════════════════════════════════════

def load_country_lookup(cur) -> dict:
    """
    Returns {normalised_string: country_id} for every country name + alias.
    Normalisation = strip + lower.
    """
    cur.execute("SELECT country_id, country_name, also_known_as FROM countries")
    lookup: dict = {}
    for cid, name, aka in cur.fetchall():
        lookup[name.strip().lower()] = cid
        if aka:
            for alias in aka.split(','):
                a = alias.strip().lower()
                if a:
                    lookup[a] = cid
    return lookup


# ════════════════════════════════════════════════════════════════════════════════
# Low-level helpers
# ════════════════════════════════════════════════════════════════════════════════

def _strip_card_prefix(s: str) -> str:
    """Remove leading card-number tokens like '(492). ' or '02. '."""
    return _RE_CARD_PREFIX.sub('', s).strip()


def _extract_parens(s: str):
    """
    Return (base_text, [paren_content_1, paren_content_2, ...]).
    Unclosed parens (malformed like 'Name (text without close') are salvaged:
    the content after the last '(' is treated as a pseudo-paren so it doesn't
    bleed into the base name (which could trigger false team-suffix detections).
    """
    contents = [p.strip() for p in _RE_PAREN.findall(s) if p.strip()]
    base = _RE_PAREN.sub('', s)
    if '(' in base:
        idx   = base.rfind('(')
        extra = base[idx + 1:].strip()
        base  = base[:idx].strip()
        if extra:
            contents.append(extra)   # treat as a regular paren
    return base, contents


def _classify_paren(text: str, country_lookup: dict):
    """
    Classify a parenthetical string.
    Returns ('sport'|'country'|'club'|'nickname'|'note', country_id_or_None)
    """
    low = text.lower()

    # Numeric only → leaked card number
    if re.match(r'^\d+$', text):
        return 'other', None

    # Annotations / notes
    if low.startswith(('sic', 'stamped', 'showing', 'error', 'verso')):
        return 'note', None

    # Sport keyword
    if low in SPORT_KEYWORDS:
        return 'sport', None

    # Exact country match
    if low in country_lookup:
        return 'country', country_lookup[low]

    # Slash-separated: "Club/Country" or "Club/Club"
    if '/' in text:
        parts = [p.strip() for p in text.split('/')]
        for p in parts:
            if p.lower() in country_lookup:
                return 'country', country_lookup[p.lower()]
        return 'club', None

    return 'club', None


# Separator for "Club and Country" / "Club & Country" patterns
_RE_AND_SEP = re.compile(r'\s+(?:and|&)\s+', re.IGNORECASE)


def _split_and_pattern(text: str, country_lookup: dict):
    """
    Split "Club and Country" (or "Club & Country") into parts.
    Returns (club_str_or_None, country_id_or_None).
    Only acts if at least one part matches a country — otherwise returns (None, None).

    Examples:
        "Arsenal and England"          → ("Arsenal", <england_id>)
        "Blackpool and England"        → ("Blackpool", <england_id>)
        "Wolves and Eire"              → ("Wolves", <ireland_id>)
        "Arsenal and Chelsea"          → (None, None)   # no country found
    """
    parts = [p.strip() for p in _RE_AND_SEP.split(text) if p.strip()]
    if len(parts) < 2:
        return None, None
    club_part  = None
    country_id = None
    for p in parts:
        if p.lower() in country_lookup:
            if country_id is None:
                country_id = country_lookup[p.lower()]
        elif club_part is None:
            club_part = p
    if country_id is None:
        return None, None   # no country found → don't split
    return club_part, country_id


# Prepositions that connect a personal name to a club qualifier,
# e.g. "Marculeta del Athletic Club de Madrid" — not a standalone team.
_RE_CONNECTIVE = re.compile(
    r'\b(?:del|de\s+la|de\s+los|de\s+las|de|van\s+den|van\s+der|van|vom|du|di|da)\b',
    re.IGNORECASE
)


def _is_standalone_team(name: str) -> bool:
    """
    True when the ENTIRE string represents a club/team, not a player entry.
    Strategy: require a team suffix AND exclude strings with connective prepositions
    that signal "PersonName [del/de/van] ClubName".

    e.g. "Hexham F.C."                         → True   (standalone team)
         "Crystal Palace F.C."                  → True
         "Marculeta del Athletic Club de Madrid" → False  (connective 'del'/'de')
         "Rangers FC - 2nd XI"                  → True
    """
    if not _RE_TEAM_SUFFIX.search(name):
        return False
    if _RE_CONNECTIVE.search(name):
        return False
    return True


def _looks_like_person(name: str) -> bool:
    """
    Heuristic: does this string plausibly contain a personal name?
    Rough criteria:
      - not empty
      - not a pure score (2-1)
      - not purely digits/punctuation
      - not too many lowercase-starting words (captions)
    """
    if not name:
        return False
    if _RE_SCORE.search(name):
        return False
    if _RE_DIGITS_ONLY.match(name):
        return False
    words = name.split()
    if len(words) > 7:
        lower_count = sum(1 for w in words if w and w[0].islower())
        if lower_count >= 3:
            return False
    return True


def _looks_like_first_name(s: str) -> bool:
    """True if s could plausibly be a first name (short, starts with capital)."""
    parts = s.strip().split()
    if not parts or len(parts) > 3:
        return False
    if not parts[0] or not parts[0][0].isupper():
        return False
    # Reject if it contains lowercase words that look like sentence fragments
    if sum(1 for w in parts if w and w[0].islower()) >= 1:
        return False
    return True


# ════════════════════════════════════════════════════════════════════════════════
# Card parsing  (set_cards.name_in_set → club_raw, country_id)
# ════════════════════════════════════════════════════════════════════════════════

def parse_name_in_set(text: str, country_lookup: dict) -> CardResult:
    """
    Parse a single name_in_set value and return what we found.

    Strategy:
      1. Strip card-number prefix.
      2. Extract parentheticals → classify each.
         - country  → set country_id  (prefer over club)
         - club     → set club_raw    (use last club-classified paren)
         - sport    → ignore for club_raw purposes
         - nickname → ignore (e.g. "(Ted)" before the club paren)
      3. If no club/country from parens, try double-dash separator.
      4. Handle slash patterns inside parens: "Wolves/Eire" → split.
    """
    result = CardResult()
    if not text:
        return result

    s = _strip_card_prefix(text.strip())

    # Remove trailing double-dash note for base extraction,
    # but keep the original to check the dash for a club later.
    s_no_note = _RE_DDASH.sub('', s).strip()

    base, parens = _extract_parens(s_no_note)

    # ── Classify each paren ───────────────────────────────────────────────────
    for paren in parens:
        kind, cid = _classify_paren(paren, country_lookup)
        if kind == 'country' and result.country_id is None:
            result.country_id = cid
        elif kind == 'club' and result.club_raw is None:
            result.club_raw = paren
        # sport / note / other → skip

    # ── Split "Club and Country" in club_raw ─────────────────────────────────
    # e.g. "Arsenal and England" → club_raw="Arsenal", country_id=<england>
    if result.club_raw is not None and result.country_id is None:
        club_part, cid = _split_and_pattern(result.club_raw, country_lookup)
        if cid is not None:
            result.country_id = cid
            result.club_raw   = club_part   # refined to club portion only

    # ── Handle slash parens not yet resolved ─────────────────────────────────
    if result.club_raw is None and result.country_id is None:
        for paren in parens:
            if '/' in paren:
                parts = [p.strip() for p in paren.split('/')]
                for p in parts:
                    if p.lower() in country_lookup:
                        if result.country_id is None:
                            result.country_id = country_lookup[p.lower()]
                    else:
                        if result.club_raw is None:
                            result.club_raw = p

    # ── If still nothing, try double-dash separator ───────────────────────────
    # Covers "Name  -  Halifax RFL" (no parens) or trailing sport note
    if result.club_raw is None and result.country_id is None:
        m = _RE_DDASH.search(s)
        if m:
            candidate = m.group(1).strip()
            low = candidate.lower()
            if low in SPORT_KEYWORDS:
                pass  # sport label after the dash, not a club
            elif low in country_lookup:
                result.country_id = country_lookup[low]
            else:
                # May also be "Club and Country" via dash: "Name  -  Wolves and Eire"
                club_part, cid = _split_and_pattern(candidate, country_lookup)
                if cid is not None:
                    result.club_raw   = club_part
                    result.country_id = cid
                else:
                    result.club_raw = candidate

    return result


# ════════════════════════════════════════════════════════════════════════════════
# Player parsing  (players.name_raw → first_name, last_name, is_non_player)
# ════════════════════════════════════════════════════════════════════════════════

def parse_player_name(name_raw: str, country_lookup: dict) -> PlayerResult:
    """
    Parse a single name_raw into structured fields.

    is_non_player = 1 when:
      - The base name doesn't look like a person AND all parens are sport labels
      - The name looks like a team name (ends in F.C., United, etc.)
      - The name is a pure description (score, long caption, digits-only)

    Name splitting priority:
      1. "Last, First"  comma format  (if 'First' looks like a first name)
      2. "J.S. Smith"   initial format
      3. "First Last"   plain two-word (or multi-word)
      4. Single word    → last_name only
    """
    result = PlayerResult()
    if not name_raw:
        return result

    s = _strip_card_prefix(name_raw.strip())
    base, parens = _extract_parens(s)

    # Strip trailing double-dash notes from the base name
    base = _RE_DDASH.sub('', base).strip()

    # ── Non-player detection ──────────────────────────────────────────────────
    sport_parens = [p for p in parens if p.lower() in SPORT_KEYWORDS]
    non_sport_parens = [p for p in parens if p.lower() not in SPORT_KEYWORDS
                        and not re.match(r'^\d+$', p)
                        and not p.lower().startswith(('sic', 'stamped', 'showing'))]

    base_is_person = _looks_like_person(base)

    if sport_parens and not base_is_person:
        # e.g. "Der Abwurf (Athletics)" — "Der Abwurf" is German for "The Throw"
        result.is_non_player = 1
    elif _is_standalone_team(base) and not non_sport_parens:
        # e.g. "Rothes F.C." — standalone team name with no person context
        result.is_non_player = 1
    elif not base_is_person and not parens:
        # e.g. "Eigenartige Blütenformen", "Hoensbroek - Staatsmijn Emma 2-2"
        result.is_non_player = 1

    # ── Name splitting ────────────────────────────────────────────────────────
    # Use base which already has parens and trailing notes stripped.
    # Also strip a trailing dash-club like "  -  Halifax RFL" from the base.
    name = _RE_DDASH.sub('', base).strip()

    if not name:
        return result

    # 1.  "Last, First" format
    m = _RE_LAST_FIRST.match(name)
    if m and _looks_like_first_name(m.group(2)):
        result.last_name  = m.group(1).strip()[:100]
        result.first_name = m.group(2).strip()[:100]
        return result

    # 2.  "J.S. Smith" — initial(s) + last name
    m = _RE_INITIAL_LAST.match(name)
    if m:
        result.first_name = m.group(1).strip()[:100]
        result.last_name  = m.group(2).strip()[:100]
        return result

    # 3.  Plain split: "First Last" or "First Middle Last"
    parts = name.split()
    if len(parts) == 1:
        result.last_name = parts[0][:100]
    elif len(parts) >= 2:
        result.first_name = ' '.join(parts[:-1])[:100]
        result.last_name  = parts[-1][:100]

    return result


# ════════════════════════════════════════════════════════════════════════════════
# Database passes
# ════════════════════════════════════════════════════════════════════════════════

def pass1_set_cards(conn, country_lookup: dict, dry_run: bool, limit: int):
    """
    Pass 1: read set_cards.name_in_set, write club_raw and country_id.
    Skips rows that already have club_raw or country_id set (idempotent re-runs).
    """
    print("\n── Pass 1: set_cards ─────────────────────────────────────────────────────")
    cur  = conn.cursor(buffered=True)   # buffered so wcur can write on same connection
    wcur = conn.cursor()

    sql = "SELECT card_id, name_in_set FROM set_cards WHERE club_raw IS NULL AND country_id IS NULL ORDER BY card_id"
    if limit:
        sql += f" LIMIT {limit}"
    cur.execute(sql)

    total = updated = skipped = country_hits = 0
    updates = []
    samples = []  # first 5 with club_raw for display

    for card_id, name_in_set in cur:
        total += 1
        r = parse_name_in_set(name_in_set or '', country_lookup)

        if r.club_raw is not None or r.country_id is not None:
            updated += 1
            if r.country_id:
                country_hits += 1
            updates.append((r.club_raw, r.country_id, card_id))
            if len(samples) < 5 and r.club_raw:
                samples.append((name_in_set, r.club_raw, r.country_id))
        else:
            skipped += 1

        if not dry_run and len(updates) >= BATCH:
            wcur.executemany(
                "UPDATE set_cards SET club_raw=%s, country_id=%s WHERE card_id=%s",
                updates
            )
            conn.commit()
            updates.clear()

    if not dry_run and updates:
        wcur.executemany(
            "UPDATE set_cards SET club_raw=%s, country_id=%s WHERE card_id=%s",
            updates
        )
        conn.commit()

    print(f"  Eligible rows : {total:,}  (already-set rows skipped)")
    print(f"  Updated       : {updated:,}  (club_raw and/or country_id extracted)")
    print(f"    → country_id set : {country_hits:,}")
    print(f"  No match      : {skipped:,}")
    if dry_run:
        print("  [DRY RUN — no DB writes]")
    if samples:
        print("  Sample club_raw extractions:")
        for raw, club, cid in samples:
            print(f"    {raw!r:55s}  →  club_raw={club!r}  country_id={cid}")

    cur.close()
    wcur.close()


def pass2_players(conn, country_lookup: dict, dry_run: bool, limit: int):
    """
    Pass 2: read players.name_raw, write first_name, last_name, is_non_player.
    Skips rows that already have first_name or last_name set (idempotent).
    """
    print("\n── Pass 2: players ───────────────────────────────────────────────────────")
    cur  = conn.cursor(buffered=True)   # buffered so wcur can write on same connection
    wcur = conn.cursor()

    sql = ("SELECT player_id, name_raw FROM players "
           "WHERE first_name IS NULL AND last_name IS NULL "
           "ORDER BY player_id")
    if limit:
        sql += f" LIMIT {limit}"
    cur.execute(sql)

    total = updated = non_player_count = 0
    updates = []
    np_samples = []   # sample non-player detections
    name_samples = [] # sample name splits

    for player_id, name_raw in cur:
        total += 1
        r = parse_player_name(name_raw or '', country_lookup)
        updated += 1

        if r.is_non_player:
            non_player_count += 1
            if len(np_samples) < 5:
                np_samples.append(name_raw)
        elif len(name_samples) < 5:
            name_samples.append((name_raw, r.first_name, r.last_name))

        updates.append((r.first_name, r.last_name, r.is_non_player, player_id))

        if not dry_run and len(updates) >= BATCH:
            wcur.executemany(
                "UPDATE players SET first_name=%s, last_name=%s, is_non_player=%s WHERE player_id=%s",
                updates
            )
            conn.commit()
            updates.clear()

    if not dry_run and updates:
        wcur.executemany(
            "UPDATE players SET first_name=%s, last_name=%s, is_non_player=%s WHERE player_id=%s",
            updates
        )
        conn.commit()

    print(f"  Eligible rows     : {total:,}  (already-set rows skipped)")
    print(f"  Updated           : {updated:,}")
    print(f"  Flagged non-player: {non_player_count:,}  ({100*non_player_count/max(total,1):.1f}%)")
    if dry_run:
        print("  [DRY RUN — no DB writes]")
    if np_samples:
        print("  Sample non-player detections:")
        for s in np_samples:
            print(f"    {s!r}")
    if name_samples:
        print("  Sample name splits:")
        for raw, fn, ln in name_samples:
            print(f"    {raw!r:45s}  →  first={fn!r}  last={ln!r}")

    cur.close()
    wcur.close()


# ════════════════════════════════════════════════════════════════════════════════
# Pass 3: reclassify is_non_player (re-evaluates all players, name fields untouched)
# ════════════════════════════════════════════════════════════════════════════════

def pass3_reclassify(conn, country_lookup: dict, dry_run: bool, limit: int):
    """
    Re-evaluate is_non_player for ALL players without touching first/last name.
    Use this whenever the non-player detection logic is updated and you want
    to re-score rows that were already processed by pass2.
    """
    print("\n── Pass 3: reclassify is_non_player ─────────────────────────────────────")
    cur  = conn.cursor(buffered=True)
    wcur = conn.cursor()

    sql = "SELECT player_id, name_raw FROM players ORDER BY player_id"
    if limit:
        sql += f" LIMIT {limit}"
    cur.execute(sql)

    total = non_player_count = 0
    updates  = []
    np_samples = []

    for player_id, name_raw in cur:
        total += 1
        r = parse_player_name(name_raw or '', country_lookup)
        if r.is_non_player:
            non_player_count += 1
            if len(np_samples) < 10:
                np_samples.append(name_raw)
        updates.append((r.is_non_player, player_id))

        if not dry_run and len(updates) >= BATCH:
            wcur.executemany(
                "UPDATE players SET is_non_player=%s WHERE player_id=%s",
                updates
            )
            conn.commit()
            updates.clear()

    if not dry_run and updates:
        wcur.executemany(
            "UPDATE players SET is_non_player=%s WHERE player_id=%s",
            updates
        )
        conn.commit()

    print(f"  Total players     : {total:,}")
    print(f"  Flagged non-player: {non_player_count:,}  ({100*non_player_count/max(total,1):.1f}%)")
    if dry_run:
        print("  [DRY RUN — no DB writes]")
    if np_samples:
        print("  Sample non-player detections:")
        for s in np_samples:
            print(f"    {s!r}")

    cur.close()
    wcur.close()


# ════════════════════════════════════════════════════════════════════════════════
# Post-run verification queries (printed for manual review)
# ════════════════════════════════════════════════════════════════════════════════

VERIFY_SQL = """
-- ── Verification (run manually after parse_players.py) ────────────────────
SELECT 'set_cards club_raw coverage' AS check_name,
       COUNT(*)                       AS total,
       SUM(club_raw IS NOT NULL)      AS has_club_raw,
       SUM(country_id IS NOT NULL)    AS has_country_id
FROM set_cards;

SELECT 'players name split coverage' AS check_name,
       COUNT(*)                       AS total,
       SUM(first_name IS NOT NULL)    AS has_first_name,
       SUM(last_name  IS NOT NULL)    AS has_last_name,
       SUM(is_non_player = 1)         AS is_non_player
FROM players;

-- Top 20 club_raw values (review for obvious grouping / canonical clubs)
SELECT club_raw, COUNT(*) AS cnt
FROM set_cards
WHERE club_raw IS NOT NULL
GROUP BY club_raw
ORDER BY cnt DESC
LIMIT 20;

-- Country matches
SELECT c.country_name, COUNT(*) AS cnt
FROM set_cards sc
JOIN countries c USING (country_id)
GROUP BY c.country_name
ORDER BY cnt DESC
LIMIT 20;

-- Sample non-players (sanity check the flag)
SELECT player_id, name_raw
FROM players
WHERE is_non_player = 1
ORDER BY RAND()
LIMIT 20;
"""


# ════════════════════════════════════════════════════════════════════════════════
# Main
# ════════════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dry-run', action='store_true',
                    help='Preview what would change; write nothing to DB')
    ap.add_argument('--limit', type=int, default=0,
                    help='Process only first N eligible rows per pass (0 = all)')
    ap.add_argument('--pass', dest='pass_', default='both',
                    choices=['cards', 'players', 'both', 'reclassify'],
                    help='Which pass to run (default: both); '
                         'reclassify = re-score is_non_player for all players')
    args = ap.parse_args()

    print("=" * 70)
    print("parse_players.py — Phase 1 name parsing")
    mode = "DRY RUN" if args.dry_run else "LIVE"
    print(f"Mode  : {mode}")
    print(f"Pass  : {args.pass_}")
    print(f"Limit : {args.limit or 'none (all rows)'}")
    print("=" * 70)

    try:
        conn = mysql.connector.connect(**DB_CONFIG)
    except mysql.connector.Error as e:
        sys.exit(f"ERROR: Cannot connect to MySQL: {e}")

    cur = conn.cursor()
    country_lookup = load_country_lookup(cur)
    cur.close()
    print(f"\nLoaded {len(country_lookup):,} country/alias entries from countries table")

    if args.pass_ in ('cards', 'both'):
        pass1_set_cards(conn, country_lookup, args.dry_run, args.limit)

    if args.pass_ in ('players', 'both'):
        pass2_players(conn, country_lookup, args.dry_run, args.limit)

    if args.pass_ == 'reclassify':
        pass3_reclassify(conn, country_lookup, args.dry_run, args.limit)

    conn.close()
    print("\n" + "=" * 70)
    print("Done.")
    print("\nVerification queries to run after a live run:")
    print(VERIFY_SQL)


if __name__ == '__main__':
    main()
