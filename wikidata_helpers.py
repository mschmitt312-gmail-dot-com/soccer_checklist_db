"""
wikidata_helpers.py
-------------------
SPARQL client + query builders + result parsers for the player Wikidata lookup
pass.  Used by wikidata_lookup_players.py and any future scoring/apply scripts.

Wikidata Query Service notes:
  - Endpoint:  https://query.wikidata.org/sparql
  - Rate cap: ~60 unauthenticated requests per minute, 5 concurrent.
  - User-Agent identifying the tool is required by their bot policy.
  - The service is FREE but slow; queries scoped to a small entity set
    (e.g. footballers via P106 occupation) finish in under a second,
    queries that scan all humans frequently time out.

Wikidata properties used:
  P31  instance of            (we filter to Q5 = human)
  P106 occupation             (we filter to Q937857 = association football player,
                               or Q628020 = footballer for older entries)
  P735 given name             (entity reference; resolves "Bobby" → "Robert" etc.)
  P734 family name            (entity reference)
  P569 date of birth
  P19  place of birth
  P27  country of citizenship
  P54  member of sports team  (with P580 start time / P582 end time qualifiers)
"""

import json
import re
import time
import unicodedata
from typing import Optional

import requests


SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"

# Identifies the script per Wikidata's bot policy. Update the URL/email if the
# repo or contact changes.
USER_AGENT = (
    "soccer_checklist_db/1.0 "
    "(https://github.com/mschmitt312-gmail-dot-com/soccer_checklist_db; "
    "mschmitt312@gmail.com) python-requests"
)

# Q-IDs for the occupation filter. Q937857 = association football player,
# Q628020 = footballer (broader, older entries sometimes use this).
FOOTBALLER_OCCUPATIONS = ["Q937857", "Q628020"]
OCCUPATION_VALUES_CLAUSE = " ".join(f"wd:{q}" for q in FOOTBALLER_OCCUPATIONS)

# Languages we ask Wikidata's label service to consider. Covers most vintage
# football-card publishing markets.
LABEL_LANGS = "en,es,pt,it,de,fr,nl,hu"


# ─────────────────────────────────────────────────────────────────────────────
# HTTP layer
# ─────────────────────────────────────────────────────────────────────────────

class WikidataError(Exception):
    """Raised when a SPARQL request fails after the configured retries."""


def run_sparql(query: str, timeout: int = 60, retries: int = 3) -> dict:
    """POST a SPARQL query to WDQS and return parsed JSON results.

    Retries on 429 (Too Many Requests) and 5xx with exponential backoff.
    Honours the Retry-After header on 429 when present.
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept":     "application/sparql-results+json",
    }
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        try:
            resp = requests.post(
                SPARQL_ENDPOINT,
                data={"query": query, "format": "json"},
                headers=headers,
                timeout=timeout,
            )
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", "5") or "5") + (2 ** attempt)
                time.sleep(wait)
                continue
            if 500 <= resp.status_code < 600:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as e:
            last_err = e
            time.sleep(2 ** attempt)
    raise WikidataError(f"SPARQL request failed after {retries} retries: {last_err}")


# ─────────────────────────────────────────────────────────────────────────────
# String helpers
# ─────────────────────────────────────────────────────────────────────────────

def _escape_sparql_string(s: str) -> str:
    """Escape for safe inclusion inside a SPARQL double-quoted literal."""
    return (s or "").replace("\\", "\\\\").replace('"', '\\"')


def normalize_name(name: str) -> str:
    """Lowercase, strip diacritics, collapse whitespace.

    Used for client-side scoring; SPARQL filters use LCASE(STR(?name)) which
    is not diacritic-insensitive, so we run a parallel normalize when comparing.
    """
    if not name:
        return ""
    decomposed = unicodedata.normalize("NFD", name)
    no_diacritics = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", no_diacritics.strip().lower())


def qid_from_uri(uri: Optional[str]) -> Optional[str]:
    """Extract the Q-ID from a Wikidata entity URI."""
    if not uri:
        return None
    return uri.rsplit("/", 1)[-1]


def _extract_year(time_str: Optional[str]) -> Optional[int]:
    """Wikidata times look like '+1923-02-01T00:00:00Z' or '-0500-...'.
    Extract just the year, return None if implausible."""
    if not time_str:
        return None
    m = re.match(r"^([+-]?\d{1,5})-", time_str)
    if not m:
        return None
    try:
        y = int(m.group(1))
        return y if 1800 <= y <= 2100 else None
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Query builders
# ─────────────────────────────────────────────────────────────────────────────

# Common SELECT clause for player lookups. The four ?Label variables are
# auto-filled by SERVICE wikibase:label. ?given/?family are entity references
# whose labels resolve real first/last names (e.g. Q-ID for "Bobby" yields
# "Robert" via the P735 chain — useful when expanding "B." into the canonical
# given name).
_PLAYER_SELECT = """
SELECT DISTINCT ?p ?pLabel ?pDescription ?dob ?nationality ?nationalityLabel
                ?birthPlace ?birthPlaceLabel ?given ?givenLabel ?family ?familyLabel
"""

_PLAYER_OPTIONALS = f"""
      OPTIONAL {{ ?p wdt:P569 ?dob. }}
      OPTIONAL {{ ?p wdt:P27   ?nationality. }}
      OPTIONAL {{ ?p wdt:P19   ?birthPlace. }}
      OPTIONAL {{ ?p wdt:P735  ?given. }}
      OPTIONAL {{ ?p wdt:P734  ?family. }}

      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "{LABEL_LANGS}". }}
"""


def build_full_name_query(first_name: str, last_name: str) -> str:
    """Strategy 'full_name': exact match against label or altLabel for
    "<first> <last>", filtered to footballers."""
    full = _escape_sparql_string(f"{first_name} {last_name}".strip().lower())
    return f"""{_PLAYER_SELECT}WHERE {{
      VALUES ?occupation {{ {OCCUPATION_VALUES_CLAUSE} }}
      ?p wdt:P106 ?occupation;
         wdt:P31  wd:Q5.

      ?p ?labelProp ?name.
      VALUES ?labelProp {{ rdfs:label skos:altLabel }}
      FILTER(LCASE(STR(?name)) = "{full}")
{_PLAYER_OPTIONALS}
    }}
    LIMIT 25
    """


def build_initial_only_query(initial: str, last_name: str) -> str:
    """Strategy 'initial_only': footballers whose label ends with " <last>" and
    starts with <initial>. Local scoring picks the right one based on year/club
    overlap with the candidate's `set_cards`.
    """
    initial = (initial or "").strip(".").lower()[:1]
    last_lc = _escape_sparql_string((last_name or "").strip().lower())
    initial_filter = ""
    if initial and initial.isalpha():
        initial_filter = f'\n      FILTER(STRSTARTS(LCASE(STR(?name)), "{initial}"))'
    return f"""{_PLAYER_SELECT}WHERE {{
      VALUES ?occupation {{ {OCCUPATION_VALUES_CLAUSE} }}
      ?p wdt:P106 ?occupation;
         wdt:P31  wd:Q5.

      ?p ?labelProp ?name.
      VALUES ?labelProp {{ rdfs:label skos:altLabel }}
      FILTER(STRENDS(LCASE(STR(?name)), " {last_lc}")){initial_filter}
{_PLAYER_OPTIONALS}
    }}
    LIMIT 50
    """


def build_last_name_only_query(last_name: str) -> str:
    """Strategy 'last_name_only': footballers whose label ends with " <last>"
    or whose label IS just "<last>" (used when the player record has no first
    name at all)."""
    last_lc = _escape_sparql_string((last_name or "").strip().lower())
    return f"""{_PLAYER_SELECT}WHERE {{
      VALUES ?occupation {{ {OCCUPATION_VALUES_CLAUSE} }}
      ?p wdt:P106 ?occupation;
         wdt:P31  wd:Q5.

      ?p ?labelProp ?name.
      VALUES ?labelProp {{ rdfs:label skos:altLabel }}
      FILTER(STRENDS(LCASE(STR(?name)), " {last_lc}") || LCASE(STR(?name)) = "{last_lc}")
{_PLAYER_OPTIONALS}
    }}
    LIMIT 50
    """


def build_single_name_query(display_name: str) -> str:
    """Strategy 'single_name': for one-word stage names like "Pelé" or
    "Garrincha". Exact match against label/altLabel."""
    name = _escape_sparql_string((display_name or "").strip().lower())
    return f"""{_PLAYER_SELECT}WHERE {{
      VALUES ?occupation {{ {OCCUPATION_VALUES_CLAUSE} }}
      ?p wdt:P106 ?occupation;
         wdt:P31  wd:Q5.

      ?p ?labelProp ?name.
      VALUES ?labelProp {{ rdfs:label skos:altLabel }}
      FILTER(LCASE(STR(?name)) = "{name}")
{_PLAYER_OPTIONALS}
    }}
    LIMIT 25
    """


def build_club_history_query(qid: str) -> str:
    """Fetch the player's P54 statements with start/end year qualifiers."""
    qid = qid.strip().upper()
    if not re.match(r"^Q\d+$", qid):
        raise ValueError(f"Invalid Q-ID: {qid!r}")
    return f"""
    SELECT ?team ?teamLabel ?startTime ?endTime WHERE {{
      wd:{qid} p:P54 ?stmt.
      ?stmt   ps:P54 ?team.
      OPTIONAL {{ ?stmt pq:P580 ?startTime. }}
      OPTIONAL {{ ?stmt pq:P582 ?endTime. }}
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
    }}
    ORDER BY ?startTime
    """


# ─────────────────────────────────────────────────────────────────────────────
# Result parsers
# ─────────────────────────────────────────────────────────────────────────────

def _val(b: dict, key: str) -> Optional[str]:
    return b.get(key, {}).get("value")


def parse_player_binding(b: dict) -> dict:
    """Convert one SPARQL result row into a candidate dict matching the
    column names in player_wikidata_candidates."""
    qid = qid_from_uri(_val(b, "p"))

    dob_iso = None
    birth_year = None
    raw_dob = _val(b, "dob")
    if raw_dob:
        m = re.match(r"^([+-]?\d{1,4})-(\d{2})-(\d{2})", raw_dob)
        if m:
            try:
                year = int(m.group(1))
            except ValueError:
                year = None
            if year and 1800 <= year <= 2100:
                birth_year = year
                # Always store as YYYY-MM-DD; partial-precision dates from
                # Wikidata still arrive in this format with month/day = 01
                dob_iso = f"{year:04d}-{m.group(2)}-{m.group(3)}"

    return {
        "qid":             qid,
        "label_en":        _val(b, "pLabel"),
        "description_en":  _val(b, "pDescription"),
        "given_name":      _val(b, "givenLabel"),
        "family_name":     _val(b, "familyLabel"),
        "date_of_birth":   dob_iso,
        "birth_year":      birth_year,
        "nationality":     _val(b, "nationalityLabel"),
        "nationality_qid": qid_from_uri(_val(b, "nationality")),
        "birth_place":     _val(b, "birthPlaceLabel"),
    }


def merge_player_bindings(bindings: list) -> dict:
    """A single Wikidata player can produce multiple result rows when several
    OPTIONAL clauses each have multiple values (e.g. dual nationality). Group
    by Q-ID and keep the first non-null seen for each field."""
    out: dict = {}
    for b in bindings:
        cand = parse_player_binding(b)
        qid = cand.get("qid")
        if not qid:
            continue
        existing = out.get(qid)
        if existing is None:
            out[qid] = cand
        else:
            for k, v in cand.items():
                if v and not existing.get(k):
                    existing[k] = v
    return out


def parse_club_bindings(bindings: list) -> list:
    """Convert SPARQL club-history bindings to [{qid,label,start_year,end_year}, ...]
    deduplicated across the rare duplicate-team-statement cases."""
    seen: dict = {}
    for b in bindings:
        team_uri = _val(b, "team")
        team_qid = qid_from_uri(team_uri)
        if not team_qid:
            continue
        start_year = _extract_year(_val(b, "startTime"))
        end_year   = _extract_year(_val(b, "endTime"))
        key = (team_qid, start_year)
        if key in seen:
            # Replace if the new row has an end_year and the existing didn't
            if end_year and not seen[key].get("end_year"):
                seen[key]["end_year"] = end_year
            continue
        seen[key] = {
            "qid":        team_qid,
            "label":      _val(b, "teamLabel"),
            "start_year": start_year,
            "end_year":   end_year,
        }
    return sorted(
        seen.values(),
        key=lambda c: (c["start_year"] is None, c["start_year"] or 0),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Strategy selector
# ─────────────────────────────────────────────────────────────────────────────

def pick_strategy_and_query(player: dict) -> tuple:
    """Given a players-table row dict, return (strategy_name, sparql_query).

    player keys we care about:
      first_name, last_name, display_name, name_raw

    Returns (strategy, query) where strategy is one of:
      'full_name', 'initial_only', 'last_name_only', 'single_name'
    or (None, None) if the player has no usable name.
    """
    first   = (player.get("first_name") or "").strip()
    last    = (player.get("last_name")  or "").strip()
    display = (player.get("display_name") or "").strip()

    is_initial = bool(re.match(r"^[A-Za-z]\.?$", first))

    if first and last and not is_initial and len(first) > 1:
        return "full_name", build_full_name_query(first, last)
    if last and is_initial:
        return "initial_only", build_initial_only_query(first, last)
    if last:
        return "last_name_only", build_last_name_only_query(last)
    if display and " " not in display:
        return "single_name", build_single_name_query(display)
    if first and " " not in first and not last:
        # e.g. raw "Pele" parsed into first_name only
        return "single_name", build_single_name_query(first)
    return None, None
