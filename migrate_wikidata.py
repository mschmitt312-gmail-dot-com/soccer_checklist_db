"""
Migration: create the two Wikidata-lookup cache tables.

  - player_wikidata_candidates : one row per (player_id, qid)
  - player_wikidata_lookups    : one row per lookup attempt

Both are pure caches — no audit history, no triggers — so this migration
does not touch the players table or its triggers. The eventual
players.wikidata_qid column will be added in a later (Phase 3) migration
when we start writing resolved Q-IDs back onto the live row.

Run from the project root:
    python migrate_wikidata.py
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "webapp"))

from database import query, execute


def _table_exists(name: str) -> bool:
    row = query(
        "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
        (name,), one=True,
    )
    return row is not None


CREATE_CANDIDATES = """
CREATE TABLE player_wikidata_candidates (
    candidate_id     INT AUTO_INCREMENT PRIMARY KEY,
    player_id        INT          NOT NULL,
    qid              VARCHAR(20)  NOT NULL,
    label_en         VARCHAR(255) NULL,
    description_en   VARCHAR(500) NULL,
    given_name       VARCHAR(255) NULL,
    family_name      VARCHAR(255) NULL,
    aliases_json     JSON         NULL,
    date_of_birth    DATE         NULL,
    birth_year       SMALLINT     NULL,
    nationality      VARCHAR(100) NULL,
    nationality_qid  VARCHAR(20)  NULL,
    birth_place      VARCHAR(255) NULL,
    clubs_json       JSON         NULL,
    sparql_strategy  VARCHAR(50)  NULL,
    similarity_score DECIMAL(4,3) NULL,
    fetched_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    UNIQUE KEY uq_pwc_player_qid (player_id, qid),
    KEY idx_pwc_player_id (player_id),
    KEY idx_pwc_qid       (qid),
    KEY idx_pwc_score     (similarity_score),
    CONSTRAINT fk_pwc_player_id
        FOREIGN KEY (player_id) REFERENCES players(player_id) ON DELETE CASCADE
)
"""


CREATE_LOOKUPS = """
CREATE TABLE player_wikidata_lookups (
    lookup_id        INT AUTO_INCREMENT PRIMARY KEY,
    player_id        INT          NOT NULL,
    strategy         VARCHAR(50)  NOT NULL,
    candidates_found INT          NOT NULL DEFAULT 0,
    succeeded        TINYINT(1)   NOT NULL DEFAULT 1,
    error_msg        TEXT         NULL,
    duration_ms      INT          NULL,
    attempted_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    KEY idx_pwl_player_id    (player_id),
    KEY idx_pwl_attempted_at (attempted_at),
    CONSTRAINT fk_pwl_player_id
        FOREIGN KEY (player_id) REFERENCES players(player_id) ON DELETE CASCADE
)
"""


def run():
    if _table_exists("player_wikidata_candidates"):
        print("player_wikidata_candidates already exists — skipping.")
    else:
        execute(CREATE_CANDIDATES)
        print("Created table player_wikidata_candidates.")

    if _table_exists("player_wikidata_lookups"):
        print("player_wikidata_lookups already exists — skipping.")
    else:
        execute(CREATE_LOOKUPS)
        print("Created table player_wikidata_lookups.")

    print("\nMigration complete.")
    print("Next: run wikidata_lookup_players.py to populate the cache.")


if __name__ == "__main__":
    run()
