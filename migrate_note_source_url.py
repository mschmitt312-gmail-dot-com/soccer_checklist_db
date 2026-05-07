"""
Migration: add source_url column to notes table and pre-populate
scraped notes with the corresponding set's source_url.

Run from the project root:
    python migrate_note_source_url.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "webapp"))

from database import query, execute

def run():
    # 1. Add the column (safe to re-run — checks first)
    cols = query("SHOW COLUMNS FROM notes LIKE 'source_url'")
    if cols:
        print("Column 'source_url' already exists on notes — skipping ALTER TABLE.")
    else:
        execute("ALTER TABLE notes ADD COLUMN source_url VARCHAR(2048) NULL AFTER note_source")
        print("Added source_url column to notes.")

    # 2. Pre-populate scraped notes with the set's source_url
    from mysql.connector import pooling
    from config import DB_CONFIG
    import mysql.connector

    conn = mysql.connector.connect(**DB_CONFIG)
    cur = conn.cursor()
    cur.execute("""
        UPDATE notes n
        JOIN sets s ON n.set_id = s.set_id
        SET n.source_url = s.source_url
        WHERE n.note_source = 'scraped'
          AND s.source_url IS NOT NULL
          AND n.source_url IS NULL
    """)
    conn.commit()
    migrated = cur.rowcount
    cur.close()
    conn.close()

    print(f"Pre-populated {migrated} scraped note(s) with their set's source URL.")
    print("Migration complete. Notes and sets now have independent source URLs.")

if __name__ == "__main__":
    run()
