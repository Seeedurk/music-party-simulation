-- Idempotently add the initial ten-person roster.
-- Apply from the project root with:
-- psql -h 127.0.0.1 -p 4000 -U postgres -d postgres -v ON_ERROR_STOP=1 -f Backend/seed_guests.sql

INSERT INTO guests (name, role, joined_round)
SELECT seed.name, seed.role, 1
FROM (VALUES
    ('Alex Morgan', 'guest'),
    ('Blair Kim', 'guest'),
    ('Casey Patel', 'guest'),
    ('Dev Walker', 'guest'),
    ('Eli Chen', 'guest'),
    ('Fran Rivera', 'guest'),
    ('Gray Thompson', 'guest'),
    ('Harper Jones', 'guest'),
    ('Indigo Brooks', 'host'),
    ('Jules Reed', 'guest')
) AS seed(name, role)
WHERE NOT EXISTS (
    SELECT 1 FROM guests existing WHERE lower(existing.name) = lower(seed.name)
);
