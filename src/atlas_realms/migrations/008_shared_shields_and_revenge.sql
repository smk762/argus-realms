-- One shield rule for everyone (a daily budget, no fixed timer), subscribers' shield schedule, and revenge pursuit.
ALTER TABLE units DROP COLUMN shield_until;
ALTER TABLE units ADD COLUMN shield_from smallint;
ALTER TABLE units ADD COLUMN shield_to smallint;
ALTER TABLE units ADD COLUMN pursue integer REFERENCES users (id) ON DELETE SET NULL;
