# bahnwatch

Tells you **when to leave**, not just what's late.

A live commute alerter for Berlin public transport. Watches BVG departures,
learns what "normal" looks like for each line at each hour, and sends a browser
notification when a delay actually changes when you need to walk out the door.

Built because I kept discovering my train was broken *at the platform*.

The journey it is configured for is my own: **S2 from Blankenburg to
Friedrichstraße, change to the S7 to Potsdam.** That connection has about two
minutes of slack, and the S7 runs every twenty minutes at that hour — which is
what makes it worth building something for.

---

## The problem with delay alerts

Every transit app shows you the current delay. That number is nearly useless on
its own, for three reasons.

**An 8-minute delay might cost you nothing.** If the line runs every 3 minutes,
you catch the next train and lose 3 minutes, not 8. If it runs every 20, a
4-minute delay might make you miss it and cost you the full 20. **Headway, not
delay magnitude, decides what a delay costs you.**

**The delay you care about is at boarding time, not now.** A train 12 minutes
late at 08:00 is usually not 12 minutes late at 08:25 — operators pad schedules,
hold at termini, and skip stops to recover. Alerting on the current number
systematically tells people to leave early for disruptions that will have
cleared.

**"8 minutes late" means nothing without context.** Is that normal for a Tuesday
at 18:00 on this line, or has something actually gone wrong? No app answers
this, because it needs historical baselines nobody is collecting.

### And a connection turns a small delay into a big one

A real commute has changes, and a change is where the risk actually lives.

On the configured journey the S2 arrives at Friedrichstraße with roughly two
minutes to spare before the S7 leaves. The S7 to Potsdam runs every twenty
minutes at that hour. So:

| S2 delay | Connection | Cost |
|---|---|---|
| 1.5 min | made | **0 min** |
| 9 min | missed | **~22 min** |
| 25 min | missed | **~22 min** |

The cost function is **discontinuous**. Adding up delays gets it wrong in both
directions — over-alerting on a large delay that still makes a generous
connection, under-alerting on a small one that breaks a tight one. Only
propagating delay forward through the legs and checking each connection gives
the right answer.

Actual output, with a 9-minute delay injected:

```
S2 delay breaks the S7 connection, costing 22 min.
  S2  +15.7  slack -3.8m  misses the 22:24 S7 — next is 22:46
  S7   +2.6               would arrive 20 min after target
>>> You'll miss the S2 connection — leave at 21:25 instead of 21:48
```

bahnwatch answers all three. From an actual run:

```
15 min delay now; likely ~9 min by the time you board;
but trains run every ~5 min, so it costs you about 5.
```

Then: **leave at 08:26 instead of 08:31.** That's the output. Not a number — a
decision.

---

## Running it

```bash
make install
make test      # 38 tests, no network needed
make run       # http://localhost:8000
```

Runs on the built-in simulator by default so it works offline and the demo never
shows a blank screen. For the real feed:

```bash
BAHNWATCH_SIMULATE=0 make run
```

The BVG data comes from `v6.bvg.transport.rest` — a community HAFAS wrapper with
no key and no registration.

**Configuring your own journey.** Edit `JOURNEYS` in `src/bahnwatch/api.py`.
Stop IDs are not guessable and a wrong one silently watches the wrong platform,
so look them up:

```bash
python scripts/find_stops.py Blankenburg Friedrichstr "Potsdam Hbf"
```

**Demoing at the wrong time of day.** An evening commute alerter is awkward to
look at over lunch, so the clock can be moved:

```bash
BAHNWATCH_SIMULATE=1 BAHNWATCH_DEMO_TIME=21:40 uvicorn bahnwatch.api:app --port 8000
```

Then in the browser: **Enable notifications** → **Simulate S2 disruption**. The
connection breaks, the leave time jumps back about twenty minutes, and a
notification says you will miss the S7. **Clear disruption** to watch it
recover.

**In the UI:** click *Enable notifications*, then *Simulate U8 disruption*. You
get a real browser notification telling you to leave 5 minutes early, and
*Clear disruption* triggers the recovery notification.

---

## The alert decision

This is where the engineering is. Five gates, each corresponding to a specific
way notification systems become worthless and get muted:

| Gate | Why |
|---|---|
| **Action window** — only within 50 min of normal departure | Telling someone at 06:00 about an 08:30 train is noise; conditions will change twice |
| **Already left** — nothing after departure time | Advice after the fact is a reproach |
| **Actionability** — shift must exceed ~4 min | Nobody can act on "leave 90 seconds earlier" |
| **Hysteresis** — require 5 min more deterioration to re-alert | A delay hovering at the threshold otherwise produces a stream of pings |
| **Daily cap** — 3 per commute | If the system wants to send five, the system is wrong |

**A muted notification system has negative value.** It's worse than not existing,
because the user now believes they're covered. Every one of those gates exists to
prevent the mute.

**Recovery notifications** — "you can leave at the normal time after all" — are
sent once when a disruption clears. It gives back time the user had already
written off, and no transit app does it.

---

## Design decisions

**Exponential delay decay, half-life 25 minutes.**

```python
delay(t) = delay_now * 2^(-t / half_life)
```

Three alternatives, each wrong in a specific way. *Persistence* (delay stays
constant) over-predicts, because networks actively recover — schedules contain
padding precisely so they can. *Linear decay* hits zero and goes negative, which
needs clamping, and the clamp is where the model stops describing anything real.
*Regression on features* would probably beat this eventually, but needs months of
per-line history before it beats a sensible prior, and it fails opaquely. A
single interpretable parameter that can be fitted per line as history accumulates
is the right starting point.

**Effective cost is bounded by headway.**

```python
cost = min(delay, headway)
```

Above the headway the passenger switches to the next service; below it they pay
the delay directly. This is the line of code the whole project is built around —
it's why raw-delay alerting produces both false alarms (big delay, frequent line,
no impact) and misses (small delay, infrequent line, missed connection).

**Baselines cut by (line, hour, day type).** Delay distributions differ sharply
along all three axes. Pooling them produces an average that describes no actual
situation — the 03:00 U8 and the 08:00 U8 are different systems. Three day
buckets, not seven: weekday distributions are close enough that splitting them
wastes limited data, while Saturday and Sunday genuinely differ.

**Baselines below 30 observations are not trusted.** Below roughly that, the p90
of a skewed distribution is estimated from three or four points and swings wildly
between refreshes. The system falls back to fixed thresholds and says so, rather
than pretending to know a distribution it doesn't.

**Median headway, never mean.** Service gaps are heavily right-skewed — the
overnight break between last and first train is hours. One such gap drags a mean
headway from 5 minutes to 40. The median is immune to however many extreme gaps
exist.

**Percentiles for delays, not mean and standard deviation.** Delays are lognormal:
bounded below by zero, right-skewed, occasionally enormous. A normal distribution
generates negative delays, which then have to be clipped.

**WebSocket push, not client polling.** With a 20-second refresh, ten open tabs
polling is ten times the upstream requests for identical data. One poller
broadcasting to N sockets is one. It also means an alert reaches the page the
instant it's decided rather than up to a full interval later — which matters when
the entire point is telling someone to leave *now*.

**The client reconnects with exponential backoff and jitter.** A tight reconnect
loop against a restarting server is a self-inflicted denial of service.

**API failures return empty, never raise.** A commute alerter that crashes when
the transit API hiccups is useless at exactly the moment it matters — during
disruption, when that API is also under load. Callers treat "no data" as a
distinct state, not as "everything is fine".

**One malformed row doesn't lose the other fifty-nine.** The parser is defensive
per-row. This is third-party data with optional fields that genuinely go missing:
`when` is null for cancelled services, `delay` is null when no realtime data
exists — which is *not* the same as zero delay, though zero is the safe default.

**Timezone-aware throughout, always Europe/Berlin.** A departure "at 07:42" is a
different instant in March than in July, and the DST transition happens at 02:00
on a Sunday while the U-Bahn is still running.

---

## A bug worth keeping in the record

The simulator generated departures as offsets from `now`, giving each one a trip
ID derived from its absolute timestamp. Since `now` moved between polls, **every
departure looked new on every cycle** — 97 fresh "observations" every 20 seconds
for the same 97 trains.

Nothing errored. The dashboard looked perfect. But the baselines those
observations feed would have been meaningless: a train sitting in the feed for
40 minutes would contribute 120 observations and completely dominate the
distribution it was supposed to be measured against.

Caught by watching the `new_observations` counter, which sat at 97 when it should
have dropped to 0.

The fix: departures sit on an **absolute grid anchored to midnight**, so the same
scheduled service keeps its identity across polls. Real timetables are absolute;
the simulator had to be too. There's a test for it now —
`test_simulator_produces_stable_trip_ids`.

The database-side defence was already there: a `UNIQUE(trip_id, stop_id,
scheduled)` constraint with `INSERT OR IGNORE`. It just couldn't help when the
IDs themselves were unstable.

---

## Two bugs found by running it on someone else's machine

Both were dependency declarations that were correct on the machine I built on
and broken everywhere else.

**No `tzdata` on Windows.** Linux and macOS ship a system timezone database;
Windows does not. `ZoneInfo("Europe/Berlin")` therefore raises at import on a
clean Windows install, and every test fails during collection. Fixed with a
platform-conditional dependency: `tzdata; sys_platform == "win32"`.

**Bare `uvicorn` has no WebSocket support.** The dependency was declared as
`uvicorn>=0.27`, which installs without the `websockets` library — so `/ws`
returned 404 and the browser sat in a reconnect loop while the page looked
almost fine. The core transport of the application silently did not work. Fixed
by declaring `uvicorn[standard]>=0.27`.

Neither would have been caught by CI on Ubuntu with a warm environment. They are
a good argument for testing an install from scratch on a platform you do not
develop on.

## Tests

**44 tests, no network required.**

Every test runs against the simulator. That matters more here than usual: the
system exists to be reliable during disruption, which is exactly when the
upstream API is least reliable — so the test suite must not depend on it.

The connection tests encode the central claim: a delay inside the slack costs
nothing, a delay that breaks the connection costs a full headway regardless of
its size, and a missing feed is only an anomaly when the journey is imminent.

The alert-decision tests encode the failure modes that get systems muted:

- no alert far outside the action window
- no alert for a shift under the actionable threshold
- hysteresis blocks a repeat alert under identical conditions
- daily cap holds even when hysteresis is deliberately defeated
- recovery notification fires exactly once
- no alert on a day the commute doesn't run
- no alert after the departure time has passed
- alert state resets across days

Plus: effective cost is capped by headway and never negative, delay decay is
monotonic and never negative, plans ignore departures you physically cannot
reach, headway estimation is immune to an overnight gap, the store deduplicates
repeated observations, and the parser survives malformed rows.

---

## Stack

Python 3.11+ · FastAPI · WebSockets · asyncio · NumPy · SQLite · vanilla JS
frontend, no build step.

---

## What I'd add next

- **Per-line decay fitting.** The 25-minute half-life is one global constant.
  Fitting it per line from observed decay is a small amount of code and would
  meaningfully improve prediction on lines that recover differently.
- **Web Push with VAPID.** Notifications currently need the tab open. A service
  worker plus VAPID keys would deliver them with the browser closed, which is
  what makes this genuinely useful at 07:00.
- **Alert quality measurement.** The right metric is precision: what fraction of
  alerts were acted on and turned out to be warranted. Without it the tuning of
  those five gates is judgement rather than evidence.
- **Disruption clustering.** A delay on one U8 train is noise; simultaneous
  delays across every U8 in both directions is an incident, and should be said
  differently.
