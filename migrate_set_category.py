"""
Migration: add category column to sets table.
Default value 'football'; allowed values: 'football', 'other'.

Run from the project root:
    python migrate_set_category.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "webapp"))

from database import query, execute

def run():
    cols = query("SHOW COLUMNS FROM sets LIKE 'category'")
    if cols:
        print("Column 'category' already exists on sets — nothing to do.")
        return

    execute("""
        ALTER TABLE sets
        ADD COLUMN category ENUM('football','other') NOT NULL DEFAULT 'football'
        AFTER country
    """)
    print("Added category column — all existing sets defaulted to 'football'.")

    count = (query("SELECT COUNT(*) AS n FROM sets WHERE category = 'football'", one=True) or {}).get("n", 0)
    print(f"{count} sets now categorised as 'football'.")

if __name__ == "__main__":
    run()
