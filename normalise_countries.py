"""
normalise_countries.py
----------------------
Cleans up the sets.country field:
  - Normalises known variants to canonical names
  - NULLs out values that are clearly not countries
    (publisher names, catalog refs, dates, etc.)
  - Leaves ambiguous multi-country values alone (flagged for manual review)

Run from the project root:
    python normalise_countries.py --dry-run    # preview changes
    python normalise_countries.py              # apply changes
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

# ── Normalise these to canonical country names ────────────────────────────────
NORMALISE = {
    "Holland":                  "Netherlands",
    "The Netherlands":          "Netherlands",
    "U.K.":                     "UK",
    "U.S.A.":                   "USA",
    "Republic of Ireland":      "Ireland",
}

# ── NULL these out — clearly not a country ────────────────────────────────────
NULL_VALUES = {
    # Garbage / unknown
    "?", "? plates", "? supplements", "? adverts (1 football)",
    "? known", "? prints", "? rosettes", "? scraps",
    "-", "- 29-01-1939 - #1002", "- issue no. 81, dated 31 March, 1923",
    ", Duisburg", "A", "N.B.", "Neil", "Anonymous", "eBay",
    "(30212-01)", "(T905-350) Radiovoetbalspel",
    "ball", "ball-Kampfbilder", "ll Clubs (Paper Shields)",
    "Unkbown number",
    "Issue No.  -\nDate  -  Free Gift  -  Detail  -  Original CSGB Reference",

    # Date / update strings
    "Updated (3 September, 2013)",
    "UPDATE (04-09-2020 11:56):", "UPDATE (06-12-2019 19:31):",
    "UPDATE (15-06-2017 22:41):", "UPDATE (23-12-2022 08:04):",
    "UPDATE (30-11-2024 01:06):",

    # Catalog / reference codes
    "DAI-180-3-2a/b / DAA-2 Footballers (SPORTFOTO)",
    "SPO-130 / DAA-2 Footballers (Sportfoto)",
    "PAC-040-2/PAC-1-1 Footballers - Unnumbered - B&W",
    "BPM-14.1 The New Wonder Books",
    "Name normal spacing; All text in serifs, 'Sportfoto' in title case",
    "TOP-200-1c/TOA-16-2-32 - Footballers - Large (B&W) -  Irish - caption at centre",
    "F1394 Italiaanische Volkespelen (Dutch)",
    "F1399 Jeux Populaires (French)",
    "g / OK Kaugummi / BBB Ballon-Bon-Bon",
    "de Voetbalkampioenen",
    "Sport - Serie O",

    # Publisher / magazine names
    "D.C. Thomson / Adventure",
    "D.C. Thomson / Adventure / The Rover / The Hotspur / The Wizard",
    "D.C. Thomson / Adventure / The Rover / The Skipper / The Wizard",
    "D.C. Thomson / The Wizard",
    "Thompson, D.C. / Vanguard",
    "Chocolat Tobler",
    "OK Kaugummi / B.B.B. Ballon-Bon-Bon",
    "Chocolates y Bombones El Tirolés S.L.",
    "Bruguera",
    "Excitement - Perry Colour Books Ltd",
    "Raphael Tuck & Sons",
    "Wills",
    "Cadet Sweets",
    "D. Buchner & Co.",
    "Editorial Tiket",
    "Football & Sports Favourite",
    "Marcus",
    "Portola Schokoladefabrik",
    "The Magnet Library",
    "Weekly Record (Scottish)",
    "Zepter Zigarettenfabrik, Dresden",
    "Topical Times",
    "Topical Times - 23 May, 1922 - Noted Football-Cricketers",
    "Topical Times - 30 January, 1926 - Cup-Tie Keepers",
    "A. Americana",
    "Aplin & Barrett Ltd. / St. Ivel Cheese",
    "Aurelia Zigarettenfabrik (Dresden)",
    "Barry / Creme de Perolas de Barry",
    "Black & White magazine, Manchester",
    "Boys' Magazine",
    "British American Tobacco Co.",
    "Chada S.A. (Chicles Tabay)",
    "Chicles y Regaliz \"Orsay\"",
    "Chix Confectionery Co. Ltd.",
    "Chocolat L'Aiglon / Club",
    "Chocolate Eduardo Pi",
    "Chocolate Jaime Boix",
    "Chums",
    "Clarnico (Clarke, Nicholls & Coombs) / H. Poppleton & Sons Ltd.",
    "Cohen, Weenen & Co.",
    "Cornelius Penaat Tee-Import / Penaaten-Tee",
    "Cromwell High Class Beste Tyrkiske",
    "D. Buchner & Co.",
    "Daily Citizen",
    "Deportivas Salas",
    "Didasco, Milano",
    "El Legionario y Los Novios - with EL LEGIONARIO printed on the front - backs in red",
    "Fachring-zentrale Bilderdienst, Osnabrück",
    "Footballers (Coloured, with inset portrait) - 1914-15",
    "Foster, Brighton & Hove",
    "Gerard Scully",
    "Godfrey Phillips",
    "Haribo",
    "J.A. Pattreiouex",
    "John Levitt",
    "John Sinclair Ltd.",
    "Julian Ayuso",
    "Knorr",
    "La Mascota",
    "Liam Devlin & Sons.",
    "Liverpool Courier",
    "Liverpool Weekly Courier",
    "Luis Garcia Fayos / Cirugía Ortopedia, Valencia",
    "Marks & Spencer",
    "Martin Bennett",
    "Merrysweets Ltd / Soccer Bubble Gum",
    "Mundial Futebol 1958",
    "Napro Productions",
    "Neil Hawkins",
    "Nestlé, Peter, Cailler, Kohler",
    "Phillips Ltd.",
    "Pinkerton Tobacco Co.",
    "Pluck",
    "Prärie-Serier & Vilda Västerns",
    "Presented by Scottish Daily Express - Bold, Normal",
    "Preston's, Bolton",
    "Putney Furnishing Company / Mr. J.H. Custance",
    "Rattler / Dazzler comics",
    "Reuter / Pildoritas de Reuter",
    "Rostrons, New Wortley",
    "Seix & Barral Hermanos / Chocolates Sultana y Americano",
    "Série 4 - Football ~ Les Avants",
    "Sheet 2 - 20 from the following",
    "Šonda Čokolada /",
    "Sport & Adventure",
    "Sports Budget",
    "Sports Pictures & Football Mirror",
    "Stålmannen (Superman) comic",
    "Star magazine",
    "Team Tab",
    "Thanks to Trevor Cotterell",
    "The Boxer's Who's Who",
    "The Boy's Own Paper",
    "The Boys' Friend",
    "The Cup-Final Teams (1925-26) - Manchester City F.C.",
    "The Rover",
    "The World's Greatest Fights",
    "University of Leicester",
    "Vicente Galindo",
    "Volume Anneés 1931-1932",
    "by Town F.C. / Grimsby Evening Telegraph",
    "itt & Son, Upper Norwood",
    "Park Ground - Sunderland v",
    "England International Team 1957",
    "Boys' Magazine",
    "Pluck",
    "Chums",
    "Sport - Serie O",
}

# ── Leave these as-is (multi-country or genuinely ambiguous) ──────────────────
# USA/UK, UK USA, UK USA Canada, UK & New Zealand, UK Denmark,
# France Algeria Tunisia, France Germany Italy etc.,
# Netherlands United Kingdom, Canary Islands Spain, Trinidad British West Indies
# These are flagged below for visibility but not touched.

LEAVE_ALONE = {
    "USA/UK", "UK, USA", "UK, USA, Canada", "UK & New Zealand",
    "UK, Denmark", "France, Algeria, Tunisia", "France, Germany, Italy, etc.",
    "Netherlands, United Kingdom", "Canary Islands, Spain",
    "Trinidad, British West Indies",
}


def main(dry_run: bool):
    conn = mysql.connector.connect(**DB_CONFIG)
    cur  = conn.cursor(dictionary=True)

    cur2 = conn.cursor()

    normalised_count = 0
    nulled_count     = 0

    # Fetch all distinct non-null country values
    cur.execute("""
        SELECT country, COUNT(*) AS cnt
        FROM sets
        WHERE country IS NOT NULL AND country != ''
        GROUP BY country
        ORDER BY cnt DESC, country
    """)
    rows = cur.fetchall()

    print(f"{'ACTION':<12} {'SETS':>5}  VALUE")
    print("-" * 70)

    for row in rows:
        val = row["country"]
        cnt = row["cnt"]

        if val in NORMALISE:
            new_val = NORMALISE[val]
            print(f"{'NORMALISE':<12} {cnt:>5}  '{val}'  →  '{new_val}'")
            if not dry_run:
                cur2.execute(
                    "UPDATE sets SET country = %s WHERE country = %s",
                    (new_val, val)
                )
            normalised_count += cnt

        elif val in NULL_VALUES:
            print(f"{'NULL OUT':<12} {cnt:>5}  '{val}'")
            if not dry_run:
                cur2.execute(
                    "UPDATE sets SET country = NULL WHERE country = %s",
                    (val,)
                )
            nulled_count += cnt

        elif val in LEAVE_ALONE:
            print(f"{'LEAVE ALONE':<12} {cnt:>5}  '{val}'")

        else:
            print(f"{'KEEP':<12} {cnt:>5}  '{val}'")

    print()
    print(f"  Would normalise : {normalised_count} sets")
    print(f"  Would NULL out  : {nulled_count} sets")

    if dry_run:
        print("\nDry run — no changes written.")
    else:
        conn.commit()
        print("\nCommitted to database.")

    cur.close()
    cur2.close()
    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview changes without writing to the database")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
