-- Initial schema, following the design doc's data model.
-- Ticks are bigint ids (one per cron step); wall-clock times are only for audit.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS h3;
CREATE EXTENSION IF NOT EXISTS h3_postgis CASCADE;

CREATE TYPE zone AS ENUM ('spawn', 'corridor', 'land', 'water', 'fog');
CREATE TYPE terrain AS ENUM ('flat', 'hills', 'mountains', 'forest', 'urban', 'wetland', 'water');
CREATE TYPE unit_mode AS ENUM ('none', 'double_march', 'heal');
CREATE TYPE clan_role AS ENUM ('founder', 'council', 'member');
CREATE TYPE battle_outcome AS ENUM ('attacker_won', 'defender_won', 'both_wounded');

CREATE TABLE ticks (
    id bigint PRIMARY KEY,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    duration_ms integer,
    tunables jsonb NOT NULL  -- the Tunables the tick ran with, so it can be replayed
);

-- Commit-reveal for battle seeds: commitment published before `day`, secret revealed after it.
CREATE TABLE daily_secrets (
    day date PRIMARY KEY,
    commitment text NOT NULL,
    secret bytea NOT NULL,
    revealed_at timestamptz
);

CREATE TABLE users (
    id bigserial PRIMARY KEY,
    oauth_provider text NOT NULL,
    oauth_subject text NOT NULL,
    display_name text NOT NULL UNIQUE,
    treasury bigint NOT NULL DEFAULT 0 CHECK (treasury >= 0),
    subscriber_tier text,
    created_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    UNIQUE (oauth_provider, oauth_subject)
);

CREATE TABLE clans (
    id bigserial PRIMARY KEY,
    name text NOT NULL UNIQUE,
    founder_id bigint NOT NULL REFERENCES users,
    capital_hex h3index,
    discord_url text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE clan_members (
    user_id bigint PRIMARY KEY REFERENCES users,  -- a player is in at most one clan
    clan_id bigint NOT NULL REFERENCES clans ON DELETE CASCADE,
    role clan_role NOT NULL DEFAULT 'member',
    can_invite boolean NOT NULL DEFAULT false,
    joined_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON clan_members (clan_id);

CREATE TABLE invites (
    code_hash bytea PRIMARY KEY,  -- only the hash of the code is stored
    clan_id bigint NOT NULL REFERENCES clans ON DELETE CASCADE,
    issued_by bigint NOT NULL REFERENCES users,
    expires_at timestamptz NOT NULL,
    used_by bigint REFERENCES users,
    used_at timestamptz
);
CREATE INDEX ON invites (clan_id) WHERE used_by IS NULL;

CREATE TABLE chunks (
    z smallint NOT NULL,
    x integer NOT NULL,
    y integer NOT NULL,
    styles jsonb NOT NULL DEFAULT '{}',  -- style name -> bake status
    ready boolean NOT NULL DEFAULT false,  -- baked and aligned in every live style
    PRIMARY KEY (z, x, y)
);

CREATE TABLE hexes (
    h3 h3index PRIMARY KEY,
    zone zone NOT NULL,
    tier smallint CHECK (tier BETWEEN 1 AND 5),
    raw_scores jsonb NOT NULL DEFAULT '{}',  -- kept so tiers can be re-cut without recomputing
    terrain terrain NOT NULL,
    river_edges smallint NOT NULL DEFAULT 0,  -- bitmask over the 6 edges
    playable boolean NOT NULL DEFAULT false,
    chunk_z smallint,
    chunk_x integer,
    chunk_y integer,
    owner_id bigint REFERENCES users,
    owner_since_tick bigint,
    flip_user_id bigint REFERENCES users,
    flip_progress integer NOT NULL DEFAULT 0,
    is_user_capital boolean NOT NULL DEFAULT false,
    garrison_hp smallint,
    FOREIGN KEY (chunk_z, chunk_x, chunk_y) REFERENCES chunks,
    CHECK (owner_id IS NULL OR zone = 'land'),
    CHECK (NOT is_user_capital OR owner_id IS NOT NULL)
);
CREATE INDEX ON hexes (owner_id) WHERE owner_id IS NOT NULL;
CREATE UNIQUE INDEX one_user_capital ON hexes (owner_id) WHERE is_user_capital;

ALTER TABLE clans ADD FOREIGN KEY (capital_hex) REFERENCES hexes;

CREATE TABLE ai_events (
    id bigserial PRIMARY KEY,
    target_user_id bigint REFERENCES users,
    target_clan_id bigint REFERENCES clans,
    announced_tick bigint NOT NULL,
    starts_tick bigint NOT NULL,
    ends_tick bigint NOT NULL,
    status text NOT NULL DEFAULT 'announced',
    CHECK ((target_user_id IS NULL) <> (target_clan_id IS NULL))
);

CREATE TABLE units (
    id bigserial PRIMARY KEY,
    user_id bigint UNIQUE REFERENCES users,  -- one unit per user; NULL for AI units
    ai_event_id bigint REFERENCES ai_events,
    hex h3index NOT NULL REFERENCES hexes,
    hp smallint NOT NULL CHECK (hp BETWEEN 0 AND 100),
    path h3index[] NOT NULL DEFAULT '{}',
    progress real NOT NULL DEFAULT 0,
    mode unit_mode NOT NULL DEFAULT 'none',
    attack_bonus real NOT NULL DEFAULT 0.15,
    defence_bonus real NOT NULL DEFAULT 0.05,
    arrival_tick bigint,
    CHECK ((user_id IS NULL) <> (ai_event_id IS NULL))
);
CREATE INDEX ON units (hex);

CREATE TABLE orders (
    id bigserial PRIMARY KEY,
    user_id bigint NOT NULL REFERENCES users,
    kind text NOT NULL,  -- move, cancel, mode, over_cap_choice, respawn_choice, ...
    payload jsonb NOT NULL DEFAULT '{}',
    submitted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    applied_tick bigint REFERENCES ticks
);
CREATE INDEX pending_orders ON orders (submitted_at, id) WHERE applied_tick IS NULL;

CREATE TABLE battles (
    id bigserial PRIMARY KEY,
    tick bigint NOT NULL REFERENCES ticks,
    hex h3index NOT NULL,
    attacker_unit bigint,
    defender_unit bigint,  -- NULL when fighting a capital garrison
    seed bytea NOT NULL,
    log jsonb NOT NULL,  -- the full BattleResult
    outcome battle_outcome NOT NULL
);
CREATE INDEX ON battles (hex, tick DESC);
CREATE INDEX ON battles (tick);

CREATE TABLE ledger (
    id bigserial PRIMARY KEY,
    user_id bigint NOT NULL REFERENCES users,
    tick bigint REFERENCES ticks,
    amount bigint NOT NULL,
    reason text NOT NULL,
    admin_id bigint REFERENCES users  -- set for admin actions, which always carry a reason
);
CREATE INDEX ON ledger (user_id, id DESC);

CREATE TABLE bake_jobs (
    id bigserial PRIMARY KEY,
    z smallint NOT NULL,
    x integer NOT NULL,
    y integer NOT NULL,
    style text NOT NULL,
    priority integer NOT NULL DEFAULT 0,  -- distinct players bordering the frontier there
    status text NOT NULL DEFAULT 'queued',  -- queued, claimed, done, failed
    claimed_by text,
    claimed_at timestamptz,
    atlas_job_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (z, x, y, style)
);
-- The worker claims with ORDER BY priority DESC ... FOR UPDATE SKIP LOCKED.
CREATE INDEX queued_bakes ON bake_jobs (priority DESC, id) WHERE status = 'queued';
