"""Flask API for the local party loop simulation."""

from flask import Blueprint, jsonify, request
import psycopg
from threading import Lock

from .db import connect_db, json_safe
from .party import INITIAL_GUESTS, current_simulation, start_new_game
from .selection import (
    CROSS_NOVELTY_FLOOR,
    RECENCY_COOLDOWN,
    choice_history_effects,
    room_vote_effects,
    button_strategy_label,
    selection_history_from_db,
)


api = Blueprint("api", __name__, url_prefix="/api")
simulation_request_lock = Lock()


def error(message, status):
    return jsonify({"error": str(message)}), status


def reset_database_rounds_and_start():
    with connect_db() as conn:
        # One simulated room: discard all old round-scoped state on each game.
        conn.execute("DELETE FROM rounds")
        # Catalog additions persist globally and are available from the start of
        # the next game; pending one-night offers do not carry over.
        conn.execute("UPDATE tracks SET added_round=1")
        conn.execute("DELETE FROM pending_choice_offers")
        # A new game restores the original ten guests and hides any arrivals
        # from the prior game, while retaining old guest identities for history.
        conn.execute("UPDATE guests SET is_active=false")
        for name, role in INITIAL_GUESTS:
            guest = conn.execute(
                """UPDATE guests SET role=%s,joined_round=1,is_active=true
                   WHERE lower(name)=lower(%s)
                   RETURNING id""",
                (role, name),
            ).fetchone()
            if guest is None:
                conn.execute(
                    """INSERT INTO guests(name,role,joined_round,is_active)
                       VALUES (%s,%s,1,true)""",
                    (name, role),
                )
        guests = conn.execute(
            "SELECT id,name,role,joined_round,is_active FROM guests WHERE is_active ORDER BY id"
        ).fetchall()
        tracks = conn.execute(
            "SELECT id,song_name,artist,genre,year,duration_sec,mood FROM tracks ORDER BY id"
        ).fetchall()
    return start_new_game(json_safe(guests), json_safe(tracks))


def sim_or_error(action=None):
    try:
        with simulation_request_lock:
            sim = current_simulation()
            if sim is None:
                sim = reset_database_rounds_and_start()
            result = action(sim) if action else None
            state = sim.state()
            if result is not None:
                state["action_result"] = result
            return jsonify(state)
    except ValueError as exc:
        return error(exc, 409)
    except psycopg.Error as exc:
        return error(f"Could not access guest roster: {exc}", 503)


@api.get("/health")
def health():
    try:
        with connect_db() as conn:
            row = conn.execute(
                "SELECT current_database() AS database,current_user AS user"
            ).fetchone()
        return jsonify({"ok": True, **row})
    except psycopg.Error as exc:
        return error(f"Database connection failed: {exc}", 503)


@api.get("/guests")
@api.get("/party")
def party_state():
    return sim_or_error()


@api.post("/party/new")
def new_party():
    try:
        with simulation_request_lock:
            return jsonify(reset_database_rounds_and_start().state())
    except ValueError as exc:
        return error(exc, 409)
    except psycopg.Error as exc:
        return error(f"Could not reset game data: {exc}", 503)


@api.get("/rounds/<int:round_number>/weights")
def round_weights(round_number):
    try:
        # For an open round, calculate a non-mutating preview for each offered
        # choice using the same resolver, inputs, and round/choice seed as close.
        with simulation_request_lock:
            sim = current_simulation()
            if sim is not None and sim.round_number == round_number:
                sim.state()  # Advance/close the clock before deciding live vs historical.
                if sim.round_number == round_number:
                    with connect_db() as conn:
                        open_round = conn.execute(
                            "SELECT id,round_number,status,is_seed_round,time_limit_minutes FROM rounds WHERE round_number=%s",
                            (round_number,),
                        ).fetchone()
                    if open_round and open_round["status"] == "open":
                        choices = sim.track_weight_previews()
                        return jsonify(json_safe({
                            "round": open_round,
                            "mode": "live",
                            "cross_novelty_recorded": True,
                            "choices": choices,
                            "resolution": None,
                            "inputs": {
                                "formula": "weight = base_weight × recency_factor × cross_novelty_factor; probability = weight / sum(eligible weights)",
                                "base_weight": "Flat 1.0 for each eligible track; no per-track popularity is invented.",
                                "recency_factor": f"min(1, rounds_since_played / {RECENCY_COOLDOWN}); tracks played recently receive less weight.",
                                "cross_novelty_factor": f"Era and derived mood-group novelty from attributes outside the winning criterion; min-max normalized within the eligible pool and clamped to {CROSS_NOVELTY_FLOOR:.1f}–1.0. Single-candidate resolutions keep this factor at 1.0.",
                                "era_novelty": "Rounds since any track in the candidate's decade last played; omitted when era is the winning criterion.",
                                "mood_novelty": "Rounds since any track in the candidate's derived mood group last played; omitted when mood is the winning criterion.",
                                "mood_grouping": "Mood labels are tokenized from the catalog. Each label joins the group named by its most-shared informative token across distinct labels; no per-track mood groups are hand-entered.",
                                "slot_occupancy": "Tracks locked in Now Playing, Up Next, or On Deck receive zero weight. A criterion with no unlocked track outside the five-round cooldown yields to another voted criterion; if no alternative received votes, it uses the no-vote fallback. If all criteria are depleted, it resolves from the unlocked catalog.",
                                "vote_history": "The button's decayed win/interest inputs are shown beside its strategy. They affect which category is offered, not track weights inside that category.",
                                "sampling": "SHA-256 of round number and choice ID selects from the displayed cumulative probability distribution.",
                            },
                        }))

        with connect_db() as conn:
            round_row = conn.execute(
                "SELECT id,round_number,status,is_seed_round,time_limit_minutes FROM rounds WHERE round_number=%s",
                (round_number,),
            ).fetchone()
            if not round_row:
                return error("Round not found", 404)
            details = conn.execute(
                """SELECT re.method,re.tie_break_rule,re.choice_id,c.choice_type,c.choice_value,
                          c.button_index,c.operator_added,
                          re.winning_track_id,rt.song_name AS winning_song,
                          rt.artist AS winning_artist
                   FROM resolution_events re
                   LEFT JOIN choices c ON c.id=re.choice_id
                   JOIN tracks rt ON rt.id=re.winning_track_id
                   WHERE re.round_id=%s ORDER BY re.id LIMIT 1""",
                (round_row["id"],),
            ).fetchone()
            distribution = conn.execute(
                """SELECT s.track_id,t.song_name,t.artist,t.genre,t.year,
                          s.base_weight,s.recency_factor,s.era_novelty_rounds,s.mood_group,
                          s.mood_novelty_rounds,s.cross_novelty_factor,s.weight,s.probability,
                          s.is_eligible,s.is_selected,s.exception
                   FROM resolution_weight_snapshots s
                   JOIN tracks t ON t.id=s.track_id
                   WHERE s.round_id=%s ORDER BY s.track_id""",
                (round_row["id"],),
            ).fetchall()
            prior_history = [
                item for item in selection_history_from_db(conn)
                if item["round_number"] < round_number
            ]
        choices = []
        if details:
            choice_label = details["choice_value"]
            if details["choice_type"] == "wildcard":
                choice_label = "Surprise me"
            elif details["choice_type"] == "era":
                choice_label = f"{choice_label}s"
            choices.append({
                "index": 1,
                "choice_id": details["choice_id"],
                "choice_type": details["choice_type"],
                "choice_value": details["choice_value"],
                "choice_label": choice_label,
                "strategy": "Operator addition" if details["operator_added"] else (
                    button_strategy_label(
                        details["button_index"], round_number,
                        round_row["is_seed_round"],
                    )
                ),
                "choice_history": choice_history_effects({
                    "key": f"{details['choice_type']}|{details['choice_value']}"
                }, prior_history, round_number),
                "room_vote_effects": room_vote_effects({
                    "key": f"{details['choice_type']}|{details['choice_value']}"
                }, prior_history, round_number),
                "eligible_track_count": sum(1 for item in distribution if item["is_eligible"]),
                "preview_track": {
                    "id": details["winning_track_id"],
                    "song_name": details["winning_song"],
                    "artist": details["winning_artist"],
                },
                "exception": next((item["exception"] for item in distribution if item["exception"]), None),
                "distribution": distribution,
            })
        cross_novelty_recorded = any(item["mood_group"] is not None for item in distribution)
        return jsonify(json_safe({
            "round": round_row,
            "mode": "snapshot",
            "cross_novelty_recorded": cross_novelty_recorded,
            "choices": choices,
            "resolution": details,
            "inputs": {
                "formula": "weight = base_weight × recency_factor × cross_novelty_factor; probability = weight / sum(eligible weights)",
                "base_weight": "Flat 1.0 for each eligible track; no per-track popularity is invented.",
                "recency_factor": f"Saved rounds-since-played factor; cooldown is {RECENCY_COOLDOWN} rounds.",
                "cross_novelty_factor": (
                    f"Era and derived mood-group novelty, clamped to {CROSS_NOVELTY_FLOOR:.1f}–1.0, normalized within the eligible pool."
                    if cross_novelty_recorded else
                    "Not applied or recorded in this legacy round; the saved weight used base × recency only."
                ),
                "era_novelty": "Saved rounds since any track from the candidate's decade last played; null when era was the winning criterion." if cross_novelty_recorded else "Not recorded for this legacy round.",
                "mood_novelty": "Saved rounds since any track from the candidate's mood group last played; null when mood was the winning criterion." if cross_novelty_recorded else "Not recorded for this legacy round.",
                "mood_grouping": "Mood labels are grouped by their most-shared informative token across distinct catalog labels." if cross_novelty_recorded else "No mood-group snapshot exists for this legacy round.",
                "slot_occupancy": "The saved eligibility flag follows slot exclusion and any recorded exception fallback. A depleted criterion yields to another voted criterion, or the no-vote/catalog fallback when no alternative can win.",
                "vote_history": "The saved button's decayed win/interest inputs are recomputed from rounds before this round. They affect category offering, not candidate track weights.",
                "sampling": "The saved probability snapshot and selected-track flag record the deterministic resolution used.",
            },
        }))
    except psycopg.Error as exc:
        return error(f"Could not load round weights: {exc}", 503)


@api.post("/vote")
def cast_vote():
    payload = request.get_json(silent=True) or {}
    try:
        guest_id = int(payload["guest_id"])
        choice_index = int(payload["choice_index"])
    except (KeyError, TypeError, ValueError):
        return error("Provide integer guest_id and choice_index fields", 400)
    return sim_or_error(lambda sim: sim.vote(guest_id, choice_index))


@api.post("/guest/select")
def select_guest():
    payload = request.get_json(silent=True) or {}
    try:
        guest_id = int(payload["guest_id"])
    except (KeyError, TypeError, ValueError):
        return error("Provide an integer guest_id field", 400)
    return sim_or_error(lambda sim: sim.select_guest(guest_id))


@api.post("/guest/add")
def add_guest():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload.get("name"), str):
        return error("Provide a guest name", 400)
    return sim_or_error(lambda sim: sim.add_guest(payload["name"]))


@api.post("/guest/remove")
def remove_guest():
    payload = request.get_json(silent=True) or {}
    try:
        guest_id = int(payload["guest_id"])
    except (KeyError, TypeError, ValueError):
        return error("Provide an integer guest_id field", 400)
    return sim_or_error(lambda sim: sim.remove_guest(guest_id))


@api.post("/catalog/track")
def add_catalog_track():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return error("Provide song fields as a JSON object", 400)
    return sim_or_error(lambda sim: sim.add_catalog_track(payload))


@api.post("/catalog/choice")
def add_catalog_choice():
    payload = request.get_json(silent=True) or {}
    choice_type = payload.get("choice_type")
    choice_value = payload.get("choice_value")
    if not isinstance(choice_type, str) or not isinstance(choice_value, str):
        return error("Provide choice_type and choice_value strings", 400)
    return sim_or_error(lambda sim: sim.add_catalog_choice(choice_type, choice_value))


@api.post("/clock")
def clock_controls():
    payload = request.get_json(silent=True) or {}

    def update(sim):
        if "speed" in payload:
            sim.set_speed(int(payload["speed"]))
        if "paused" in payload:
            sim.set_paused(payload["paused"])

    try:
        return sim_or_error(update)
    except (TypeError, ValueError):
        return error("Invalid clock control value", 400)


@api.post("/host/skip")
def host_skip():
    payload = request.get_json(silent=True) or {}
    try:
        guest_id = int(payload["guest_id"])
    except (KeyError, TypeError, ValueError):
        return error("Provide the acting host's integer guest_id", 400)
    return sim_or_error(lambda sim: sim.skip(guest_id))


@api.post("/host/remove-choice")
def host_remove_choice():
    payload = request.get_json(silent=True) or {}
    try:
        guest_id = int(payload["guest_id"])
        choice_index = int(payload["choice_index"])
    except (KeyError, TypeError, ValueError):
        return error("Provide integer guest_id and choice_index fields", 400)
    return sim_or_error(lambda sim: sim.remove_choice(guest_id, choice_index))


@api.post("/party/rewind")
def rewind_party():
    payload = request.get_json(silent=True) or {}
    try:
        round_number = int(payload["round_number"])
    except (KeyError, TypeError, ValueError):
        return error("Provide an integer round_number to rewind to", 400)
    return sim_or_error(lambda sim: sim.rewind_to(round_number))


@api.post("/party/end")
def end_party():
    return sim_or_error(lambda sim: sim.end_party())


@api.get("/party/analytics")
def party_analytics():
    sim = current_simulation()
    if sim is None or not sim.ended:
        return error("End the night before loading its analytics", 409)
    return jsonify(sim.analytics)
