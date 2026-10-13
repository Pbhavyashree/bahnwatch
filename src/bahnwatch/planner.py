"""Planning a multi-leg journey under live conditions.

The single idea here: **propagate delay forward through the legs and check each
connection**. A delay costs nothing until it breaks a connection, and then it
costs a whole headway of the next service.

That makes the cost function discontinuous, which is exactly why the naive
version -- add up the delays -- gives the wrong answer in both directions. It
over-alerts on a 15-minute delay that still makes a generous connection, and
under-alerts on a 6-minute delay that misses a tight one.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from bahnwatch.baseline import BaselineStore, day_type
from bahnwatch.commute import predict_delay_at_boarding
from bahnwatch.journey import Journey, JourneyPlan, Leg, LegStatus
from bahnwatch.models import Departure


def _headway_for(leg: Leg, store: BaselineStore | None, when: datetime) -> float:
    """Learned headway if there is enough history, otherwise the nominal one."""
    if store is not None:
        bl = store.baseline(leg.line, when.hour, day_type(when))
        if bl and bl.is_reliable and bl.headway_minutes > 0:
            return bl.headway_minutes
    return leg.nominal_headway


def _normalise_direction(text: str) -> str:
    """Reduce a destination to something two feeds can agree on.

    A configured leg says "Blankenfelde". The live feed says
    "S Blankenfelde (TF) Bhf". Neither spelling is wrong, and demanding equality
    between them matches nothing.
    """
    text = text.lower()
    text = re.sub(r"\(.*?\)", " ", text)                      # (TF), (Berlin)
    text = re.sub(r"\b(s|u|bhf|bahnhof|hbf|str|strasse|straße)\b", " ", text)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _direction_matches(wanted: str, actual: str) -> bool:
    """Whether a departure is heading the way this leg needs.

    Matching on line alone is wrong at any through station, and the simulator
    could never show it: `SimulatedSource` gives each line exactly one
    destination, so S2 there is always southbound. At the real S Blankenburg the
    S2 alternates — Bernau and Buch northbound, Blankenfelde and Lichtenrade
    south — so "the next S2" is the right train roughly half the time. The
    planner was quietly timing someone onto a train going the other way.

    But a terminus is not a direction, and conflating them is the same bug
    wearing different clothes. Southbound S2s out of Blankenburg terminate at
    Lichtenrade, Marienfelde *and* Blankenfelde on a rotation; every one of them
    passes Friedrichstrasse. Pinning the leg to a single terminus throws away
    two thirds of the service and makes a ten-minute headway look like thirty —
    which feeds the cost model and overstates what every delay is worth.

    So `wanted` is a list of acceptable destinations separated by `|`. The
    traveller cares where the train goes through, and the feed only reports
    where it ends up.
    """
    if not wanted:
        return True          # unconfigured direction means "any", as before
    a = _normalise_direction(actual)
    for alternative in wanted.split("|"):
        w = _normalise_direction(alternative)
        if w and (w in a or a in w):
            return True
    return False


def _pick_departure(departures: list[Departure], leg: Leg,
                    not_before: datetime) -> Departure | None:
    """Earliest departure of this line, in the right direction, that is boardable.

    `not_before` already includes walking or transfer time, so anything earlier
    is unreachable no matter how punctual it is. Filtering on scheduled rather
    than actual time would let a delayed train that has not yet left look
    catchable when it is not.
    """
    options = [
        d for d in departures
        if d.line == leg.line and not d.cancelled and d.actual >= not_before
        and _direction_matches(leg.direction, d.direction)
    ]
    return min(options, key=lambda d: d.actual) if options else None


def _timetabled_leave(journey: Journey,
                      departures_by_stop: dict[str, list[Departure]],
                      target: datetime) -> datetime | None:
    """When to leave, derived from services that exist rather than from durations.

    `Journey.scheduled_leave` subtracts walk + ride + transfer + buffer from the
    target. That treats transit as continuous, and transit is not continuous.
    For the real 23:02 Potsdam target it returned 21:48 — which puts you on the
    platform at 21:55, onto the 22:00 S2 rather than the 21:50, into
    Friedrichstrasse one minute after the 22:24 S7 has gone, and home at 23:22.
    Every individual number in that subtraction was right. The answer was twenty
    minutes wrong, because the arithmetic has no idea that trains leave at times.

    So: work backwards through actual departures. Take the last service on the
    final leg that still arrives by the target, then the last service on the leg
    before it that connects into that one, and so on to the door. Scheduled
    times, not realtime — this is the "if everything runs" baseline that the
    delay cost is then measured against, and mixing live delays in here would
    double-count them.

    Returns None when no feasible chain exists, which is a real answer: either
    the feed is thin or the target cannot be met. The caller falls back to the
    arithmetic and says so.
    """
    chosen: list[Departure] = []
    deadline = target
    for i in reversed(range(len(journey.legs))):
        leg = journey.legs[i]
        reachable = [
            d for d in departures_by_stop.get(leg.origin_stop_id, [])
            if d.line == leg.line and not d.cancelled
            and _direction_matches(leg.direction, d.direction)
            and d.scheduled + timedelta(minutes=leg.ride_minutes) <= deadline
        ]
        if not reachable:
            return None
        pick = max(reachable, key=lambda d: d.scheduled)
        chosen.append(pick)
        if i > 0:
            # The previous leg must arrive early enough to make this change.
            deadline = pick.scheduled - timedelta(
                minutes=journey.legs[i - 1].transfer_minutes
            )

    first = chosen[-1]
    return first.scheduled - timedelta(
        minutes=journey.walk_minutes + journey.buffer_minutes
    )


def plan_journey(journey: Journey, departures_by_stop: dict[str, list[Departure]],
                 store: BaselineStore | None, now: datetime) -> JourneyPlan:
    """Work out when to leave, accounting for connection risk on every leg."""
    target = journey.target_arrival(now)
    # Prefer the timetable over the arithmetic whenever the feed can supply it.
    normal_leave = (_timetabled_leave(journey, departures_by_stop, target)
                    or journey.scheduled_leave(now))

    statuses: list[LegStatus] = []
    total_cost = 0.0
    anomalous = False
    reasons: list[str] = []

    # When the traveller can first be at the first platform.
    ready_at = max(now, normal_leave) + timedelta(minutes=journey.walk_minutes)

    for i, leg in enumerate(journey.legs):
        deps = departures_by_stop.get(leg.origin_stop_id, [])
        headway = _headway_for(leg, store, now)

        chosen = _pick_departure(deps, leg, ready_at)

        if chosen is None:
            # No live data for this leg. Whether that is a problem depends
            # entirely on timing: a feed reaching 90 minutes ahead legitimately
            # has nothing to say about a journey twenty hours away, and flagging
            # that as an anomaly would mean the dashboard is permanently red for
            # everything except the hour before departure.
            imminent = (normal_leave - now).total_seconds() / 60.0 <= 90.0
            statuses.append(LegStatus(
                line=leg.line, from_stop=leg.origin_stop_name, to_stop=leg.dest_stop_name,
                scheduled_departure=None, current_delay_min=0.0, predicted_delay_min=0.0,
                headway_min=headway,
                note=("no live departures — expected, journey is not imminent"
                      if not imminent else "no live departures in the feed"),
            ))
            ready_at = ready_at + timedelta(minutes=leg.ride_minutes + leg.transfer_minutes)
            if imminent:
                reasons.append(f"{leg.line}: no live data")
                anomalous = True
            continue

        minutes_until_boarding = max(0.0, (chosen.actual - now).total_seconds() / 60.0)
        predicted = predict_delay_at_boarding(chosen.delay_minutes, minutes_until_boarding)

        # Arrival at the far end of this leg, carrying the delay forward.
        arrival = (chosen.scheduled + timedelta(minutes=predicted + leg.ride_minutes))

        # Baseline anomaly check.
        if store is not None:
            bl = store.baseline(leg.line, now.hour, day_type(now))
            if bl and bl.is_reliable and chosen.delay_minutes > bl.p90_delay:
                anomalous = True
                reasons.append(
                    f"{leg.line} is worse than its usual {bl.p90_delay:.0f} min worst case"
                )

        status = LegStatus(
            line=leg.line, from_stop=leg.origin_stop_name, to_stop=leg.dest_stop_name,
            scheduled_departure=chosen.scheduled,
            current_delay_min=chosen.delay_minutes,
            predicted_delay_min=predicted,
            headway_min=headway,
        )

        is_last = i == len(journey.legs) - 1
        if is_last:
            # On the final leg the delay is paid directly -- there is no
            # connection to protect, only the arrival time.
            lateness = max(0.0, (arrival - target).total_seconds() / 60.0)
            # Lateness caused by the timetable rather than by any delay: the
            # service you can actually catch is *scheduled* to arrive after the
            # target. Leaving earlier cannot fix it and a delay did not cause
            # it, so reporting it as a delay is a lie in both directions — and
            # reporting nothing while the leg says "21 min after target" is
            # worse. It is a standing fact about the configuration, so it is
            # explained but never allowed to raise an alert: a condition that is
            # true every single evening is the fastest way to get an alerter
            # muted.
            scheduled_arrival = chosen.scheduled + timedelta(minutes=leg.ride_minutes)
            structural = max(0.0, (scheduled_arrival - target).total_seconds() / 60.0)

            if predicted > 1.0:
                total_cost += predicted
                reasons.append(f"{leg.line} running {predicted:.0f} min late")

            if structural > 0.5:
                status.note = (f"the {chosen.scheduled.strftime('%H:%M')} {leg.line} is "
                               f"scheduled to arrive {structural:.0f} min after your target")
                reasons.append(
                    f"this journey cannot meet {target.strftime('%H:%M')} — the best "
                    f"connection arrives {structural:.0f} min late with everything on time"
                )
            elif lateness > 0:
                status.note = f"would arrive {lateness:.0f} min after target"
        else:
            nxt = journey.legs[i + 1]
            can_board_next_at = arrival + timedelta(minutes=leg.transfer_minutes)

            next_deps = departures_by_stop.get(nxt.origin_stop_id, [])
            planned_next = _pick_departure(
                next_deps, nxt,
                # What the connection would look like with everything on time.
                chosen.scheduled + timedelta(minutes=leg.ride_minutes + leg.transfer_minutes),
            )
            actual_next = _pick_departure(next_deps, nxt, can_board_next_at)

            if planned_next is not None and actual_next is not None:
                slack = (planned_next.actual - can_board_next_at).total_seconds() / 60.0
                status.connection_slack_min = slack

                if actual_next.trip_id != planned_next.trip_id:
                    # The connection is broken: the cost is the wait for the
                    # service after the one that was planned, not the delay.
                    missed_by = (actual_next.actual - planned_next.actual).total_seconds() / 60.0
                    status.connection_missed = True
                    status.note = (f"misses the {planned_next.actual.strftime('%H:%M')} "
                                   f"{nxt.line} — next is "
                                   f"{actual_next.actual.strftime('%H:%M')}")
                    total_cost += max(0.0, missed_by)
                    anomalous = True
                    reasons.append(
                        f"{leg.line} delay breaks the {nxt.line} connection, "
                        f"costing {missed_by:.0f} min"
                    )
                elif slack < 2.0:
                    # The slack chip beside this already prints the number, so
                    # the note says only what the chip cannot.
                    status.note = "tight connection"
                    reasons.append(f"{nxt.line} connection is tight ({slack:.0f} min)")
                else:
                    status.note = ""
            else:
                nh = _headway_for(nxt, store, now)
                status.connection_slack_min = None
                if predicted > leg.transfer_minutes:
                    total_cost += min(predicted, nh)

        statuses.append(status)
        ready_at = arrival + timedelta(minutes=leg.transfer_minutes)

    adjusted = normal_leave - timedelta(minutes=total_cost)

    return JourneyPlan(
        journey_id=journey.id,
        label=journey.label,
        normal_leave_at=normal_leave,
        adjusted_leave_at=adjusted,
        shift_minutes=total_cost,
        total_cost_minutes=total_cost,
        legs=statuses,
        anomalous=anomalous,
        explanation=("; ".join(reasons) + "." if reasons else _quiet_explanation(journey, normal_leave, now)),
        minutes_until_leave=(adjusted - now).total_seconds() / 60.0,
    )


def _quiet_explanation(journey: Journey, normal_leave: datetime, now: datetime) -> str:
    """Message when nothing is wrong, which is most of the time."""
    mins = (normal_leave - now).total_seconds() / 60.0
    if mins > 90:
        hours = mins / 60.0
        return (f"Nothing to report yet — live data appears about 90 minutes "
                f"before departure ({hours:.0f}h away).")
    return "Everything running to schedule."
