"""
analyse_tier2_dedup.py
----------------------
Analyses how many initial-only players (e.g. "T. Finney") can be
confidently matched to a full-name player (e.g. "Tom Finney") using:

  Round 1 — Year overlap (±1 year)
             If exactly 1 full-name candidate exists → MERGE
             If 2 candidates → try club tiebreaker (Round 2)
             If 3+ candidates → SKIP (too ambiguous)

  Round 2 — Club tiebreaker (for 2-candidate cases)
             If exactly 1 candidate shares a club → MERGE
             Otherwise → SKIP

Results are printed by category with examples so you can spot-check
before building the actual dedup script.

Run from the project root:
    python analyse_tier2_dedup.py
"""

import mysql.connector
from collections import defaultdict

DB_CONFIG = dict(
    host="127.0.0.1",
    port=3306,
    user="sc_loader",
    password="Gator888",
    database="soccer_checklist_db",
    charset="utf8mb4",
)

YEAR_BUFFER = 1   # ±1 year either side


def is_initial(name: str) -> bool:
    """True if name is a single letter optionally followed by a period."""
    clean = name.strip().rstrip(".")
    return len(clean) == 1 and clean.isalpha()


def clubs_overlap(clubs_a: set, clubs_b: set) -> bool:
    """
    True if any club in A partially matches any club in B.
    Uses simple substring matching to handle 'Arsenal' vs 'Arsenal F.C.' etc.
    """
    for a in clubs_a:
        a_lower = a.lower().strip()
        for b in clubs_b:
            b_lower = b.lower().strip()
            if len(a_lower) >= 4 and len(b_lower) >= 4:
                if a_lower in b_lower or b_lower in a_lower:
                    return True
    return False


def main():
    conn = mysql.connector.connect(**DB_CONFIG)
    cur  = conn.cursor(dictionary=True)

    print("Loading players...")

    # Load all non-redundant, non-non-player players with year ranges and clubs
    cur.execute("""
        SELECT
            p.player_id,
            p.first_name,
            p.last_name,
            MIN(s.year_start) AS year_min,
            MAX(s.year_start) AS year_max,
            GROUP_CONCAT(DISTINCT sc.club_raw SEPARATOR '||') AS clubs
        FROM players p
        JOIN set_cards sc ON p.player_id = sc.player_id
        JOIN sets s       ON sc.set_id   = s.set_id
        WHERE p.is_non_player = 0
          AND p.canonical_player_id IS NULL
          AND p.last_name  IS NOT NULL AND p.last_name  != ''
          AND p.first_name IS NOT NULL AND p.first_name != ''
          AND s.year_start IS NOT NULL
        GROUP BY p.player_id
    """)
    all_players = cur.fetchall()
    print(f"  Loaded {len(all_players):,} players with set appearances")

    # Split into initials vs full names, index by last_name
    initials_by_last  = defaultdict(list)   # last_name → [player rows]
    fullname_by_last  = defaultdict(list)

    for p in all_players:
        p["clubs_set"] = set(
            c for c in (p["clubs"] or "").split("||") if c.strip()
        )
        if is_initial(p["first_name"]):
            initials_by_last[p["last_name"]].append(p)
        else:
            fullname_by_last[p["last_name"]].append(p)

    print(f"  Initial-only players: {sum(len(v) for v in initials_by_last.values()):,}")
    print(f"  Full-name players:    {sum(len(v) for v in fullname_by_last.values()):,}")

    # ── Match each initial player to candidates ───────────────────────────────
    results = {
        "merge_year":        [],   # 1 candidate after year filter
        "merge_club":        [],   # 2 candidates, club breaks tie
        "skip_ambiguous":    [],   # 3+ year-matched candidates
        "skip_club_tied":    [],   # 2 candidates, club doesn't resolve
        "skip_no_candidate": [],   # 0 year-matched candidates
    }

    for last_name, initials in initials_by_last.items():
        full_players = fullname_by_last.get(last_name, [])
        if not full_players:
            continue

        for ip in initials:
            initial_letter = ip["first_name"].strip().rstrip(".").upper()
            ip_min = ip["year_min"]
            ip_max = ip["year_max"]

            # Find candidates: same last_name, same first letter, year overlap ±buffer
            candidates = []
            for fp in full_players:
                if not fp["first_name"][0].upper() == initial_letter:
                    continue
                fp_min = fp["year_min"]
                fp_max = fp["year_max"]
                # Year overlap with buffer
                if (ip_max + YEAR_BUFFER >= fp_min - YEAR_BUFFER and
                        ip_min - YEAR_BUFFER <= fp_max + YEAR_BUFFER):
                    candidates.append(fp)

            if len(candidates) == 0:
                results["skip_no_candidate"].append((ip, candidates))

            elif len(candidates) == 1:
                results["merge_year"].append((ip, candidates))

            elif len(candidates) == 2:
                # Club tiebreaker
                matching = [c for c in candidates
                            if clubs_overlap(ip["clubs_set"], c["clubs_set"])]
                if len(matching) == 1:
                    results["merge_club"].append((ip, [matching[0]]))
                else:
                    results["skip_club_tied"].append((ip, candidates))

            else:  # 3+
                results["skip_ambiguous"].append((ip, candidates))

    # ── Report ────────────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("TIER 2 ANALYSIS RESULTS")
    print("=" * 70)

    def show(label, key, show_n=8):
        items = results[key]
        print(f"\n── {label} ({len(items):,} players) {'─'*(40-len(label))}")
        for ip, candidates in items[:show_n]:
            cand_str = ", ".join(
                f"{c['first_name']} {c['last_name']} "
                f"[{c['year_min']}-{c['year_max']}]"
                for c in candidates
            )
            clubs_preview = ", ".join(list(ip["clubs_set"])[:3]) or "no club data"
            print(f"  {ip['first_name']:4} {ip['last_name']:<20} "
                  f"[{ip['year_min']}-{ip['year_max']}]  →  {cand_str}")
            if key in ("merge_club", "skip_club_tied"):
                print(f"       clubs: {clubs_preview}")
        if len(items) > show_n:
            print(f"  ... and {len(items) - show_n} more")

    show("MERGE — year match only (1 candidate)",   "merge_year")
    show("MERGE — club tiebreaker (2→1 candidate)", "merge_club")
    show("SKIP  — club tied (2 candidates, no club resolution)", "skip_club_tied")
    show("SKIP  — too ambiguous (3+ candidates)",   "skip_ambiguous")
    show("SKIP  — no year-matched candidate",       "skip_no_candidate")

    print("\n── SUMMARY " + "─" * 58)
    total_merge = len(results["merge_year"]) + len(results["merge_club"])
    total_skip  = (len(results["skip_ambiguous"]) +
                   len(results["skip_club_tied"]) +
                   len(results["skip_no_candidate"]))
    print(f"  Would merge : {total_merge:,}  "
          f"({len(results['merge_year']):,} year-only + "
          f"{len(results['merge_club']):,} club tiebreaker)")
    print(f"  Would skip  : {total_skip:,}  "
          f"({len(results['skip_ambiguous']):,} ambiguous + "
          f"{len(results['skip_club_tied']):,} club tied + "
          f"{len(results['skip_no_candidate']):,} no candidate)")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
