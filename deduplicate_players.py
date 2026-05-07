"""
deduplicate_players.py
----------------------
Two-step player cleanup:

  Step 1 — Reclassify obvious non-players that slipped through
            (Team Photo, Team Group, album names, etc.)

  Step 2 — Tier 1 deduplication: merge player records with an
            identical first_name + last_name by pointing all
            set_cards at the canonical record (most appearances)
            and setting canonical_player_id on the redundant ones.
            Nothing is deleted — records are only linked.

Run from the project root:
    python deduplicate_players.py --dry-run    # preview
    python deduplicate_players.py              # apply
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

# ── Step 1: Mark these as non-players ────────────────────────────────────────
# Matched on (first_name, last_name) pairs. Add more as discovered.
NON_PLAYER_NAMES = [
    ("Team",          "Photo"),
    ("Team",          "Group"),
    ("Team Group",    ")"),
    ("Balas Futebol", "Album"),
    ("Bandera",       "Profesional"),
]

# ── Step 2: Skip dedup for these names ───────────────────────────────────────
# Names that are genuinely common — two different players could share them.
EXCLUDE_FROM_DEDUP = {
    ("José",  "Luis"),      # very common in Spanish football
    ("Di",    "Stefano"),   # parsed oddly; Alfredo Di Stefano should be verified manually
}


# ─────────────────────────────────────────────────────────────────────────────

def step1_reclassify(conn, dry_run: bool):
    cur = conn.cursor(dictionary=True)
    print("\n── STEP 1: Reclassify non-players ───────────────────────────────────")
    total_updated = 0

    for first, last in NON_PLAYER_NAMES:
        cur.execute("""
            SELECT COUNT(*) AS cnt FROM players
            WHERE first_name = %s AND last_name = %s AND is_non_player = 0
        """, (first, last))
        cnt = cur.fetchone()["cnt"]
        if cnt == 0:
            print(f"  [SKIP]  '{first} {last}' — none found with is_non_player=0")
            continue

        print(f"  {'[DRY RUN]' if dry_run else '[UPDATE]'} "
              f"'{first} {last}' — {cnt} records → is_non_player = 1")
        if not dry_run:
            cur.execute("""
                UPDATE players SET is_non_player = 1
                WHERE first_name = %s AND last_name = %s AND is_non_player = 0
            """, (first, last))
        total_updated += cnt

    print(f"\n  {'Would update' if dry_run else 'Updated'}: {total_updated} records")
    cur.close()


def step2_dedup(conn, dry_run: bool):
    cur = conn.cursor(dictionary=True)
    print("\n── STEP 2: Tier 1 deduplication ─────────────────────────────────────")

    # Find all duplicate groups (exact first+last match, real players only)
    cur.execute("""
        SELECT first_name, last_name, COUNT(*) AS cnt
        FROM players
        WHERE is_non_player = 0
          AND first_name IS NOT NULL AND first_name != ''
          AND last_name  IS NOT NULL AND last_name  != ''
          AND first_name NOT LIKE '%.%'
          AND LENGTH(first_name) > 1
        GROUP BY first_name, last_name
        HAVING COUNT(*) > 1
        ORDER BY cnt DESC, last_name
    """)
    groups = cur.fetchall()

    merged_groups  = 0
    merged_records = 0
    skipped_groups = 0

    for g in groups:
        first = g["first_name"]
        last  = g["last_name"]
        cnt   = g["cnt"]

        if (first, last) in EXCLUDE_FROM_DEDUP:
            print(f"  [EXCLUDE] '{first} {last}' ({cnt} records) — on exclusion list")
            skipped_groups += 1
            continue

        # Fetch all player_ids in this group, ranked by number of card appearances
        cur.execute("""
            SELECT p.player_id,
                   COUNT(sc.card_id) AS appearance_count
            FROM players p
            LEFT JOIN set_cards sc ON p.player_id = sc.player_id
            WHERE p.first_name = %s AND p.last_name = %s AND p.is_non_player = 0
            GROUP BY p.player_id
            ORDER BY appearance_count DESC, p.player_id ASC
        """, (first, last))
        members = cur.fetchall()

        if len(members) < 2:
            continue  # race condition guard

        canonical   = members[0]
        redundant   = members[1:]
        canon_id    = canonical["player_id"]
        redund_ids  = [r["player_id"] for r in redundant]

        print(f"  MERGE '{first} {last}' ({cnt} records) → "
              f"canonical player_id={canon_id} "
              f"({canonical['appearance_count']} appearances), "
              f"merging {redund_ids}")

        if not dry_run:
            # Re-point all set_cards from redundant ids to canonical
            fmt = ",".join(["%s"] * len(redund_ids))
            cur.execute(f"""
                UPDATE set_cards SET player_id = %s
                WHERE player_id IN ({fmt})
            """, [canon_id] + redund_ids)

            # Mark redundant players with canonical_player_id
            cur.execute(f"""
                UPDATE players SET canonical_player_id = %s
                WHERE player_id IN ({fmt})
            """, [canon_id] + redund_ids)

        merged_groups  += 1
        merged_records += len(redund_ids)

    print(f"\n  {'Would merge' if dry_run else 'Merged'}: "
          f"{merged_groups} groups, {merged_records} redundant records linked")
    print(f"  Skipped (exclusion list): {skipped_groups} groups")
    cur.close()


def main(dry_run: bool):
    conn = mysql.connector.connect(**DB_CONFIG)

    step1_reclassify(conn, dry_run)
    step2_dedup(conn, dry_run)

    if not dry_run:
        conn.commit()
        print("\nAll changes committed.")
    else:
        print("\nDry run complete — no changes written.")

    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview changes without writing to the database")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
