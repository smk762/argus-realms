-- Nemesis (a subscriber's chosen target: a user or a clan) and hunt mode.
ALTER TABLE users ADD COLUMN bot boolean NOT NULL DEFAULT false;
ALTER TABLE users ADD COLUMN nemesis_user bigint REFERENCES users ON DELETE SET NULL;
ALTER TABLE users ADD COLUMN nemesis_clan bigint REFERENCES clans ON DELETE SET NULL;
ALTER TABLE users ADD COLUMN nemesis_tick integer;
ALTER TABLE users ADD CONSTRAINT one_nemesis CHECK (nemesis_user IS NULL OR nemesis_clan IS NULL);
ALTER TABLE units ADD COLUMN hunting boolean NOT NULL DEFAULT false;
ALTER TABLE units ADD COLUMN hunt_used integer NOT NULL DEFAULT 0;
ALTER TABLE units ADD COLUMN hunt_day integer;
