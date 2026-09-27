"""In-process round loop for the single-operator local simulation."""

from copy import deepcopy
import re
from threading import Lock
import time

from .db import connect_db
from .selection import (
    choose_buttons,
    choice_has_fresh_track,
    played_rounds_from_db,
    resolve_track,
    selection_history_from_db,
    winning_choice,
    choice_history_effects,
    room_vote_effects,
    button_strategy_label,
)


ROUND_SECONDS = 180
SLOT_NAMES = ["Now Playing", "Up Next", "On Deck"]
INITIAL_GUESTS = (
    ("Alex Morgan", "guest"),
    ("Blair Kim", "guest"),
    ("Casey Patel", "guest"),
    ("Dev Walker", "guest"),
    ("Eli Chen", "guest"),
    ("Fran Rivera", "guest"),
    ("Gray Thompson", "guest"),
    ("Harper Jones", "guest"),
    ("Indigo Brooks", "host"),
    ("Jules Reed", "guest"),
)


def public_track(track):
    if track is None:
        return None
    return {
        "id": track["id"], "song_name": track["song_name"], "artist": track["artist"],
        "genre": track["genre"], "year": track["year"],
        "duration_sec": track["duration_sec"], "mood": track["mood"],
    }


class PartySimulation:
    def __init__(self, guests, tracks):
        self.guests = guests
        self.tracks = tracks
        self.round_number = 1
        self.seeding = True
        self.seed_index = 0
        self.slots = [None, None, None]
        self.round_id = None
        self.choice_ids = []
        self.history = []
        self.votes = []
        self.tallies = [0, 0, 0]
        self.removed_choice_index = None
        self.choices = []
        self.remaining = ROUND_SECONDS
        self.round_duration = ROUND_SECONDS
        self.speed = 1
        self.paused = False
        self.acting_guest_id = None
        self.ended = False
        self.analytics = None
        self.last_clock = time.monotonic()
        with connect_db() as conn:
            self.round_id, self.choice_ids, self.choices = self._insert_open_round(
                conn, self.round_number, self.seeding, self.slots,
            )
        self._choose_next_guest()

    @staticmethod
    def _slot_key(slot_name):
        return slot_name.lower().replace(" ", "_")

    def _insert_open_round(self, conn, round_number, is_seed, slots):
        self.tracks = conn.execute(
            """SELECT id,song_name,artist,genre,year,duration_sec,mood
               FROM tracks WHERE added_round<=%s ORDER BY id""",
            (round_number,),
        ).fetchall()
        history = selection_history_from_db(conn)
        duration_seconds = ROUND_SECONDS if is_seed else int(slots[0]["duration_sec"])
        opening_guest_id = self.acting_guest_id
        if opening_guest_id not in {guest["id"] for guest in self.guests}:
            opening_guest_id = self.guests[0]["id"] if self.guests else None
        round_id = conn.execute(
            """INSERT INTO rounds
               (round_number,is_seed_round,time_limit_minutes,start_speed,start_acting_guest_id)
               VALUES (%s,%s,%s,%s,%s) RETURNING id""",
            (round_number, is_seed, duration_seconds / 60, self.speed, opening_guest_id),
        ).fetchone()["id"]
        # The persisted round ID makes button picks reproducible on replay,
        # while distinct New Game rounds still get a fresh sample.
        offers = conn.execute(
            """SELECT id,choice_type,choice_value FROM pending_choice_offers
               WHERE created_round<%s AND consumed_round IS NULL
               ORDER BY id LIMIT 3""",
            (round_number,),
        ).fetchall()
        choices = choose_buttons(self.tracks, history, round_number, round_id, offers)
        for choice in choices:
            if choice.get("operator_added"):
                conn.execute(
                    """UPDATE pending_choice_offers SET consumed_round=%s
                       WHERE created_round<%s AND consumed_round IS NULL
                         AND choice_type=%s AND choice_value=%s""",
                    (round_number, round_number, choice["type"], choice["value"]),
                )
        conn.cursor().executemany(
            "INSERT INTO slots (round_id,slot_name,track_id) VALUES (%s,%s,%s)",
            [(round_id, self._slot_key(SLOT_NAMES[index]), track["id"] if track else None)
             for index, track in enumerate(slots)],
        )
        choice_ids = []
        for index, choice in enumerate(choices, start=1):
            choice_id = conn.execute(
                """INSERT INTO choices
                   (round_id,button_index,choice_type,choice_value,operator_added)
                   VALUES (%s,%s,%s,%s,%s) RETURNING id""",
                (round_id, index, choice["type"], choice["value"],
                 choice.get("operator_added", False)),
            ).fetchone()["id"]
            choice_ids.append(choice_id)
        return round_id, choice_ids, choices

    def _choose_next_guest(self):
        voted = {vote["guest_id"] for vote in self.votes}
        eligible = [guest for guest in self.guests if guest.get("is_active", True)
                    and guest["id"] not in voted
                    and guest.get("joined_round", 1) <= self.round_number]
        if self.acting_guest_id not in {guest["id"] for guest in eligible}:
            self.acting_guest_id = eligible[0]["id"] if eligible else None

    def _locked_track_ids(self):
        return [track["id"] for track in self.slots if track is not None]

    def _effective_resolution_choice(self, proposed_index, tallies, fresh_indices, history):
        """Apply the same exhausted-choice fallback used at round close."""
        if proposed_index in fresh_indices:
            return self.choices[proposed_index], "normal", None, None, False

        fallback_from = self.choices[proposed_index]["label"]
        voted_fresh = [index for index in fresh_indices if tallies[index] > 0]
        if voted_fresh:
            winner, method, tie_break_rule = winning_choice(
                self.choices, tallies, self.choice_ids,
                self.round_number, history, eligible_indices=voted_fresh,
            )
            return winner, method, tie_break_rule, fallback_from, False

        if fresh_indices:
            winner, method, tie_break_rule = winning_choice(
                self.choices, [0] * len(self.choices), self.choice_ids,
                self.round_number, history, eligible_indices=fresh_indices,
            )
            return winner, method, tie_break_rule, fallback_from, False

        # Every offered criterion is exhausted. Preserve the deterministic
        # empty-window winner, but resolve it against the full unlocked catalog.
        winner, method, tie_break_rule = winning_choice(
            self.choices, [0] * len(self.choices), self.choice_ids,
            self.round_number, history,
        )
        return winner, method, tie_break_rule, fallback_from, True

    def track_weight_previews(self):
        """Preview the per-track distribution the close logic would actually use."""
        with connect_db() as conn:
            last_played = played_rounds_from_db(conn)
            history = selection_history_from_db(conn)
            fresh_indices = [
                index for index, choice in enumerate(self.choices)
                if choice_has_fresh_track(
                    choice, self.tracks, self._locked_track_ids(),
                    last_played, self.round_number,
                )
            ]
            fresh_status = [index in fresh_indices for index in range(len(self.choices))]
            tracks_by_id = {track["id"]: track for track in self.tracks}
            previews = []
            for index, choice in enumerate(self.choices):
                (effective_choice, method, tie_break_rule, fallback_from,
                 force_catalog_fallback) = self._effective_resolution_choice(
                    index, self.tallies, fresh_indices, history,
                )
                effective_index = self.choices.index(effective_choice)
                effective_choice_id = self.choice_ids[effective_index]
                resolution_choice = (
                    {**effective_choice, "type": "wildcard"}
                    if force_catalog_fallback else effective_choice
                )
                track, exception, candidate_count, distribution = resolve_track(
                    resolution_choice, self.tracks, self._locked_track_ids(), last_played,
                    self.round_number, effective_choice_id,
                )
                if force_catalog_fallback:
                    exception = "all_criteria_in_cooldown_catalog_fallback"
                elif fallback_from is not None:
                    exception = (
                        f"exhausted_criteria_empty_window:{fallback_from}"
                        if method == "empty_window" else
                        f"exhausted_criteria_alternate_vote:{fallback_from}"
                    )
                if fallback_from is None:
                    resolution_note = None
                    fallback_kind = None
                elif force_catalog_fallback:
                    resolution_note = (
                        f"This choice is exhausted and all offered choices are depleted. "
                        f"The close logic uses the unlocked-catalog fallback; its distribution "
                        f"selects {track['song_name']} · {track['artist']}."
                    )
                    fallback_kind = "catalog_fallback"
                elif method == "empty_window":
                    resolution_note = (
                        f"This choice is exhausted. No fresh choice has votes, so the "
                        f"empty-window rule currently selects {effective_choice['label']} "
                        f"from the fresh choices. Its distribution is shown."
                    )
                    fallback_kind = "empty_window"
                else:
                    resolution_note = (
                        f"This choice is exhausted. With the current votes, close logic "
                        f"redirects to the fresh choice {effective_choice['label']}; its "
                        f"distribution is shown. It can change if the votes change."
                    )
                    fallback_kind = "alternate_vote"
                previews.append({
                    "index": index + 1,
                    "choice_id": self.choice_ids[index],
                    "choice_type": choice["type"],
                    "choice_value": choice["value"],
                    "choice_label": choice["label"],
                    "effective_choice_label": effective_choice["label"],
                    "resolution_fallback": fallback_kind,
                    "strategy": "Operator addition" if choice.get("operator_added") else (
                        button_strategy_label(index + 1, self.round_number, self.seeding)
                    ),
                    "choice_history": choice_history_effects(
                        choice, history, self.round_number,
                    ),
                    "room_vote_effects": room_vote_effects(
                        choice, history, self.round_number,
                    ),
                    "eligible_track_count": sum(1 for item in distribution if item["is_eligible"]),
                    "criteria_has_fresh_tracks": fresh_status[index],
                    "resolution_note": resolution_note,
                    "preview_track": public_track(track),
                    "exception": exception,
                    "distribution": [
                        {
                            **item,
                            "song_name": tracks_by_id[item["track_id"]]["song_name"],
                            "artist": tracks_by_id[item["track_id"]]["artist"],
                            "genre": tracks_by_id[item["track_id"]]["genre"],
                            "year": tracks_by_id[item["track_id"]]["year"],
                        }
                        for item in distribution
                    ],
                })
        return previews

    def _record_round(self, close_reason):
        with connect_db() as conn:
            history = selection_history_from_db(conn)
            last_played = played_rounds_from_db(conn)
            winner, method, tie_break_rule = winning_choice(
                self.choices, self.tallies, self.choice_ids, self.round_number, history,
            )
            winning_index = self.choices.index(winner)
            fresh_indices = [
                index for index, choice in enumerate(self.choices)
                if choice_has_fresh_track(
                    choice, self.tracks, self._locked_track_ids(),
                    last_played, self.round_number,
                )
            ]
            fallback_from = None
            force_catalog_fallback = False
            if winning_index not in fresh_indices:
                (winner, method, tie_break_rule, fallback_from,
                 force_catalog_fallback) = self._effective_resolution_choice(
                    winning_index, self.tallies, fresh_indices, history,
                )

            choice_id = self.choice_ids[self.choices.index(winner)]
            resolution_choice = {**winner, "type": "wildcard"} if force_catalog_fallback else winner
            track, exception, candidate_count, distribution = resolve_track(
                resolution_choice, self.tracks, self._locked_track_ids(),
                last_played, self.round_number, choice_id,
            )
            if force_catalog_fallback:
                exception = "all_criteria_in_cooldown_catalog_fallback"
            elif fallback_from is not None:
                if method == "empty_window":
                    exception = f"exhausted_criteria_empty_window:{fallback_from}"
                else:
                    exception = f"exhausted_criteria_alternate_vote:{fallback_from}"
            track_data = public_track(track)
            round_record = {
                "round_number": self.round_number,
                "is_seeding": self.seeding,
                "seed_slot": SLOT_NAMES[self.seed_index] if self.seeding else None,
                "choices": [
                    {**deepcopy(choice), "index": index + 1,
                     "removed": self.removed_choice_index == index + 1}
                    for index, choice in enumerate(self.choices)
                ],
                "tallies": list(self.tallies),
                "votes": deepcopy(self.votes),
                "close_reason": close_reason,
                "played_track": None if self.seeding else public_track(self.slots[0]),
                "winner_key": winner["key"] if method != "empty_window" else None,
                "winner_label": (
                    f"No fresh voted criteria · no-vote fallback: {winner['label']}"
                    if fallback_from is not None and method == "empty_window" else
                    f"{winner['label']} · alternate to cooled-down {fallback_from}"
                    if fallback_from is not None else
                    winner["label"] if method != "empty_window" else f"No votes · {winner['label']} fallback"
                ),
                "method": method,
                "tie_break_rule": tie_break_rule,
                "track": track_data,
                "resolution_exception": exception,
            }

            if self.seeding:
                next_slots = list(self.slots)
                event_slot = self._slot_key(SLOT_NAMES[self.seed_index])
                next_slots[self.seed_index] = track_data
            else:
                # Resolve the current On Deck choice, then shift slots atomically.
                event_slot = "on_deck"
                next_slots = [self.slots[1], track_data, None]

            database_close_reason = (
                "operator_forced" if close_reason in ("host_skip", "operator_end")
                else "empty_seed_cap" if self.seeding
                else "track_ended"
            )
            database_method = method
            next_round_number = self.round_number + 1
            next_is_seed = next_round_number <= 3

            conn.execute(
                "UPDATE slots SET track_id=%s WHERE round_id=%s AND slot_name=%s",
                (track_data["id"], self.round_id, event_slot),
            )
            conn.execute(
                """INSERT INTO resolution_events
                   (round_id,choice_id,winning_track_id,method,tie_break_rule,slot_name)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                (self.round_id, choice_id, track_data["id"], database_method,
                 tie_break_rule, event_slot),
            )
            conn.cursor().executemany(
                """INSERT INTO resolution_weight_snapshots
                   (round_id,choice_id,track_id,base_weight,recency_factor,
                    era_novelty_rounds,mood_group,mood_novelty_rounds,cross_novelty_factor,weight,
                    probability,is_eligible,is_selected,exception)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                [(self.round_id, choice_id, item["track_id"], item["base_weight"],
                  item["recency_factor"], item["era_novelty_rounds"],
                  item["mood_group"], item["mood_novelty_rounds"],
                  item["cross_novelty_factor"], item["weight"], item["probability"],
                  item["is_eligible"], item["is_selected"], item["exception"])
                 for item in distribution],
            )
            conn.execute(
                """UPDATE rounds SET status='closed',ended_at=now(),close_reason=%s,
                   elapsed_minutes=%s WHERE id=%s""",
                (database_close_reason, (self.round_duration - max(0, self.remaining)) / 60, self.round_id),
            )
            next_round_id, next_choice_ids, next_choices = self._insert_open_round(
                conn, next_round_number, next_is_seed, next_slots,
            )

        self.history.append(round_record)
        self.slots = next_slots
        if self.seeding:
            self.seed_index += 1
            if self.seed_index == 3:
                self.seeding = False
        self.round_number = next_round_number
        self.round_id = next_round_id
        self.choice_ids = next_choice_ids
        self.choices = next_choices
        self.round_duration = ROUND_SECONDS if next_is_seed else int(next_slots[0]["duration_sec"])
        self.votes = []
        self.tallies = [0, 0, 0]
        self.removed_choice_index = None
        self.remaining = self.round_duration
        self._choose_next_guest()

    def _clock(self):
        if self.ended:
            return
        now = time.monotonic()
        if not self.paused:
            self.remaining -= (now - self.last_clock) * self.speed
        self.last_clock = now
        while self.remaining <= 0:
            self._record_round("timer")

    def state(self):
        self._clock()
        voted = {vote["guest_id"] for vote in self.votes}
        guests = [{**guest, "is_active": guest.get("is_active", True),
                   "has_voted": guest["id"] in voted,
                   "eligible_this_round": guest.get("is_active", True)
                   and guest.get("joined_round", 1) <= self.round_number}
                  for guest in self.guests]
        return {
            "guests": guests,
            "eligible_guest_count": sum(guest["eligible_this_round"] for guest in guests),
            "round_number": self.round_number,
            "is_seeding": self.seeding,
            "seed_slot": SLOT_NAMES[self.seed_index] if self.seeding else None,
            "slots": [
                {"name": name, "track": public_track(self.slots[index])}
                for index, name in enumerate(SLOT_NAMES)
            ],
            "choices": [
                {**choice, "index": index + 1, "votes": self.tallies[index],
                 "removed": self.removed_choice_index == index + 1,
                 "strategy": "Operator addition" if choice.get("operator_added") else
                 button_strategy_label(index + 1, self.round_number, self.seeding)}
                for index, choice in enumerate(self.choices)
            ],
            "removed_choice_index": self.removed_choice_index,
            "votes": deepcopy(self.votes),
            "history": deepcopy(self.history),
            "remaining_seconds": max(0, int(self.remaining)),
            "round_duration_seconds": self.round_duration,
            "speed": self.speed,
            "paused": self.paused,
            "acting_guest_id": self.acting_guest_id,
            "ended": self.ended,
            "analytics": deepcopy(self.analytics) if self.ended else None,
            "catalog_track_count": len(self.tracks),
        }

    def vote(self, guest_id, choice_index):
        round_at_tap = self.round_number
        self._clock()
        if self.ended:
            raise ValueError("The night has ended; start a new game to continue")
        if self.round_number != round_at_tap:
            raise ValueError("The round ended; the vote was not counted")
        if self.paused:
            raise ValueError("Voting is paused")
        if choice_index not in (1, 2, 3):
            raise ValueError("Choice must be 1, 2, or 3")
        if self.removed_choice_index == choice_index:
            raise ValueError("The host removed this choice; it no longer accepts votes")
        guest = next((item for item in self.guests if item["id"] == guest_id), None)
        if not guest or not guest.get("is_active", True):
            raise ValueError("Guest not found")
        if guest.get("joined_round", 1) > self.round_number:
            raise ValueError("This guest becomes eligible next round")
        if guest_id != self.acting_guest_id:
            raise ValueError("Switch to this guest before voting")
        if any(vote["guest_id"] == guest_id for vote in self.votes):
            raise ValueError("This guest has already voted this round")
        choice = self.choices[choice_index - 1]
        with connect_db() as conn:
            conn.execute(
                "INSERT INTO votes (round_id,choice_id,guest_id) VALUES (%s,%s,%s)",
                (self.round_id, self.choice_ids[choice_index - 1], guest_id),
            )
        self.votes.append({
            "guest_id": guest_id, "guest_name": guest["name"],
            "choice_index": choice_index, "choice_key": choice["key"],
            "choice_label": choice["label"],
        })
        self.tallies[choice_index - 1] += 1
        self._choose_next_guest()

    def select_guest(self, guest_id):
        self._clock()
        if self.ended:
            raise ValueError("The night has ended; start a new game to continue")
        if not any(guest["id"] == guest_id and guest.get("is_active", True)
                   for guest in self.guests):
            raise ValueError("Guest not found")
        guest = next(item for item in self.guests if item["id"] == guest_id)
        if guest.get("joined_round", 1) > self.round_number:
            raise ValueError("This guest becomes eligible next round")
        has_voted = any(vote["guest_id"] == guest_id for vote in self.votes)
        if has_voted and guest["role"] != "host":
            raise ValueError("That guest has already voted this round")
        self.acting_guest_id = guest_id

    def add_guest(self, name):
        self._clock()
        if self.ended:
            raise ValueError("The night has ended; start a new game to continue")
        if not isinstance(name, str):
            raise ValueError("Enter a guest name")
        name = name.strip()
        if not name:
            raise ValueError("Enter a guest name")
        if len(name) > 64:
            raise ValueError("Guest names must be 64 characters or fewer")
        joined_round = self.round_number + 1
        with connect_db() as conn:
            if conn.execute(
                "SELECT 1 FROM guests WHERE lower(name)=lower(%s) LIMIT 1", (name,),
            ).fetchone():
                raise ValueError("A guest with that name has already been used")
            guest = conn.execute(
                """INSERT INTO guests(name,role,joined_round)
                   VALUES (%s,'guest',%s)
                   RETURNING id,name,role,joined_round""",
                (name, joined_round),
            ).fetchone()
        self.guests.append({**dict(guest), "is_active": True})
        return guest

    def remove_guest(self, guest_id):
        self._clock()
        if self.ended:
            raise ValueError("The night has ended; start a new game to continue")
        guest = next((item for item in self.guests
                      if item["id"] == guest_id and item.get("is_active", True)), None)
        if guest is None:
            raise ValueError("Guest not found")
        if guest["role"] == "host":
            raise ValueError("The host cannot be removed")
        with connect_db() as conn:
            conn.execute("UPDATE guests SET is_active=false WHERE id=%s", (guest_id,))
        guest["is_active"] = False
        if self.acting_guest_id == guest_id:
            self.acting_guest_id = None
        self._choose_next_guest()
        return {"removed_guest": {"id": guest["id"], "name": guest["name"]}}

    @staticmethod
    def _clean_text(value, field, limit=180):
        if not isinstance(value, str):
            raise ValueError(f"Enter a {field}")
        value = value.strip()
        if not value:
            raise ValueError(f"Enter a {field}")
        if len(value) > limit:
            raise ValueError(f"{field.capitalize()} must be {limit} characters or fewer")
        return value

    def _queue_choice_offer(self, conn, choice_type, choice_value):
        queued = conn.execute(
            """SELECT count(*) AS count FROM pending_choice_offers
               WHERE created_round<%s AND consumed_round IS NULL""",
            (self.round_number + 1,),
        ).fetchone()["count"]
        if queued >= 3:
            raise ValueError("The next-round choice queue is full; add this after the next round opens")
        duplicate = conn.execute(
            """SELECT 1 FROM pending_choice_offers
               WHERE choice_type=%s AND choice_value=%s AND consumed_round IS NULL LIMIT 1""",
            (choice_type, choice_value),
        ).fetchone()
        if duplicate:
            raise ValueError("That choice is already queued for a future round")
        conn.execute(
            """INSERT INTO pending_choice_offers(choice_type,choice_value,created_round)
               VALUES (%s,%s,%s)""",
            (choice_type, choice_value, self.round_number),
        )

    def add_catalog_track(self, payload):
        self._clock()
        if self.ended:
            raise ValueError("The night has ended; start a new game to edit the catalog")
        song_name = self._clean_text(payload.get("song_name"), "song title")
        artist = self._clean_text(payload.get("artist"), "artist")
        genre = self._clean_text(payload.get("genre"), "genre")
        mood = self._clean_text(payload.get("mood"), "mood")
        try:
            year = int(payload.get("year"))
        except (TypeError, ValueError):
            raise ValueError("Enter a four-digit release year")
        if not 1800 <= year <= 2200:
            raise ValueError("Release year must be between 1800 and 2200")
        duration = payload.get("duration")
        if not isinstance(duration, str) or not re.fullmatch(r"\d{1,3}:[0-5]\d", duration.strip()):
            raise ValueError("Duration must use m:ss format, such as 3:35")
        minutes, seconds = (int(part) for part in duration.strip().split(":"))
        duration_sec = minutes * 60 + seconds
        if duration_sec <= 0:
            raise ValueError("Duration must be longer than zero")

        with connect_db() as conn:
            duplicate = conn.execute(
                "SELECT 1 FROM tracks WHERE song_name=%s AND artist=%s AND year=%s LIMIT 1",
                (song_name, artist, year),
            ).fetchone()
            if duplicate:
                raise ValueError("That song by this artist and year is already in the catalog")
            track = conn.execute(
                """INSERT INTO tracks(song_name,artist,genre,year,duration_sec,mood,added_round)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)
                   RETURNING id,song_name,artist,genre,year,duration_sec,mood""",
                (song_name, artist, genre, year, duration_sec, mood, self.round_number + 1),
            ).fetchone()
            self._queue_choice_offer(conn, "specific_song", str(track["id"]))
        return {
            "track": public_track(track),
            "available_round": self.round_number + 1,
            "queued_choice": song_name,
        }

    def add_catalog_choice(self, choice_type, raw_value):
        self._clock()
        if self.ended:
            raise ValueError("The night has ended; start a new game to edit choices")
        if choice_type not in ("genre", "artist", "era"):
            raise ValueError("Choice type must be genre, artist, or era")
        value = self._clean_text(raw_value, "choice value")
        with connect_db() as conn:
            if choice_type == "era":
                era_text = value[:-1] if value.lower().endswith("s") else value
                if not re.fullmatch(r"\d{2}|\d{4}", era_text):
                    raise ValueError("Enter an era such as 90s or 2010s")
                year = int(era_text)
                if year < 100:
                    year = (2000 + year) if year < 30 else (1900 + year)
                value = str((year // 10) * 10)
                exists = conn.execute(
                    """SELECT 1 FROM tracks WHERE added_round<=%s
                       AND (year/10)*10=%s LIMIT 1""",
                    (self.round_number + 1, int(value)),
                ).fetchone()
            else:
                column = "genre" if choice_type == "genre" else "artist"
                exists = conn.execute(
                    f"""SELECT {column} AS value FROM tracks
                        WHERE added_round<=%s AND lower({column})=lower(%s) LIMIT 1""",
                    (self.round_number + 1, value),
                ).fetchone()
                if exists:
                    value = exists["value"]
            if not exists:
                raise ValueError("No catalog track matches that category in the next round")
            self._queue_choice_offer(conn, choice_type, value)
        return {
            "choice_type": choice_type,
            "choice_value": value,
            "available_round": self.round_number + 1,
        }

    def build_analytics(self):
        choice_totals = {}
        guest_totals = {
            guest["id"]: {
                "guest_id": guest["id"], "guest_name": guest["name"],
                "votes": 0, "winning_picks": 0,
            }
            for guest in self.guests
        }
        winners = []
        tracks_played = []
        for round_record in self.history:
            for vote in round_record["votes"]:
                key = vote["choice_key"]
                tally = choice_totals.setdefault(key, {
                    "choice_key": key,
                    "choice_label": vote["choice_label"],
                    "choice_type": key.split("|", 1)[0],
                    "choice_value": key.split("|", 1)[1],
                    "votes": 0,
                })
                tally["votes"] += 1
                guest_total = guest_totals.setdefault(vote["guest_id"], {
                    "guest_id": vote["guest_id"], "guest_name": vote["guest_name"],
                    "votes": 0, "winning_picks": 0,
                })
                guest_total["votes"] += 1
                if round_record.get("winner_key") == key:
                    guest_total["winning_picks"] += 1

            winners.append({
                "round_number": round_record["round_number"],
                "winning_choice": round_record["winner_label"],
                "winning_choice_key": round_record.get("winner_key"),
                "resolved_track": round_record["track"],
                "method": round_record["method"],
                "close_reason": round_record["close_reason"],
            })
            if round_record.get("played_track"):
                tracks_played.append({
                    "round_number": round_record["round_number"],
                    **round_record["played_track"],
                })

        guest_rankings = []
        for item in guest_totals.values():
            if item["votes"]:
                guest_rankings.append({
                    **item,
                    "hit_rate": item["winning_picks"] / item["votes"],
                    "eligible_for_award": item["votes"] >= 3,
                })
        eligible_rankings = [item for item in guest_rankings if item["eligible_for_award"]]
        eligible_rankings.sort(key=lambda item: (
            -item["hit_rate"], -item["winning_picks"], -item["votes"],
            item["guest_name"].casefold(),
        ))
        return {
            "rounds_completed": len(self.history),
            "total_votes": sum(item["votes"] for item in choice_totals.values()),
            "most_voted_choices": sorted(
                choice_totals.values(),
                key=lambda item: (-item["votes"], item["choice_label"].casefold()),
            ),
            "winners_per_round": winners,
            "tracks_played": tracks_played,
            "guest_vote_records": sorted(
                guest_rankings, key=lambda item: item["guest_name"].casefold(),
            ),
            "best_taste": {
                "metric": "winning picks / votes cast",
                "minimum_votes": 3,
                "few_vote_rule": (
                    "Guests with fewer than three votes are not eligible, so a tiny sample "
                    "cannot win on a perfect hit rate. Ties favor more winning picks, then "
                    "more votes, then alphabetical guest name."
                ),
                "winner": eligible_rankings[0] if eligible_rankings else None,
            },
        }

    def end_party(self):
        if self.ended:
            return
        starting_round = self.round_number
        self._clock()
        if self.round_number == starting_round:
            self._record_round("operator_end")

        # Round resolution opens the next round as part of the normal loop.
        # Remove that empty placeholder so a finished night has no open round.
        open_round_number = self.round_number
        if self.round_id is not None:
            with connect_db() as conn:
                conn.execute(
                    "DELETE FROM rounds WHERE id=%s AND status='open'",
                    (self.round_id,),
                )
                conn.execute(
                    "UPDATE pending_choice_offers SET consumed_round=NULL WHERE consumed_round=%s",
                    (open_round_number,),
                )

        self.round_number = self.history[-1]["round_number"] if self.history else starting_round
        self.round_id = None
        self.choice_ids = []
        self.choices = []
        self.votes = []
        self.tallies = [0, 0, 0]
        self.remaining = 0
        self.paused = True
        self.ended = True
        self.analytics = self.build_analytics()

    def set_speed(self, speed):
        self._clock()
        if self.ended:
            raise ValueError("The night has ended; start a new game to continue")
        if speed not in (1, 5, 10, 20):
            raise ValueError("Speed must be 1x, 5x, 10x, or 20x")
        self.speed = speed

    def set_paused(self, paused):
        self._clock()
        if self.ended:
            raise ValueError("The night has ended; start a new game to continue")
        self.paused = bool(paused)

    def rewind_to(self, round_number):
        """Discard the selected round's outcome and future, reopening its start state."""
        self._clock()
        if self.ended:
            raise ValueError("The night has ended; start a new game to rewind")
        try:
            round_number = int(round_number)
        except (TypeError, ValueError):
            raise ValueError("Choose a completed round to rewind to")
        if not any(item["round_number"] == round_number for item in self.history):
            raise ValueError("Only a completed round from this run can be rewound")

        with connect_db() as conn:
            target = conn.execute(
                """SELECT id,round_number,is_seed_round,start_speed,start_acting_guest_id
                   FROM rounds WHERE round_number=%s AND status='closed'""",
                (round_number,),
            ).fetchone()
            if target is None:
                raise ValueError("The selected round is not available to rewind")
            slot_rows = conn.execute(
                "SELECT slot_name,track_id FROM slots WHERE round_id=%s",
                (target["id"],),
            ).fetchall()
            choice_rows = conn.execute(
                """SELECT id,button_index,choice_type,choice_value,operator_added
                   FROM choices WHERE round_id=%s ORDER BY button_index""",
                (target["id"],),
            ).fetchall()
            if len(choice_rows) != 3:
                raise ValueError("The selected round does not contain all three choices")

            # Delete the abandoned branch first so all future votes, resolutions,
            # weight snapshots, and slot rows cascade out of the database.
            conn.execute("DELETE FROM rounds WHERE round_number>%s", (round_number,))
            conn.execute("DELETE FROM votes WHERE round_id=%s", (target["id"],))
            conn.execute("DELETE FROM resolution_events WHERE round_id=%s", (target["id"],))
            conn.execute(
                "DELETE FROM resolution_weight_snapshots WHERE round_id=%s",
                (target["id"],),
            )
            conn.execute(
                "UPDATE choices SET removed_at=NULL WHERE round_id=%s",
                (target["id"],),
            )
            conn.execute("DELETE FROM guests WHERE joined_round>%s", (round_number,))
            conn.execute(
                "UPDATE tracks SET added_round=2147483647 WHERE added_round>%s",
                (round_number,),
            )
            conn.execute(
                "DELETE FROM pending_choice_offers WHERE created_round>=%s",
                (round_number,),
            )
            conn.execute(
                "UPDATE pending_choice_offers SET consumed_round=NULL WHERE consumed_round>%s",
                (round_number,),
            )
            self.tracks = conn.execute(
                """SELECT id,song_name,artist,genre,year,duration_sec,mood
                   FROM tracks WHERE added_round<=%s ORDER BY id""",
                (round_number,),
            ).fetchall()

            slot_ids = {row["slot_name"]: row["track_id"] for row in slot_rows}
            if round_number <= 3:
                # A seeding round only fills its own slot as it closes.
                for slot_name in SLOT_NAMES[round_number - 1:]:
                    slot_ids[self._slot_key(slot_name)] = None
            else:
                # A closed normal-round row stores its resolved On Deck track.
                # The opening slots are the final slots from the prior round.
                previous_slot_rows = conn.execute(
                    """SELECT s.slot_name,s.track_id
                       FROM slots s JOIN rounds r ON r.id=s.round_id
                       WHERE r.round_number=%s""",
                    (round_number - 1,),
                ).fetchall()
                if len(previous_slot_rows) != len(SLOT_NAMES):
                    raise ValueError("The selected round's starting slots are unavailable")
                slot_ids = {row["slot_name"]: row["track_id"] for row in previous_slot_rows}
            conn.cursor().executemany(
                "UPDATE slots SET track_id=%s WHERE round_id=%s AND slot_name=%s",
                [(slot_ids.get(self._slot_key(name)), target["id"], self._slot_key(name))
                 for name in SLOT_NAMES],
            )

            now_playing = slot_ids.get("now_playing")
            duration_seconds = (
                ROUND_SECONDS if target["is_seed_round"]
                else int(next(track["duration_sec"] for track in self.tracks
                              if track["id"] == now_playing))
            )
            conn.execute(
                """UPDATE rounds SET status='open',started_at=now(),ended_at=NULL,
                   close_reason=NULL,elapsed_minutes=0,time_limit_minutes=%s
                   WHERE id=%s""",
                (duration_seconds / 60, target["id"]),
            )

        tracks_by_id = {track["id"]: track for track in self.tracks}
        self.slots = [
            tracks_by_id.get(slot_ids.get(self._slot_key(name))) for name in SLOT_NAMES
        ]
        self.round_number = round_number
        self.seeding = bool(target["is_seed_round"])
        self.seed_index = round_number - 1 if self.seeding else 3
        self.round_id = target["id"]
        self.choice_ids = [row["id"] for row in choice_rows]
        self.choices = []
        for row in choice_rows:
            value = row["choice_value"]
            label = "Surprise me" if row["choice_type"] == "wildcard" else (
                f"{value}s" if row["choice_type"] == "era" else value
            )
            self.choices.append({
                "type": row["choice_type"], "value": value,
                "key": f"{row['choice_type']}|{value}", "label": label,
                "operator_added": row["operator_added"],
            })
        self.history = [item for item in self.history if item["round_number"] < round_number]
        self.guests = [guest for guest in self.guests
                       if guest.get("joined_round", 1) <= round_number]
        self.votes = []
        self.tallies = [0, 0, 0]
        self.removed_choice_index = None
        self.round_duration = duration_seconds
        self.remaining = float(duration_seconds)
        self.speed = target["start_speed"] or 1
        known_guest_ids = {guest["id"] for guest in self.guests}
        saved_guest_id = target["start_acting_guest_id"]
        self.acting_guest_id = saved_guest_id if saved_guest_id in known_guest_ids else None
        self.paused = False
        self.last_clock = time.monotonic()
        self._choose_next_guest()

    def skip(self, guest_id):
        round_at_tap = self.round_number
        self._clock()
        if self.ended:
            raise ValueError("The night has ended; start a new game to continue")
        if self.round_number != round_at_tap:
            raise ValueError("The round already ended")
        guest = next((item for item in self.guests if item["id"] == guest_id), None)
        if not guest or guest["role"] != "host" or self.acting_guest_id != guest_id:
            raise ValueError("Switch to the host to skip the round")
        if self.paused:
            raise ValueError("Resume the party before skipping")
        self._record_round("host_skip")

    def remove_choice(self, guest_id, choice_index):
        round_at_tap = self.round_number
        self._clock()
        if self.ended:
            raise ValueError("The night has ended; start a new game to continue")
        if self.round_number != round_at_tap:
            raise ValueError("The round already ended")
        guest = next((item for item in self.guests if item["id"] == guest_id), None)
        if not guest or guest["role"] != "host" or self.acting_guest_id != guest_id:
            raise ValueError("Switch to the host to remove a choice")
        if self.paused:
            raise ValueError("Resume the party before removing a choice")
        if choice_index not in (1, 2, 3):
            raise ValueError("Choice must be 1, 2, or 3")
        if self.removed_choice_index is not None:
            raise ValueError("The host has already removed a choice this round")
        choice_id = self.choice_ids[choice_index - 1]
        with connect_db() as conn:
            conn.execute(
                "UPDATE choices SET removed_at=now() WHERE id=%s AND round_id=%s",
                (choice_id, self.round_id),
            )
        self.removed_choice_index = choice_index

simulation = None
simulation_lock = Lock()


def current_simulation():
    return simulation


def start_new_game(guests, tracks):
    global simulation
    with simulation_lock:
        simulation = PartySimulation(guests, tracks)
        return simulation
