"""
migrate_player_photo_position.py
---------------------------------
Adds photo_position column to the players table.

Run from the project root:
    python migrate_player_photo_position.py
"""

import sys
import mysql.connector

sys.path.insert(0, "webapp")
from config import DB_CONFIG

def main():
    conn = mysql.connector.connect(**DB_CONFIG)
    cur = conn.cursor()

    # Check if column already exists
    cur.execute("""
        SELECT COUNT(*) FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name   = 'players'
          AND column_name  = 'photo_position'
    """)
    exists = cur.fetchone()[0]

    if exists:
        print("photo_position column already exists — nothing to do.")
    else:
        cur.execute("""
            ALTER TABLE players
            ADD COLUMN photo_position VARCHAR(20) DEFAULT NULL
            AFTER photo_url
        """)
        conn.commit()
        print("Added photo_position column to players table.")

    cur.close()
    conn.close()

if __name__ == "__main__":
    main()
