# Music Party Simulation

A local, single-operator simulation of a party music voting system. The React interface lets an operator act as named guests, control the round clock, inspect votes and track-selection weights, rewind rounds, add guests and catalog entries, use host powers, and export a night summary. The backend reads the supplied `songs_300.csv` catalog into PostgreSQL. No audio, wallet passes, hardware, or external services are used.

## Requirements

- Python 3.10 or newer
- Node.js 20.19+ or 22.12+
- PostgreSQL with a database and user the operator can access
- `psql` available in PowerShell

The commands below use PostgreSQL at `127.0.0.1:4000`, database `postgres`, user `postgres`. Change those values if your local PostgreSQL uses different settings.

## Set up the database

Run these commands from the project root (`Music Party`):

```powershell
psql -h 127.0.0.1 -p 4000 -U postgres -d postgres -v ON_ERROR_STOP=1 -f Backend/schema.sql
psql -h 127.0.0.1 -p 4000 -U postgres -d postgres -v ON_ERROR_STOP=1 -f Backend/import_songs.sql
psql -h 127.0.0.1 -p 4000 -U postgres -d postgres -v ON_ERROR_STOP=1 -f Backend/seed_guests.sql
```

The import script reads `songs_300.csv` from the project root, converts each `m:ss` duration to seconds, and imports the catalog fields. The guest script creates the initial ten simulated guests, including the host.

## Install dependencies

From the project root:

```powershell
py -3 -m pip install -r Backend/requirements.txt
cd my-react-app
npm install
cd ..
```

## Run the app

Open two PowerShell windows in the project root.

**Window 1 — backend:**

```powershell
py -3 Backend/app.py
```

The backend listens on `http://127.0.0.1:5000` by default and reads PostgreSQL at port `4000` by default. To change these settings, set `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, or `PGPASSWORD`; set `FLASK_PORT` to change the backend port.

**Window 2 — frontend:**

```powershell
cd my-react-app
npm run dev
```

Open the local URL printed by Vite, usually `http://localhost:5173`. The Vite development server proxies `/api` requests to the backend on port `5000`. If you changed the backend port, set `VITE_API_TARGET` before starting Vite, for example:

```powershell
$env:VITE_API_TARGET = 'http://127.0.0.1:5001'
npm run dev
```

## Start a new game

The first request after starting the backend opens a fresh simulation. Use **New game** in the interface to clear the previous game's rounds, votes, histories, and queued choices while retaining the guest roster and catalog. The app starts with three timed seeding rounds, then switches to track-duration voting rounds.

## How the choices learn

- Rounds 1–3 use random catalog-backed choices to seed the slots.
- Button 1 favors choices that have won, using decayed win history.
- Button 2 is uniform through round 4. From round 5 to round 10, its weights gradually incorporate the room's decayed vote share, reaching 75% vote influence at round 10. The remaining 25% is uniform exploration so every eligible choice keeps some chance.
- Button 3 favors choices that received votes but have fewer wins.
- Each button avoids repeating its exact choice from the previous three rounds. If that leaves no option, the oldest restriction is relaxed until a choice is available.
- All sampling uses deterministic SHA-256 seeds. The button histories, track weight distributions, and recorded round outcomes can be inspected in the operator interface.

See [Backend/README.md](Backend/README.md) for the API endpoints, database behavior, and additional resolution details.

## Build the frontend

```powershell
cd my-react-app
npm run build
```

The production files are written to `my-react-app/dist/`; the project is intended to be run locally with the development command above.
