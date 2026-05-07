"""
migrate_player_links.py
-----------------------
Creates the player_links table.

Run from the project root:
    python migrate_player_links.py
"""

import sys
import mysql.connector

sys.path.insert(0, "webapp")
from config import DB_CONFIG

def main():
    conn = mysql.connector.connect(**DB_CONFIG)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS player_links (
            link_id    INT AUTO_INCREMENT PRIMARY KEY,
            player_id  INT NOT NULL,
            link_name  VARCHAR(255) NOT NULL,
            link_url   VARCHAR(2048) NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (player_id) REFERENCES players(player_id) ON DELETE CASCADE
        )
    """)
    conn.commit()
    print("player_links table created (or already exists).")
    cur.close()
    conn.close()

if __name__ == "__main__":
    main()
