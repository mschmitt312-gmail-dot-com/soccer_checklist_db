"""
analyse_players.py
------------------
Analyses the scale of player duplication across three tiers:

  Tier 1 — Exact full name match (same first_name + last_name, both non-null)
            Safest to merge automatically.

  Tier 2 — Same last_name + same first initial, different first_name spellings
            e.g. "Tom Finney" and "T. Finney" — likely same person, needs care.

  Tier 3 — Same last_name only (multiple players possible)
            Too risky to auto-merge; shown for awareness only.

Run from the project root:
    python analyse_players.py
"""

import mysql.connector

DB_CONFIG = dict(
    host="127.0.0.1",
    port=3306,
    user="sc_loader",
    password="Gator888",
    database="soccer_checklist_db",
    charset="utf8mb4",
)

def main():
    conn = mysql.connector.connect(**DB_CONFIG)
    cur  = conn.cursor(dictionary=True)

    print("=" * 70)
    print("PLAYER DEDUPLICATION ANALYSIS")
    print("=" * 70)

    # Overall stats
    cur.execute("SELECT COUNT(*) AS cnt FROM players WHERE is_non_player = 0")
    total = cur.fetchone()["cnt"]
    print(f"\nTotal players (non-players excluded): {total:,}")

    # ── Tier 1: Exact full name duplicates ───────────────────────────────────
    print("\n── TIER 1: Exact full name duplicates (first_name + last_name) ─────")
    cur.execute("""
        SELECT first_name, last_name, COUNT(*) AS cnt,
               GROUP_CONCAT(player_id ORDER BY player_id SEPARATOR ', ') AS ids
        FROM players
        WHERE is_non_player = 0
          AND first_name IS NOT NULL AND first_name != ''
          AND last_name  IS NOT NULL AND last_name  != ''
          AND first_name NOT LIKE '%.%'   -- exclude initials-only first names
          AND LENGTH(first_name) > 1
        GROUP BY first_name, last_name
        HAVING COUNT(*) > 1
        ORDER BY cnt DESC, last_name
        LIMIT 30
    """)
    rows = cur.fetchall()

    cur.execute("""
        SELECT COUNT(*) AS grp_count,
               SUM(cnt - 1) AS duplicate_records
        FROM (
            SELECT COUNT(*) AS cnt
            FROM players
            WHERE is_non_player = 0
              AND first_name IS NOT NULL AND first_name != ''
              AND last_name  IS NOT NULL AND last_name  != ''
              AND first_name NOT LIKE '%.%'
              AND LENGTH(first_name) > 1
            GROUP BY first_name, last_name
            HAVING COUNT(*) > 1
        ) t
    """)
    t1_summary = cur.fetchone()
    print(f"  {t1_summary['grp_count']:,} duplicate name groups → "
          f"{t1_summary['duplicate_records']:,} redundant records")
    print(f"\n  Top examples:")
    print(f"  {'Count':>5}  {'First name':<20} {'Last name':<25} Player IDs")
    print(f"  {'-'*75}")
    for r in rows:
        ids_preview = r["ids"] if len(r["ids"]) < 40 else r["ids"][:37] + "..."
        print(f"  {r['cnt']:>5}  {r['first_name']:<20} {r['last_name']:<25} {ids_preview}")

    # ── Tier 2: Same last name + same first initial, different spellings ──────
    print("\n── TIER 2: Same last_name + first initial, different spellings ──────")
    cur.execute("""
        SELECT last_name,
               LEFT(first_name, 1)  AS initial,
               COUNT(*)             AS cnt,
               GROUP_CONCAT(DISTINCT first_name ORDER BY first_name SEPARATOR ' | ') AS names
        FROM players
        WHERE is_non_player = 0
          AND first_name IS NOT NULL AND first_name != ''
          AND last_name  IS NOT NULL AND last_name  != ''
          AND LENGTH(first_name) > 1
        GROUP BY last_name, LEFT(first_name, 1)
        HAVING COUNT(*) > 1
           AND COUNT(DISTINCT first_name) > 1
        ORDER BY cnt DESC, last_name
        LIMIT 30
    """)
    rows = cur.fetchall()

    cur.execute("""
        SELECT COUNT(*) AS grp_count, SUM(cnt - 1) AS potential_dupes
        FROM (
            SELECT COUNT(*) AS cnt
            FROM players
            WHERE is_non_player = 0
              AND first_name IS NOT NULL AND first_name != ''
              AND last_name  IS NOT NULL AND last_name  != ''
              AND LENGTH(first_name) > 1
            GROUP BY last_name, LEFT(first_name, 1)
            HAVING COUNT(*) > 1 AND COUNT(DISTINCT first_name) > 1
        ) t
    """)
    t2_summary = cur.fetchone()
    print(f"  {t2_summary['grp_count']:,} potential groups → "
          f"up to {t2_summary['potential_dupes']:,} potential duplicates")
    print(f"  (Note: not all are duplicates — same initial ≠ same person)\n")
    print(f"  {'Count':>5}  {'Last name':<25} {'First names found'}")
    print(f"  {'-'*70}")
    for r in rows:
        names_preview = r["names"] if len(r["names"]) < 40 else r["names"][:37] + "..."
        print(f"  {r['cnt']:>5}  {r['last_name']:<25} {names_preview}")

    # ── Tier 3: Same last name only ───────────────────────────────────────────
    print("\n── TIER 3: Same last_name only (shown for awareness, too risky to auto-merge)")
    cur.execute("""
        SELECT COUNT(*) AS grp_count
        FROM (
            SELECT last_name
            FROM players
            WHERE is_non_player = 0
              AND last_name IS NOT NULL AND last_name != ''
            GROUP BY last_name
            HAVING COUNT(*) > 1
        ) t
    """)
    t3 = cur.fetchone()
    print(f"  {t3['grp_count']:,} last names shared by more than one player record")
    print(f"  (Not actionable without more info — skip for now)")

    # ── Players with only last_name set (no first_name) ──────────────────────
    print("\n── Players with last_name only (first_name is null/empty) ───────────")
    cur.execute("""
        SELECT COUNT(*) AS cnt FROM players
        WHERE is_non_player = 0
          AND (first_name IS NULL OR first_name = '')
          AND last_name IS NOT NULL AND last_name != ''
    """)
    print(f"  {cur.fetchone()['cnt']:,} players have no parsed first name")

    print("\n" + "=" * 70)

    cur.close()
    conn.close()

if __name__ == "__main__":
    main()
