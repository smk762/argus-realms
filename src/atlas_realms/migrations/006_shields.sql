-- Shields: once a day a unit can raise a shield that soaks damage before its HP, for up to 12 hours.
ALTER TABLE units ADD COLUMN shield_hp integer NOT NULL DEFAULT 0;
ALTER TABLE units ADD COLUMN shield_until integer;
ALTER TABLE units ADD COLUMN shield_tick integer;
