# Music Party local simulation

Flask reads guests and the song catalog from PostgreSQL. The active clock and UI state run in memory in the single local backend process; rounds, offered choices, votes, slot snapshots, and resolutions are also written to PostgreSQL so choice selection can use the database history. A new game clears previous round-scoped rows while keeping the guest roster and catalog.

## Prepare PostgreSQL

From the project root, create the schema, import the provided catalog, and add the ten simulated guests:

```powershell
psql -h 127.0.0.1 -p 4000 -U postgres -d postgres -v ON_ERROR_STOP=1 -f Backend/schema.sql
psql -h 127.0.0.1 -p 4000 -U postgres -d postgres -v ON_ERROR_STOP=1 -f Backend/import_songs.sql
psql -h 127.0.0.1 -p 4000 -U postgres -d postgres -v ON_ERROR_STOP=1 -f Backend/seed_guests.sql
```

`import_songs.sql` uses `psql`'s client-side `\copy` to read `songs_300.csv` from the project root. It checks for 300 rows, parses `m:ss` durations into seconds, and inserts or updates by `(song_name, artist, year)`. The app reads these rows to offer genre, artist, era, and wildcard choices and to resolve votes to actual tracks.

After pulling schema changes, reapply the idempotent schema so tie-break, host choice-removal, rewind-start metadata, full resolution-weight snapshots, and live catalog additions are available:

```powershell
psql -h 127.0.0.1 -p 4000 -U postgres -d postgres -v ON_ERROR_STOP=1 -f Backend/schema.sql
```

The defaults are PostgreSQL at `127.0.0.1:4000`, database `postgres`, user `postgres`. Use your local settings if they differ; Flask reads `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, and optionally `PGPASSWORD`.

## Install and run

Install dependencies once:

```powershell
py -3 -m pip install -r Backend/requirements.txt
cd my-react-app
npm install
```

Start the backend in one PowerShell window from the project root:

```powershell
py -3 Backend/app.py
```

Start the frontend in another window:

```powershell
cd "C:\Users\sedri\Projects\Music Party\my-react-app"
npm run dev
```

Open the Vite URL it prints, usually `http://localhost:5173`. Vite proxies `/api` calls to Flask on port 5000.

## Current loop behavior

- Rounds 1–3 are seeding windows capped at 180 simulated seconds. From round 4 onward, each window lasts for the exact duration of the current Now Playing track (read from the imported catalog). Clock speed can be set to 1×, 5×, 10×, or 20×, and pause freezes the clock.
- Rounds 1–3 offer three random catalog-backed choices per round. Each guest may vote once in each round; the winning category resolves to a track and fills the next slot.
- From round 4 on, Button 1 weights choices by decayed wins; Button 3 weights choices by decayed rounds with votes divided by one plus decayed wins. Button 2 is uniform through round 4, then gradually learns from the room's votes: its vote influence rises linearly from 12.5% in round 5 to 75% in round 10 and stays capped there. The remaining 25% stays uniform, so every eligible choice keeps an exploration chance. Each vote contributes to popularity, with the shared `0.9` decay rate favoring recent votes.
- Each button avoids its own exact choices from the previous three rounds; if that blocks every available choice, the oldest restriction is relaxed until an option is available. The remaining candidates use the button's existing weighting and stable SHA-256 seeded roll.
- If Button 1 or Button 3 has a zero total weight (no previous wins or no prior vote activity), that button falls back to uniform sampling so it can still offer a choice. Button 3's underlying score for never-voted choices remains zero.
- The scores query closed-round winners from `resolution_events` and choice votes from `votes`. The operator can inspect each track's normalized probability distribution live and from saved rounds.
- When a round closes, its winning choice resolves to a track, then slots advance: Up Next to Now Playing, resolved On Deck to Up Next, and a fresh On Deck opens.
- Indirect choices resolve from matching catalog rows. Tracks in Now Playing, Up Next, and On Deck are excluded. A choice is considered depleted when it has no unlocked candidate that has been unplayed for at least the five-round recency cooldown. If a depleted choice leads, the highest-voted other non-depleted choice wins, using the existing tie-break rule if needed. If no other non-depleted choice received votes, the round follows the empty-window rule over non-depleted choices. If every offered choice is depleted, the empty-window outcome resolves from the unlocked full catalog and records an exception. This is the repeat rule for a one-track artist: a second win during its cooldown yields to another voted category, or follows the empty-window fallback when no alternative received votes.
- Eligible tracks have flat base weight, a recency factor that reaches full weight after five rounds since last playing, and a cross-novelty factor. The latter considers the candidate's decade and derived mood group, omitting the attribute used by the winning criterion. Mood labels are grouped deterministically under their most-shared informative token across distinct catalog labels; this derives groups from the provided data without hand-authored per-track metadata. Combined novelty is min-max normalized within the eligible pool and clamped to 0.5–1.0; a constant or single-track pool uses 1.0. Final weight is base × recency × cross-novelty, then normalized to probabilities. The roll is SHA-256 seeded by round number and winning choice ID. Every resolution stores the factor breakdown, weights, and normalized probabilities for the full catalog; ineligible tracks have 0%.
- The operator's Track Weights & Likelihoods panel previews the live distribution for each offered button using the exact resolver and seed. Selecting any closed round loads its saved full-catalog snapshot. The panel shows base weight, recent-play factor, decade and mood novelty inputs, cross-novelty factor, final weight, eligibility, probability, the selected track, and the button's decayed win/vote-history scores. Vote history affects category offering; it is not an additional per-track resolution multiplier.
- Round-start speed and acting guest are recorded. Rewind reopens a selected completed round with its original three offered choices, opening slots, empty tallies, guest eligibility, speed, and pre-round history; it deletes that round's votes and outcome plus every later round and snapshot. The discarded branch therefore cannot affect recency or button-selection history. Replay votes can differ. The same round/choice seed makes track resolution deterministic when the same choice wins from the same state.
- Guests added mid-round appear in the roster immediately, but their `joined_round` is set to the next round. They cannot be selected or vote in the current round and become eligible as soon as that next round opens. Their earlier history rows are marked as not yet in the party, with an explicit empty-history state before they have voted.
- Guests persist into a new game, where their `joined_round` resets to 1 so everyone on the roster is eligible immediately.
- Songs added during a round enter the active catalog next round and are queued as a specific-song button. Their genre, artist, and decade become usable catalog categories too. The operator can also queue a matching genre, artist, or decade directly. Up to three queued additions are placed on the next round's buttons; the queue rejects further additions until a slot opens. New-game resets make all saved catalog songs available from round 1 and clear pending button offers.
- Highest tally wins. Vote ties favor the choice that has gone the longest without winning, then use a hash of the round and sorted tied choice IDs only when novelty is tied. Zero-vote rounds select among the three offered choices by a stable hash, then resolve that choice's category.
- The host can skip a round and remove one choice per round. Removing a choice closes that button to future votes while preserving any votes already cast; the choice remains visible in the round history. These controls appear only while acting as the host. The host can be selected after voting to use these powers, but cannot cast a second vote that round.
- The operator can also end any unpaused round immediately; this is distinct from the in-party host skip.
- “End night” closes the current window and shows an analytics screen with the most-voted choices, each round's winner and resolved track, and the tracks that played. The best-taste award ranks winning picks divided by votes cast. Guests need at least three votes to qualify; ties go to more winning picks, then more votes, then alphabetical name. If nobody qualifies, no award is given. The screen exports its report as JSON.
- “New game” and the first app request after a backend start clear all rows in `rounds`; dependent round data such as votes, choices, slot snapshots, and resolution events are removed by the database foreign keys. Guests and tracks remain. The active clock and displayed history are process-local and reset when Flask restarts.

## API

- `GET /api/health` checks the PostgreSQL connection.
- `GET /api/party` returns guests, slots, choices, timer, current round, and round history.
- `POST /api/party/new` clears old round records and starts a fresh game.
- `POST /api/party/rewind` with `{"round_number": 4}` discards that round's outcome and all later rounds, restoring the start of round 4.
- `GET /api/rounds/<round_number>/weights` returns the resolution rule and the stored full-catalog weight/probability snapshot.
- `POST /api/vote` with `{"guest_id": 1, "choice_index": 1}` casts a vote.
- `POST /api/guest/select` with `{"guest_id": 1}` switches the acting guest.
- `POST /api/guest/add` with `{"name": "Jordan Lee"}` adds a guest who becomes eligible in the next round.
- `POST /api/catalog/track` accepts `song_name`, `artist`, `genre`, `year`, `duration` (`m:ss`), and `mood`; it adds the song for the next round and queues a specific-song button.
- `POST /api/catalog/choice` with `{"choice_type": "genre", "choice_value": "Hip Hop"}` queues a matching genre, artist, or decade button for the next round.
- `POST /api/party/end` resolves the active round and returns the completed night summary. `GET /api/party/analytics` reads that summary until a new game starts.
- `POST /api/clock` accepts `{"speed": 5}` or `{"paused": true}`.
- `POST /api/host/skip` with `{"guest_id": 9}` skips when that guest is the acting host.
- `POST /api/host/remove-choice` with `{"guest_id": 9, "choice_index": 2}` removes one button for the round when that guest is the acting host. The removed choice stops accepting votes; previously cast votes remain in the tally and history.
- `POST /api/round/end` ends the current unpaused round at the operator's request.
