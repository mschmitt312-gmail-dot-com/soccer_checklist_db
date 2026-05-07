"""
link_card_images_test.py
------------------------
Test script: links the existing set-level image from the
"1905 Singleton & Cole Footballers" set to card #1 and card #13
by inserting new card-specific rows in the images table.

Run from the project root:
    python link_card_images_test.py
    python link_card_images_test.py --dry-run
"""

import sys
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

def main(dry_run: bool):
    conn = mysql.connector.connect(**DB_CONFIG)
    cur  = conn.cursor(dictionary=True)

    # 1. Find the set
    cur.execute("""
        SELECT set_id, COALESCE(set_name, og_title) AS set_name
        FROM sets
        WHERE og_title LIKE '%Singleton%Cole%'
           OR set_name LIKE '%Singleton%Cole%'
        LIMIT 5
    """)
    sets = cur.fetchall()
    if not sets:
        print("ERROR: Could not find Singleton & Cole set.")
        sys.exit(1)
    if len(sets) > 1:
        print("Multiple matches — picking the first:")
        for s in sets:
            print(f"  set_id={s['set_id']}  {s['set_name']}")
    the_set = sets[0]
    set_id  = the_set["set_id"]
    print(f"Set: {the_set['set_name']}  (set_id={set_id})")

    # 2. Find the existing set-level image
    cur.execute("""
        SELECT image_id, filename, storage_url, sort_order
        FROM images
        WHERE set_id = %s AND card_id IS NULL
        LIMIT 1
    """, (set_id,))
    img = cur.fetchone()
    if not img:
        print("ERROR: No set-level image found for this set.")
        sys.exit(1)
    print(f"Image: image_id={img['image_id']}  filename={img['filename']}")

    # 3. Find cards #1 and #13
    cur.execute("""
        SELECT card_id, card_number, name_in_set
        FROM set_cards
        WHERE set_id = %s AND card_number IN ('1', '13')
        ORDER BY CAST(card_number AS UNSIGNED)
    """, (set_id,))
    cards = cur.fetchall()
    if not cards:
        print("ERROR: Could not find cards #1 or #13 in this set.")
        sys.exit(1)
    for c in cards:
        print(f"Card #{c['card_number']}: card_id={c['card_id']}  {c['name_in_set']}")

    # 4. Insert card-specific image rows
    for card in cards:
        # Check if a card-specific row already exists to avoid duplicates
        cur.execute("""
            SELECT COUNT(*) AS cnt FROM images
            WHERE card_id = %s AND filename = %s
        """, (card["card_id"], img["filename"]))
        if cur.fetchone()["cnt"] > 0:
            print(f"  [SKIP] Card #{card['card_number']} already has this image linked.")
            continue

        if dry_run:
            print(f"  [DRY RUN] Would insert image row: card_id={card['card_id']} card_number={card['card_number']}")
        else:
            cur.execute("""
                INSERT INTO images (set_id, card_id, filename, storage_url, sort_order)
                VALUES (%s, %s, %s, %s, 0)
            """, (set_id, card["card_id"], img["filename"], img["storage_url"]))
            print(f"  Linked image to card #{card['card_number']} (card_id={card['card_id']})")

    if not dry_run:
        conn.commit()
        print("\nDone — committed to database.")
    else:
        print("\nDry run complete — no changes written.")

    cur.close()
    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
