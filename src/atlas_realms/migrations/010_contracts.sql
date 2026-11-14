-- Bot services: mercenary, guard and protection contracts, queued, active or finished.
CREATE TABLE contracts (
    id bigint PRIMARY KEY,  -- assigned by the tick, so a replay assigns the same ids
    bot bigint NOT NULL REFERENCES users,
    hirer bigint NOT NULL REFERENCES users,
    kind text NOT NULL CHECK (kind IN ('merc', 'guard', 'protect')),
    price bigint NOT NULL CHECK (price >= 0),
    start integer NOT NULL,
    minutes integer NOT NULL,
    target_user bigint REFERENCES users,
    target_clan bigint REFERENCES clans,
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'active', 'done', 'broken', 'cancelled')),
    began integer
);
CREATE INDEX contracts_open ON contracts (bot) WHERE status IN ('queued', 'active');
