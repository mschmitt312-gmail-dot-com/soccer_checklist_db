"""
Migration: create set_links table for storing named URLs relevant to a set.

Run from the project root:
    python migrate_set_links.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "webapp"))

from database import query, execute

def run():
    tables = query("SHOW TABLES LIKE 'set_links'")
    if tables:
        print("Table 'set_links' already exists — skipping CREATE TABLE.")
    else:
        execute("""
            CREATE TABLE set_links (
                link_id    INT AUTO_INCREMENT PRIMARY KEY,
                set_id     INT NOT NULL,
                link_name  VARCHAR(255) NOT NULL,
                link_url   VARCHAR(2048) NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (set_id) REFERENCES sets(set_id) ON DELETE CASCADE
            )
        """)
        print("Created set_links table.")

    print("Migration complete.")

if __name__ == "__main__":
    run()
