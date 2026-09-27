-- Load the provided songs_300.csv from the project root with psql:
-- psql -h 127.0.0.1 -p 4000 -U postgres -d postgres -v ON_ERROR_STOP=1 -f Backend/import_songs.sql
-- The CSV is client-side input to psql's \copy command; no server file access is needed.

BEGIN;

CREATE TEMPORARY TABLE songs_csv_import (
    song_name text NOT NULL,
    artist    text NOT NULL,
    genre     text NOT NULL,
    year      integer NOT NULL,
    duration  text NOT NULL,
    mood      text NOT NULL
) ON COMMIT DROP;

\copy songs_csv_import (song_name, artist, genre, year, duration, mood) FROM 'songs_300.csv' WITH (FORMAT csv, HEADER true, ENCODING 'UTF8')

DO $validate$
BEGIN
    IF (SELECT count(*) FROM songs_csv_import) <> 300 THEN
        RAISE EXCEPTION 'Expected 300 catalog rows, found %', (SELECT count(*) FROM songs_csv_import);
    END IF;
    IF EXISTS (
        SELECT 1 FROM songs_csv_import
        WHERE duration !~ '^[0-9]{1,2}:[0-5][0-9]$'
    ) THEN
        RAISE EXCEPTION 'CSV contains a duration that is not valid m:ss';
    END IF;
END
$validate$;

INSERT INTO tracks (song_name, artist, genre, year, duration_sec, mood)
SELECT song_name,
       artist,
       genre,
       year::smallint,
       split_part(duration, ':', 1)::integer * 60
           + split_part(duration, ':', 2)::integer,
       mood
FROM songs_csv_import
ON CONFLICT (song_name, artist, year) DO UPDATE
SET genre = EXCLUDED.genre,
    duration_sec = EXCLUDED.duration_sec,
    mood = EXCLUDED.mood;

COMMIT;

SELECT count(*) AS tracks_in_catalog FROM tracks;
