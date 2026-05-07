"""
deduplicate_tier2.py
--------------------
Merges initial-only player records (e.g. "T. Finney") into their
matching full-name record (e.g. "Tom Finney") using:

  Round 1 — Year overlap (±1 year), exactly 1 candidate → MERGE
  Round 2 — Club tiebreaker for 2-candidate cases → MERGE if 1 club match

The full-name record is always the canonical. Initial-only records have
their set_cards re-pointed and canonical_player_id set. Nothing deleted.

Run from the project root:
    python deduplicate_tier2.py --dry-run    # preview
    python deduplicate_tier2.py              # apply
"""

import argparse
import mysql.connector
from collections import defaultdict

DB_CONFIG = dict(
    host="127.0.0.1",
    port=3306,
    user="sc_loader",
    password="Gator888",
    database="soccer_checklist_db",
    charset="utf8mb4",
)

YEAR_BUFFER = 1


def is_initial(name: str) -> bool:
    clean = name.strip().rstrip(".")
    return len(clean) == 1 and clean.isalpha()


def clubs_overlap(clubs_a: set, clubs_b: set) -> bool:
    for a in clubs_a:
        a_lower = a.lower().strip()
        for b in clubs_b:
            b_lower = b.lower().strip()
            if len(a_lower) >= 4 and len(b_lower) >= 4:
                if a_lower in b_lower or b_lower in a_lower:
                    return True
    return False


def main(dry_run: bool):
    conn = mysql.connector.connect(**DB_CONFIG)
    cur  = conn.cursor(dictionary=True)
    cur2 = conn.cursor()

    print("Loading players...")
    cur.execute("""
        SELECT
            p.player_id,
            p.first_name,
            p.last_name,
            MIN(s.year_start) AS year_min,
            MAX(s.year_start) AS year_max,
            GROUP_CONCAT(DISTINCT sc.club_raw SEPARATOR '||') AS clubs
        FROM players p
        JOIN set_cards sc ON p.player_id = sc.player_id
        JOIN sets s       ON sc.set_id   = s.set_id
        WHERE p.is_non_player = 0
          AND p.canonical_player_id IS NULL
          AND p.last_name  IS NOT NULL AND p.last_name  != ''
          AND p.first_name IS NOT NULL AND p.first_name != ''
          AND s.year_start IS NOT NULL
        GROUP BY p.player_id
    """)
    all_players = cur.fetchall()
    print(f"  Loaded {len(all_players):,} players")

    initials_by_last  = defaultdict(list)
    fullname_by_last  = defaultdict(list)

    for p in all_players:
        p["clubs_set"] = set(
            c for c in (p["clubs"] or "").split("||") if c.strip()
        )
        if is_initial(p["first_name"]):
            initials_by_last[p["last_name"]].append(p)
        else:
            fullname_by_last[p["last_name"]].append(p)

    # Build merge plan — keyed by canonical_id to handle multiple initials
    # mapping to the same full-name player
    merge_plan = {}   # initial player_id → canonical player_id
    by_year    = 0
    by_club    = 0
    skipped    = 0

    for last_name, initials in initials_by_last.items():
        full_players = fullname_by_last.get(last_name, [])
        if not full_players:
            continue

        for ip in initials:
            initial_letter = ip["first_name"].strip().rstrip(".").upper()
            ip_min = ip["year_min"]
            ip_max = ip["year_max"]

            candidates = []
            for fp in full_players:
                if fp["first_name"][0].upper() != initial_letter:
                    continue
                if (ip_max + YEAR_BUFFER >= fp["year_min"] - YEAR_BUFFER and
                        ip_min - YEAR_BUFFER <= fp["year_max"] + YEAR_BUFFER):
                    candidates.append(fp)

            if len(candidates) == 1:
                merge_plan[ip["player_id"]] = candidates[0]["player_id"]
                by_year += 1

            elif len(candidates) == 2:
                matching = [c for c in candidates
                            if clubs_overlap(ip["clubs_set"], c["clubs_set"])]
                if len(matching) == 1:
                    merge_plan[ip["player_id"]] = matching[0]["player_id"]
                    by_club += 1
                else:
                    skipped += 1
            else:
                skipped += 1

    print(f"\n  Merge plan: {len(merge_plan):,} records")
    print(f"    {by_year:,} resolved by year match")
    print(f"    {by_club:,} resolved by club tiebreaker")
    print(f"    {skipped:,} skipped")

    if dry_run:
        print("\nDry run — showing first 20 planned merges:")
        print(f"  {'Initial player_id':>18}  →  canonical player_id")
        print(f"  {'-'*40}")
        for i, (init_id, canon_id) in enumerate(list(merge_plan.items())[:20]):
            print(f"  {init_id:>18}  →  {canon_id}")
        print("\nDry run complete — no changes written.")
        cur.close()
        cur2.close()
        conn.close()
        return

    # Apply merges in batches
    print("\nApplying merges...")
    cards_updated   = 0
    players_updated = 0

    items = list(merge_plan.items())
    batch_size = 500

    for i in range(0, len(items), batch_size):
        batch = items[i:i + batch_size]

        for init_id, canon_id in batch:
            # Re-point set_cards
            cur2.execute("""
                UPDATE set_cards SET player_id = %s WHERE player_id = %s
            """, (canon_id, init_id))
            cards_updated += cur2.rowcount

            # Mark the initial record as redundant
            cur2.execute("""
                UPDATE players SET canonical_player_id = %s WHERE player_id = %s
            """, (canon_id, init_id))
            players_updated += cur2.rowcount

        conn.commit()
        done = min(i + batch_size, len(items))
        print(f"  {done:,} / {len(items):,} processed...")

    print(f"\nComplete.")
    print(f"  set_cards rows re-pointed : {cards_updated:,}")
    print(f"  player records linked     : {players_updated:,}")

    cur.close()
    cur2.close()
    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
