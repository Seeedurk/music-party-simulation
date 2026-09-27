import { useCallback, useEffect, useState } from 'react'
import './App.css'

const buttonStrategies = ['Favors past winners', 'Random exploration', 'Voted for, few wins']

function initials(name = '') {
  return name.split(/\s+/).map((part) => part[0]).join('').slice(0, 2).toUpperCase()
}

function timeLabel(totalSeconds = 0) {
  const seconds = Math.max(0, totalSeconds)
  return `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`
}

function closeReasonLabel(reason) {
  return ({ timer: 'track duration elapsed', host_skip: 'host skip', operator_end: 'operator ended round' })[reason] ?? reason
}

function resolutionMethodLabel(round) {
  if (round.method === 'tie_break') return `tie-break (${round.tie_break_rule === 'hash_fallback' ? 'stable hash fallback' : 'novelty'})`
  if (round.method === 'empty_window') return 'empty-window fallback'
  return round.method === 'normal' ? 'normal win' : round.method
}

function choiceHistoryExplanation(choice) {
  const rate = choice.choice_history.decay_rate.toFixed(2)
  if (choice.strategy === 'Operator addition') {
    return 'The operator queued this category for this round. It was placed on a button without sampling the normal history weights.'
  }
  if (choice.strategy?.startsWith('Room learning')) {
    const effects = choice.room_vote_effects
    return `Button 2 blends ${Math.round(effects.learning_influence * 100)}% decayed vote share with ${Math.round(effects.uniform_exploration_share * 100)}% uniform exploration. This category has decayed vote mass ${effects.decayed_vote_mass.toFixed(2)} (decay ${rate}); every vote counts, and less-voted categories retain a chance.`
  }
  if (choice.strategy === 'Random exploration' || choice.strategy === 'Seed exploration') {
    return 'This button samples available category choices uniformly; history does not change its button weight.'
  }
  if (choice.strategy === 'Favors past winners') {
    return `Button weight is winScore(C) = Σ ${rate}^(current round − win round). This category's decayed win score is ${choice.choice_history.win_score.toFixed(3)}.`
  }
  return `Button weight is interest(C) / (1 + winScore(C)); interest sums ${rate}^(current round − vote round). Here interest=${choice.choice_history.interest_score.toFixed(3)}, winScore=${choice.choice_history.win_score.toFixed(3)}, score=${choice.choice_history.demonstrated_interest_score.toFixed(3)}.`
}

async function post(path, body) {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  const result = await response.json()
  if (!response.ok) throw new Error(result.error || 'Request failed')
  return result
}

function App() {
  const [party, setParty] = useState(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('Connecting to the local party backend…')
  const [inspectedGuestId, setInspectedGuestId] = useState(null)
  const [inspectedRoundNumber, setInspectedRoundNumber] = useState(null)
  const [weightRound, setWeightRound] = useState(null)
  const [weightChoiceIndex, setWeightChoiceIndex] = useState(1)
  const [weightData, setWeightData] = useState(null)
  const [weightError, setWeightError] = useState('')
  const [trackFilter, setTrackFilter] = useState('')
  const [rewindTarget, setRewindTarget] = useState('')
  const [newGuestName, setNewGuestName] = useState('')
  const [newTrack, setNewTrack] = useState({ song_name: '', artist: '', genre: '', year: '', duration: '', mood: '' })
  const [newCategory, setNewCategory] = useState({ choice_type: 'genre', choice_value: '' })

  const refresh = useCallback(async () => {
    try {
      const response = await fetch('/api/party')
      const state = await response.json()
      if (!response.ok) throw new Error(state.error || 'Backend request failed')
      setParty(state)
      setError('')
    } catch (err) {
      setError(err.message)
    }
  }, [])

  useEffect(() => {
    refresh()
    const interval = window.setInterval(refresh, 250)
    return () => window.clearInterval(interval)
  }, [refresh])

  const act = useCallback(async (path, body, successMessage) => {
    try {
      const state = await post(path, body)
      setParty(state)
      setError('')
      if (successMessage) setNotice(successMessage)
    } catch (err) {
      setError(err.message)
    }
  }, [])

  const rewindTo = useCallback(async (roundNumber) => {
    try {
      const state = await post('/api/party/rewind', { round_number: Number(roundNumber) })
      setParty(state)
      setError('')
      setNotice(`Rewound to the start of round ${roundNumber}; later rounds were discarded.`)
      setWeightRound(null)
      setWeightChoiceIndex(1)
      setInspectedRoundNumber(Number(roundNumber))
    } catch (err) {
      setError(err.message)
    }
  }, [])

  const addGuest = useCallback(async (event) => {
    event.preventDefault()
    try {
      const state = await post('/api/guest/add', { name: newGuestName })
      setParty(state)
      setNewGuestName('')
      setError('')
      const addedGuest = state.guests.at(-1)
      setNotice(`${addedGuest.name} joined the roster and can vote starting round ${addedGuest.joined_round}.`)
      setInspectedGuestId(addedGuest.id)
    } catch (err) {
      setError(err.message)
    }
  }, [newGuestName])

  const removeGuest = useCallback((guest) => {
    if (guest.role === 'host' || !window.confirm(`Remove ${guest.name} from the active roster? Their recorded vote history will be kept.`)) return
    act('/api/guest/remove', { guest_id: guest.id }, `${guest.name} removed from the active roster.`)
  }, [act])

  const addCatalogTrack = useCallback(async (event) => {
    event.preventDefault()
    try {
      const state = await post('/api/catalog/track', newTrack)
      setParty(state)
      setNewTrack({ song_name: '', artist: '', genre: '', year: '', duration: '', mood: '' })
      setError('')
      setNotice(`${state.action_result.track.song_name} was added and queued as a song choice for round ${state.action_result.available_round}.`)
    } catch (err) {
      setError(err.message)
    }
  }, [newTrack])

  const addCatalogChoice = useCallback(async (event) => {
    event.preventDefault()
    try {
      const state = await post('/api/catalog/choice', newCategory)
      setParty(state)
      setNewCategory((current) => ({ ...current, choice_value: '' }))
      setError('')
      setNotice(`${state.action_result.choice_type} · ${state.action_result.choice_value} queued for round ${state.action_result.available_round}.`)
    } catch (err) {
      setError(err.message)
    }
  }, [newCategory])

  const downloadAnalytics = useCallback(() => {
    if (!party?.analytics) return
    const blob = new Blob([JSON.stringify(party.analytics, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = 'music-party-night-summary.json'
    link.click()
    URL.revokeObjectURL(url)
  }, [party])

  const vote = useCallback((index) => {
    if (!party || party.acting_guest_id == null) return
    const guest = party.guests.find((item) => item.id === party.acting_guest_id)
    act('/api/vote', { guest_id: party.acting_guest_id, choice_index: index }, `${guest?.name ?? 'Guest'} voted for ${party.choices[index - 1].label}.`)
  }, [act, party])

  const chooseRandomGuest = useCallback(() => {
    if (!party) return
    const eligible = party.guests.filter((guest) => guest.is_active !== false && guestEligibleThisRound(guest) && !guest.has_voted)
    if (!eligible.length) return
    const guest = eligible[Math.floor(Math.random() * eligible.length)]
    act('/api/guest/select', { guest_id: guest.id }, `Acting as ${guest.name}.`)
  }, [act, party])

  useEffect(() => {
    function onKeyDown(event) {
      if (event.repeat || event.altKey || event.ctrlKey || event.metaKey) return
      const target = event.target
      if (target instanceof HTMLElement && (target.isContentEditable || ['INPUT', 'SELECT', 'TEXTAREA'].includes(target.tagName))) return
      if (['1', '2', '3'].includes(event.key)) vote(Number(event.key))
      if (event.key.toLowerCase() === 'r') chooseRandomGuest()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [chooseRandomGuest, party, vote])

  const actingGuest = party?.guests.find((guest) => guest.id === party.acting_guest_id)
  const guestEligibleThisRound = (guest) => guest.eligible_this_round ?? (guest.joined_round ?? 1) <= (party?.round_number ?? 1)
  const isHost = actingGuest?.role === 'host'
  const actingHasVoted = Boolean(actingGuest?.has_voted)
  const votedCount = party?.guests.filter((guest) => guest.is_active !== false && guest.has_voted && guestEligibleThisRound(guest)).length ?? 0
  const seedIndex = party?.history.filter((round) => round.is_seeding).length ?? 0
  const timerState = party?.paused ? 'PAUSED' : party?.is_seeding ? `SEED ${seedIndex + 1} / 3` : 'PLAYING'
  const allRounds = party ? [...party.history, {
    round_number: party.round_number, is_seeding: party.is_seeding, seed_slot: party.seed_slot,
    choices: party.choices, tallies: party.choices.map((choice) => choice.votes), votes: party.votes,
    track: null, winner_label: null, method: null, close_reason: 'in_progress',
  }] : []
  const completedRounds = party?.history ?? []
  const selectedRewindRound = completedRounds.find((round) => String(round.round_number) === rewindTarget) ?? completedRounds.at(-1)
  const inspectedGuest = party?.guests.find((guest) => guest.id === (inspectedGuestId ?? party.guests[0]?.id))
  const inspectedGuestHasAnyVotes = Boolean(inspectedGuest && allRounds.some((round) =>
    (round.votes ?? []).some((vote) => vote.guest_id === inspectedGuest.id),
  ))
  const inspectedRound = allRounds.find((round) => round.round_number === (inspectedRoundNumber ?? party?.round_number))
  const selectedWeightRound = weightRound ?? party?.round_number
  const selectedWeightChoice = weightData?.choices.find((choice) => choice.index === weightChoiceIndex) ?? weightData?.choices[0]
  const weightDistribution = selectedWeightChoice?.distribution ?? []
  const normalizedProbabilityTotal = weightDistribution.reduce((total, item) => total + item.probability, 0)
  const visibleWeightRows = weightDistribution.filter((item) =>
    `${item.song_name} ${item.artist} ${item.genre} ${item.year}`.toLowerCase().includes(trackFilter.toLowerCase()),
  )

  useEffect(() => {
    if (selectedWeightRound == null) return undefined
    let cancelled = false
    setWeightData(null)
    setWeightError('')
    fetch(`/api/rounds/${selectedWeightRound}/weights`)
      .then(async (response) => {
        const result = await response.json()
        if (!response.ok) throw new Error(result.error || 'Could not load track weights')
        if (!cancelled) setWeightData(result)
      })
      .catch((err) => { if (!cancelled) setWeightError(err.message) })
    return () => { cancelled = true }
  }, [selectedWeightRound])

  useEffect(() => {
    if (weightData && !weightData.choices.some((choice) => choice.index === weightChoiceIndex)) {
      setWeightChoiceIndex(weightData.choices[0]?.index ?? 1)
    }
  }, [weightData, weightChoiceIndex])

  useEffect(() => {
    if (!party) return
    if (!party.guests.some((guest) => guest.id === inspectedGuestId)) setInspectedGuestId(party.guests[0]?.id ?? null)
    if (!allRounds.some((round) => round.round_number === inspectedRoundNumber)) setInspectedRoundNumber(party.round_number)
  }, [party, inspectedGuestId, inspectedRoundNumber])

  if (party?.ended) {
    const analytics = party.analytics
    const award = analytics?.best_taste
    return <main className="app-shell end-screen">
      <header className="topbar"><a className="brand" href="#top"><span className="brand-mark">♫</span><span>MUSIC<br />PARTY</span></a><div className="party-title"><span className="eyebrow">NIGHT COMPLETE</span><strong>Friday Night Mix</strong></div><button className="end-button" onClick={() => act('/api/party/new', {}, 'New game started; old round data cleared.')}>Start a new game</button></header>
      <section className="night-summary-hero"><span className="eyebrow">THE ROOM HAS SPOKEN</span><h1>Night summary</h1><p>{analytics?.rounds_completed ?? 0} rounds · {analytics?.total_votes ?? 0} votes · {analytics?.tracks_played?.length ?? 0} tracks played</p><button className="end-button" onClick={downloadAnalytics}>Download JSON summary</button></section>
      <section className="best-taste-card"><span className="eyebrow">BEST TASTE</span>{award?.winner ? <><h2>{award.winner.guest_name}</h2><p>{award.winner.winning_picks} winning picks from {award.winner.votes} votes · {(award.winner.hit_rate * 100).toFixed(1)}% hit rate</p></> : <><h2>No award this night</h2><p>No guest reached the minimum of {award?.minimum_votes ?? 3} votes required to qualify.</p></>}<small>{award?.few_vote_rule}</small></section>
      <section className="analytics-grid">
        <article className="analytics-panel"><h2>Most-voted choices</h2>{analytics?.most_voted_choices.length ? <ol>{analytics.most_voted_choices.map((item) => <li key={item.choice_key}><strong>{item.choice_label}</strong><span>{item.votes} votes</span></li>)}</ol> : <p>No votes were cast.</p>}</article>
        <article className="analytics-panel"><h2>Best taste standings</h2>{analytics?.guest_vote_records.length ? <ol>{analytics.guest_vote_records.map((item) => <li key={item.guest_id}><strong>{item.guest_name}{!item.eligible_for_award && <small> · below {award.minimum_votes} vote minimum</small>}</strong><span>{item.winning_picks}/{item.votes} · {(item.hit_rate * 100).toFixed(0)}%</span></li>)}</ol> : <p>No guest votes to rank.</p>}</article>
        <article className="analytics-panel"><h2>Winners by round</h2><ol>{analytics?.winners_per_round.map((item) => <li key={item.round_number}><strong>Round {item.round_number} · {item.winning_choice}</strong><span>{item.resolved_track.song_name} · {item.resolved_track.artist}</span></li>)}</ol></article>
        <article className="analytics-panel"><h2>Tracks played</h2>{analytics?.tracks_played.length ? <ol>{analytics.tracks_played.map((item) => <li key={`${item.round_number}-${item.id}`}><strong>Round {item.round_number} · {item.song_name}</strong><span>{item.artist} · {item.genre} · {item.year}</span></li>)}</ol> : <p>No tracks reached playback.</p>}</article>
      </section>
      <footer><span>ONE PARTY. TEN OPINIONS. THREE MUSIC CHOICES.</span><span>NIGHT SUMMARY</span></footer>
    </main>
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <a className="brand" href="#top" aria-label="Music Party home"><span className="brand-mark">♫</span><span>MUSIC<br />PARTY</span></a>
        <div className="party-title"><span className="eyebrow">LOCAL SIMULATION</span><strong>Friday Night Mix</strong></div>
        <div className="round-status"><span className="live-dot" />ROUND {party?.round_number ?? '—'}<span className="status-divider" />{party?.paused ? 'PAUSED' : party?.is_seeding ? 'OPENING SET' : 'VOTING LIVE'}</div>
        <section className="operator-section" aria-label="Operator controls">
          <span className="operator-label">OPERATOR CONTROLS <small>Clock, pause, and party actions</small></span>
          <div className="top-controls">
            <div className="speed-control" aria-label="Round clock speed">
              {[1, 5, 10, 20].map((speed) => <button key={speed} className={party?.speed === speed ? 'speed active' : 'speed'} onClick={() => act('/api/clock', { speed }, `Clock speed set to ${speed}×.`)}>{speed}×</button>)}
            </div>
            <button className="pause-button" onClick={() => act('/api/clock', { paused: !party?.paused }, party?.paused ? 'Party resumed.' : 'Party paused.')}>{party?.paused ? '▶ Resume' : 'Ⅱ Pause'}</button>
            <button className="end-button" onClick={() => act('/api/party/end', {}, 'Night ended. Summary is ready.')}>End night</button>
            {isHost && <button className="end-button" disabled={party?.paused} onClick={() => act('/api/host/skip', { guest_id: actingGuest.id }, 'Host skipped the round.')}>Host skip</button>}
            <button className="pause-button" onClick={() => act('/api/party/new', {}, 'New game started; old round data cleared.')}>New game</button>
          </div>
        </section>
      </header>

      <section className={`round-banner ${party?.is_seeding ? 'seeding' : 'track-vote'} ${party?.paused ? 'paused' : ''}`} aria-label="Round phase and timer">
        <div className="round-banner-copy">
          <span className="phase-chip">{party?.is_seeding ? `OPENING SET · SEED ${seedIndex + 1} OF 3` : 'TRACK PLAYING · VOTE FOR WHAT’S NEXT'}</span>
          <h1>{party?.is_seeding ? 'Choose the opening set' : 'Vote for the next track'}</h1>
          <p>{party?.is_seeding ? `No track is playing yet. This three-minute window fills ${party?.seed_slot ?? 'the next slot'}.` : `The current track sets the voting window${party?.slots?.[0]?.track?.song_name ? ` · now playing: ${party.slots[0].track.song_name}` : ''}.`}</p>
        </div>
        <div className="large-clock" role="timer" aria-label={`${timeLabel(party?.remaining_seconds)} remaining`}>
          <strong>{timeLabel(party?.remaining_seconds)}</strong>
          <span>{party?.paused ? 'PAUSED · CLOCK STOPPED' : party?.is_seeding ? 'TIME LEFT IN SEEDING WINDOW' : 'TIME LEFT IN TRACK WINDOW'}</span>
        </div>
      </section>

      <section className="guest-section" aria-labelledby="guest-heading">
        <div className="section-heading guest-heading">
          <div><span className="eyebrow">THE CROWD · {timerState}</span><h2 id="guest-heading">Guests <span className="muted-count">{votedCount} / {party?.eligible_guest_count ?? party?.guests.length ?? 0} eligible guests voted</span></h2></div>
          <button className="random-button" onClick={chooseRandomGuest} disabled={!party?.guests.some((guest) => guest.is_active !== false && guestEligibleThisRound(guest) && !guest.has_voted)} aria-label="Switch to a random eligible guest">R <span>Random guest</span></button>
        </div>
        <form className="guest-add-form" onSubmit={addGuest}><label htmlFor="new-guest-name">ADD A GUEST</label><input id="new-guest-name" value={newGuestName} onChange={(event) => setNewGuestName(event.target.value)} maxLength={64} placeholder="Guest name" /><button className="pause-button" type="submit" disabled={!newGuestName.trim()}>Add guest</button><small>They appear now and become eligible next round.</small></form>
        <div className="guest-list">
          {party?.guests.filter((guest) => guest.is_active !== false).map((guest) => {
            const selectable = guestEligibleThisRound(guest) && (!guest.has_voted || guest.role === 'host')
            return <div className="guest-entry" key={guest.id}><button className={`guest-chip ${party.acting_guest_id === guest.id ? 'selected' : ''} ${guest.has_voted ? 'has-voted' : ''}`} onClick={() => act('/api/guest/select', { guest_id: guest.id }, `Acting as ${guest.name}.`)} disabled={!selectable} title={guest.has_voted && guest.role !== 'host' ? `${guest.name} has voted this round` : `Act as ${guest.name}`}>
              <span className="guest-avatar">{initials(guest.name)}</span>
              <span className="guest-name">{guest.name.split(' ')[0]}{guest.role === 'host' && <small className="host-tag">HOST</small>}{!guestEligibleThisRound(guest) && <small className="host-tag">JOINS NEXT ROUND</small>}</span>
              {guest.has_voted && <span className="voted-check">✓</span>}
            </button>{guest.role !== 'host' && <button className="remove-guest" onClick={() => removeGuest(guest)} title={`Remove ${guest.name}`}>Remove</button>}</div>
          })}
        </div>
      </section>

      <section className="slots-section" aria-label="Song slots">
        {party?.slots.map((slot, index) => <article key={slot.name} className={`slot-card slot-${index + 1}`}>
          <div className="slot-topline"><span className="slot-index">0{index + 1}</span><span className="slot-label">{slot.name}</span>{slot.name === 'On Deck' && <span className="vote-open">VOTE OPEN</span>}</div>
          {index < 2 && <div className={`record-art ${index === 0 ? 'art-one' : 'art-two'}`}><span>♫</span></div>}
          {index === 2 && <div className="deck-visual"><span className="deck-ring">＋</span><span>UP NEXT<br />TO BE SET</span></div>}
          <div className="track-info"><h3>{slot.track?.song_name ?? 'Empty'}</h3><p>{slot.track ? `${slot.track.artist} · ${slot.track.genre} · ${slot.track.year}` : 'Waiting for seeding'}</p></div>
          {index === 2 && <div className="deck-tally"><span>LIVE TALLY</span><strong>{party.choices.reduce((sum, choice) => sum + choice.votes, 0)} <small>votes</small></strong></div>}
        </article>)}
      </section>

      <section className="vote-section" aria-labelledby="vote-heading">
        <div className="vote-title-row"><div><span className="eyebrow">{party?.is_seeding ? `SEEDING · FILL ${party.seed_slot}` : 'VOTING ROUND'}</span><h2 id="vote-heading">Cast one vote this round</h2></div><span className="keyboard-hint">PRESS <kbd>1</kbd> <kbd>2</kbd> <kbd>3</kbd> TO VOTE · <kbd>R</kbd> RANDOM GUEST</span></div>
        <div className="choice-grid">
          {party?.choices.map((choice) => <button key={choice.index} className={`choice-button choice-${choice.index} ${choice.removed ? 'choice-removed' : ''}`} onClick={() => vote(choice.index)} disabled={party.paused || actingHasVoted || !actingGuest || choice.removed}>
            <span className="choice-number">0{choice.index}</span><span className="choice-copy"><strong>{choice.label}</strong><small>{choice.removed ? 'REMOVED BY HOST · VOTING CLOSED' : `${choice.operator_added ? 'New addition' : choice.strategy ?? (choice.index === 2 && party.round_number >= 5 ? 'Room learning' : buttonStrategies[choice.index - 1])} · ${choice.type === 'wildcard' ? 'any track' : choice.type === 'era' ? 'release decade' : choice.type}`}</small></span><span className="choice-tally">{choice.votes}</span>
          </button>)}
        </div>
        {isHost && <div className="host-choice-tools" aria-label="Host choice controls"><strong>HOST POWERS</strong><span>Remove one choice from this round:</span>{party?.choices.map((choice) => <button key={choice.index} className="host-remove-choice" disabled={party.paused || party.removed_choice_index != null} onClick={() => act('/api/host/remove-choice', { guest_id: actingGuest.id, choice_index: choice.index }, `${choice.label} removed from this round.`)}>{party.removed_choice_index === choice.index ? `Removed: ${choice.label}` : `Remove ${choice.label}`}</button>)}</div>}
        <div className="acting-row"><div className="acting-now"><span className="acting-icon">{actingGuest ? initials(actingGuest.name) : '—'}</span><span><small>YOU ARE ACTING AS</small><strong>{actingGuest?.name ?? 'No eligible guest'} {isHost && <em className="host-pill">HOST</em>}</strong></span></div>
          <label className="guest-picker">PICK GUEST <select value={actingGuest?.id ?? ''} onChange={(event) => act('/api/guest/select', { guest_id: Number(event.target.value) }, 'Guest selected.')}><option value="" disabled>Select guest</option>{party?.guests.filter((guest) => guest.is_active !== false && guestEligibleThisRound(guest) && (!guest.has_voted || guest.role === 'host')).map((guest) => <option key={guest.id} value={guest.id}>{guest.name}{guest.role === 'host' ? ' · Host' : ''}{guest.has_voted ? ' · voted' : ''}</option>)}</select></label>
          <span className={`eligibility ${actingHasVoted ? 'complete' : ''}`}>{party?.paused ? 'PARTY PAUSED' : actingHasVoted ? (isHost ? 'HOST POWERS AVAILABLE' : 'ALREADY VOTED') : !actingGuest ? 'ROUND COMPLETE' : 'READY TO VOTE'}</span>
        </div>
        <div className="notice" role="status">{error || notice}{party?.paused && !error && <strong> · Paused</strong>}</div>
        <p className="loop-note">The first three seeding windows last 3 simulated minutes. After seeding, each voting window lasts for the duration of the Now Playing track. Later rounds use preference, random exploration, and demonstrated-interest buttons.</p>
      </section>

      <section className="catalog-section" aria-label="Live catalog changes">
        <div className="section-heading"><div><span className="eyebrow">LIVE CATALOG</span><h2>Add songs and choices</h2></div><span className="catalog-count">{party?.catalog_track_count ?? 0} active tracks</span></div>
        <p className="catalog-note">Additions stay out of the current round. A new song is queued as a direct song button next round; its genre, artist, and era also enter the category pool.</p>
        <div className="catalog-forms">
          <form className="catalog-form" onSubmit={addCatalogTrack}><h3>Add a song</h3><label>Song title<input required maxLength="180" value={newTrack.song_name} onChange={(event) => setNewTrack({ ...newTrack, song_name: event.target.value })} /></label><label>Artist<input required maxLength="180" value={newTrack.artist} onChange={(event) => setNewTrack({ ...newTrack, artist: event.target.value })} /></label><label>Genre<input required maxLength="180" value={newTrack.genre} onChange={(event) => setNewTrack({ ...newTrack, genre: event.target.value })} /></label><label>Year<input required type="number" min="1800" max="2200" value={newTrack.year} onChange={(event) => setNewTrack({ ...newTrack, year: event.target.value })} /></label><label>Duration (m:ss)<input required pattern="[0-9]{1,3}:[0-5][0-9]" placeholder="3:35" value={newTrack.duration} onChange={(event) => setNewTrack({ ...newTrack, duration: event.target.value })} /></label><label>Mood<input required maxLength="180" value={newTrack.mood} onChange={(event) => setNewTrack({ ...newTrack, mood: event.target.value })} /></label><button className="pause-button" type="submit">Add song for next round</button></form>
          <form className="catalog-form" onSubmit={addCatalogChoice}><h3>Queue a category</h3><label>Category type<select value={newCategory.choice_type} onChange={(event) => setNewCategory({ ...newCategory, choice_type: event.target.value })}><option value="genre">Genre</option><option value="artist">Artist</option><option value="era">Era / decade</option></select></label><label>Value<input required maxLength="180" placeholder={newCategory.choice_type === 'era' ? '2010s' : 'Must match a catalog value'} value={newCategory.choice_value} onChange={(event) => setNewCategory({ ...newCategory, choice_value: event.target.value })} /></label><button className="pause-button" type="submit">Queue for next round</button><small>Genre, artist, and decade values must match a track available next round.</small></form>
        </div>
      </section>

      <section className="inspection-section" aria-label="Guest and round inspection">
        <div className="section-heading"><div><span className="eyebrow">LIVE PARTY RECORD</span><h2>Inspect guest behavior</h2></div><label className="guest-picker">ROUND <select value={inspectedRound?.round_number ?? ''} onChange={(event) => setInspectedRoundNumber(Number(event.target.value))}>{allRounds.map((round) => <option key={round.round_number} value={round.round_number}>Round {round.round_number}{round.close_reason === 'in_progress' ? ' · live' : ''}</option>)}</select></label></div>
        <div className="inspection-grid">
          <div className="inspection-guests"><h3>Guests</h3>{party?.guests.map((guest) => <button key={guest.id} className={`inspection-guest ${inspectedGuest?.id === guest.id ? 'selected' : ''}`} onClick={() => setInspectedGuestId(guest.id)}><span className="guest-avatar">{initials(guest.name)}</span><span>{guest.name}{guest.role === 'host' && <small className="host-tag">HOST</small>}{guest.is_active === false && <small className="host-tag">REMOVED</small>}</span></button>)}</div>
          <div className="inspection-history"><h3>{inspectedGuest?.name ?? 'Guest'} · every round</h3>{inspectedGuest && !inspectedGuestHasAnyVotes && <p className="guest-history-empty">No votes recorded yet. {inspectedGuest.joined_round > party.round_number ? `Eligible starting round ${inspectedGuest.joined_round}.` : 'Their round history will appear here as they vote.'}</p>}<ol>{allRounds.map((round) => { const vote = (round.votes ?? []).find((item) => item.guest_id === inspectedGuest?.id); const notJoined = inspectedGuest && inspectedGuest.joined_round > round.round_number; return <li key={round.round_number}><span>Round {round.round_number}</span><strong>{notJoined ? 'NOT IN PARTY YET' : vote ? `Voted: ${vote.choice_label}` : 'NO VOTE'}</strong><small>{round.played_track ? `Now playing: ${round.played_track.song_name} · ${round.played_track.artist}` : round.is_seeding ? 'Seeding window · no track playing' : round.close_reason === 'in_progress' ? 'Outcome pending' : 'No track playing record'}</small>{round.track && <small>Resolved for next slot: {round.track.song_name} · {round.track.artist}</small>}</li> })}</ol></div>
          <div className="inspection-breakdown"><h3>Round {inspectedRound?.round_number} · {inspectedRound?.close_reason === 'in_progress' ? 'in progress' : 'breakdown'}</h3>{inspectedRound?.choices.map((choice, index) => { const voters = (inspectedRound.votes ?? []).filter((vote) => vote.choice_index === choice.index).map((vote) => vote.guest_name); return <article key={choice.key}><strong>{choice.label}{choice.removed ? ' · removed by host' : ''}</strong><span>{inspectedRound.tallies[index] ?? 0} votes</span><small>{voters.length ? `Voters: ${voters.join(', ')}` : 'No votes'}{choice.removed ? ' · no votes accepted after removal' : ''}</small></article> })}{inspectedRound && inspectedRound.close_reason !== 'in_progress' && <div className="round-outcome"><p><strong>Winning choice:</strong> {inspectedRound.winner_label}</p><p><strong>Resolved track:</strong> {inspectedRound.track?.song_name} · {inspectedRound.track?.artist}</p><p><strong>Resolution:</strong> {resolutionMethodLabel(inspectedRound)}{inspectedRound.resolution_exception ? ` · exception: ${inspectedRound.resolution_exception}` : ''}</p><small>Closed because: {closeReasonLabel(inspectedRound.close_reason)}.{inspectedRound.played_track ? ` Now Playing was ${inspectedRound.played_track.song_name} · ${inspectedRound.played_track.artist}.` : ' No track was playing during seeding.'}</small></div>}</div>
        </div>
      </section>

      <section className="weights-section" aria-label="Track selection weights">
        <div className="section-heading weights-heading"><div><span className="eyebrow">WHY A TRACK WAS CHOSEN</span><h2>Track weights &amp; likelihoods</h2></div><div className="weight-controls"><label className="guest-picker">ROUND <select value={selectedWeightRound ?? ''} onChange={(event) => { const value = Number(event.target.value); setWeightRound(value === party?.round_number ? null : value); setWeightChoiceIndex(1) }}>{allRounds.map((round) => <option key={round.round_number} value={round.round_number}>Round {round.round_number}{round.close_reason === 'in_progress' ? ' · live' : ''}</option>)}</select></label>{weightRound != null && <button className="pause-button" onClick={() => { setWeightRound(null); setWeightChoiceIndex(1) }}>Live round</button>}</div></div>
        {weightError && <p className="weight-error" role="alert">{weightError}</p>}
        {!weightError && !weightData && <p className="weight-loading">Loading the catalog distribution…</p>}
        {weightData && <>
          <div className="weight-meta"><span>Round {weightData.round.round_number} · {weightData.mode === 'live' ? 'live preview' : 'saved resolution snapshot'}</span><span>{weightDistribution.length} catalog tracks</span><span>Probability total: {(normalizedProbabilityTotal * 100).toFixed(2)}%</span>{weightData.mode === 'snapshot' && <span className={weightDistribution.some((item) => item.is_selected && item.track_id === weightData.resolution?.winning_track_id) ? 'snapshot-match' : 'snapshot-warning'}>{weightDistribution.some((item) => item.is_selected && item.track_id === weightData.resolution?.winning_track_id) ? 'Saved distribution matches resolved track' : 'Resolved-track check unavailable'}</span>}</div>
          <div className="weight-explainer"><strong>Inputs used</strong><span>{weightData.inputs.formula}</span><ul><li><b>Base weight:</b> {weightData.inputs.base_weight}</li><li><b>Recent-play effect:</b> {weightData.inputs.recency_factor}</li><li><b>Cross-novelty factor:</b> {weightData.inputs.cross_novelty_factor}</li><li><b>Era novelty:</b> {weightData.inputs.era_novelty}</li><li><b>Mood novelty:</b> {weightData.inputs.mood_novelty}</li><li><b>Mood groups:</b> {weightData.inputs.mood_grouping}</li><li><b>Slot eligibility:</b> {weightData.inputs.slot_occupancy}</li><li><b>Vote/win history:</b> {weightData.inputs.vote_history}</li><li><b>Sampling:</b> {weightData.inputs.sampling}</li></ul></div>
          <div className="weight-distribution-heading"><div><strong>{selectedWeightChoice?.choice_label ?? 'No choice data'}</strong><small>{selectedWeightChoice?.strategy} · {selectedWeightChoice?.choice_type} · {selectedWeightChoice?.eligible_track_count ?? 0} eligible tracks{selectedWeightChoice?.resolution_fallback ? ` · effective pool: ${selectedWeightChoice.effective_choice_label}` : ''}{selectedWeightChoice?.exception ? ` · exception: ${selectedWeightChoice.exception}` : ''}</small></div><div className="weight-controls">{weightData.mode === 'live' && <label className="guest-picker">IF THIS BUTTON LEADS <select value={selectedWeightChoice?.index ?? 1} onChange={(event) => setWeightChoiceIndex(Number(event.target.value))}>{weightData.choices.map((choice) => <option key={choice.choice_id} value={choice.index}>Button {choice.index}: {choice.choice_label}</option>)}</select></label>}<input className="track-filter" type="search" value={trackFilter} onChange={(event) => setTrackFilter(event.target.value)} placeholder="Filter catalog tracks" aria-label="Filter catalog tracks" /></div></div>
          {selectedWeightChoice?.choice_history && <div className="choice-history-effects"><strong>Button selection history · {selectedWeightChoice.strategy}</strong><span>{choiceHistoryExplanation(selectedWeightChoice)}</span></div>}
          <p className="weight-preview-note">{selectedWeightChoice?.resolution_note ?? (weightData.mode === 'live' ? `If this category wins, the current deterministic roll selects ${selectedWeightChoice?.preview_track?.song_name} · ${selectedWeightChoice?.preview_track?.artist}. Its probability distribution is shown below; another button has its own pool and distribution.` : `This is the saved distribution for the winning category. It selected ${selectedWeightChoice?.preview_track?.song_name} · ${selectedWeightChoice?.preview_track?.artist}.`)}</p>
          {!weightData.cross_novelty_recorded && <p className="weight-error">This saved round predates cross-attribute novelty. Its displayed weights are the original base × recency snapshot.</p>}
          <div className="weight-table-wrap"><table className="weight-table"><thead><tr><th>Track</th><th>Pool</th><th>Base</th><th>Recent-play factor</th><th>Era novelty<br />(rounds)</th><th>Mood group</th><th>Mood novelty<br />(rounds)</th><th>Cross-novelty factor</th><th>Final weight</th><th>Likelihood</th></tr></thead><tbody>{visibleWeightRows.map((item) => <tr key={item.track_id} className={item.is_selected ? 'selected-track' : ''}><td><strong>{item.song_name}</strong><small>{item.artist} · {item.genre} · {item.year}</small></td><td>{item.is_eligible ? 'Eligible' : 'Excluded'}</td><td>{item.base_weight.toFixed(2)}</td><td>{item.recency_factor.toFixed(2)}</td><td>{item.era_novelty_rounds ?? '—'}</td><td>{weightData.cross_novelty_recorded ? item.mood_group : '—'}</td><td>{item.mood_novelty_rounds ?? '—'}</td><td>{weightData.cross_novelty_recorded ? item.cross_novelty_factor.toFixed(3) : '—'}</td><td>{item.weight.toFixed(4)}</td><td><span className="probability-cell"><i><b style={{ width: `${Math.min(100, item.probability * 100)}%` }} /></i><span>{(item.probability * 100).toFixed(2)}%</span></span></td></tr>)}</tbody></table>{visibleWeightRows.length === 0 && <p className="weight-loading">No catalog tracks match this filter.</p>}</div>
        </>}
      </section>

      <section className="round-history" aria-label="Round history">
        <div className="section-heading"><div><span className="eyebrow">CLOSED ROUNDS</span><h2>Rewind and re-simulate</h2></div><div className="rewind-controls"><label className="guest-picker">RETURN TO START OF ROUND <select value={selectedRewindRound?.round_number ?? ''} onChange={(event) => setRewindTarget(event.target.value)} disabled={!completedRounds.length}><option value="" disabled>Select round</option>{completedRounds.map((round) => <option key={round.round_number} value={round.round_number}>Round {round.round_number}</option>)}</select></label><button className="end-button" disabled={!selectedRewindRound} onClick={() => rewindTo(selectedRewindRound.round_number)}>Rewind &amp; discard future</button></div></div>
        <p className="rewind-note">Reopening a completed round restores its starting slots, choices, guest eligibility, clock speed, and history. Votes and outcomes from that round onward are discarded; you can vote differently on the replay.</p>
        {party?.history.length ? <ol>{party.history.map((round) => <li key={round.round_number}>Round {round.round_number}: {round.is_seeding ? `seeded ${round.seed_slot}` : `played ${round.played_track?.song_name ?? 'track unavailable'}`} · {round.winner_label} resolved to {round.track.song_name} · {resolutionMethodLabel(round)} · {closeReasonLabel(round.close_reason)} · {round.votes.length} votes</li>)}</ol> : <p>No rounds have closed yet.</p>}
      </section>

      <footer><span>ONE PARTY. TEN OPINIONS. THREE MUSIC CHOICES.</span><span>LOCAL SIMULATION <i>●</i></span></footer>
    </main>
  )
}

export default App
