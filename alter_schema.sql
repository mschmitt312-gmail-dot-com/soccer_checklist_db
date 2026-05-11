-- =============================================================
-- alter_schema.sql
-- Phase 1 schema additions:
--   1. countries  -- ISO 3166 reference table
--   2. clubs      -- club reference table
--   3. set_cards  -- add club_raw, club_id, country_id columns
--   4. players    -- add canonical_player_id for Phase 2 deduplication
--
-- Safe to run against an existing populated database.
-- All new columns are nullable so existing rows are unaffected.
-- Run this BEFORE parse_players.py.
-- =============================================================


-- -------------------------------------------------------------
-- 1. COUNTRIES  (seeded from ISO 3166-1)
-- -------------------------------------------------------------

CREATE TABLE IF NOT EXISTS countries (
    country_id      INT AUTO_INCREMENT PRIMARY KEY,
    country_code    CHAR(3)      NOT NULL,             -- ISO 3166-1 alpha-2 e.g. "GB"; CHAR(3) to accommodate home nations (ENG/SCO/WAL/NIR) and historical codes
    country_name    VARCHAR(100) NOT NULL,             -- canonical English name
    also_known_as   VARCHAR(500) NULL,                 -- comma-separated aliases for matching
    created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_updated    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                        ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY uq_countries_code (country_code),
    UNIQUE KEY uq_countries_name (country_name),
    KEY idx_countries_name (country_name)
);


CREATE TABLE IF NOT EXISTS countries_history (
    history_id      INT AUTO_INCREMENT PRIMARY KEY,
    action          ENUM('UPDATE','DELETE') NOT NULL,
    changed_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    changed_by      VARCHAR(255) NULL,
    country_id      INT          NOT NULL,
    country_code    CHAR(3),
    country_name    VARCHAR(100),
    also_known_as   VARCHAR(500),
    created_at      DATETIME,
    last_updated    DATETIME,

    KEY idx_countries_history_id         (country_id),
    KEY idx_countries_history_changed_at (changed_at)
);


DELIMITER $$

CREATE TRIGGER trg_countries_before_update
BEFORE UPDATE ON countries
FOR EACH ROW
BEGIN
    INSERT INTO countries_history (
        action, changed_at, changed_by,
        country_id, country_code, country_name, also_known_as,
        created_at, last_updated
    ) VALUES (
        'UPDATE', NOW(), @current_user,
        OLD.country_id, OLD.country_code, OLD.country_name, OLD.also_known_as,
        OLD.created_at, OLD.last_updated
    );
END$$

CREATE TRIGGER trg_countries_before_delete
BEFORE DELETE ON countries
FOR EACH ROW
BEGIN
    INSERT INTO countries_history (
        action, changed_at, changed_by,
        country_id, country_code, country_name, also_known_as,
        created_at, last_updated
    ) VALUES (
        'DELETE', NOW(), @current_user,
        OLD.country_id, OLD.country_code, OLD.country_name, OLD.also_known_as,
        OLD.created_at, OLD.last_updated
    );
END$$

DELIMITER ;


-- -------------------------------------------------------------
-- 2. CLUBS
-- -------------------------------------------------------------

CREATE TABLE IF NOT EXISTS clubs (
    club_id         INT AUTO_INCREMENT PRIMARY KEY,
    club_name       VARCHAR(255) NOT NULL,             -- canonical name e.g. "West Bromwich Albion"
    country_id      INT          NULL,                 -- FK to countries
    also_known_as   VARCHAR(500) NULL,                 -- comma-separated aliases e.g. "W.B.A.,West Brom"
    created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_updated    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                        ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY uq_clubs_name (club_name),
    KEY idx_clubs_country_id (country_id),
    KEY idx_clubs_name       (club_name),
    CONSTRAINT fk_clubs_country_id
        FOREIGN KEY (country_id) REFERENCES countries(country_id)
);


CREATE TABLE IF NOT EXISTS clubs_history (
    history_id      INT AUTO_INCREMENT PRIMARY KEY,
    action          ENUM('UPDATE','DELETE') NOT NULL,
    changed_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    changed_by      VARCHAR(255) NULL,
    club_id         INT          NOT NULL,
    club_name       VARCHAR(255),
    country_id      INT,
    also_known_as   VARCHAR(500),
    created_at      DATETIME,
    last_updated    DATETIME,

    KEY idx_clubs_history_club_id    (club_id),
    KEY idx_clubs_history_changed_at (changed_at)
);


DELIMITER $$

CREATE TRIGGER trg_clubs_before_update
BEFORE UPDATE ON clubs
FOR EACH ROW
BEGIN
    INSERT INTO clubs_history (
        action, changed_at, changed_by,
        club_id, club_name, country_id, also_known_as,
        created_at, last_updated
    ) VALUES (
        'UPDATE', NOW(), @current_user,
        OLD.club_id, OLD.club_name, OLD.country_id, OLD.also_known_as,
        OLD.created_at, OLD.last_updated
    );
END$$

CREATE TRIGGER trg_clubs_before_delete
BEFORE DELETE ON clubs
FOR EACH ROW
BEGIN
    INSERT INTO clubs_history (
        action, changed_at, changed_by,
        club_id, club_name, country_id, also_known_as,
        created_at, last_updated
    ) VALUES (
        'DELETE', NOW(), @current_user,
        OLD.club_id, OLD.club_name, OLD.country_id, OLD.also_known_as,
        OLD.created_at, OLD.last_updated
    );
END$$

DELIMITER ;


-- -------------------------------------------------------------
-- 3. SET_CARDS  -- add club and country affiliation columns
-- -------------------------------------------------------------

ALTER TABLE set_cards
    ADD COLUMN club_raw     VARCHAR(500) NULL
        COMMENT 'Raw parenthetical text as scraped e.g. "Charlton Athletic and England"'
        AFTER name_in_set,
    ADD COLUMN club_id      INT          NULL
        COMMENT 'FK to clubs -- populated by parse_players.py'
        AFTER club_raw,
    ADD COLUMN country_id   INT          NULL
        COMMENT 'FK to countries -- when parenthetical references a country/national team'
        AFTER club_id;

ALTER TABLE set_cards
    ADD CONSTRAINT fk_set_cards_club_id
        FOREIGN KEY (club_id)    REFERENCES clubs(club_id),
    ADD CONSTRAINT fk_set_cards_country_id
        FOREIGN KEY (country_id) REFERENCES countries(country_id);

ALTER TABLE set_cards
    ADD KEY idx_set_cards_club_id    (club_id),
    ADD KEY idx_set_cards_country_id (country_id);


-- Mirror the new columns in the history table
ALTER TABLE set_cards_history
    ADD COLUMN club_raw   VARCHAR(500) NULL AFTER name_in_set,
    ADD COLUMN club_id    INT          NULL AFTER club_raw,
    ADD COLUMN country_id INT          NULL AFTER club_id;


-- Update the set_cards history trigger to capture the new columns
DROP TRIGGER IF EXISTS trg_set_cards_before_update;
DROP TRIGGER IF EXISTS trg_set_cards_before_delete;

DELIMITER $$

CREATE TRIGGER trg_set_cards_before_update
BEFORE UPDATE ON set_cards
FOR EACH ROW
BEGIN
    INSERT INTO set_cards_history (
        action, changed_at, changed_by,
        card_id, set_id, player_id, card_number,
        name_in_set, club_raw, club_id, country_id,
        confirmed, created_at, last_updated
    ) VALUES (
        'UPDATE', NOW(), @current_user,
        OLD.card_id, OLD.set_id, OLD.player_id, OLD.card_number,
        OLD.name_in_set, OLD.club_raw, OLD.club_id, OLD.country_id,
        OLD.confirmed, OLD.created_at, OLD.last_updated
    );
END$$

CREATE TRIGGER trg_set_cards_before_delete
BEFORE DELETE ON set_cards
FOR EACH ROW
BEGIN
    INSERT INTO set_cards_history (
        action, changed_at, changed_by,
        card_id, set_id, player_id, card_number,
        name_in_set, club_raw, club_id, country_id,
        confirmed, created_at, last_updated
    ) VALUES (
        'DELETE', NOW(), @current_user,
        OLD.card_id, OLD.set_id, OLD.player_id, OLD.card_number,
        OLD.name_in_set, OLD.club_raw, OLD.club_id, OLD.country_id,
        OLD.confirmed, OLD.created_at, OLD.last_updated
    );
END$$

DELIMITER ;


-- -------------------------------------------------------------
-- 4. PLAYERS  -- add canonical_player_id for Phase 2 dedup
-- -------------------------------------------------------------

ALTER TABLE players
    ADD COLUMN canonical_player_id INT NULL
        COMMENT 'Points to the master player record. NULL = this IS the canonical record.'
        AFTER display_name,
    ADD COLUMN is_non_player TINYINT(1) NOT NULL DEFAULT 0
        COMMENT '1 = entry is a sport/team/category label, not an individual player'
        AFTER canonical_player_id;

ALTER TABLE players
    ADD CONSTRAINT fk_players_canonical
        FOREIGN KEY (canonical_player_id) REFERENCES players(player_id);

ALTER TABLE players
    ADD KEY idx_players_canonical_id (canonical_player_id),
    ADD KEY idx_players_is_non_player (is_non_player);


-- Mirror new columns in players history table
ALTER TABLE players_history
    ADD COLUMN canonical_player_id INT          NULL AFTER display_name,
    ADD COLUMN is_non_player       TINYINT(1)   NULL AFTER canonical_player_id;


-- Update players history triggers to capture new columns
DROP TRIGGER IF EXISTS trg_players_before_update;
DROP TRIGGER IF EXISTS trg_players_before_delete;

DELIMITER $$

CREATE TRIGGER trg_players_before_update
BEFORE UPDATE ON players
FOR EACH ROW
BEGIN
    IF NEW.date_of_birth IS NOT NULL THEN
        SET NEW.birth_year = YEAR(NEW.date_of_birth);
    END IF;
    INSERT INTO players_history (
        action, changed_at, changed_by,
        player_id, name_raw, first_name, last_name, display_name,
        canonical_player_id, is_non_player,
        nationality, date_of_birth, birth_year,
        created_at, last_updated
    ) VALUES (
        'UPDATE', NOW(), @current_user,
        OLD.player_id, OLD.name_raw, OLD.first_name, OLD.last_name, OLD.display_name,
        OLD.canonical_player_id, OLD.is_non_player,
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
        canonical_player_id, is_non_player,
        nationality, date_of_birth, birth_year,
        created_at, last_updated
    ) VALUES (
        'DELETE', NOW(), @current_user,
        OLD.player_id, OLD.name_raw, OLD.first_name, OLD.last_name, OLD.display_name,
        OLD.canonical_player_id, OLD.is_non_player,
        OLD.nationality, OLD.date_of_birth, OLD.birth_year,
        OLD.created_at, OLD.last_updated
    );
END$$

DELIMITER ;


-- -------------------------------------------------------------
-- 5. ISO 3166-1 COUNTRY SEED DATA
-- -------------------------------------------------------------
-- Includes common aliases in also_known_as to help the matching
-- script map scraped country strings to canonical records.
-- Add more aliases as you encounter them during parsing.
-- -------------------------------------------------------------

INSERT INTO countries (country_code, country_name, also_known_as) VALUES
('AF', 'Afghanistan', NULL),
('AL', 'Albania', NULL),
('DZ', 'Algeria', NULL),
('AD', 'Andorra', NULL),
('AO', 'Angola', NULL),
('AG', 'Antigua and Barbuda', NULL),
('AR', 'Argentina', NULL),
('AM', 'Armenia', NULL),
('AU', 'Australia', NULL),
('AT', 'Austria', 'Österreich'),
('AZ', 'Azerbaijan', NULL),
('BS', 'Bahamas', NULL),
('BH', 'Bahrain', NULL),
('BD', 'Bangladesh', NULL),
('BB', 'Barbados', NULL),
('BY', 'Belarus', NULL),
('BE', 'Belgium', 'Belgique,Belgien'),
('BZ', 'Belize', NULL),
('BJ', 'Benin', NULL),
('BT', 'Bhutan', NULL),
('BO', 'Bolivia', NULL),
('BA', 'Bosnia and Herzegovina', 'Bosnia,Herzegovina'),
('BW', 'Botswana', NULL),
('BR', 'Brazil', 'Brasil,Brésil'),
('BN', 'Brunei', NULL),
('BG', 'Bulgaria', NULL),
('BF', 'Burkina Faso', 'Upper Volta'),
('BI', 'Burundi', NULL),
('CV', 'Cape Verde', NULL),
('KH', 'Cambodia', NULL),
('CM', 'Cameroon', NULL),
('CA', 'Canada', NULL),
('CF', 'Central African Republic', NULL),
('TD', 'Chad', NULL),
('CL', 'Chile', NULL),
('CN', 'China', 'PRC,Peoples Republic of China'),
('CO', 'Colombia', NULL),
('KM', 'Comoros', NULL),
('CG', 'Congo', NULL),
('CD', 'DR Congo', 'Zaire,Belgian Congo'),
('CR', 'Costa Rica', NULL),
('HR', 'Croatia', 'Hrvatska'),
('CU', 'Cuba', NULL),
('CY', 'Cyprus', NULL),
('CZ', 'Czech Republic', 'Czechoslovakia,Czechia'),
('DK', 'Denmark', 'Danmark'),
('DJ', 'Djibouti', NULL),
('DM', 'Dominica', NULL),
('DO', 'Dominican Republic', NULL),
('EC', 'Ecuador', NULL),
('EG', 'Egypt', NULL),
('SV', 'El Salvador', NULL),
('GQ', 'Equatorial Guinea', NULL),
('ER', 'Eritrea', NULL),
('EE', 'Estonia', NULL),
('SZ', 'Eswatini', 'Swaziland'),
('ET', 'Ethiopia', NULL),
('FJ', 'Fiji', NULL),
('FI', 'Finland', 'Suomi'),
('FR', 'France', NULL),
('GA', 'Gabon', NULL),
('GM', 'Gambia', NULL),
('GE', 'Georgia', NULL),
('DE', 'Germany', 'West Germany,East Germany,Deutschland,Federal Republic of Germany,German Democratic Republic'),
('GH', 'Ghana', 'Gold Coast'),
('GR', 'Greece', 'Hellas'),
('GD', 'Grenada', NULL),
('GT', 'Guatemala', NULL),
('GN', 'Guinea', NULL),
('GW', 'Guinea-Bissau', NULL),
('GY', 'Guyana', 'British Guiana'),
('HT', 'Haiti', NULL),
('HN', 'Honduras', NULL),
('HU', 'Hungary', 'Magyarország'),
('IS', 'Iceland', NULL),
('IN', 'India', NULL),
('ID', 'Indonesia', NULL),
('IR', 'Iran', 'Persia'),
('IQ', 'Iraq', NULL),
('IE', 'Ireland', 'Republic of Ireland,Eire'),
('IL', 'Israel', NULL),
('IT', 'Italy', 'Italia'),
('JM', 'Jamaica', NULL),
('JP', 'Japan', NULL),
('JO', 'Jordan', NULL),
('KZ', 'Kazakhstan', NULL),
('KE', 'Kenya', NULL),
('KI', 'Kiribati', NULL),
('KP', 'North Korea', NULL),
('KR', 'South Korea', NULL),
('KW', 'Kuwait', NULL),
('KG', 'Kyrgyzstan', NULL),
('LA', 'Laos', NULL),
('LV', 'Latvia', NULL),
('LB', 'Lebanon', NULL),
('LS', 'Lesotho', NULL),
('LR', 'Liberia', NULL),
('LY', 'Libya', NULL),
('LI', 'Liechtenstein', NULL),
('LT', 'Lithuania', NULL),
('LU', 'Luxembourg', NULL),
('MG', 'Madagascar', NULL),
('MW', 'Malawi', NULL),
('MY', 'Malaysia', NULL),
('MV', 'Maldives', NULL),
('ML', 'Mali', NULL),
('MT', 'Malta', NULL),
('MH', 'Marshall Islands', NULL),
('MR', 'Mauritania', NULL),
('MU', 'Mauritius', NULL),
('MX', 'Mexico', NULL),
('FM', 'Micronesia', NULL),
('MD', 'Moldova', NULL),
('MC', 'Monaco', NULL),
('MN', 'Mongolia', NULL),
('ME', 'Montenegro', NULL),
('MA', 'Morocco', NULL),
('MZ', 'Mozambique', NULL),
('MM', 'Myanmar', 'Burma'),
('NA', 'Namibia', NULL),
('NR', 'Nauru', NULL),
('NP', 'Nepal', NULL),
('NL', 'Netherlands', 'Holland,Nederland'),
('NZ', 'New Zealand', NULL),
('NI', 'Nicaragua', NULL),
('NE', 'Niger', NULL),
('NG', 'Nigeria', NULL),
('MK', 'North Macedonia', 'Macedonia'),
('NO', 'Norway', 'Norge'),
('OM', 'Oman', NULL),
('PK', 'Pakistan', NULL),
('PW', 'Palau', NULL),
('PA', 'Panama', NULL),
('PG', 'Papua New Guinea', NULL),
('PY', 'Paraguay', NULL),
('PE', 'Peru', NULL),
('PH', 'Philippines', NULL),
('PL', 'Poland', 'Polska'),
('PT', 'Portugal', NULL),
('QA', 'Qatar', NULL),
('RO', 'Romania', NULL),
('RU', 'Russia', 'USSR,Soviet Union,CCCP'),
('RW', 'Rwanda', NULL),
('KN', 'Saint Kitts and Nevis', NULL),
('LC', 'Saint Lucia', NULL),
('VC', 'Saint Vincent and the Grenadines', NULL),
('WS', 'Samoa', NULL),
('SM', 'San Marino', NULL),
('ST', 'Sao Tome and Principe', NULL),
('SA', 'Saudi Arabia', NULL),
('SN', 'Senegal', NULL),
('RS', 'Serbia', 'Yugoslavia,Serbia and Montenegro'),
('SC', 'Seychelles', NULL),
('SL', 'Sierra Leone', NULL),
('SG', 'Singapore', NULL),
('SK', 'Slovakia', NULL),
('SI', 'Slovenia', NULL),
('SB', 'Solomon Islands', NULL),
('SO', 'Somalia', NULL),
('ZA', 'South Africa', NULL),
('SS', 'South Sudan', NULL),
('ES', 'Spain', 'España'),
('LK', 'Sri Lanka', 'Ceylon'),
('SD', 'Sudan', NULL),
('SR', 'Suriname', NULL),
('SE', 'Sweden', 'Sverige'),
('CH', 'Switzerland', 'Schweiz,Suisse,Svizzera'),
('SY', 'Syria', NULL),
('TW', 'Taiwan', NULL),
('TJ', 'Tajikistan', NULL),
('TZ', 'Tanzania', 'Tanganyika'),
('TH', 'Thailand', 'Siam'),
('TL', 'Timor-Leste', NULL),
('TG', 'Togo', NULL),
('TO', 'Tonga', NULL),
('TT', 'Trinidad and Tobago', NULL),
('TN', 'Tunisia', NULL),
('TR', 'Turkey', 'Türkiye'),
('TM', 'Turkmenistan', NULL),
('TV', 'Tuvalu', NULL),
('UG', 'Uganda', NULL),
('UA', 'Ukraine', NULL),
('AE', 'United Arab Emirates', 'UAE'),
('GB', 'United Kingdom', 'UK,Britain,Great Britain'),
('US', 'United States', 'USA,United States of America,America'),
('UY', 'Uruguay', NULL),
('UZ', 'Uzbekistan', NULL),
('VU', 'Vanuatu', NULL),
('VE', 'Venezuela', NULL),
('VN', 'Vietnam', NULL),
('YE', 'Yemen', NULL),
('ZM', 'Zambia', 'Northern Rhodesia'),
('ZW', 'Zimbabwe', 'Rhodesia,Southern Rhodesia'),
-- British home nations (important for football history)
('ENG', 'England', NULL),
('SCO', 'Scotland', NULL),
('WAL', 'Wales', NULL),
('NIR', 'Northern Ireland', NULL),
-- Historical / Olympic contexts
('XYU', 'Yugoslavia', 'Jugoslawien'),
('XCS', 'Czechoslovakia', NULL),
('XSU', 'Soviet Union', 'USSR,CCCP'),
('XGE', 'East Germany', 'German Democratic Republic,DDR'),
('XGW', 'West Germany', 'Federal Republic of Germany,BRD');


-- =============================================================
-- VERIFICATION  -- run after applying this script
-- =============================================================
-- SELECT COUNT(*) FROM countries;          -- expect ~200
-- SELECT COUNT(*) FROM clubs;              -- expect 0 (populated by parse script)
-- DESCRIBE set_cards;                      -- confirm club_raw, club_id, country_id present
-- DESCRIBE players;                        -- confirm canonical_player_id, is_non_player present
-- SHOW TRIGGERS LIKE '%set_cards%';        -- confirm triggers updated
-- SHOW TRIGGERS LIKE '%players%';          -- confirm triggers updated
-- =============================================================


-- =============================================================
-- WIKIDATA LOOKUP CACHE  (Phase 0 of Wikidata-driven dedup)
-- =============================================================
-- Two cache tables that back the wikidata_lookup_players.py script.
-- They are pure cache — no audit history, no triggers — so the script
-- can re-run cheaply and the data here is regenerable from Wikidata.
--
-- The eventual `players.wikidata_qid` column is intentionally NOT added
-- here; it lands later in the apply phase so the Phase 1 lookup pass
-- can run without touching the players audit triggers.
-- =============================================================


-- -------------------------------------------------------------
-- player_wikidata_candidates -- one row per (player_id, qid)
-- -------------------------------------------------------------
-- Stores every Q-ID we ever saw for a given player, with the metadata
-- we'll need to score them in Phase 2.  Re-running the lookup updates
-- existing rows in place via the (player_id, qid) unique key.

CREATE TABLE IF NOT EXISTS player_wikidata_candidates (
    candidate_id     INT AUTO_INCREMENT PRIMARY KEY,
    player_id        INT          NOT NULL,
    qid              VARCHAR(20)  NOT NULL,            -- e.g. "Q161501"
    label_en         VARCHAR(255) NULL,                -- canonical English label
    description_en   VARCHAR(500) NULL,                -- short description (often "English footballer")
    given_name       VARCHAR(255) NULL,                -- P735 label (resolved given name, e.g. "Robert" for "Bobby")
    family_name      VARCHAR(255) NULL,                -- P734 label
    aliases_json     JSON         NULL,                -- skos:altLabel values across languages
    date_of_birth    DATE         NULL,                -- P569 (may be partial; year-only stored in birth_year)
    birth_year       SMALLINT     NULL,                -- convenience copy for fuzzy matching
    nationality      VARCHAR(100) NULL,                -- P27 label (English)
    nationality_qid  VARCHAR(20)  NULL,                -- P27 entity Q-ID
    birth_place      VARCHAR(255) NULL,                -- P19 label
    clubs_json       JSON         NULL,                -- [{qid,label,start,end}, ...] from P54 statements
    sparql_strategy  VARCHAR(50)  NULL,                -- which query produced this hit (full_name, initial_only, single_name, ...)
    similarity_score DECIMAL(4,3) NULL,                -- populated by Phase 2 scoring
    fetched_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    UNIQUE KEY uq_pwc_player_qid (player_id, qid),
    KEY idx_pwc_player_id (player_id),
    KEY idx_pwc_qid       (qid),
    KEY idx_pwc_score     (similarity_score),
    CONSTRAINT fk_pwc_player_id
        FOREIGN KEY (player_id) REFERENCES players(player_id) ON DELETE CASCADE
);


-- -------------------------------------------------------------
-- player_wikidata_lookups -- one row per attempt
-- -------------------------------------------------------------
-- Lets the script skip players it has already queried (resumability)
-- and gives a record of failures (network errors, malformed names, etc.)
-- for diagnosis.  Multiple rows per player are fine — re-runs append.

CREATE TABLE IF NOT EXISTS player_wikidata_lookups (
    lookup_id        INT AUTO_INCREMENT PRIMARY KEY,
    player_id        INT          NOT NULL,
    strategy         VARCHAR(50)  NOT NULL,            -- 'full_name', 'initial_only', 'single_name', 'last_name_only'
    candidates_found INT          NOT NULL DEFAULT 0,
    succeeded        TINYINT(1)   NOT NULL DEFAULT 1,  -- 0 if SPARQL errored after retries
    error_msg        TEXT         NULL,
    duration_ms      INT          NULL,                -- wall time for the SPARQL round-trip(s)
    attempted_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    KEY idx_pwl_player_id    (player_id),
    KEY idx_pwl_attempted_at (attempted_at),
    CONSTRAINT fk_pwl_player_id
        FOREIGN KEY (player_id) REFERENCES players(player_id) ON DELETE CASCADE
);


-- =============================================================
-- VERIFICATION
-- =============================================================
-- DESCRIBE player_wikidata_candidates;
-- DESCRIBE player_wikidata_lookups;
-- SELECT COUNT(*) FROM player_wikidata_candidates;   -- expect 0 until lookup script runs
-- SELECT COUNT(*) FROM player_wikidata_lookups;      -- expect 0 until lookup script runs
-- =============================================================
