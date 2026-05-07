"""
analyse_countries.py
--------------------
Shows all distinct values in sets.country with their set counts,
so we can build an accurate normalization mapping.

Run from the project root:
    python analyse_countries.py
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
    cur  = conn.cursor()

    cur.execute("""
        SELECT country, COUNT(*) AS cnt
        FROM sets
        WHERE country IS NOT NULL AND country != ''
        GROUP BY country
        ORDER BY cnt DESC, country
    """)
    rows = cur.fetchall()

    print(f"{'Count':>6}  Country value")
    print("-" * 50)
    for country, cnt in rows:
        print(f"{cnt:>6}  {country}")

    print(f"\n{len(rows)} distinct values across {sum(r[1] for r in rows)} sets.")

    cur.execute("SELECT COUNT(*) FROM sets WHERE country IS NULL OR country = ''")
    null_count = cur.fetchone()[0]
    print(f"{null_count} sets have no country value.")

    cur.close()
    conn.close()

if __name__ == "__main__":
    main()
