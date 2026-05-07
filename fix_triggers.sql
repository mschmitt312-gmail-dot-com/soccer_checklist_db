-- fix_triggers.sql
-- Recreates trg_players_before_update, which was accidentally dropped
-- during a failed migration attempt.
-- Run this in MySQL Workbench as root.

SET GLOBAL log_bin_trust_function_creators = 1;

DROP TRIGGER IF EXISTS `trg_players_before_update`;

DELIMITER $$

CREATE TRIGGER `trg_players_before_update`
BEFORE UPDATE ON `players`
FOR EACH ROW
BEGIN
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
        OLD.canonical_player_id, OLD.is_non_player, @current_user
    );
END$$

DELIMITER ;

SET GLOBAL log_bin_trust_function_creators = 0;
