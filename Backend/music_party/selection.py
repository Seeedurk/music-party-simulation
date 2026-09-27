"""Button choice, tie-break, and song resolution algorithms."""

import hashlib
import re
from collections import Counter


DECAY_RATE = 0.9
RECENCY_COOLDOWN = 5
CROSS_NOVELTY_FLOOR = 0.5
ROOM_LEARNING_START_ROUND = 5
ROOM_LEARNING_FULL_ROUND = 10
ROOM_VOTE_MAX_INFLUENCE = 0.75


def catalog_choices(tracks):
    """Build available genre, artist, era, and wildcard choices from catalog data."""
    options = {("wildcard", "*")}
    for track in tracks:
        options.add(("genre", track["genre"]))
        options.add(("artist", track["artist"]))
        options.add(("era", (int(track["year"]) // 10) * 10))
    choices = []
    for choice_type, value in sorted(options, key=lambda item: (item[0], str(item[1]))):
        label = "Surprise me" if choice_type == "wildcard" else (
            f"{value}s" if choice_type == "era" else str(value)
        )
        choices.append({
            "type": choice_type,
            "value": str(value),
            "key": f"{choice_type}|{value}",
            "label": label,
        })
    return choices


def selection_history_from_db(conn):
    """Read wins from resolution_events and interest from votes."""
    history_by_round = {}
    vote_rows = conn.execute(
        """SELECT r.round_number,c.choice_type,c.choice_value
           FROM votes v JOIN rounds r ON r.id=v.round_id
           JOIN choices c ON c.id=v.choice_id
           WHERE r.status='closed'
           ORDER BY r.round_number,v.id"""
    ).fetchall()
    for row in vote_rows:
        record = history_by_round.setdefault(row["round_number"], {
            "round_number": row["round_number"], "winner_key": None, "votes": [],
            "buttons": {},
        })
        record["votes"].append({
            "choice_key": f"{row['choice_type']}|{row['choice_value']}",
        })

    win_rows = conn.execute(
        """SELECT r.round_number,c.choice_type,c.choice_value
           FROM resolution_events re JOIN rounds r ON r.id=re.round_id
           LEFT JOIN choices c ON c.id=re.choice_id
           WHERE r.status='closed' AND re.method <> 'empty_window'
           ORDER BY r.round_number,re.id"""
    ).fetchall()
    for row in win_rows:
        if row["choice_type"] is None:
            continue
        record = history_by_round.setdefault(row["round_number"], {
            "round_number": row["round_number"], "winner_key": None, "votes": [],
            "buttons": {},
        })
        record["winner_key"] = f"{row['choice_type']}|{row['choice_value']}"

    button_rows = conn.execute(
        """SELECT r.round_number,c.button_index,c.choice_type,c.choice_value
           FROM choices c JOIN rounds r ON r.id=c.round_id
           WHERE r.status='closed'
           ORDER BY r.round_number,c.button_index"""
    ).fetchall()
    for row in button_rows:
        record = history_by_round.setdefault(row["round_number"], {
            "round_number": row["round_number"], "winner_key": None, "votes": [],
            "buttons": {},
        })
        record["buttons"][row["button_index"]] = (
            f"{row['choice_type']}|{row['choice_value']}"
        )
    return [history_by_round[number] for number in sorted(history_by_round)]


def played_rounds_from_db(conn):
    """Return the most recent playback round for every track that has played."""
    rows = conn.execute(
        """SELECT s.track_id,max(r.round_number) AS last_played_round
           FROM slots s JOIN rounds r ON r.id=s.round_id
           WHERE s.slot_name='now_playing' AND r.is_seed_round=false
                 AND s.track_id IS NOT NULL
           GROUP BY s.track_id"""
    ).fetchall()
    return {row["track_id"]: row["last_played_round"] for row in rows}


def _win_score(history, current_round):
    wins_by_choice = {}
    for record in history:
        key = record.get("winner_key")
        if key:
            wins_by_choice.setdefault(key, set()).add(record["round_number"])
    return {
        key: sum(DECAY_RATE ** (current_round - won_round) for won_round in rounds)
        for key, rounds in wins_by_choice.items()
    }


def _interest_score(history, current_round):
    voted_rounds_by_choice = {}
    for record in history:
        for vote in record["votes"]:
            voted_rounds_by_choice.setdefault(vote["choice_key"], set()).add(record["round_number"])
    return {
        key: sum(DECAY_RATE ** (current_round - vote_round) for vote_round in rounds)
        for key, rounds in voted_rounds_by_choice.items()
    }


def room_learning_influence(current_round):
    """Ramp Button 2 from uniform at round 4 to 75% room influence at round 10."""
    progress = (current_round - (ROOM_LEARNING_START_ROUND - 1)) / (
        ROOM_LEARNING_FULL_ROUND - (ROOM_LEARNING_START_ROUND - 1)
    )
    return ROOM_VOTE_MAX_INFLUENCE * min(1.0, max(0.0, progress))


def _room_vote_mass(history, current_round):
    """Count each vote, with the shared decay rate favoring recent rounds."""
    mass = {}
    for record in history:
        rounds_ago = max(0, current_round - record["round_number"])
        decay = DECAY_RATE ** rounds_ago
        for vote in record.get("votes", []):
            key = vote["choice_key"]
            mass[key] = mass.get(key, 0.0) + decay
    return mass


def room_vote_weights(candidates, history, current_round):
    """Blend uniform exploration with the room's decayed share of all votes."""
    influence = room_learning_influence(current_round)
    mass = _room_vote_mass(history, current_round)
    eligible_total = sum(mass.get(item["key"], 0.0) for item in candidates)
    if not candidates or eligible_total <= 0:
        return {item["key"]: 1.0 for item in candidates}
    count = len(candidates)
    return {
        item["key"]: (1.0 - influence)
        + influence * (mass.get(item["key"], 0.0) * count / eligible_total)
        for item in candidates
    }


def room_vote_effects(choice, history, current_round):
    return {
        "decayed_vote_mass": _room_vote_mass(history, current_round).get(choice["key"], 0.0),
        "learning_influence": room_learning_influence(current_round),
        "uniform_exploration_share": 1.0 - room_learning_influence(current_round),
        "decay_rate": DECAY_RATE,
    }


def button_strategy_label(button_index, round_number, is_seeding=False):
    if is_seeding:
        return "Seed exploration"
    if button_index == 1:
        return "Favors past winners"
    if button_index == 2:
        influence = room_learning_influence(round_number)
        if influence <= 0:
            return "Random exploration"
        percent = int(influence * 100 + 0.5)
        return f"Room learning - {percent}% vote influence"
    return "Voted for, few wins"


def choice_history_effects(choice, history, current_round):
    """Expose the same decayed history inputs used by history-based buttons."""
    wins = _win_score(history, current_round)
    interest = _interest_score(history, current_round)
    key = choice["key"]
    return {
        "win_score": wins.get(key, 0.0),
        "interest_score": interest.get(key, 0.0),
        "demonstrated_interest_score": interest.get(key, 0.0) / (1 + wins.get(key, 0.0)),
        "decay_rate": DECAY_RATE,
    }


def _seeded_sample(candidates, weights, seed_parts):
    """Normalize weights and map a stable SHA-256 roll onto their ranges."""
    ordered = sorted(candidates, key=lambda item: item["key"])
    raw_weights = [max(0.0, float(weights.get(item["key"], 0.0))) for item in ordered]
    total = sum(raw_weights)
    if total <= 0:
        # A zero-score strategy still needs a choice at cold start.
        raw_weights = [1.0] * len(ordered)
        total = len(ordered)
    probabilities = [weight / total for weight in raw_weights]
    seed = "|".join(str(part) for part in seed_parts)
    roll = int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest()[:8], "big") / (2 ** 64)
    cumulative = 0.0
    for candidate, probability in zip(ordered, probabilities):
        cumulative += probability
        if roll < cumulative:
            return candidate
    return ordered[-1]


def _variety_candidates(candidates, history, button_index):
    """Avoid this button's exact choices from the previous three rounds.

    If that blocks every candidate still available for this round, relax the
    oldest round's restriction until at least one option is available.
    """
    recent = [
        (record["round_number"], record.get("buttons", {}).get(button_index))
        for record in sorted(history, key=lambda item: item["round_number"])
        if record.get("buttons", {}).get(button_index)
    ][-3:]
    while recent:
        blocked = {key for _, key in recent}
        available = [item for item in candidates if item["key"] not in blocked]
        if available:
            return available
        recent.pop(0)
    return list(candidates)


def _type_balanced_candidates(candidates, selected, remaining_picks):
    """Keep button types distinct when enough unused types remain to fill slots."""
    used_types = {choice["type"] for choice in selected}
    unused_type_candidates = [
        item for item in candidates if item["type"] not in used_types
    ]
    unused_types = {item["type"] for item in unused_type_candidates}
    if len(unused_types) >= remaining_picks:
        return unused_type_candidates
    return list(candidates)


def choose_buttons(tracks, history, round_number, game_seed, required_choices=None):
    """Select three distinct category buttons with the three configured schemes."""
    options = catalog_choices(tracks)
    required_choices = required_choices or []
    options_by_key = {item["key"]: item for item in options}
    track_by_id = {str(track["id"]): track for track in tracks}
    for required in required_choices:
        choice_type, value = required["choice_type"], str(required["choice_value"])
        if choice_type == "specific_song" and value in track_by_id:
            track = track_by_id[value]
            options_by_key[f"specific_song|{value}"] = {
                "type": "specific_song", "value": value,
                "key": f"specific_song|{value}", "label": track["song_name"],
            }
        elif f"{choice_type}|{value}" in options_by_key:
            continue
        else:
            continue
    options = sorted(options_by_key.values(), key=lambda item: (item["type"], item["key"]))
    required = []
    for offer in required_choices:
        item = options_by_key.get(f"{offer['choice_type']}|{offer['choice_value']}")
        if item is not None and item["key"] not in {choice["key"] for choice in required}:
            required.append({**item, "operator_added": True})

    if len(options) < 3:
        raise ValueError("The catalog needs at least three distinct choices")

    if round_number <= 3:
        selected = list(required[:3])
        remaining = [item for item in options if item["key"] not in {choice["key"] for choice in selected}]
        for button_number in range(len(selected) + 1, 4):
            candidates = _variety_candidates(remaining, history, button_number)
            candidates = _type_balanced_candidates(
                candidates, selected, 4 - button_number
            )
            choice = _seeded_sample(
                candidates, {}, (game_seed, round_number, button_number,
                              *(item["key"] for item in selected)),
            )
            selected.append(choice)
            remaining = [item for item in remaining if item["key"] != choice["key"]]
        return selected

    wins = _win_score(history, round_number)
    interest = _interest_score(history, round_number)
    remaining = list(options)

    # Reserve the interest button's highest-signal pool before the other
    # strategies remove choices to keep the three visible buttons distinct.
    interest_candidates = _type_balanced_candidates(
        _variety_candidates(remaining, history, 3), [], 3
    )
    interest_weights = {
        item["key"]: interest.get(item["key"], 0.0) / (1 + wins.get(item["key"], 0.0))
        for item in interest_candidates
    }
    demonstrated_interest = _seeded_sample(
        interest_candidates, interest_weights,
        (game_seed, round_number, 3),
    )
    remaining.remove(demonstrated_interest)

    preference_candidates = _type_balanced_candidates(
        _variety_candidates(remaining, history, 1), [demonstrated_interest], 2
    )
    preference = _seeded_sample(
        preference_candidates, wins,
        (game_seed, round_number, 1, demonstrated_interest["key"]),
    )
    remaining.remove(preference)

    exploration_candidates = _type_balanced_candidates(
        _variety_candidates(remaining, history, 2),
        [demonstrated_interest, preference], 1,
    )
    exploration = _seeded_sample(
        exploration_candidates,
        room_vote_weights(exploration_candidates, history, round_number),
        (game_seed, round_number, 2,
                        preference["key"], demonstrated_interest["key"]),
    )
    selected = [preference, exploration, demonstrated_interest]
    for required_choice in required[:3]:
        matching_index = next(
            (index for index, choice in enumerate(selected)
             if choice["key"] == required_choice["key"]),
            None,
        )
        if matching_index is not None:
            selected[matching_index] = {**selected[matching_index], "operator_added": True}
            continue
        replacement_index = next(
            (index for index in (1, 0, 2)
             if selected[index]["type"] == required_choice["type"]
             and not selected[index].get("operator_added")),
            None,
        )
        if replacement_index is None:
            replacement_index = next(
                (index for index in (1, 0, 2)
                 if not selected[index].get("operator_added")),
                None,
            )
        if replacement_index is not None:
            selected[replacement_index] = required_choice
    return selected


def _stable_index(round_number, sorted_ids, count):
    seed = f"{round_number}|{'|'.join(str(item) for item in sorted_ids)}"
    value = int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest()[:8], "big")
    return value % count


def winning_choice(choices, tallies, choice_ids, round_number, history, eligible_indices=None):
    """Choose the tally leader, using win novelty then a stable hash for ties."""
    eligible_indices = list(range(len(choices))) if eligible_indices is None else list(eligible_indices)
    if not eligible_indices:
        eligible_indices = list(range(len(choices)))
    high = max((tallies[index] for index in eligible_indices), default=0)
    if high == 0:
        all_ids = sorted(choice_ids[index] for index in eligible_indices)
        selected_id = all_ids[_stable_index(round_number, all_ids, len(all_ids))]
        return choices[choice_ids.index(selected_id)], "empty_window", None

    leaders = [index for index in eligible_indices if tallies[index] == high]
    if len(leaders) == 1:
        return choices[leaders[0]], "normal", None

    last_win = {}
    for record in history:
        key = record.get("winner_key")
        if key:
            last_win[key] = max(last_win.get(key, 0), record["round_number"])
    novelty = {
        index: round_number - last_win[choices[index]["key"]]
        if choices[index]["key"] in last_win else round_number
        for index in leaders
    }
    most_novel = max(novelty.values())
    novelty_leaders = [index for index in leaders if novelty[index] == most_novel]
    if len(novelty_leaders) == 1:
        return choices[novelty_leaders[0]], "tie_break", "novelty"

    tied_ids = sorted(choice_ids[index] for index in novelty_leaders)
    selected_id = tied_ids[_stable_index(round_number, tied_ids, len(tied_ids))]
    return choices[choice_ids.index(selected_id)], "tie_break", "hash_fallback"


def choice_has_fresh_track(choice, tracks, locked_track_ids, last_played_rounds, round_number):
    """Whether a criterion still has an unlocked candidate outside its cooldown."""
    value = choice["value"]
    if choice["type"] == "wildcard":
        pool = tracks
    elif choice["type"] == "specific_song":
        pool = [track for track in tracks if track["id"] == int(value)]
    elif choice["type"] == "genre":
        pool = [track for track in tracks if track["genre"] == value]
    elif choice["type"] == "artist":
        pool = [track for track in tracks if track["artist"] == value]
    elif choice["type"] == "era":
        pool = [track for track in tracks if (int(track["year"]) // 10) * 10 == int(value)]
    else:
        pool = []

    locked = set(locked_track_ids)
    return any(
        track["id"] not in locked
        and (
            track["id"] not in last_played_rounds
            or round_number - last_played_rounds[track["id"]] >= RECENCY_COOLDOWN
        )
        for track in pool
    )


def _track_roll(round_number, choice_id):
    seed = f"{round_number}|{choice_id}"
    return int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest()[:8], "big") / (2 ** 64)


def mood_groups_for_tracks(tracks):
    """Derive stable mood groups from the most-shared informative mood token.

    Token frequency is measured across distinct catalog mood labels. This lets
    phrases such as "Arm-Around-Shoulder Singalong" join the singalong group
    without maintaining hand-authored labels for individual tracks.
    """
    stop_words = {"a", "an", "and", "as", "at", "around", "for", "from", "in", "of", "on", "the", "to", "with"}
    distinct_moods = sorted({str(track.get("mood") or "").strip() for track in tracks})
    tokens_by_mood = {}
    frequency = Counter()
    for mood in distinct_moods:
        tokens = [
            token for token in re.findall(r"[a-z0-9]+", mood.lower().replace("-", ""))
            if token not in stop_words
        ]
        tokens = sorted(set(tokens)) or ["unclassified"]
        tokens_by_mood[mood] = tokens
        frequency.update(tokens)

    return {
        mood: min(tokens, key=lambda token: (-frequency[token], token))
        for mood, tokens in tokens_by_mood.items()
    }


def resolve_track(choice, tracks, locked_track_ids, last_played_rounds,
                  round_number, choice_id):
    """Return a deterministically sampled track and full-catalog distribution."""
    if not tracks:
        raise ValueError("The song catalog is empty; import songs_300.csv first")
    locked = set(locked_track_ids)

    def matches(track):
        if choice["type"] == "wildcard":
            return True
        if choice["type"] == "specific_song":
            return track["id"] == int(choice["value"])
        value = choice["value"]
        if choice["type"] == "genre":
            return track["genre"] == value
        if choice["type"] == "artist":
            return track["artist"] == value
        if choice["type"] == "era":
            return (int(track["year"]) // 10) * 10 == int(value)
        return False

    category_tracks = [track for track in tracks if matches(track)]
    if choice["type"] == "specific_song" and not category_tracks:
        raise ValueError("The selected specific song is missing from the catalog")

    eligible = [track for track in category_tracks if track["id"] not in locked]
    exception = None
    if not eligible:
        # Preserve the winning choice's outcome while still honoring locked
        # slots: broaden to the catalog rather than allowing a duplicate.
        eligible = [track for track in tracks if track["id"] not in locked]
        exception = "catalog_fallback"
    if not eligible:
        raise ValueError("No unlocked catalog track is available to resolve this choice")

    eligible_ids = {track["id"] for track in eligible}
    single_candidate = (
        (choice["type"] == "specific_song" or len(category_tracks) == 1)
        and len(eligible) == 1
        and eligible[0]["id"] in {track["id"] for track in category_tracks}
    )

    mood_groups = mood_groups_for_tracks(tracks)
    era_last_played = {}
    mood_last_played = {}
    for track in tracks:
        last_played = last_played_rounds.get(track["id"])
        if last_played is None:
            continue
        era = (int(track["year"]) // 10) * 10
        mood_group = mood_groups.get(str(track.get("mood") or "").strip(), "unclassified")
        era_last_played[era] = max(era_last_played.get(era, 0), last_played)
        mood_last_played[mood_group] = max(mood_last_played.get(mood_group, 0), last_played)

    includes_era = choice["type"] != "era"
    includes_mood = choice["type"] != "mood"
    era_novelty = {}
    mood_novelty = {}
    raw_cross_novelty = {}
    for track in tracks:
        era = (int(track["year"]) // 10) * 10
        mood_group = mood_groups.get(str(track.get("mood") or "").strip(), "unclassified")
        era_last = era_last_played.get(era)
        mood_last = mood_last_played.get(mood_group)
        era_novelty[track["id"]] = (
            max(0, round_number - era_last) if era_last is not None else round_number
        ) if includes_era else None
        mood_novelty[track["id"]] = (
            max(0, round_number - mood_last) if mood_last is not None else round_number
        ) if includes_mood else None
        raw_cross_novelty[track["id"]] = sum(
            value for value in (era_novelty[track["id"]], mood_novelty[track["id"]])
            if value is not None
        )

    pool_novelty = [raw_cross_novelty[track["id"]] for track in eligible]
    min_novelty = min(pool_novelty, default=0)
    max_novelty = max(pool_novelty, default=0)
    cross_novelty = {}
    for track in tracks:
        track_id = track["id"]
        if single_candidate or (not includes_era and not includes_mood) or max_novelty == min_novelty:
            factor = 1.0
        elif track_id not in eligible_ids:
            # Ineligible tracks carry zero final weight; neutral factor keeps
            # their per-input table legible without entering normalization.
            factor = 1.0
        else:
            normalized = (raw_cross_novelty[track_id] - min_novelty) / (max_novelty - min_novelty)
            factor = CROSS_NOVELTY_FLOOR + (1.0 - CROSS_NOVELTY_FLOOR) * normalized
        cross_novelty[track_id] = factor

    recency = {}
    weights = {}
    for track in tracks:
        last_played = last_played_rounds.get(track["id"])
        rounds_since_played = round_number if last_played is None else max(0, round_number - last_played)
        recency_factor = 1.0 if single_candidate or last_played is None else min(
            1.0, rounds_since_played / RECENCY_COOLDOWN,
        )
        recency[track["id"]] = recency_factor
        cross_factor = cross_novelty[track["id"]]
        weights[track["id"]] = (
            1.0 if single_candidate and track["id"] in eligible_ids
            else recency_factor * cross_factor if track["id"] in eligible_ids
            else 0.0
        )

    total_weight = sum(weights.values())
    if total_weight <= 0:
        exception = exception or "zero_weight_uniform_fallback"
        for track in eligible:
            weights[track["id"]] = 1.0
        total_weight = len(eligible)

    probabilities = {track_id: weight / total_weight for track_id, weight in weights.items()}
    snapshot = [{
        "track_id": track["id"],
        "base_weight": 1.0,
        "recency_factor": recency[track["id"]],
        "era_novelty_rounds": era_novelty[track["id"]],
        "mood_group": mood_groups.get(str(track.get("mood") or "").strip(), "unclassified"),
        "mood_novelty_rounds": mood_novelty[track["id"]],
        "cross_novelty_factor": cross_novelty[track["id"]],
        "weight": weights[track["id"]],
        "probability": probabilities[track["id"]],
        "is_eligible": track["id"] in eligible_ids,
        "is_selected": False,
        "exception": exception,
    } for track in tracks]

    if single_candidate:
        selected = eligible[0]
    else:
        roll = _track_roll(round_number, choice_id)
        cumulative = 0.0
        ordered = sorted(eligible, key=lambda item: item["id"])
        selected = ordered[-1]
        for track in ordered:
            cumulative += probabilities[track["id"]]
            if roll < cumulative:
                selected = track
                break
    next(item for item in snapshot if item["track_id"] == selected["id"])["is_selected"] = True
    return selected, exception, len(category_tracks), snapshot
