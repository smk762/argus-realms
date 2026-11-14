-- What the tick engine needs beyond the initial schema.

-- A unit at 0 HP is off the map until it respawns: `hex` keeps where it fell.
ALTER TABLE units ADD COLUMN respawn_deadline bigint;
-- Submit order of the unit's current move: attackers on one hex fight in this order.
ALTER TABLE units ADD COLUMN order_seq bigint;

-- Single precision would round 0.15 to 0.1500000006 on the way back, so a replay from the
-- database would roll different strengths than the tick that ran. Doubles round-trip exactly.
ALTER TABLE units ALTER COLUMN progress TYPE double precision;
ALTER TABLE units ALTER COLUMN attack_bonus TYPE double precision;
ALTER TABLE units ALTER COLUMN defence_bonus TYPE double precision;

-- Live map styles. A chunk is ready only once it is baked in every live style.
CREATE TABLE styles (
    name text PRIMARY KEY,
    live boolean NOT NULL DEFAULT true
);

-- Order results, so a player can see why an order was ignored.
ALTER TABLE orders ADD COLUMN result text;

COMMENT ON COLUMN users.treasury IS 'milli-coins: 1 coin = 1000';
COMMENT ON COLUMN ledger.amount IS 'milli-coins: 1 coin = 1000';
