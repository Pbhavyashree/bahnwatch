"""Tests.

Everything runs against the simulator -- no network, no BVG dependency, no
flakiness from an upstream feed being slow. That matters more here than usual:
the whole system exists to be reliable during disruption, which is exactly when
the upstream API is least reliable.

The alert-decision tests are the important ones. They encode the failure modes
that make notification systems get muted.
"""

from __future__ import annotations

import asyncio
import itertools
from datetime import datetime, timedelta

import pytest

from bahnwatch.baseline import BaselineStore, _estimate_headway, day_type
from bahnwatch.bvg import BvgClient, SimulatedSource
from bahnwatch.commute import (
    AlertState,
    build_plan,
    decide_alert,
    effective_cost,
    predict_delay_at_boarding,
)
from bahnwatch.models import BERLIN, AlertKind, Baseline, Commute, Departure, Mode


def _monday(hour: int = 8, minute: int = 15) -> datetime:
    """A fixed Monday, so weekday-dependent logic is deterministic."""
    return datetime(2026, 3, 2, hour, minute, tzinfo=BERLIN)  # 2 Mar 2026 is a Monday


def _commute(**over) -> Commute:
    base = {
        "id": "work", "label": "to work", "origin_stop_id": "X", "origin_stop_name": "Alex",
        "line": "U8", "direction": "Wittenau", "walk_minutes": 6, "ride_minutes": 18,
        "buffer_minutes": 5, "arrive_by_hour": 9, "arrive_by_minute": 0,
    }
    base.update(over)
    return Commute(**base)


def _dep(minutes_from: datetime, offset: float, delay_min: float = 0.0,
         line: str = "U8", cancelled: bool = False, trip: str = "t1") -> Departure:
    sched = minutes_from + timedelta(minutes=offset)
    return Departure(
        trip_id=trip, line=line, mode=Mode.UBAHN, direction="Wittenau",
        stop_id="X", stop_name="Alex", scheduled=sched,
        delay_seconds=int(delay_min * 60), cancelled=cancelled,
        observed_at=minutes_from,
    )


# ===========================================================================
# the core maths
# ===========================================================================
def test_effective_cost_is_capped_by_headway():
    """A big delay on a frequent line costs you at most one headway."""
    assert effective_cost(8.0, 3.0) == 3.0
    assert effective_cost(30.0, 10.0) == 10.0


def test_effective_cost_is_paid_in_full_below_headway():
    """A small delay on an infrequent line costs the full delay."""
    assert effective_cost(4.0, 20.0) == 4.0


def test_effective_cost_never_negative():
    assert effective_cost(-5.0, 10.0) == 0.0


def test_effective_cost_handles_zero_headway():
    assert effective_cost(7.0, 0.0) == 7.0


def test_delay_decays_toward_zero():
    assert predict_delay_at_boarding(10.0, 0) == 10.0
    assert predict_delay_at_boarding(10.0, 25) == pytest.approx(5.0)
    assert predict_delay_at_boarding(10.0, 50) == pytest.approx(2.5)


def test_delay_decay_never_goes_negative():
    """The reason for exponential rather than linear decay."""
    assert predict_delay_at_boarding(10.0, 10_000) >= 0.0


def test_delay_decay_is_monotonic():
    vals = [predict_delay_at_boarding(12.0, t) for t in range(0, 60, 5)]
    assert all(a >= b for a, b in itertools.pairwise(vals))


# ===========================================================================
# planning
# ===========================================================================
def test_plan_ignores_departures_you_cannot_reach():
    """A train leaving in 2 minutes is irrelevant if the walk takes 6."""
    now = _monday()
    c = _commute(walk_minutes=6)
    deps = [
        _dep(now, 2, 0.0, trip="too-soon"),    # unreachable
        _dep(now, 12, 9.0, trip="reachable"),
    ]
    plan = build_plan(c, deps, None, now)
    # the 9-min delay belongs to the only catchable train
    assert plan.current_delay_min == pytest.approx(9.0)


def test_plan_handles_no_departures():
    now = _monday()
    plan = build_plan(_commute(), [], None, now)
    assert plan.anomalous
    assert "no upcoming departures" in plan.explanation


def test_plan_detects_all_cancelled():
    now = _monday()
    deps = [_dep(now, 15, 0.0, cancelled=True, trip=f"c{i}") for i in range(3)]
    plan = build_plan(_commute(), deps, None, now)
    assert plan.anomalous
    assert "cancelled" in plan.explanation


def test_plan_normal_departure_is_arrival_minus_journey():
    c = _commute(walk_minutes=6, ride_minutes=18, buffer_minutes=5,
                 arrive_by_hour=9, arrive_by_minute=0)
    plan = build_plan(c, [_dep(_monday(), 20, 0.0)], None, _monday())
    assert plan.normal_leave_at.strftime("%H:%M") == "08:31"


def test_plan_flags_anomaly_against_baseline():
    now = _monday()
    bl = Baseline(line="U8", hour=8, day_type="weekday", n_observations=200,
                  median_delay=1.0, p75_delay=2.5, p90_delay=4.0, iqr=1.5,
                  headway_minutes=5.0)
    normal = build_plan(_commute(), [_dep(now, 15, 3.0)], bl, now)
    unusual = build_plan(_commute(), [_dep(now, 15, 11.0)], bl, now)
    assert not normal.anomalous
    assert unusual.anomalous


def test_unreliable_baseline_is_not_used_for_anomaly():
    """Below the observation threshold, percentiles are noise."""
    now = _monday()
    thin = Baseline(line="U8", hour=8, day_type="weekday", n_observations=4,
                    median_delay=1.0, p75_delay=1.2, p90_delay=1.5, iqr=0.3,
                    headway_minutes=5.0)
    assert not thin.is_reliable
    assert not build_plan(_commute(), [_dep(now, 15, 9.0)], thin, now).anomalous


# ===========================================================================
# alert decisions -- the failure modes that get systems muted
# ===========================================================================
def test_no_alert_far_outside_the_action_window():
    """Telling someone at 06:00 about an 08:31 departure is noise."""
    now = _monday(6, 0)
    plan = build_plan(_commute(), [_dep(now, 20, 25.0)], None, now)
    assert decide_alert(plan, _commute(), AlertState(), now) is None


def test_no_alert_for_a_trivial_shift():
    """Nobody can act on 'leave 90 seconds earlier'."""
    now = _monday()
    plan = build_plan(_commute(), [_dep(now, 20, 1.5)], None, now)
    assert not plan.actionable
    assert decide_alert(plan, _commute(), AlertState(), now) is None


def test_alert_fires_for_a_real_shift():
    now = _monday()
    c = _commute()
    plan = build_plan(c, [_dep(now, 20, 30.0)], None, now)
    alert = decide_alert(plan, c, AlertState(), now)
    assert alert is not None
    assert alert.kind == AlertKind.LEAVE_EARLIER
    assert alert.shift_minutes >= 4.0


def test_hysteresis_prevents_repeat_alerts():
    """A delay hovering at the threshold must not produce a stream of pings."""
    now = _monday()
    c = _commute()
    state = AlertState()

    first = decide_alert(build_plan(c, [_dep(now, 20, 30.0)], None, now), c, state, now)
    assert first is not None

    # Same conditions again -- no second alert.
    second = decide_alert(build_plan(c, [_dep(now, 20, 30.0)], None, now), c, state, now)
    assert second is None


def test_daily_cap_is_enforced():
    now = _monday()
    c = _commute()
    state = AlertState()
    fired = 0
    # Escalate hard each time so hysteresis never blocks; only the cap should.
    for extra in range(0, 400, 40):
        state.last_shift_alerted = 0.0   # defeat hysteresis deliberately
        plan = build_plan(c, [_dep(now, 20, 20.0 + extra)], None, now)
        if decide_alert(plan, c, state, now):
            fired += 1
    assert fired <= 3


def test_recovery_alert_is_sent_once():
    """Giving back time the user had written off -- and only telling them once."""
    now = _monday()
    c = _commute()
    state = AlertState()

    decide_alert(build_plan(c, [_dep(now, 20, 30.0)], None, now), c, state, now)

    calm = build_plan(c, [_dep(now, 20, 0.0)], None, now)
    rec = decide_alert(calm, c, state, now)
    assert rec is not None and rec.kind == AlertKind.RECOVERED

    assert decide_alert(calm, c, state, now) is None


def test_no_alert_on_a_day_the_commute_does_not_run():
    saturday = datetime(2026, 3, 7, 8, 15, tzinfo=BERLIN)
    c = _commute(days=(0, 1, 2, 3, 4))
    plan = build_plan(c, [_dep(saturday, 20, 30.0)], None, saturday)
    assert decide_alert(plan, c, AlertState(), saturday) is None


def test_no_alert_after_departure_time_has_passed():
    """Advice after the fact is a reproach, not a notification."""
    now = _monday(8, 45)     # normal departure was 08:31
    c = _commute()
    plan = build_plan(c, [_dep(now, 10, 30.0)], None, now)
    assert decide_alert(plan, c, AlertState(), now) is None


def test_state_resets_across_days():
    c = _commute()
    state = AlertState()
    day1 = _monday()
    decide_alert(build_plan(c, [_dep(day1, 20, 30.0)], None, day1), c, state, day1)
    assert state.alerts_sent_today >= 1

    day2 = day1 + timedelta(days=1)
    decide_alert(build_plan(c, [_dep(day2, 20, 30.0)], None, day2), c, state, day2)
    assert state.day == day2.date().isoformat()
    assert state.alerts_sent_today == 1


# ===========================================================================
# baselines and storage
# ===========================================================================
def test_day_type_buckets():
    assert day_type(datetime(2026, 3, 2, 9, tzinfo=BERLIN)) == "weekday"
    assert day_type(datetime(2026, 3, 7, 9, tzinfo=BERLIN)) == "saturday"
    assert day_type(datetime(2026, 3, 8, 9, tzinfo=BERLIN)) == "sunday"


def test_headway_uses_median_not_mean():
    """One overnight gap must not distort the estimate."""
    base = datetime(2026, 3, 2, 6, tzinfo=BERLIN)
    times = [base + timedelta(minutes=5 * i) for i in range(20)]
    times.append(base + timedelta(hours=6))     # a long gap
    assert _estimate_headway(times) == pytest.approx(5.0)


def test_store_deduplicates_repeated_observations():
    """The poller sees the same train many times; it must count once."""
    store = BaselineStore(":memory:")
    now = _monday()
    deps = [_dep(now, 10, 3.0, trip="trip-a")]
    assert store.record(deps) == 1
    assert store.record(deps) == 0
    assert store.observation_count() == 1
    store.close()


def test_store_builds_baseline_from_history():
    store = BaselineStore(":memory:")
    now = _monday()
    deps = [_dep(now, 5 * i, delay_min=float(i % 6), trip=f"t{i}") for i in range(60)]
    store.record(deps)
    bl = store.baseline("U8", deps[0].scheduled.hour, "weekday")
    assert bl is not None
    assert bl.n_observations > 0
    assert bl.median_delay <= bl.p75_delay <= bl.p90_delay
    store.close()


def test_baseline_absent_without_data():
    store = BaselineStore(":memory:")
    assert store.baseline("U9", 8, "weekday") is None
    store.close()


# ===========================================================================
# data source
# ===========================================================================
async def test_simulator_produces_stable_trip_ids():
    """The bug that polluted baselines: identity must not depend on poll time."""
    src = SimulatedSource(seed=1)
    a = await src.departures("X", 30)
    await asyncio.sleep(0.05)
    b = await src.departures("X", 30)
    common = {d.trip_id for d in a} & {d.trip_id for d in b}
    assert len(common) > 5, "the same scheduled departure must keep its id across polls"


async def test_simulator_returns_multiple_lines_and_modes():
    deps = await SimulatedSource(seed=2).departures("X", 60)
    assert len({d.line for d in deps}) >= 5
    assert len({d.mode for d in deps}) >= 3


async def test_simulator_delays_are_non_negative():
    deps = await SimulatedSource(seed=5).departures("X", 120)
    assert all(d.delay_seconds >= 0 for d in deps)


async def test_disruption_increases_delay_on_target_line():
    calm = SimulatedSource(seed=9)
    busy = SimulatedSource(seed=9, disruption_line="U8", disruption_minutes=15)

    def mean_u8(deps):
        u8 = [d.delay_minutes for d in deps if d.line == "U8"]
        return sum(u8) / len(u8)

    assert mean_u8(await busy.departures("X", 60)) > mean_u8(await calm.departures("X", 60))


def test_bvg_parser_tolerates_malformed_rows():
    """One bad row must not lose the rest of the response."""
    payload = {"departures": [
        {"plannedWhen": "2026-03-02T08:15:00+01:00", "line": {"name": "U8", "product": "subway"},
         "stop": {"id": "X", "name": "Alex"}, "direction": "Wittenau", "delay": 120,
         "tripId": "good-1"},
        {"nonsense": True},
        {"plannedWhen": None, "line": {"name": "U8"}},
        {"plannedWhen": "2026-03-02T08:20:00+01:00", "line": {"name": "S41", "product": "suburban"},
         "stop": {"id": "X", "name": "Alex"}, "direction": "Ring", "delay": None,
         "tripId": "good-2"},
    ]}
    out = BvgClient._parse(payload)
    assert len(out) == 2
    assert {d.trip_id for d in out} == {"good-1", "good-2"}


def test_bvg_parser_treats_null_delay_as_zero_not_missing():
    """No realtime data is not the same as on time, but zero is the safe default."""
    payload = {"departures": [{
        "plannedWhen": "2026-03-02T08:20:00+01:00",
        "line": {"name": "S41", "product": "suburban"},
        "stop": {"id": "X", "name": "Alex"}, "direction": "Ring",
        "delay": None, "tripId": "t",
    }]}
    assert BvgClient._parse(payload)[0].delay_seconds == 0


def test_departure_lateness_threshold():
    now = _monday()
    assert not _dep(now, 10, 1.0).is_late
    assert _dep(now, 10, 3.0).is_late


# ===========================================================================
# api layer
# ===========================================================================
async def test_poll_cycle_produces_a_full_snapshot(monkeypatch, tmp_path):
    monkeypatch.setenv("BAHNWATCH_SIMULATE", "1")
    monkeypatch.setenv("BAHNWATCH_DB", str(tmp_path / "t.db"))
    import importlib

    from bahnwatch import api
    importlib.reload(api)

    snap = await api.poll_once()
    assert snap["status"] == "ok"
    assert snap["source"] == "simulator"
    assert len(snap["plans"]) >= 1
    assert snap["board"], "board should not be empty"
    for p in snap["plans"]:
        for key in ("adjusted_leave_at", "explanation", "total_cost_minutes", "legs"):
            assert key in p
        assert len(p["legs"]) >= 1


async def test_poll_cycle_deduplicates_across_calls(monkeypatch, tmp_path):
    monkeypatch.setenv("BAHNWATCH_SIMULATE", "1")
    monkeypatch.setenv("BAHNWATCH_DB", str(tmp_path / "t2.db"))
    import importlib

    from bahnwatch import api
    importlib.reload(api)

    first = await api.poll_once()
    second = await api.poll_once()
    assert first["new_observations"] > 0
    assert second["new_observations"] == 0, "same departures must not be recorded twice"


async def test_hub_broadcasts_and_drops_dead_sockets():
    from bahnwatch.api import Hub

    class Good:
        def __init__(self): self.got = []
        async def send_json(self, p): self.got.append(p)

    class Dead:
        async def send_json(self, p): raise RuntimeError("closed")

    hub = Hub()
    good, dead = Good(), Dead()
    hub.sockets.update({good, dead})

    await hub.broadcast({"status": "ok"})
    assert good.got == [{"status": "ok"}]
    assert dead not in hub.sockets, "a closed socket must be removed, not retried forever"
    assert good in hub.sockets


async def test_disruption_endpoint_changes_the_plan(monkeypatch, tmp_path):
    monkeypatch.setenv("BAHNWATCH_SIMULATE", "1")
    monkeypatch.setenv("BAHNWATCH_DB", str(tmp_path / "t3.db"))
    import importlib

    from bahnwatch import api
    importlib.reload(api)

    # Shift the clock so the journey is imminent and live data is in range.
    monkeypatch.setenv("BAHNWATCH_CLOCK_OFFSET_MIN", str(_offset_to_journey()))
    before = await api.poll_once()
    api.source.set_disruption("S2", 20.0)
    after = await api.poll_once()

    def s2_delay(snap):
        for p in snap["plans"]:
            for leg in p["legs"]:
                if leg["line"] == "S2":
                    return leg["current_delay_min"]
        return 0.0

    assert s2_delay(after) > s2_delay(before)


def _offset_to_journey() -> float:
    """Minutes to shift the clock so the configured evening journey is close."""
    from datetime import datetime

    from bahnwatch.models import BERLIN
    now = datetime.now(BERLIN)
    target = now.replace(hour=21, minute=30, second=0, microsecond=0)
    return (target - now).total_seconds() / 60.0


# ===========================================================================
# multi-leg journeys and connection risk
# ===========================================================================
def _journey():
    from bahnwatch.journey import Journey, Leg
    return Journey(
        id="j", label="Blankenburg to Potsdam",
        walk_minutes=7.0, buffer_minutes=4.0,
        arrive_by_hour=23, arrive_by_minute=2,
        legs=(
            # All three southbound termini, as api.py configures it: pinning
            # this to one of them is the bug the live board exposed.
            Leg(line="S2", direction="Blankenfelde|Lichtenrade|Marienfelde",
                origin_stop_id="A",
                origin_stop_name="S Blankenburg", dest_stop_name="Friedrichstr.",
                ride_minutes=22.0, transfer_minutes=3.0, nominal_headway=20.0),
            Leg(line="S7", direction="Potsdam Hbf", origin_stop_id="B",
                origin_stop_name="Friedrichstr.", dest_stop_name="Potsdam Hbf",
                ride_minutes=38.0, transfer_minutes=0.0, nominal_headway=20.0),
        ),
    )


# Destinations as the live feed spells them, not as the legs configure them.
# The planner has to bridge that gap, so the fixtures must not paper over it.
_FEED_DIRECTIONS = {"S2": "S Blankenfelde (TF) Bhf", "S7": "S Potsdam Hauptbahnhof"}


def _sdep(when, line, delay=0.0, trip="t", stop="A", direction=None):
    return Departure(
        trip_id=trip, line=line, mode=Mode.SBAHN,
        direction=_FEED_DIRECTIONS.get(line, "x") if direction is None else direction,
        stop_id=stop, stop_name=stop, scheduled=when,
        delay_seconds=int(delay * 60), observed_at=when,
    )


def test_target_arrival_rolls_to_tomorrow_once_passed():
    """Otherwise the countdown reads '924 minutes ago', which is useless."""
    j = _journey()
    late_at_night = datetime(2026, 3, 2, 23, 55, tzinfo=BERLIN)
    assert j.target_arrival(late_at_night).day == 3


def test_connection_made_when_delay_fits_the_slack():
    from bahnwatch.planner import plan_journey
    j = _journey()
    now = datetime(2026, 3, 2, 21, 40, tzinfo=BERLIN)
    s2 = datetime(2026, 3, 2, 21, 57, tzinfo=BERLIN)
    s7 = datetime(2026, 3, 2, 22, 24, tzinfo=BERLIN)

    deps = {
        "A": [_sdep(s2, "S2", 1.0, "s2-a")],
        "B": [_sdep(s7, "S7", 0.0, "s7-a", "B"),
              _sdep(s7 + timedelta(minutes=20), "S7", 0.0, "s7-b", "B")],
    }
    plan = plan_journey(j, deps, None, now)
    assert not any(leg.connection_missed for leg in plan.legs)
    assert plan.shift_minutes == pytest.approx(0.0, abs=0.01)


def test_connection_broken_costs_a_full_headway_not_the_delay():
    """The point of the whole project: cost is the wait, not the lateness."""
    from bahnwatch.planner import plan_journey
    j = _journey()
    now = datetime(2026, 3, 2, 21, 40, tzinfo=BERLIN)
    s2 = datetime(2026, 3, 2, 21, 57, tzinfo=BERLIN)
    s7 = datetime(2026, 3, 2, 22, 24, tzinfo=BERLIN)

    deps = {
        "A": [_sdep(s2, "S2", 9.0, "s2-a")],          # 9 min late, slack is ~2
        "B": [_sdep(s7, "S7", 0.0, "s7-a", "B"),
              _sdep(s7 + timedelta(minutes=20), "S7", 0.0, "s7-b", "B")],
    }
    plan = plan_journey(j, deps, None, now)
    assert any(leg.connection_missed for leg in plan.legs)
    # ~20 minutes of cost from a 9-minute delay
    assert plan.shift_minutes > 15.0


def test_small_delay_on_first_leg_costs_nothing():
    """A delay inside the slack must not trigger anything."""
    from bahnwatch.planner import plan_journey
    j = _journey()
    now = datetime(2026, 3, 2, 21, 40, tzinfo=BERLIN)
    s2 = datetime(2026, 3, 2, 21, 57, tzinfo=BERLIN)
    s7 = datetime(2026, 3, 2, 22, 24, tzinfo=BERLIN)
    deps = {
        "A": [_sdep(s2, "S2", 1.5, "s2-a")],
        "B": [_sdep(s7, "S7", 0.0, "s7-a", "B")],
    }
    plan = plan_journey(j, deps, None, now)
    assert plan.shift_minutes < 4.0
    assert not plan.actionable


def test_missing_feed_is_not_anomalous_when_journey_is_far_off():
    from bahnwatch.planner import plan_journey
    j = _journey()
    morning = datetime(2026, 3, 2, 9, 0, tzinfo=BERLIN)
    plan = plan_journey(j, {"A": [], "B": []}, None, morning)
    assert not plan.anomalous
    assert "not imminent" in plan.legs[0].note


def test_missing_feed_is_anomalous_when_journey_is_imminent():
    from bahnwatch.planner import plan_journey
    j = _journey()
    just_before = datetime(2026, 3, 2, 21, 40, tzinfo=BERLIN)
    plan = plan_journey(j, {"A": [], "B": []}, None, just_before)
    assert plan.anomalous


# ---------------------------------------------------------------------------
# VBB HAFAS ReST parser
#
# Fixtures are the real payload shape, trimmed. The parser is the only part of
# the live swap that can be wrong in a way nothing else catches: every field
# below has a documented alternative spelling or an absent-when-false
# convention, and a mistake produces a plausible-looking board rather than an
# error.
# ---------------------------------------------------------------------------

def _hafas_row(**over):
    row = {
        "JourneyDetailRef": {"ref": "1|28703|6|86|13102026"},
        "JourneyStatus": "P",
        "Product": {"name": "S 2", "line": "S2", "catOut": "S", "catCode": "4"},
        "name": "S 2",
        "type": "ST",
        "stop": "Berlin-Blankenburg",
        "stopExtId": "900130002",
        "time": "21:57:00",
        "date": "2026-10-13",
        "rtTime": "22:00:00",
        "rtDate": "2026-10-13",
        "direction": "S Blankenfelde",
        "track": "1",
    }
    row.update(over)
    return row


def _board(*rows):
    return {"Departure": list(rows)}


def test_vbb_delay_is_computed_from_realtime_minus_scheduled():
    from bahnwatch.vbb import VbbClient
    d = VbbClient.parse(_board(_hafas_row()))[0]
    assert d.delay_seconds == 180
    assert d.has_realtime
    assert d.line == "S2"
    assert d.mode is Mode.SBAHN
    assert d.trip_id == "1|28703|6|86|13102026"
    assert d.scheduled.hour == 21 and d.scheduled.minute == 57
    assert d.scheduled.tzinfo is not None


def test_vbb_missing_realtime_is_not_zero_delay():
    """The distinction the whole baseline depends on.

    No rtTime means nobody is reporting on this train. Recording that as an
    on-time arrival makes unmonitored lines look like the best on the network.
    """
    from bahnwatch.vbb import VbbClient
    row = _hafas_row()
    del row["rtTime"], row["rtDate"]
    d = VbbClient.parse(_board(row))[0]
    assert d.delay_seconds == 0
    assert not d.has_realtime


def test_vbb_hour_past_24_rolls_to_the_next_day():
    """HAFAS writes a 00:12 departure as 24:12 on the previous date."""
    from bahnwatch.vbb import VbbClient
    d = VbbClient.parse(_board(_hafas_row(
        time="24:12:00", rtTime="24:15:00", date="2026-10-13", rtDate="2026-10-13",
    )))[0]
    assert (d.scheduled.day, d.scheduled.hour, d.scheduled.minute) == (14, 0, 12)
    assert d.delay_seconds == 180


def test_vbb_midnight_crossing_without_rtdate_is_not_a_negative_day():
    """A 3-minute delay must not read as -1437 minutes."""
    from bahnwatch.vbb import VbbClient
    d = VbbClient.parse(_board(_hafas_row(
        time="23:58:00", rtTime="00:01:00", date="2026-10-13",
    ) | {"rtDate": None}))[0]
    assert d.delay_seconds == 180


def test_vbb_product_may_be_a_list():
    from bahnwatch.vbb import VbbClient
    d = VbbClient.parse(_board(_hafas_row(
        Product=[{"name": "U 8", "line": "U8", "catOut": "U"}]
    )))[0]
    assert d.line == "U8"
    assert d.mode is Mode.UBAHN


def test_vbb_cancelled_flag_is_absent_when_false():
    from bahnwatch.vbb import VbbClient
    assert not VbbClient.parse(_board(_hafas_row()))[0].cancelled
    assert VbbClient.parse(_board(_hafas_row(cancelled=True)))[0].cancelled
    assert VbbClient.parse(_board(_hafas_row(JourneyStatus="C")))[0].cancelled


def test_vbb_one_bad_row_does_not_lose_the_others():
    from bahnwatch.vbb import VbbClient
    rows = VbbClient.parse(_board(
        _hafas_row(),
        {"name": "junk"},                      # no date/time at all
        _hafas_row(time="not-a-time"),         # unparseable
        _hafas_row(time="22:17:00", rtTime="22:17:00"),
    ))
    assert len(rows) == 2
    assert rows[1].delay_seconds == 0 and rows[1].has_realtime


def test_vbb_single_departure_is_not_wrapped_in_a_list():
    from bahnwatch.vbb import VbbClient
    assert len(VbbClient.parse({"Departure": _hafas_row()})) == 1


def test_vbb_modes_map_from_category():
    from bahnwatch.vbb import VbbClient
    cases = [("S", "S2", Mode.SBAHN), ("U", "U8", Mode.UBAHN),
             ("Bus", "142", Mode.BUS), ("Tram", "M10", Mode.TRAM),
             ("RE", "RE1", Mode.REGIONAL), ("ICE", "ICE 599", Mode.EXPRESS)]
    for cat, line, expected in cases:
        d = VbbClient.parse(_board(_hafas_row(
            Product={"line": line, "catOut": cat})))[0]
        assert d.mode is expected, f"{cat}/{line} -> {d.mode}"


def test_vbb_client_without_a_key_returns_empty_rather_than_raising(monkeypatch):
    from bahnwatch.vbb import VbbClient
    monkeypatch.delenv("VBB_ACCESS_ID", raising=False)
    client = VbbClient(access_id="")
    assert asyncio.run(client.departures("900130002")) == []
    assert "VBB_ACCESS_ID" in (client.last_error or "")


def test_vbb_errors_never_leak_the_key():
    from bahnwatch.vbb import VbbClient
    client = VbbClient(access_id="super-secret-key")
    assert "super-secret-key" not in client._redact(
        "GET https://x/departureBoard?accessId=super-secret-key failed")


def test_rate_limiter_spaces_calls():
    from bahnwatch.vbb import RateLimiter

    async def run():
        limiter = RateLimiter(min_interval=0.05)
        start = asyncio.get_event_loop().time()
        for _ in range(3):
            await limiter.acquire()
        return asyncio.get_event_loop().time() - start, limiter.calls

    elapsed, calls = asyncio.run(run())
    assert calls == 3
    assert elapsed >= 0.09   # first is free, the next two each wait


def test_baseline_ignores_departures_with_no_realtime(tmp_path):
    """Scheduled-only rows are absence of evidence, not evidence of punctuality."""
    store = BaselineStore(tmp_path / "t.db")
    when = datetime(2026, 10, 13, 21, 57, tzinfo=BERLIN)
    rows = [
        Departure(trip_id="a", line="S2", mode=Mode.SBAHN, direction="x",
                  stop_id="1", stop_name="s", scheduled=when,
                  delay_seconds=180, has_realtime=True),
        Departure(trip_id="b", line="S2", mode=Mode.SBAHN, direction="x",
                  stop_id="1", stop_name="s", scheduled=when + timedelta(minutes=20),
                  delay_seconds=0, has_realtime=False),
    ]
    assert store.record(rows) == 1
    assert store.observation_count() == 1


def test_baseline_keeps_cancellations_even_without_realtime(tmp_path):
    """A cancellation is a fact about the service, not a missing prediction."""
    store = BaselineStore(tmp_path / "t2.db")
    when = datetime(2026, 10, 13, 21, 57, tzinfo=BERLIN)
    assert store.record([
        Departure(trip_id="c", line="S2", mode=Mode.SBAHN, direction="x",
                  stop_id="1", stop_name="s", scheduled=when,
                  cancelled=True, has_realtime=False)
    ]) == 1


# ---------------------------------------------------------------------------
# VBB transport behaviour.
#
# The retry, quota and auth paths only run when something has already gone
# wrong, which is precisely when nobody is watching. Exercised here with a mock
# transport so they are covered without a live key.
# ---------------------------------------------------------------------------

def _client(handler, **kw):
    import httpx

    from bahnwatch.vbb import VbbClient
    return VbbClient(access_id="k", min_interval=0.0,
                     transport=httpx.MockTransport(handler), **kw)


def test_vbb_auth_failure_is_reported_not_retried():
    import httpx
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401, json={})

    client = _client(handler)
    assert asyncio.run(client.departures("900130002")) == []
    assert len(calls) == 1, "401 is permanent — retrying only burns quota"
    assert "VBB_ACCESS_ID" in client.last_error


def test_vbb_application_error_inside_a_200_is_caught():
    """HAFAS reports failures in the body with an HTTP 200."""
    import httpx

    def handler(request):
        return httpx.Response(200, json={"errorCode": "SVC_LOC",
                                         "errorText": "location missing"})

    client = _client(handler)
    assert asyncio.run(client.departures("nope")) == []
    assert "SVC_LOC" in client.last_error


def test_vbb_retries_a_429_then_succeeds():
    import httpx
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429)
        return httpx.Response(200, json=_board(_hafas_row()))

    client = _client(handler)
    out = asyncio.run(client.departures("900130002"))
    assert attempts["n"] == 2
    assert len(out) == 1 and client.last_error is None


def test_vbb_network_failure_returns_empty_and_records_the_error():
    import httpx

    def handler(request):
        raise httpx.ConnectError("connection refused")

    client = _client(handler, max_retries=0)
    assert asyncio.run(client.departures("900130002")) == []
    assert "ConnectError" in client.last_error


def test_vbb_sends_the_key_and_a_bounded_duration():
    import httpx
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(200, json=_board(_hafas_row()))

    asyncio.run(_client(handler).departures("900130002", minutes=99999))
    assert seen["accessId"] == "k"
    assert seen["id"] == "900130002"
    assert int(seen["duration"]) == 1439, "HAFAS rejects a duration above 1439"


def test_vbb_sends_a_portable_rtmode_by_default():
    """The HAFAS reference documents FULL; the VBB deployment rejects it."""
    import httpx
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(200, json=_board(_hafas_row()))

    asyncio.run(_client(handler).departures("900142001"))
    assert seen["rtMode"] == "SERVER_DEFAULT"


def test_vbb_drops_rtmode_and_retries_when_the_server_rejects_it():
    """A host that refuses the parameter is describing itself, not the request.

    Failing every poll because one optional parameter is spelled differently on
    this deployment is the wrong trade: one extra call recovers the service.
    """
    import httpx
    seen = []

    def handler(request):
        params = dict(request.url.params)
        seen.append(params)
        if "rtMode" in params:
            return httpx.Response(200, json={
                "errorCode": "API_PARAM",
                "errorText": "Value X for param rtMode is not valid. "
                             "Has to be one of [OFF, SERVER_DEFAULT]"})
        return httpx.Response(200, json=_board(_hafas_row()))

    client = _client(handler, rt_mode="X")
    out = asyncio.run(client.departures("900142001"))
    assert len(seen) == 2 and "rtMode" not in seen[1]
    assert len(out) == 1 and client.last_error is None


def test_vbb_other_api_errors_are_not_retried_forever():
    import httpx
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"errorCode": "SVC_LOC",
                                         "errorText": "unknown stop"})

    client = _client(handler)
    assert asyncio.run(client.departures("nope")) == []
    assert len(calls) == 1
    assert "SVC_LOC" in client.last_error


# ---------------------------------------------------------------------------
# Direction matching.
#
# Found the moment live data arrived, and invisible to the simulator by
# construction: SimulatedSource gives every line exactly one destination, so S2
# there is always southbound. At the real S Blankenburg the S2 alternates.
# ---------------------------------------------------------------------------

def test_direction_matching_tolerates_feed_spelling():
    from bahnwatch.planner import _direction_matches
    assert _direction_matches("Blankenfelde", "S Blankenfelde (TF) Bhf")
    assert _direction_matches("Potsdam Hbf", "S Potsdam Hauptbahnhof")
    assert not _direction_matches("Blankenfelde", "S Bernau Bhf")
    assert not _direction_matches("Blankenfelde", "S Buch (Berlin)")
    assert not _direction_matches("Blankenfelde", "S Lichtenrade")


def test_unset_direction_still_matches_anything():
    """Backwards compatible: a leg with no direction means 'any', as before."""
    from bahnwatch.planner import _direction_matches
    assert _direction_matches("", "S Bernau Bhf")


def test_planner_ignores_the_same_line_going_the_other_way():
    from bahnwatch.journey import Leg
    from bahnwatch.planner import _pick_departure

    base = datetime(2026, 10, 13, 21, 50, tzinfo=BERLIN)
    leg = Leg(line="S2", direction="Blankenfelde", origin_stop_id="A",
              origin_stop_name="S Blankenburg", dest_stop_name="S+U Friedrichstr.",
              ride_minutes=22.0, nominal_headway=20.0)

    def dep(minute, direction):
        return Departure(trip_id=f"t{minute}{direction}", line="S2", mode=Mode.SBAHN,
                         direction=direction, stop_id="A", stop_name="S Blankenburg",
                         scheduled=base.replace(minute=minute))

    wrong_way_but_sooner = dep(52, "S Bernau Bhf")
    right_way = dep(57, "S Blankenfelde (TF) Bhf")
    picked = _pick_departure([wrong_way_but_sooner, right_way], leg, base)
    assert picked is right_way, "the next S2 is not necessarily your S2"


# ---------------------------------------------------------------------------
# Structural lateness: the timetable cannot meet the target, and no delay is
# responsible. Surfaced in words, never as an alert — a condition that holds
# every evening is the fastest way to get an alerter muted.
# ---------------------------------------------------------------------------

def test_timetable_shortfall_is_explained_not_blamed_on_a_delay():
    from bahnwatch.planner import plan_journey
    j = _journey()                                   # target 23:02
    now = datetime(2026, 3, 2, 21, 20, tzinfo=BERLIN)
    s2 = datetime(2026, 3, 2, 22, 0, tzinfo=BERLIN)
    s7 = datetime(2026, 3, 2, 22, 45, tzinfo=BERLIN)  # +38 min ride -> 23:23

    plan = plan_journey(j, {
        "A": [_sdep(s2, "S2", 0.0, "s2")],
        "B": [_sdep(s7, "S7", 0.0, "s7", "B"),
              _sdep(s7 + timedelta(minutes=20), "S7", 0.0, "s7b", "B")],
    }, None, now)

    assert "cannot meet" in plan.explanation
    assert "21 min late" in plan.explanation
    # Nothing is actually delayed, so nothing may be charged or alerted.
    assert plan.shift_minutes == pytest.approx(0.0, abs=0.01)
    assert not plan.anomalous


def test_a_healthy_connection_does_not_repeat_the_slack_chip_in_words():
    """The chip already prints '+20 min to change'. Saying it twice is noise."""
    from bahnwatch.planner import plan_journey
    j = _journey()
    now = datetime(2026, 3, 2, 21, 40, tzinfo=BERLIN)
    s2 = datetime(2026, 3, 2, 21, 57, tzinfo=BERLIN)
    s7 = datetime(2026, 3, 2, 22, 24, tzinfo=BERLIN)

    plan = plan_journey(j, {
        "A": [_sdep(s2, "S2", 0.0, "s2")],
        "B": [_sdep(s7, "S7", 0.0, "s7", "B"),
              _sdep(s7 + timedelta(minutes=20), "S7", 0.0, "s7b", "B")],
    }, None, now)

    first = plan.legs[0]
    assert first.connection_slack_min is not None
    assert "to change" not in (first.note or "")


def test_direction_accepts_several_termini_in_the_same_direction():
    """A terminus is not a direction.

    Southbound S2s out of Blankenburg end at Lichtenrade, Marienfelde or
    Blankenfelde and all three pass Friedrichstrasse. Pinning the leg to one of
    them turns a 10-minute headway into 30 and overstates what a delay costs.
    """
    from bahnwatch.planner import _direction_matches
    wanted = "Blankenfelde|Lichtenrade|Marienfelde"
    for terminus in ["S Blankenfelde (TF) Bhf", "S Lichtenrade (Berlin)",
                     "S Marienfelde (Berlin)"]:
        assert _direction_matches(wanted, terminus), terminus
    for wrong_way in ["S Bernau Bhf", "S Buch (Berlin)"]:
        assert not _direction_matches(wanted, wrong_way), wrong_way


# ---------------------------------------------------------------------------
# Leave time from the timetable, not from arithmetic.
#
# The real failure, reproduced: every duration in the subtraction was correct
# and the answer was twenty minutes wrong, because trains leave at times.
# ---------------------------------------------------------------------------

def _real_evening(now):
    """The actual S Blankenburg / Friedrichstr. timetable, from the live feed.

    Southbound S2 every 10 min at :30 :40 :50 :00, terminus rotating.
    S7 to Potsdam every 20 min at :04 :24 :44; the :54 and :14 westbound
    services stop short at Westkreuz and cannot be used.
    """
    day = now.replace(second=0, microsecond=0)
    s2 = [(21, 30, "S Lichtenrade (Berlin)"), (21, 40, "S Blankenfelde (TF) Bhf"),
          (21, 50, "S Marienfelde (Berlin)"), (22, 0, "S Blankenfelde (TF) Bhf"),
          (22, 10, "S Lichtenrade (Berlin)")]
    s7 = [(21, 44, "S Potsdam Hauptbahnhof"), (21, 54, "S Westkreuz (Berlin)"),
          (22, 4, "S Potsdam Hauptbahnhof"), (22, 14, "S Westkreuz (Berlin)"),
          (22, 24, "S Potsdam Hauptbahnhof"), (22, 44, "S Potsdam Hauptbahnhof")]
    return {
        "A": [_sdep(day.replace(hour=h, minute=m), "S2", 0.0, f"s2-{h}{m}",
                    direction=dest) for h, m, dest in s2],
        "B": [_sdep(day.replace(hour=h, minute=m), "S7", 0.0, f"s7-{h}{m}", "B",
                    direction=dest) for h, m, dest in s7],
    }


def test_leave_time_comes_from_services_that_exist():
    """23:02 target: the 21:50 S2 makes the 22:24 S7. The 22:00 does not."""
    from bahnwatch.planner import plan_journey
    now = datetime(2026, 3, 2, 21, 20, tzinfo=BERLIN)
    plan = plan_journey(_journey(), _real_evening(now), None, now)

    # 21:50 departure, minus 7 min walk and 4 min buffer.
    assert plan.normal_leave_at.hour == 21
    assert plan.normal_leave_at.minute == 39
    # The arithmetic answer -- 23:02 minus 74 minutes -- is 21:48, which misses.
    assert plan.normal_leave_at < datetime(2026, 3, 2, 21, 48, tzinfo=BERLIN)


def test_the_chosen_first_leg_actually_connects():
    from bahnwatch.planner import plan_journey
    now = datetime(2026, 3, 2, 21, 20, tzinfo=BERLIN)
    plan = plan_journey(_journey(), _real_evening(now), None, now)

    first, second = plan.legs[0], plan.legs[1]
    assert first.scheduled_departure.minute == 50
    assert second.scheduled_departure.minute == 24
    assert not first.connection_missed
    assert "cannot meet" not in plan.explanation


def test_short_running_services_are_not_mistaken_for_the_through_train():
    """Westbound S7s to Westkreuz do not reach Potsdam and must not be chosen."""
    from bahnwatch.planner import plan_journey
    now = datetime(2026, 3, 2, 21, 20, tzinfo=BERLIN)
    plan = plan_journey(_journey(), _real_evening(now), None, now)
    assert plan.legs[1].scheduled_departure.minute in (4, 24, 44)


def test_falls_back_to_arithmetic_when_the_feed_cannot_supply_a_chain():
    from bahnwatch.planner import plan_journey
    j = _journey()
    now = datetime(2026, 3, 2, 21, 20, tzinfo=BERLIN)
    plan = plan_journey(j, {"A": [], "B": []}, None, now)
    assert plan.normal_leave_at == j.scheduled_leave(now)


# ---------------------------------------------------------------------------
# Quota-aware polling. A journey that happens once a day does not need a
# 60-second poll at 04:00, and a key with a request budget makes that a cost
# rather than just waste.
# ---------------------------------------------------------------------------

def test_poller_idles_outside_the_commute_window():
    from bahnwatch import api

    j = api.JOURNEYS[0]
    day = datetime(2026, 3, 2, tzinfo=BERLIN)        # a Monday
    leave = j.scheduled_leave(day)

    near = leave - timedelta(minutes=10)
    far = leave - timedelta(hours=10)

    assert api._minutes_to_next_journey(near) <= api.ACTIVE_WINDOW_MIN
    assert api._minutes_to_next_journey(far) > api.ACTIVE_WINDOW_MIN


def test_an_empty_journey_list_idles_rather_than_polling_flat_out():
    from bahnwatch import api
    saved = api.JOURNEYS
    try:
        api.JOURNEYS = []
        assert api._minutes_to_next_journey(datetime(2026, 3, 2, 9, 0, tzinfo=BERLIN)) \
            == float("inf")
    finally:
        api.JOURNEYS = saved
