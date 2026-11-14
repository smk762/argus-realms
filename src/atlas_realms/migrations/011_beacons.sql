-- The drand quicknet round mixed into each tick's battle seeds (rules/beacon.py). The signature is
-- kept so anyone can verify the round against drand's public key; randomness = sha256(signature).
CREATE TABLE tick_beacons (
    tick bigint PRIMARY KEY REFERENCES ticks,
    round bigint NOT NULL,  -- two ticks within one 3 s round may share it; the tick id keeps seeds apart
    signature bytea NOT NULL,
    randomness bytea NOT NULL
);
