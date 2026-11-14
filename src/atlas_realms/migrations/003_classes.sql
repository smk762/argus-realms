-- Player classes (rules/classes.py) and patron-only map styles.
ALTER TABLE units ADD COLUMN unit_class text NOT NULL DEFAULT 'barbarian';
ALTER TABLE units ADD COLUMN class_changed_tick bigint;
ALTER TABLE units ADD COLUMN battles_since_class integer NOT NULL DEFAULT 0;

-- Bonus styles for subscribers. They are baked like any live style; only the client gates who sees them.
ALTER TABLE styles ADD COLUMN patron_only boolean NOT NULL DEFAULT false;
