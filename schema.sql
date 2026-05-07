-- =============================================================
-- Soccer Checklist Database Schema
-- =============================================================
-- Naming conventions:
--   - Each table's PK is named {table_singular}_id
--   - FK columns use the same name as the PK they reference
--   - Every table has created_at (fixed on insert) and
--     last_updated (auto-refreshed on any change)
--   - Every live table has a paired _history table that stores
--     a full row snapshot before each UPDATE or DELETE
--   - History tables share the same column names as the live
--     table, prefixed with four audit columns:
--       history_id, action, changed_at, changed_by
-- =============================================================


-- -------------------------------------------------------------
-- SETS
-- -------------------------------------------------------------

CREATE TABLE sets (
    set_id          INT AUTO_INCREMENT PRIMARY KEY,
    og_title        VARCHAR(500)  NOT NULL,
    set_name        VARCHAR(500),
    publisher       VARCHAR(255),
    country_raw     VARCHAR(500),                   -- raw value from scraper (may be dirty)
    country         VARCHAR(100),                   -- cleaned country name
    season_raw      VARCHAR(100),                   -- raw season string e.g. "1970-71"
    year_start      SMALLINT,                       -- parsed start year, used for range queries
    year_end        SMALLINT,                       -- NULL if single-year set
    total_cards     INT,                            -- declared card count from source page
    cards_found     INT,                            -- how many cards were actually scraped
    source_url      VARCHAR(1000) NULL,             -- NULL for manually created sets
    created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_updated    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                        ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY uq_sets_source_url (source_url(255)),
    KEY idx_sets_year_start  (year_start),
    KEY idx_sets_publisher   (publisher(100)),
    KEY idx_sets_country     (country),
    FULLTEXT KEY ft_sets_search (og_title, set_name, publisher)
);


CREATE TABLE sets_history (
    history_id      INT AUTO_INCREMENT PRIMARY KEY,
    action          ENUM('UPDATE','DELETE') NOT NULL,
    changed_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    changed_by      VARCHAR(255) NULL,
    -- full snapshot of the sets row before the change
    set_id          INT          NOT NULL,
    og_title        VARCHAR(500),
    set_name        VARCHAR(500),
    publisher       VARCHAR(255),
    country_raw     VARCHAR(500),
    country         VARCHAR(100),
    season_raw      VARCHAR(100),
    year_start      SMALLINT,
    year_end        SMALLINT,
    total_cards     INT,
    cards_found     INT,
    source_url      VARCHAR(1000),
    created_at      DATETIME,
    last_updated    DATETIME,

    KEY idx_sets_history_set_id    (set_id),
    KEY idx_sets_history_changed_at (changed_at)
);


DELIMITER $$

CREATE TRIGGER trg_sets_before_update
BEFORE UPDATE ON sets
FOR EACH ROW
BEGIN
    INSERT INTO sets_history (
        action, changed_at, changed_by,
        set_id, og_title, set_name, publisher,
        country_raw, country, season_raw,
        year_start, year_end, total_cards, cards_found,
        source_url, created_at, last_updated
    ) VALUES (
        'UPDATE', NOW(), @current_user,
        OLD.set_id, OLD.og_title, OLD.set_name, OLD.publisher,
        OLD.country_raw, OLD.country, OLD.season_raw,
        OLD.year_start, OLD.year_end, OLD.total_cards, OLD.cards_found,
        OLD.source_url, OLD.created_at, OLD.last_updated
    );
END$$

CREATE TRIGGER trg_sets_before_delete
BEFORE DELETE ON sets
FOR EACH ROW
BEGIN
    INSERT INTO sets_history (
        action, changed_at, changed_by,
        set_id, og_title, set_name, publisher,
        country_raw, country, season_raw,
        year_start, year_end, total_cards, cards_found,
        source_url, created_at, last_updated
    ) VALUES (
        'DELETE', NOW(), @current_user,
        OLD.set_id, OLD.og_title, OLD.set_name, OLD.publisher,
        OLD.country_raw, OLD.country, OLD.season_raw,
        OLD.year_start, OLD.year_end, OLD.total_cards, OLD.cards_found,
        OLD.source_url, OLD.created_at, OLD.last_updated
    );
END$$

DELIMITER ;


-- -------------------------------------------------------------
-- PLAYERS
-- -------------------------------------------------------------

CREATE TABLE players (
    player_id       INT AUTO_INCREMENT PRIMARY KEY,
    name_raw        VARCHAR(500) NOT NULL,          -- full scraped string e.g. "Brian Clough (Middlesbrough)"
    first_name      VARCHAR(255),                   -- populated by parsing script
    last_name       VARCHAR(255),                   -- populated by parsing script
    display_name    VARCHAR(255),                   -- override for single-name players e.g. "Pele"
    nationality     VARCHAR(100),
    date_of_birth   DATE,                           -- full date when known
    birth_year      SMALLINT,                       -- auto-populated from date_of_birth; fallback when full date unknown
    created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_updated    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                        ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY uq_players_name_raw (name_raw(255)),
    KEY idx_players_last_name  (last_name),
    KEY idx_players_birth_year (birth_year),
    FULLTEXT KEY ft_players_search (name_raw, first_name, last_name, display_name)
);


CREATE TABLE players_history (
    history_id      INT AUTO_INCREMENT PRIMARY KEY,
    action          ENUM('UPDATE','DELETE') NOT NULL,
    changed_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    changed_by      VARCHAR(255) NULL,
    -- full snapshot of the players row before the change
    player_id       INT          NOT NULL,
    name_raw        VARCHAR(500),
    first_name      VARCHAR(255),
    last_name       VARCHAR(255),
    display_name    VARCHAR(255),
    nationality     VARCHAR(100),
    date_of_birth   DATE,
    birth_year      SMALLINT,
    created_at      DATETIME,
    last_updated    DATETIME,

    KEY idx_players_history_player_id  (player_id),
    KEY idx_players_history_changed_at (changed_at)
);


DELIMITER $$

-- Auto-populate birth_year from date_of_birth on INSERT
CREATE TRIGGER trg_players_before_insert
BEFORE INSERT ON players
FOR EACH ROW
BEGIN
    IF NEW.date_of_birth IS NOT NULL AND NEW.birth_year IS NULL THEN
        SET NEW.birth_year = YEAR(NEW.date_of_birth);
    END IF;
END$$

-- Keep birth_year in sync when date_of_birth is updated
CREATE TRIGGER trg_players_before_update
BEFORE UPDATE ON players
FOR EACH ROW
BEGIN
    -- Sync birth_year if date_of_birth changes
    IF NEW.date_of_birth IS NOT NULL THEN
        SET NEW.birth_year = YEAR(NEW.date_of_birth);
    END IF;
    -- Record history snapshot
    INSERT INTO players_history (
        action, changed_at, changed_by,
        player_id, name_raw, first_name, last_name, display_name,
        nationality, date_of_birth, birth_year,
        created_at, last_updated
    ) VALUES (
        'UPDATE', NOW(), @current_user,
        OLD.player_id, OLD.name_raw, OLD.first_name, OLD.last_name, OLD.display_name,
        OLD.nationality, OLD.date_of_birth, OLD.birth_year,
        OLD.created_at, OLD.last_updated
    );
END$$

CREATE TRIGGER trg_players_before_delete
BEFORE DELETE ON players
FOR EACH ROW
BEGIN
    INSERT INTO players_history (
        action, changed_at, changed_by,
        player_id, name_raw, first_name, last_name, display_name,
        nationality, date_of_birth, birth_year,
        created_at, last_updated
    ) VALUES (
        'DELETE', NOW(), @current_user,
        OLD.player_id, OLD.name_raw, OLD.first_name, OLD.last_name, OLD.display_name,
        OLD.nationality, OLD.date_of_birth, OLD.birth_year,
        OLD.created_at, OLD.last_updated
    );
END$$

DELIMITER ;


-- -------------------------------------------------------------
-- SET_CARDS  (junction: sets <-> players)
-- -------------------------------------------------------------

CREATE TABLE set_cards (
    card_id         INT AUTO_INCREMENT PRIMARY KEY,
    set_id          INT          NOT NULL,
    player_id       INT          NOT NULL,
    card_number     INT          NULL,              -- NULL for sets that don't use card numbers
    name_in_set     VARCHAR(500),                   -- raw name exactly as scraped; never changes
    confirmed       TINYINT(1)   NOT NULL DEFAULT 1,
    created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_updated    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                        ON UPDATE CURRENT_TIMESTAMP,

    KEY idx_set_cards_set_id    (set_id),
    KEY idx_set_cards_player_id (player_id),
    KEY idx_set_cards_card_number (card_number),
    CONSTRAINT fk_set_cards_set_id
        FOREIGN KEY (set_id)    REFERENCES sets(set_id)       ON DELETE CASCADE,
    CONSTRAINT fk_set_cards_player_id
        FOREIGN KEY (player_id) REFERENCES players(player_id)
);


CREATE TABLE set_cards_history (
    history_id      INT AUTO_INCREMENT PRIMARY KEY,
    action          ENUM('UPDATE','DELETE') NOT NULL,
    changed_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    changed_by      VARCHAR(255) NULL,
    -- full snapshot of the set_cards row before the change
    card_id         INT          NOT NULL,
    set_id          INT          NOT NULL,
    player_id       INT          NOT NULL,
    card_number     INT,
    name_in_set     VARCHAR(500),
    confirmed       TINYINT(1),
    created_at      DATETIME,
    last_updated    DATETIME,

    KEY idx_set_cards_history_card_id    (card_id),
    KEY idx_set_cards_history_set_id     (set_id),
    KEY idx_set_cards_history_changed_at (changed_at)
);


DELIMITER $$

CREATE TRIGGER trg_set_cards_before_update
BEFORE UPDATE ON set_cards
FOR EACH ROW
BEGIN
    INSERT INTO set_cards_history (
        action, changed_at, changed_by,
        card_id, set_id, player_id, card_number,
        name_in_set, confirmed, created_at, last_updated
    ) VALUES (
        'UPDATE', NOW(), @current_user,
        OLD.card_id, OLD.set_id, OLD.player_id, OLD.card_number,
        OLD.name_in_set, OLD.confirmed, OLD.created_at, OLD.last_updated
    );
END$$

CREATE TRIGGER trg_set_cards_before_delete
BEFORE DELETE ON set_cards
FOR EACH ROW
BEGIN
    INSERT INTO set_cards_history (
        action, changed_at, changed_by,
        card_id, set_id, player_id, card_number,
        name_in_set, confirmed, created_at, last_updated
    ) VALUES (
        'DELETE', NOW(), @current_user,
        OLD.card_id, OLD.set_id, OLD.player_id, OLD.card_number,
        OLD.name_in_set, OLD.confirmed, OLD.created_at, OLD.last_updated
    );
END$$

DELIMITER ;


-- -------------------------------------------------------------
-- IMAGES
-- -------------------------------------------------------------

CREATE TABLE images (
    image_id        INT AUTO_INCREMENT PRIMARY KEY,
    set_id          INT          NULL,              -- FK to sets  (one of set_id / card_id must be set)
    card_id         INT          NULL,              -- FK to set_cards
    filename        VARCHAR(500) NOT NULL,          -- original filename from scrape
    storage_url     VARCHAR(1000) NULL,             -- Azure Blob Storage URL; NULL until uploaded
    sort_order      SMALLINT     NOT NULL DEFAULT 0,
    created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_updated    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                        ON UPDATE CURRENT_TIMESTAMP,

    CONSTRAINT chk_images_parent
        CHECK (set_id IS NOT NULL OR card_id IS NOT NULL),
    KEY idx_images_set_id  (set_id),
    KEY idx_images_card_id (card_id),
    CONSTRAINT fk_images_set_id
        FOREIGN KEY (set_id)  REFERENCES sets(set_id)           ON DELETE CASCADE,
    CONSTRAINT fk_images_card_id
        FOREIGN KEY (card_id) REFERENCES set_cards(card_id)     ON DELETE CASCADE
);


CREATE TABLE images_history (
    history_id      INT AUTO_INCREMENT PRIMARY KEY,
    action          ENUM('UPDATE','DELETE') NOT NULL,
    changed_at      DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    changed_by      VARCHAR(255)  NULL,
    -- full snapshot of the images row before the change
    image_id        INT           NOT NULL,
    set_id          INT,
    card_id         INT,
    filename        VARCHAR(500),
    storage_url     VARCHAR(1000),
    sort_order      SMALLINT,
    created_at      DATETIME,
    last_updated    DATETIME,

    KEY idx_images_history_image_id   (image_id),
    KEY idx_images_history_set_id     (set_id),
    KEY idx_images_history_changed_at (changed_at)
);


DELIMITER $$

CREATE TRIGGER trg_images_before_update
BEFORE UPDATE ON images
FOR EACH ROW
BEGIN
    INSERT INTO images_history (
        action, changed_at, changed_by,
        image_id, set_id, card_id, filename,
        storage_url, sort_order, created_at, last_updated
    ) VALUES (
        'UPDATE', NOW(), @current_user,
        OLD.image_id, OLD.set_id, OLD.card_id, OLD.filename,
        OLD.storage_url, OLD.sort_order, OLD.created_at, OLD.last_updated
    );
END$$

CREATE TRIGGER trg_images_before_delete
BEFORE DELETE ON images
FOR EACH ROW
BEGIN
    INSERT INTO images_history (
        action, changed_at, changed_by,
        image_id, set_id, card_id, filename,
        storage_url, sort_order, created_at, last_updated
    ) VALUES (
        'DELETE', NOW(), @current_user,
        OLD.image_id, OLD.set_id, OLD.card_id, OLD.filename,
        OLD.storage_url, OLD.sort_order, OLD.created_at, OLD.last_updated
    );
END$$

DELIMITER ;


-- -------------------------------------------------------------
-- NOTES
-- -------------------------------------------------------------

CREATE TABLE notes (
    note_id         INT AUTO_INCREMENT PRIMARY KEY,
    set_id          INT          NULL,              -- FK to sets
    player_id       INT          NULL,              -- FK to players
    card_id         INT          NULL,              -- FK to set_cards
    image_id        INT          NULL,              -- FK to images
    note_text       TEXT         NOT NULL,
    note_source     ENUM('scraped','manual') NOT NULL DEFAULT 'manual',
    created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_updated    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                        ON UPDATE CURRENT_TIMESTAMP,

    CONSTRAINT chk_notes_parent
        CHECK (set_id IS NOT NULL OR player_id IS NOT NULL
               OR card_id IS NOT NULL OR image_id IS NOT NULL),
    KEY idx_notes_set_id    (set_id),
    KEY idx_notes_player_id (player_id),
    KEY idx_notes_card_id   (card_id),
    KEY idx_notes_image_id  (image_id),
    CONSTRAINT fk_notes_set_id
        FOREIGN KEY (set_id)    REFERENCES sets(set_id)             ON DELETE CASCADE,
    CONSTRAINT fk_notes_player_id
        FOREIGN KEY (player_id) REFERENCES players(player_id)       ON DELETE CASCADE,
    CONSTRAINT fk_notes_card_id
        FOREIGN KEY (card_id)   REFERENCES set_cards(card_id)       ON DELETE CASCADE,
    CONSTRAINT fk_notes_image_id
        FOREIGN KEY (image_id)  REFERENCES images(image_id)         ON DELETE CASCADE
);


CREATE TABLE notes_history (
    history_id      INT AUTO_INCREMENT PRIMARY KEY,
    action          ENUM('UPDATE','DELETE') NOT NULL,
    changed_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    changed_by      VARCHAR(255) NULL,
    -- full snapshot of the notes row before the change
    note_id         INT          NOT NULL,
    set_id          INT,
    player_id       INT,
    card_id         INT,
    image_id        INT,
    note_text       TEXT,
    note_source     ENUM('scraped','manual'),
    created_at      DATETIME,
    last_updated    DATETIME,

    KEY idx_notes_history_note_id    (note_id),
    KEY idx_notes_history_set_id     (set_id),
    KEY idx_notes_history_changed_at (changed_at)
);


DELIMITER $$

CREATE TRIGGER trg_notes_before_update
BEFORE UPDATE ON notes
FOR EACH ROW
BEGIN
    INSERT INTO notes_history (
        action, changed_at, changed_by,
        note_id, set_id, player_id, card_id, image_id,
        note_text, note_source, created_at, last_updated
    ) VALUES (
        'UPDATE', NOW(), @current_user,
        OLD.note_id, OLD.set_id, OLD.player_id, OLD.card_id, OLD.image_id,
        OLD.note_text, OLD.note_source, OLD.created_at, OLD.last_updated
    );
END$$

CREATE TRIGGER trg_notes_before_delete
BEFORE DELETE ON notes
FOR EACH ROW
BEGIN
    INSERT INTO notes_history (
        action, changed_at, changed_by,
        note_id, set_id, player_id, card_id, image_id,
        note_text, note_source, created_at, last_updated
    ) VALUES (
        'DELETE', NOW(), @current_user,
        OLD.note_id, OLD.set_id, OLD.player_id, OLD.card_id, OLD.image_id,
        OLD.note_text, OLD.note_source, OLD.created_at, OLD.last_updated
    );
END$$

DELIMITER ;


-- =============================================================
-- USAGE NOTE: changed_by
-- =============================================================
-- The @current_user session variable is read by all triggers to
-- record who made the change. The application layer should set
-- this before any UPDATE or DELETE, for example:
--
--   SET @current_user = 'mike';
--   UPDATE sets SET country = 'Brazil' WHERE set_id = 42;
--
-- If @current_user is never set, changed_by is stored as NULL.
-- =============================================================
