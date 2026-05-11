"""
wikidata_lookup_players.py
--------------------------
Phase 1 of the Wikidata-driven data-cleanup pass.

For each active player record (is_non_player = 0, canonical_player_id IS NULL),
query the Wikidata Query Service for candidate matches and stage the results.

Two layers, two purposes:

  1. **JSONL file** (`wikidata_lookup_cache.jsonl`) — the durable source of
     truth. Every SPARQL response (main + club-history) is appended as one
     JSON object per player lookup. Slow to obtain, cheap to keep, never
     re-queried unless you delete the file. Gitignored.

  2. **MySQL tables** (`player_wikidata_candidates`, `player_wikidata_lookups`)
     — the queryable view, parsed out of the JSONL during the run. If you
     change the candidate-table schema or fix a parser bug, you can wipe
     these tables and rebuild from the JSONL with --reload-from-jsonl
     instead of paying the Wikidata cost again.

This script DOES NOT modify the live `players` rows. It is a pure
read-and-cache pass — safe to run, safe to interrupt (each player is
committed individually), safe to re-run (already-attempted players are
skipped).

Usage:
    # First time — run the migration
    python migrate_wikidata.py

    # Smoke test on a single player (writes JSONL + MySQL)
    python wikidata_lookup_players.py --player-id 1234

    # Small limited batch
    python wikidata_lookup_players.py --limit 50

    # Full run, polite rate limit (default 1.2s ≈ 50 req/min, well under WDQS cap)
    python wikidata_lookup_players.py

    # Re-attempt previously failed lookups
    python wikidata_lookup_players.py --retry-failed --limit 20

    # Capture data without touching MySQL (JSONL still gets written)
    python wikidata_lookup_players.py --dry-run --limit 10

    # Rebuild MySQL from the JSONL — no Wikidata calls at all
    python wikidata_lookup_players.py --reload-from-jsonl wikidata_lookup_cache.jsonl

    # Skip the per-candidate club-history fetch (faster, less data for scoring)
    python wikidata_lookup_players.py --club-lookups 0

Exit cleanly with Ctrl+C — committed work is preserved; the in-flight
player will simply be re-attempted on the next run.
"""

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone

import mysql.connector

import wikidata_helpers as wd


DB_CONFIG = dict(
    host="127.0.0.1",
    port=3306,
    user="sc_loader",
    password="Gator888",
    database="soccer_checklist_db",
    charset="utf8mb4",
)

# Schema version stamped on every JSONL entry. Bump if the entry layout
# changes in a way that the reload path needs to handle differently.
JSONL_SCHEMA_VERSION = 1

DEFAULT_JSONL_PATH = "wikidata_lookup_cache.jsonl"


# ─────────────────────────────────────────────────────────────────────────────
# DB queries
# ─────────────────────────────────────────────────────────────────────────────

PLAYERS_TO_LOOKUP_SQL = """
    SELECT p.player_id,
           p.first_name,
           p.last_name,
           p.display_name,
           p.name_raw,
           COUNT(sc.card_id) AS appearance_count
    FROM players p
    LEFT JOIN set_cards sc ON p.player_id = sc.player_id
    WHERE p.is_non_player = 0
      AND p.canonical_player_id IS NULL
      AND NOT EXISTS (
          SELECT 1 FROM player_wikidata_lookups l
          WHERE l.player_id = p.player_id
            {failed_clause}
      )
    GROUP BY p.player_id
    ORDER BY appearance_count DESC, p.player_id ASC
    LIMIT %s
"""


SINGLE_PLAYER_SQL = """
    SELECT p.player_id,
           p.first_name,
           p.last_name,
           p.display_name,
           p.name_raw,
           COUNT(sc.card_id) AS appearance_count
    FROM players p
    LEFT JOIN set_cards sc ON p.player_id = sc.player_id
    WHERE p.player_id = %s
    GROUP BY p.player_id
"""


INSERT_CANDIDATE_SQL = """
    INSERT INTO player_wikidata_candidates
        (player_id, qid, label_en, description_en, given_name, family_name,
         aliases_json, date_of_birth, birth_year, nationality, nationality_qid,
         birth_place, clubs_json, sparql_strategy, fetched_at)
    VALUES
        (%(player_id)s, %(qid)s, %(label_en)s, %(description_en)s,
         %(given_name)s, %(family_name)s, %(aliases_json)s,
         %(date_of_birth)s, %(birth_year)s, %(nationality)s,
         %(nationality_qid)s, %(birth_place)s, %(clubs_json)s,
         %(sparql_strategy)s, NOW())
    ON DUPLICATE KEY UPDATE
         label_en        = VALUES(label_en),
         description_en  = VALUES(description_en),
         given_name      = VALUES(given_name),
         family_name     = VALUES(family_name),
         aliases_json    = VALUES(aliases_json),
         date_of_birth   = VALUES(date_of_birth),
         birth_year      = VALUES(birth_year),
         nationality     = VALUES(nationality),
         nationality_qid = VALUES(nationality_qid),
         birth_place     = VALUES(birth_place),
         clubs_json      = VALUES(clubs_json),
         sparql_strategy = VALUES(sparql_strategy),
         fetched_at      = NOW()
"""


INSERT_LOOKUP_SQL = """
    INSERT INTO player_wikidata_lookups
        (player_id, strategy, candidates_found, succeeded, error_msg,
         duration_ms, attempted_at)
    VALUES
        (%(player_id)s, %(strategy)s, %(candidates_found)s, %(succeeded)s,
         %(error_msg)s, %(duration_ms)s, NOW())
"""


# ─────────────────────────────────────────────────────────────────────────────
# JSONL helpers
# ─────────────────────────────────────────────────────────────────────────────

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _append_jsonl(entry: dict, path: str) -> None:
    """Append a single entry as one line of JSONL. Safe under Ctrl+C."""
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, separators=(",", ":"), ensure_ascii=False) + "\n")


def _build_entry(
    player: dict,
    strategy: str,
    main_response: dict,
    club_responses_by_qid: dict,
    succeeded: bool,
    error_msg: str = None,
    duration_ms: int = 0,
) -> dict:
    """Construct a JSONL entry. Player snapshot is included so the reload
    path doesn't need to look anything up — the entry is self-contained."""
    return {
        "schema_version": JSONL_SCHEMA_VERSION,
        "player_id":      player["player_id"],
        "attempted_at":   _utc_now_iso(),
        "strategy":       strategy,
        "player_snapshot": {
            "first_name":   player.get("first_name"),
            "last_name":    player.get("last_name"),
            "display_name": player.get("display_name"),
            "name_raw":     player.get("name_raw"),
        },
        "main_response":  main_response,
        "club_responses": club_responses_by_qid,
        "succeeded":      bool(succeeded),
        "error_msg":      error_msg,
        "duration_ms":    duration_ms,
    }


def _candidates_from_entry(entry: dict) -> dict:
    """Re-derive parsed candidate dicts from a JSONL entry. Returns
    {qid: candidate_dict}. Empty dict for failed / no-name entries."""
    main = entry.get("main_response")
    if not entry.get("succeeded") or not main:
        return {}
    bindings = main.get("results", {}).get("bindings", [])
    candidates = wd.merge_player_bindings(bindings)

    for qid, club_resp in (entry.get("club_responses") or {}).items():
        if qid not in candidates or not club_resp:
            continue
        club_bindings = club_resp.get("results", {}).get("bindings", [])
        candidates[qid]["clubs"] = wd.parse_club_bindings(club_bindings)

    return candidates


# ─────────────────────────────────────────────────────────────────────────────
# MySQL writer (shared by live and reload paths)
# ─────────────────────────────────────────────────────────────────────────────

def _write_entry_to_mysql(cur, entry: dict) -> int:
    """Persist a JSONL entry to MySQL. Returns the number of candidate rows
    written (0 for failures, no_name skips, or empty result sets)."""
    candidates = _candidates_from_entry(entry)
    strategy = entry.get("strategy") or "unknown"

    for qid, c in candidates.items():
        cur.execute(INSERT_CANDIDATE_SQL, {
            "player_id":       entry["player_id"],
            "qid":             qid,
            "label_en":        c.get("label_en"),
            "description_en": (c.get("description_en") or "")[:500] or None,
            "given_name":      c.get("given_name"),
            "family_name":     c.get("family_name"),
            "aliases_json":    None,  # populated by a later phase if scoring needs it
            "date_of_birth":   c.get("date_of_birth"),
            "birth_year":      c.get("birth_year"),
            "nationality":     c.get("nationality"),
            "nationality_qid": c.get("nationality_qid"),
            "birth_place":     c.get("birth_place"),
            "clubs_json":      json.dumps(c.get("clubs") or []),
            "sparql_strategy": strategy,
        })

    cur.execute(INSERT_LOOKUP_SQL, {
        "player_id":        entry["player_id"],
        "strategy":         strategy,
        "candidates_found": len(candidates),
        "succeeded":        1 if entry.get("succeeded") else 0,
        "error_msg":       (entry.get("error_msg") or None) and (entry["error_msg"][:1000]),
        "duration_ms":      entry.get("duration_ms") or 0,
    })

    return len(candidates)


# ─────────────────────────────────────────────────────────────────────────────
# Live lookup workflow (Wikidata → JSONL → MySQL)
# ─────────────────────────────────────────────────────────────────────────────

def lookup_one_player(
    cur,
    player: dict,
    jsonl_path: str,
    club_lookups: int,
    rate_delay: float,
    dry_run: bool,
) -> tuple:
    """Run a Wikidata lookup for one player. Always appends to JSONL.
    Writes to MySQL unless dry_run. Returns (strategy, n_candidates)
    where n_candidates is -1 for SPARQL errors and 0 for no-name skips."""
    strategy, query = wd.pick_strategy_and_query(player)

    # No usable name → log a no-op so we don't keep retrying
    if strategy is None:
        entry = _build_entry(player, "no_name", None, {}, succeeded=True)
        _append_jsonl(entry, jsonl_path)
        if not dry_run:
            _write_entry_to_mysql(cur, entry)
        return "no_name", 0

    t0 = time.time()
    try:
        main_response = wd.run_sparql(query)
    except wd.WikidataError as e:
        duration_ms = int((time.time() - t0) * 1000)
        entry = _build_entry(
            player, strategy, None, {}, succeeded=False,
            error_msg=str(e), duration_ms=duration_ms,
        )
        _append_jsonl(entry, jsonl_path)
        if not dry_run:
            _write_entry_to_mysql(cur, entry)
        return strategy, -1

    # Top-N candidates get full club history fetched.
    bindings = main_response.get("results", {}).get("bindings", [])
    qids_for_clubs = list(wd.merge_player_bindings(bindings).keys())[:club_lookups]

    club_responses: dict = {}
    for qid in qids_for_clubs:
        time.sleep(rate_delay)
        try:
            club_responses[qid] = wd.run_sparql(wd.build_club_history_query(qid))
        except wd.WikidataError as e:
            print(f"    [warn] club history fetch failed for {qid}: {e}",
                  file=sys.stderr)
            club_responses[qid] = None

    duration_ms = int((time.time() - t0) * 1000)

    entry = _build_entry(
        player, strategy, main_response, club_responses,
        succeeded=True, duration_ms=duration_ms,
    )
    _append_jsonl(entry, jsonl_path)

    if not dry_run:
        n = _write_entry_to_mysql(cur, entry)
    else:
        # Still report what we *would* have written
        n = len(_candidates_from_entry(entry))

    return strategy, n


# ─────────────────────────────────────────────────────────────────────────────
# Reload path (JSONL → MySQL, no Wikidata)
# ─────────────────────────────────────────────────────────────────────────────

def reload_from_jsonl(args) -> None:
    """Rebuild MySQL state from a JSONL file. Multiple entries per player
    are replayed in order — ON DUPLICATE KEY UPDATE on candidates means
    the last entry wins per (player_id, qid); lookups gets one row per
    entry, faithfully reproducing the log."""
    path = args.reload_from_jsonl
    if not os.path.exists(path):
        print(f"JSONL file not found: {path}", file=sys.stderr)
        sys.exit(2)

    only_player = args.player_id

    conn = mysql.connector.connect(**DB_CONFIG)
    cur  = conn.cursor()

    n_entries        = 0
    n_total_cands    = 0
    n_failures       = 0
    n_no_name        = 0
    n_skipped_filter = 0

    try:
        with open(path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError as e:
                    print(f"[skip] line {line_no}: malformed JSON ({e})",
                          file=sys.stderr)
                    continue

                if only_player and entry.get("player_id") != only_player:
                    n_skipped_filter += 1
                    continue

                label = _entry_label(entry)
                print(f"[reload {line_no}] player_id={entry.get('player_id')}: {label}")

                if not args.dry_run:
                    n_cands = _write_entry_to_mysql(cur, entry)
                    conn.commit()
                else:
                    n_cands = len(_candidates_from_entry(entry))

                if entry.get("strategy") == "no_name":
                    n_no_name += 1
                elif not entry.get("succeeded"):
                    n_failures += 1
                else:
                    n_total_cands += n_cands

                n_entries += 1

    except KeyboardInterrupt:
        print("\n\nInterrupted — committed work is preserved.")
    finally:
        cur.close()
        conn.close()

    print(f"\nDone. {n_entries} entries replayed, "
          f"{n_total_cands} candidates written, "
          f"{n_failures} failed lookups logged, {n_no_name} no-name skips."
          + (f" ({n_skipped_filter} skipped by --player-id filter)"
             if n_skipped_filter else ""))


def _entry_label(entry: dict) -> str:
    snap = entry.get("player_snapshot") or {}
    return (
        f"{snap.get('first_name') or ''} {snap.get('last_name') or ''}".strip()
        or snap.get("display_name")
        or snap.get("name_raw")
        or "?"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Live entry point (Wikidata → JSONL → MySQL)
# ─────────────────────────────────────────────────────────────────────────────

def run_live(args) -> None:
    conn = mysql.connector.connect(**DB_CONFIG)
    cur  = conn.cursor(dictionary=True)
    cur2 = conn.cursor()  # for INSERTs (separate from the SELECT cursor)

    if args.player_id:
        cur.execute(SINGLE_PLAYER_SQL, (args.player_id,))
    else:
        failed_clause = "AND l.succeeded = 1" if args.retry_failed else ""
        cur.execute(
            PLAYERS_TO_LOOKUP_SQL.format(failed_clause=failed_clause),
            (args.limit,),
        )
    players = cur.fetchall()

    if not players:
        print("No players to look up.")
        cur.close()
        cur2.close()
        conn.close()
        return

    print(f"Looking up {len(players)} players "
          f"(rate-delay {args.rate_delay}s, club-lookups {args.club_lookups}/player, "
          f"JSONL → {args.jsonl_file}"
          f"{', DRY RUN — no MySQL writes' if args.dry_run else ''}).\n")

    total_candidates = 0
    failures = 0
    no_name = 0

    try:
        for i, p in enumerate(players, 1):
            label = (
                f"{p.get('first_name') or ''} {p.get('last_name') or ''}".strip()
                or p.get("display_name") or p.get("name_raw") or "?"
            )
            print(f"[{i}/{len(players)}] player_id={p['player_id']} "
                  f"({p['appearance_count']} cards): {label}")

            try:
                strategy, n = lookup_one_player(
                    cur2, p,
                    jsonl_path=args.jsonl_file,
                    club_lookups=args.club_lookups,
                    rate_delay=args.rate_delay,
                    dry_run=args.dry_run,
                )
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"    [ERROR] {e}", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)
                failures += 1
                if not args.dry_run:
                    conn.rollback()
                continue

            if strategy == "no_name":
                no_name += 1
                print(f"    [skip] no usable name")
            elif n < 0:
                failures += 1
                print(f"    [fail] SPARQL error (logged for retry)")
            else:
                total_candidates += n
                print(f"    [{strategy}] {n} candidates")

            if not args.dry_run:
                conn.commit()

            if i < len(players):
                time.sleep(args.rate_delay)

    except KeyboardInterrupt:
        print("\n\nInterrupted — committed work is preserved. JSONL is up to date.")

    finally:
        cur.close()
        cur2.close()
        conn.close()

    print(f"\nDone. {len(players)} players processed, "
          f"{total_candidates} total candidates cached, "
          f"{failures} failures, {no_name} no-name skips.")


def main(args) -> None:
    if args.reload_from_jsonl:
        reload_from_jsonl(args)
    else:
        run_live(args)


def cli():
    parser = argparse.ArgumentParser(
        description="Populate player_wikidata_candidates from Wikidata, with a "
                    "JSONL cache file as the durable source of truth.")

    # Live-mode options
    parser.add_argument("--limit", type=int, default=50,
                        help="Max players to process per run in live mode "
                             "(default 50)")
    parser.add_argument("--rate-delay", type=float, default=1.2,
                        help="Seconds between SPARQL requests (default 1.2; "
                             "minimum 0.5)")
    parser.add_argument("--club-lookups", type=int, default=5,
                        help="Per-player club-history fetches for top-N "
                             "candidates (default 5; 0 disables)")
    parser.add_argument("--retry-failed", action="store_true",
                        help="Re-attempt players whose previous lookups errored")

    # Cache + reload
    parser.add_argument("--jsonl-file", default=DEFAULT_JSONL_PATH,
                        help=f"Path to the JSONL cache file "
                             f"(default {DEFAULT_JSONL_PATH})")
    parser.add_argument("--reload-from-jsonl",
                        metavar="PATH",
                        help="Skip Wikidata; rebuild MySQL from this JSONL file. "
                             "Combine with --player-id to reload a single player "
                             "or --dry-run to preview without writing.")

    # Universal
    parser.add_argument("--player-id", type=int, default=None,
                        help="Live mode: look up just this player. "
                             "Reload mode: replay only entries for this player.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip MySQL writes. In live mode, the JSONL is "
                             "still appended (useful for capturing data to "
                             "inspect before committing).")

    args = parser.parse_args()

    if not args.reload_from_jsonl and args.rate_delay < 0.5:
        print("Refusing to run with rate-delay < 0.5s. WDQS rate cap is "
              "60 req/min unauthenticated.", file=sys.stderr)
        sys.exit(2)

    main(args)


if __name__ == "__main__":
    cli()
