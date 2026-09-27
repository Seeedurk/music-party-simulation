-- Music Party database schema (PostgreSQL)
-- Apply with: psql -h 127.0.0.1 -p 4000 -U postgres -d postgres -v ON_ERROR_STOP=1 -f Backend/schema.sql

BEGIN;

CREATE TABLE IF NOT EXISTS tracks (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    song_name    text NOT NULL,
    artist       text NOT NULL,
    genre        text NOT NULL,
    year         smallint NOT NULL CHECK (year BETWEEN 1800 AND 2200),
    duration_sec integer NOT NULL CHECK (duration_sec > 0),
    mood         text NOT NULL,
    added_round  integer NOT NULL DEFAULT 1 CHECK (added_round > 0),
    CONSTRAINT tracks_catalog_identity UNIQUE (song_name, artist, year)
);

ALTER TABLE tracks ADD COLUMN IF NOT EXISTS added_round integer NOT NULL DEFAULT 1;

CREATE TABLE IF NOT EXISTS rounds (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    round_number integer NOT NULL UNIQUE CHECK (round_number > 0),
    started_at   timestamptz NOT NULL DEFAULT now(),
    ended_at     timestamptz,
    status       text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    close_reason text CHECK (close_reason IN ('track_ended', 'operator_forced', 'empty_seed_cap')),
    is_seed_round boolean NOT NULL DEFAULT false,
    time_limit_minutes numeric(9, 4),
    elapsed_minutes numeric(9, 4),
    start_speed integer NOT NULL DEFAULT 1 CHECK (start_speed IN (1, 5, 10, 20)),
    start_acting_guest_id bigint,
    CONSTRAINT rounds_close_state CHECK (
        (status = 'open' AND ended_at IS NULL AND close_reason IS NULL)
        OR (status = 'closed' AND ended_at IS NOT NULL AND close_reason IS NOT NULL)
    )
);

ALTER TABLE rounds ADD COLUMN IF NOT EXISTS start_speed integer NOT NULL DEFAULT 1;
ALTER TABLE rounds ADD COLUMN IF NOT EXISTS start_acting_guest_id bigint;

CREATE UNIQUE INDEX IF NOT EXISTS rounds_one_open_round
    ON rounds ((status)) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS guests (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name         text NOT NULL CHECK (length(trim(name)) > 0),
    role         text NOT NULL DEFAULT 'guest' CHECK (role IN ('host', 'guest')),
    is_active    boolean NOT NULL DEFAULT true,
    -- First round number in which this guest is eligible. For a mid-party arrival,
    -- set this to the next round number so they do not join a window in progress.
    joined_round integer NOT NULL DEFAULT 1 CHECK (joined_round > 0)
);

ALTER TABLE guests ADD COLUMN IF NOT EXISTS is_active boolean NOT NULL DEFAULT true;

CREATE TABLE IF NOT EXISTS slots (
    round_id   bigint NOT NULL REFERENCES rounds(id) ON DELETE CASCADE,
    slot_name  text NOT NULL CHECK (slot_name IN ('now_playing', 'up_next', 'on_deck')),
    track_id   bigint REFERENCES tracks(id),
    PRIMARY KEY (round_id, slot_name)
);

CREATE TABLE IF NOT EXISTS choices (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    round_id     bigint NOT NULL REFERENCES rounds(id) ON DELETE CASCADE,
    button_index smallint NOT NULL CHECK (button_index BETWEEN 1 AND 3),
    choice_type  text NOT NULL CHECK (choice_type IN (
        'specific_song', 'genre', 'era', 'artist', 'wildcard',
        'host_curated', 'mood', 'region', 'energy', 'more_like_this'
    )),
    -- For specific_song, store the track ID as text; for indirect choices,
    -- store the catalog key or a stable key such as "*" for wildcard.
    choice_value text NOT NULL,
    removed_at   timestamptz,
    operator_added boolean NOT NULL DEFAULT false,
    CONSTRAINT choices_round_button_unique UNIQUE (round_id, button_index),
    CONSTRAINT choices_round_id_unique UNIQUE (round_id, id),
    CONSTRAINT choices_specific_song_value CHECK (
        choice_type <> 'specific_song' OR choice_value ~ '^[0-9]+$'
    )
);

ALTER TABLE choices ADD COLUMN IF NOT EXISTS removed_at timestamptz;
ALTER TABLE choices ADD COLUMN IF NOT EXISTS operator_added boolean NOT NULL DEFAULT false;

-- Operator-created choices wait until the following round before appearing.
-- Offers are global party inputs rather than round rows so they can be queued.
CREATE TABLE IF NOT EXISTS pending_choice_offers (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    choice_type text NOT NULL CHECK (choice_type IN ('specific_song', 'genre', 'artist', 'era')),
    choice_value text NOT NULL,
    created_round integer NOT NULL CHECK (created_round > 0),
    consumed_round integer CHECK (consumed_round IS NULL OR consumed_round > 0)
);
CREATE INDEX IF NOT EXISTS pending_choice_offers_queue_idx
    ON pending_choice_offers (created_round, id) WHERE consumed_round IS NULL;

CREATE TABLE IF NOT EXISTS votes (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    round_id  bigint NOT NULL REFERENCES rounds(id) ON DELETE CASCADE,
    choice_id bigint NOT NULL,
    guest_id  bigint NOT NULL REFERENCES guests(id),
    cast_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT votes_one_per_guest_per_round UNIQUE (round_id, guest_id),
    CONSTRAINT votes_choice_same_round_fk
        FOREIGN KEY (round_id, choice_id) REFERENCES choices(round_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS resolution_events (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    round_id         bigint NOT NULL REFERENCES rounds(id) ON DELETE CASCADE,
    choice_id        bigint,
    winning_track_id bigint NOT NULL REFERENCES tracks(id),
    method           text NOT NULL CHECK (method IN ('normal', 'tie_break', 'empty_window', 'single_candidate')),
    tie_break_rule   text,
    -- Required to record all three ranked placements made by the seed round.
    slot_name        text NOT NULL CHECK (slot_name IN ('now_playing', 'up_next', 'on_deck')),
    CONSTRAINT resolution_choice_same_round_fk
        FOREIGN KEY (round_id, choice_id) REFERENCES choices(round_id, id),
    CONSTRAINT resolution_event_kind CHECK (method = 'empty_window' OR choice_id IS NOT NULL),
    CONSTRAINT resolution_one_track_per_slot UNIQUE (round_id, slot_name)
);

-- Empty rounds select one of the three offered categories as their stated
-- fallback, so their resolution event may retain that choice_id for audit.
ALTER TABLE resolution_events DROP CONSTRAINT IF EXISTS resolution_event_kind;
ALTER TABLE resolution_events ADD CONSTRAINT resolution_event_kind
    CHECK (method = 'empty_window' OR choice_id IS NOT NULL);
ALTER TABLE resolution_events ADD COLUMN IF NOT EXISTS tie_break_rule text;

CREATE TABLE IF NOT EXISTS resolution_weight_snapshots (
    round_id       bigint NOT NULL REFERENCES rounds(id) ON DELETE CASCADE,
    choice_id      bigint NOT NULL,
    track_id       bigint NOT NULL REFERENCES tracks(id),
    base_weight    double precision NOT NULL,
    recency_factor double precision NOT NULL,
    era_novelty_rounds integer CHECK (era_novelty_rounds IS NULL OR era_novelty_rounds >= 0),
    mood_group text,
    mood_novelty_rounds integer CHECK (mood_novelty_rounds IS NULL OR mood_novelty_rounds >= 0),
    cross_novelty_factor double precision NOT NULL DEFAULT 1.0
        CHECK (cross_novelty_factor BETWEEN 0.5 AND 1.0),
    weight         double precision NOT NULL,
    probability    double precision NOT NULL CHECK (probability BETWEEN 0 AND 1),
    is_eligible    boolean NOT NULL,
    is_selected    boolean NOT NULL DEFAULT false,
    exception      text,
    PRIMARY KEY (round_id, track_id),
    FOREIGN KEY (round_id, choice_id) REFERENCES choices(round_id, id) ON DELETE CASCADE
);

-- Older snapshots keep a neutral 1.0 cross-novelty factor; the nullable
-- component columns identify those pre-factor rounds in the operator UI.
ALTER TABLE resolution_weight_snapshots
    ADD COLUMN IF NOT EXISTS era_novelty_rounds integer
        CHECK (era_novelty_rounds IS NULL OR era_novelty_rounds >= 0);
ALTER TABLE resolution_weight_snapshots ADD COLUMN IF NOT EXISTS mood_group text;
ALTER TABLE resolution_weight_snapshots
    ADD COLUMN IF NOT EXISTS mood_novelty_rounds integer
        CHECK (mood_novelty_rounds IS NULL OR mood_novelty_rounds >= 0);
ALTER TABLE resolution_weight_snapshots
    ADD COLUMN IF NOT EXISTS cross_novelty_factor double precision NOT NULL DEFAULT 1.0
        CHECK (cross_novelty_factor BETWEEN 0.5 AND 1.0);

CREATE INDEX IF NOT EXISTS votes_guest_history_idx ON votes (guest_id, round_id);
CREATE INDEX IF NOT EXISTS choices_round_idx ON choices (round_id, button_index);
CREATE INDEX IF NOT EXISTS resolution_round_idx ON resolution_events (round_id, slot_name);
CREATE INDEX IF NOT EXISTS resolution_weights_round_idx ON resolution_weight_snapshots (round_id, probability DESC);
CREATE INDEX IF NOT EXISTS tracks_genre_idx ON tracks (genre);
CREATE INDEX IF NOT EXISTS tracks_artist_idx ON tracks (artist);
CREATE INDEX IF NOT EXISTS tracks_year_idx ON tracks (year);
CREATE INDEX IF NOT EXISTS tracks_mood_idx ON tracks (mood);

COMMIT;
