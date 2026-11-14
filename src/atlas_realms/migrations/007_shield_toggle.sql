-- Shield toggling: a cooldown that starts when the shield drops, and subscribers' daily shield budget.
ALTER TABLE units DROP COLUMN shield_tick;
ALTER TABLE units ADD COLUMN shield_ready integer;
ALTER TABLE units ADD COLUMN shield_used integer NOT NULL DEFAULT 0;
ALTER TABLE units ADD COLUMN shield_day integer;
