"""
Migration: add birth_place and photo_url columns to the players table.

Run from the project root:
    python migrate_player_metadata.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "webapp"))

from database import query, execute


def run():
    # ── players table ──────────────────────────────────────────────────────────
    existing = {row["Field"] for row in query("SHOW COLUMNS FROM players")}

    if "birth_place" not in existing:
        execute("""
            ALTER TABLE players
            ADD COLUMN birth_place VARCHAR(255) NULL
            AFTER birth_year
        """)
        print("Added birth_place column to players.")
    else:
        print("birth_place already exists — skipping.")

    if "photo_url" not in existing:
        execute("""
            ALTER TABLE players
            ADD COLUMN photo_url VARCHAR(2048) NULL
            AFTER birth_place
        """)
        print("Added photo_url column to players.")
    else:
        print("photo_url already exists — skipping.")

    # ── players_history table ──────────────────────────────────────────────────
    hist_existing = {row["Field"] for row in query("SHOW COLUMNS FROM players_history")}

    if "birth_place" not in hist_existing:
        execute("""
            ALTER TABLE players_history
            ADD COLUMN birth_place VARCHAR(255) NULL
            AFTER birth_year
        """)
        print("Added birth_place column to players_history.")
    else:
        print("players_history.birth_place already exists — skipping.")

    if "photo_url" not in hist_existing:
        execute("""
            ALTER TABLE players_history
            ADD COLUMN photo_url VARCHAR(2048) NULL
            AFTER birth_place
        """)
        print("Added photo_url column to players_history.")
    else:
        print("players_history.photo_url already exists — skipping.")

    # ── Rebuild triggers to capture new columns ────────────────────────────────
    # Creating triggers requires either SUPER privilege or log_bin_trust_function_creators=1.
    # Try to enable it for this session; if that fails, print manual SQL and continue.

    UPDATE_TRIGGER = """
CREATE TRIGGER trg_players_before_update
BEFORE UPDATE ON players
FOR EACH ROW BEGIN
    IF NEW.date_of_birth IS NOT NULL THEN
        SET NEW.birth_year = YEAR(NEW.date_of_birth);
    END IF;
    INSERT INTO players_history (
        player_id, name_raw, display_name, first_name, last_name,
        nationality, date_of_birth, birth_year, birth_place, photo_url,
        canonical_player_id, is_non_player, changed_by
    ) VALUES (
        OLD.player_id, OLD.name_raw, OLD.display_name, OLD.first_name, OLD.last_name,
        OLD.nationality, OLD.date_of_birth, OLD.birth_year, OLD.birth_place, OLD.photo_url,
        OLD.canonical_player_id, OLD.is_non_player, 'migration'
    );
END"""

    DELETE_TRIGGER = """
CREATE TRIGGER trg_players_before_delete
BEFORE DELETE ON players
FOR EACH ROW BEGIN
    INSERT INTO players_history (
        player_id, name_raw, display_name, first_name, last_name,
        nationality, date_of_birth, birth_year, birth_place, photo_url,
        canonical_player_id, is_non_player, changed_by
    ) VALUES (
        OLD.player_id, OLD.name_raw, OLD.display_name, OLD.first_name, OLD.last_name,
        OLD.nationality, OLD.date_of_birth, OLD.birth_year, OLD.birth_place, OLD.photo_url,
        OLD.canonical_player_id, OLD.is_non_player, 'deleted'
    );
END"""

    try:
        execute("SET GLOBAL log_bin_trust_function_creators = 1")
    except Exception:
        pass  # May not have the privilege; attempt trigger creation anyway

    try:
        execute("DROP TRIGGER IF EXISTS trg_players_before_update")
        execute(UPDATE_TRIGGER)
        execute("DROP TRIGGER IF EXISTS trg_players_before_delete")
        execute(DELETE_TRIGGER)
        print("Rebuilt both audit triggers successfully.")
    except Exception as e:
        print(f"\nCould not rebuild triggers automatically ({e}).")
        print("The new columns are fully in place — the app will work fine.")
        print("To rebuild the audit triggers manually, run these in your MySQL client:")
        print("\nDELIMITER //")
        print("DROP TRIGGER IF EXISTS trg_players_before_update //")
        print(UPDATE_TRIGGER + " //")
        print("DROP TRIGGER IF EXISTS trg_players_before_delete //")
        print(DELETE_TRIGGER + " //")
        print("DELIMITER ;")
        print("SET GLOBAL log_bin_trust_function_creators = 0; -- optional: restore")

    print("\nMigration complete. Columns are ready; app can be (re)started now.")


if __name__ == "__main__":
    run()
