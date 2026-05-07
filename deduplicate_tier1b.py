"""
deduplicate_tier1b.py
---------------------
Cleans up two remaining duplicate patterns missed by the earlier scripts:

  Pass A — Exact initial duplicates
            e.g. "W. Meredith" × 6 → keep the one with most appearances
            (Tier 1 excluded initials; Tier 2 only merged initials INTO
            full names, not initials into each other)

  Pass B — Null first_name duplicates
            e.g. (NULL, "Meredith") × 3 → keep one, with year-overlap
            check to avoid merging genuinely different players

Run from the project root:
    python deduplicate_tier1b.py --dry-run
    python deduplicate_tier1b.py
"""

import argparse
import mysql.connector

DB_CONFIG = dict(
    host="127.0.0.1",
    port=3306,
    user="sc_loader",
    password="Gator888",
    database="soccer_checklist_db",
    charset="utf8mb4",
)

YEAR_BUFFER = 2   # for null first_name matching, be slightly more generous


def main(dry_run: bool):
    conn = mysql.connector.connect(**DB_CONFIG)
    cur  = conn.cursor(dictionary=True)
    cur2 = conn.cursor()

    pass_a_groups    = 0
    pass_a_redundant = 0
    pass_b_groups    = 0
    pass_b_redundant = 0

    # ── Pass A: exact initial duplicates ─────────────────────────────────────
    print("── Pass A: exact initial duplicates (same first initial + last name) ─")

    cur.execute("""
        SELECT first_name, last_name, COUNT(*) AS cnt
        FROM players
        WHERE is_non_player = 0
          AND canonical_player_id IS NULL
          AND first_name IS NOT NULL AND first_name != ''
          AND (
              first_name REGEXP '^[A-Za-z]\\.$'    -- "W."
              OR (LENGTH(first_name) = 1 AND first_name REGEXP '^[A-Za-z]$')  -- "W"
          )
          AND last_name IS NOT NULL AND last_name != ''
        GROUP BY first_name, last_name
        HAVING COUNT(*) > 1
        ORDER BY cnt DESC, last_name
    """)
    groups = cur.fetchall()
    print(f"  Found {len(groups):,} duplicate initial groups")

    for g in groups:
        first = g["first_name"]
        last  = g["last_name"]

        # Rank by number of card appearances; lowest player_id breaks ties
        cur.execute("""
            SELECT p.player_id, COUNT(sc.card_id) AS cnt
            FROM players p
            LEFT JOIN set_cards sc ON p.player_id = sc.player_id
            WHERE p.first_name = %s AND p.last_name = %s
              AND p.is_non_player = 0 AND p.canonical_player_id IS NULL
            GROUP BY p.player_id
            ORDER BY cnt DESC, p.player_id ASC
        """, (first, last))
        members = cur.fetchall()

        if len(members) < 2:
            continue

        canon_id   = members[0]["player_id"]
        redund_ids = [m["player_id"] for m in members[1:]]

        if dry_run:
            print(f"  [DRY RUN] '{first} {last}' ({len(members)}) "
                  f"→ canonical={canon_id}, merge={redund_ids}")
        else:
            fmt = ",".join(["%s"] * len(redund_ids))
            cur2.execute(
                f"UPDATE set_cards SET player_id = %s WHERE player_id IN ({fmt})",
                [canon_id] + redund_ids
            )
            cur2.execute(
                f"UPDATE players SET canonical_player_id = %s WHERE player_id IN ({fmt})",
                [canon_id] + redund_ids
            )

        pass_a_groups    += 1
        pass_a_redundant += len(redund_ids)

    print(f"  {'Would merge' if dry_run else 'Merged'}: "
          f"{pass_a_groups} groups, {pass_a_redundant} redundant records")

    # ── Pass B: null first_name duplicates ───────────────────────────────────
    print("\n── Pass B: null first_name duplicates (last name only) ──────────────")

    cur.execute("""
        SELECT last_name, COUNT(*) AS cnt
        FROM players
        WHERE is_non_player = 0
          AND canonical_player_id IS NULL
          AND (first_name IS NULL OR first_name = '')
          AND last_name IS NOT NULL AND last_name != ''
        GROUP BY last_name
        HAVING COUNT(*) > 1
        ORDER BY cnt DESC, last_name
    """)
    groups = cur.fetchall()
    print(f"  Found {len(groups):,} duplicate last-name-only groups")

    for g in groups:
        last = g["last_name"]

        # Get year ranges per player to check overlap
        cur.execute("""
            SELECT p.player_id,
                   COUNT(sc.card_id)  AS app_count,
                   MIN(s.year_start)  AS year_min,
                   MAX(s.year_start)  AS year_max
            FROM players p
            LEFT JOIN set_cards sc ON p.player_id = sc.player_id
            LEFT JOIN sets s       ON sc.set_id   = s.set_id
            WHERE p.last_name = %s
              AND (p.first_name IS NULL OR p.first_name = '')
              AND p.is_non_player = 0
              AND p.canonical_player_id IS NULL
            GROUP BY p.player_id
            ORDER BY app_count DESC, p.player_id ASC
        """, (last,))
        members = cur.fetchall()

        if len(members) < 2:
            continue

        # Only merge members whose year range overlaps with the canonical (±buffer)
        # Members with no year data are merged conservatively (into canonical)
        canonical = members[0]
        canon_min = canonical["year_min"]
        canon_max = canonical["year_max"]

        redund_ids = []
        for m in members[1:]:
            m_min = m["year_min"]
            m_max = m["year_max"]

            # If either has no year data, merge anyway (can't tell apart)
            if canon_min is None or m_min is None:
                redund_ids.append(m["player_id"])
            elif (canon_max + YEAR_BUFFER >= m_min - YEAR_BUFFER and
                  canon_min - YEAR_BUFFER <= m_max + YEAR_BUFFER):
                redund_ids.append(m["player_id"])
            # else: year ranges too far apart — skip (different players)

        if not redund_ids:
            continue

        if dry_run:
            print(f"  [DRY RUN] '{last}' ({len(members)}) "
                  f"→ canonical={canonical['player_id']}, merge={redund_ids}")
        else:
            fmt = ",".join(["%s"] * len(redund_ids))
            cur2.execute(
                f"UPDATE set_cards SET player_id = %s WHERE player_id IN ({fmt})",
                [canonical["player_id"]] + redund_ids
            )
            cur2.execute(
                f"UPDATE players SET canonical_player_id = %s WHERE player_id IN ({fmt})",
                [canonical["player_id"]] + redund_ids
            )

        pass_b_groups    += 1
        pass_b_redundant += len(redund_ids)

    print(f"  {'Would merge' if dry_run else 'Merged'}: "
          f"{pass_b_groups} groups, {pass_b_redundant} redundant records")

    if not dry_run:
        conn.commit()
        print(f"\nAll changes committed.")
    else:
        print(f"\nDry run complete — no changes written.")

    cur.close()
    cur2.close()
    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
