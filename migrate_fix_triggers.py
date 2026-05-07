"""
migrate_fix_triggers.py
-----------------------
Reads each history trigger's current body from MySQL, replaces the hardcoded
'migration' string with @current_user, and writes fix_triggers.sql.

Run from the project root:
    python migrate_fix_triggers.py

Then open fix_triggers.sql in MySQL Workbench and run it as root (or any
account with SUPER privilege / log_bin_trust_function_creators = 1).
"""

import sys
import mysql.connector

sys.path.insert(0, "webapp")
from config import DB_CONFIG

TRIGGERS = [
    ("trg_sets_before_update",      "BEFORE UPDATE", "sets"),
    ("trg_sets_before_delete",      "BEFORE DELETE", "sets"),
    ("trg_players_before_update",   "BEFORE UPDATE", "players"),
    ("trg_players_before_delete",   "BEFORE DELETE", "players"),
    ("trg_set_cards_before_update", "BEFORE UPDATE", "set_cards"),
    ("trg_set_cards_before_delete", "BEFORE DELETE", "set_cards"),
    ("trg_images_before_update",    "BEFORE UPDATE", "images"),
    ("trg_images_before_delete",    "BEFORE DELETE", "images"),
    ("trg_notes_before_update",     "BEFORE UPDATE", "notes"),
    ("trg_notes_before_delete",     "BEFORE DELETE", "notes"),
]

def main():
    conn = mysql.connector.connect(**DB_CONFIG)
    cur = conn.cursor(dictionary=True)

    sql_lines = [
        "-- fix_triggers.sql",
        "-- Run this in MySQL Workbench as root to fix all history triggers.",
        "--",
        "SET GLOBAL log_bin_trust_function_creators = 1;",
        "",
    ]

    needs_fix = 0

    for trigger_name, timing_event, table_name in TRIGGERS:
        cur.execute("""
            SELECT ACTION_STATEMENT
            FROM INFORMATION_SCHEMA.TRIGGERS
            WHERE TRIGGER_SCHEMA = %s AND TRIGGER_NAME = %s
        """, (DB_CONFIG["database"], trigger_name))
        row = cur.fetchone()

        if not row:
            print(f"  SKIP  {trigger_name} — not found in DB")
            continue

        body = row["ACTION_STATEMENT"]

        if "'migration'" not in body:
            print(f"  OK    {trigger_name} — already correct, skipping")
            continue

        new_body = body.replace("'migration'", "@current_user")

        sql_lines += [
            f"-- Fix {trigger_name}",
            f"DROP TRIGGER IF EXISTS `{trigger_name}`;",
            f"DELIMITER $$",
            f"CREATE TRIGGER `{trigger_name}` {timing_event} ON `{table_name}` FOR EACH ROW",
            new_body + "$$",
            f"DELIMITER ;",
            "",
        ]
        print(f"  QUEUED {trigger_name} for fix")
        needs_fix += 1

    sql_lines += [
        "SET GLOBAL log_bin_trust_function_creators = 0;",
        "",
    ]

    cur.close()
    conn.close()

    if needs_fix == 0:
        print("\nAll triggers already correct — nothing to do.")
        return

    out_path = "fix_triggers.sql"
    with open(out_path, "w") as f:
        f.write("\n".join(sql_lines))

    print(f"\nWrote {out_path} — {needs_fix} trigger(s) to fix.")
    print("Open fix_triggers.sql in MySQL Workbench and run it as root.")

if __name__ == "__main__":
    main()
