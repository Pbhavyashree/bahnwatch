"""Departure planning and the decision to interrupt someone.

This is the part of the project that matters. Everything else -- fetching a
feed, storing rows -- is plumbing that any competent developer writes the same
way. The interesting problems are here, and there are three:

1.  The delay you care about is the one at *boarding time*, not now.
2.  Delay magnitude is the wrong alert trigger. Headway is what decides whether
    a delay costs the passenger anything.
3.  An alerting system that cries wolf gets muted, and a muted system has
    negative value -- worse than not existing, because the user now believes
    they are covered.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from bahnwatch.models import (
    Alert,
    AlertKind,
    Baseline,
    Commute,
    Departure,
)

# Delays decay. A train 10 minutes late at 07:00 is usually not 10 minutes late
# at 07:40 -- operators pad schedules, hold at termini, and skip stops to
# recover. This is the half-life of that recovery in minutes, estimated from
# the shape of observed delay decay rather than assumed.
#
# Using the current delay unchanged as the prediction is the naive alternative,
# and it systematically over-alerts: it tells people to leave early for
# disruptions that will have cleared by the time they board.
DELAY_HALF_LIFE_MIN = 25.0

# Below this, a change in departure time is not actionable. Nobody can act on
# "leave 90 seconds earlier", and alerting on it trains the user to ignore you.
MIN_ACTIONABLE_SHIFT_MIN = 4.0

# Once an alert has fired, require this much *additional* deterioration before
# firing again. Without hysteresis a delay oscillating around the threshold
# produces a stream of notifications -- the classic flapping-alarm failure.
HYSTERESIS_MIN = 5.0

# Alerting about an 08:00 commute at 06:00 is noise: the situation will change.
# This is how far ahead of normal departure the system starts watching.
ACTION_WINDOW_MIN = 50.0


def predict_delay_at_boarding(current_delay_min: float, minutes_until_boarding: float,
                              half_life: float = DELAY_HALF_LIFE_MIN) -> float:
    """Exponential decay of a delay toward zero.

    delay(t) = delay_now * 2^(-t / half_life)

    Chosen over three alternatives, each of which is wrong in a specific way:

    * **Persistence** (delay stays constant) over-predicts. Networks actively
      recover; schedules contain padding precisely so they can.
    * **Linear decay** hits zero at a fixed horizon and then goes negative,
      which has to be clamped, and the clamp is where the model stops
      describing anything real.
    * **Regression on features** would likely beat this, but needs months of
      history per line before it beats a sensible prior, and it fails opaquely
      when it fails. This is defensible on day one and improves smoothly as
      history accumulates.

    The half-life is a single interpretable parameter that can be fitted per
    line from observed decay once enough data exists.
    """
    if minutes_until_boarding <= 0:
        return current_delay_min
    return current_delay_min * math.pow(2.0, -minutes_until_boarding / half_life)


def effective_cost(delay_min: float, headway_min: float) -> float:
    """What a delay actually costs the passenger, in minutes.

    The insight the BVG app misses.

    If a line runs every 3 minutes and your train is 8 minutes late, you do not
    lose 8 minutes -- you catch a different train and lose at most one headway.
    If a line runs every 20 minutes and your train is 4 minutes late, you may
    still make it, but if you miss it you lose the full 20.

    So cost is bounded by the headway when the delay exceeds it: the passenger
    switches to the next service. Below the headway, the delay is paid directly.

    This is why alerting on raw delay magnitude produces both false alarms (big
    delay, high-frequency line, no real impact) and misses (small delay, low
    frequency, missed connection).
    """
    if headway_min <= 0:
        return max(0.0, delay_min)
    return min(max(0.0, delay_min), headway_min)


@dataclass
class Plan:
    """When to leave, and why."""

    commute_id: str
    normal_leave_at: datetime
    adjusted_leave_at: datetime
    shift_minutes: float           # positive = leave earlier than normal
    current_delay_min: float
    predicted_delay_min: float
    effective_cost_min: float
    headway_min: float
    minutes_until_leave: float
    anomalous: bool
    explanation: str

    @property
    def actionable(self) -> bool:
        return abs(self.shift_minutes) >= MIN_ACTIONABLE_SHIFT_MIN

    def to_dict(self) -> dict:
        return {
            "commute_id": self.commute_id,
            "normal_leave_at": self.normal_leave_at.isoformat(),
            "adjusted_leave_at": self.adjusted_leave_at.isoformat(),
            "shift_minutes": round(self.shift_minutes, 1),
            "current_delay_min": round(self.current_delay_min, 1),
            "predicted_delay_min": round(self.predicted_delay_min, 1),
            "effective_cost_min": round(self.effective_cost_min, 1),
            "headway_min": round(self.headway_min, 1),
            "minutes_until_leave": round(self.minutes_until_leave, 1),
            "anomalous": self.anomalous,
            "explanation": self.explanation,
        }


def build_plan(commute: Commute, departures: list[Departure],
               baseline: Baseline | None, now: datetime) -> Plan:
    """Work out when the user should leave, given live conditions.

    Only departures the user could actually catch are considered -- one that
    leaves before they could physically walk to the stop is irrelevant, however
    punctual it is. Filtering those out is easy to forget and produces a system
    that cheerfully reports everything is fine using trains that already left.
    """
    normal_leave = commute.baseline_departure(now)
    headway = baseline.headway_minutes if baseline else _assume_headway(commute.line)

    earliest_boardable = now + timedelta(minutes=commute.walk_minutes)
    candidates = [
        d for d in departures
        if d.line == commute.line and not d.cancelled and d.actual >= earliest_boardable
    ]

    if not candidates:
        cancelled = [d for d in departures if d.line == commute.line and d.cancelled]
        note = ("all upcoming services cancelled" if cancelled
                else "no upcoming departures in the feed")
        return Plan(
            commute_id=commute.id,
            normal_leave_at=normal_leave,
            adjusted_leave_at=normal_leave - timedelta(minutes=headway),
            shift_minutes=headway,
            current_delay_min=0.0, predicted_delay_min=0.0,
            effective_cost_min=headway, headway_min=headway,
            minutes_until_leave=(normal_leave - now).total_seconds() / 60.0,
            anomalous=True,
            explanation=note,
        )

    target = min(candidates, key=lambda d: d.actual)
    current_delay = target.delay_minutes
    minutes_until_boarding = max(0.0, (target.actual - now).total_seconds() / 60.0)

    predicted = predict_delay_at_boarding(current_delay, minutes_until_boarding)
    cost = effective_cost(predicted, headway)

    anomalous = False
    if baseline and baseline.is_reliable:
        # Compared against this line's own p90 for this hour and day type, not
        # a fixed threshold. Five minutes on the U8 at 08:00 may be routine;
        # the same five minutes at 22:00 may not be.
        anomalous = current_delay > baseline.p90_delay

    adjusted_leave = normal_leave - timedelta(minutes=cost)
    shift = (normal_leave - adjusted_leave).total_seconds() / 60.0

    return Plan(
        commute_id=commute.id,
        normal_leave_at=normal_leave,
        adjusted_leave_at=adjusted_leave,
        shift_minutes=shift,
        current_delay_min=current_delay,
        predicted_delay_min=predicted,
        effective_cost_min=cost,
        headway_min=headway,
        minutes_until_leave=(adjusted_leave - now).total_seconds() / 60.0,
        anomalous=anomalous,
        explanation=_explain(current_delay, predicted, cost, headway,
                             minutes_until_boarding, anomalous, baseline),
    )


def _explain(current: float, predicted: float, cost: float, headway: float,
             until_boarding: float, anomalous: bool, baseline: Baseline | None) -> str:
    """Plain-language reason. Every alert should say why, not just what."""
    if current < 2.0:
        return "Running normally."

    bits = [f"{current:.0f} min delay now"]

    if until_boarding > 5 and predicted < current - 1:
        bits.append(f"likely ~{predicted:.0f} min by the time you board")

    if cost < predicted - 0.5:
        bits.append(f"but trains run every ~{headway:.0f} min, so it costs you about {cost:.0f}")

    if anomalous and baseline:
        bits.append(f"this is above the usual {baseline.p90_delay:.0f} min worst case for this hour")
    elif baseline and baseline.is_reliable and current <= baseline.p75_delay:
        bits.append("which is normal for this line at this time")

    return "; ".join(bits) + "."


def _assume_headway(line: str) -> float:
    """Fallback headway when there is no history yet.

    Rough Berlin daytime frequencies. Used only until a real baseline exists,
    and deliberately conservative -- overestimating headway makes the system
    alert more readily, which is the safer direction while it is still learning.
    """
    if line.startswith("U"):
        return 5.0
    if line.startswith("S"):
        return 10.0
    if line.startswith("M"):
        return 7.0
    if line.startswith(("RE", "RB")):
        return 30.0
    return 12.0


# ---------------------------------------------------------------------------
# alert decision
# ---------------------------------------------------------------------------
@dataclass
class AlertState:
    """What has already been sent for one commute today.

    Kept per commute per day so the hysteresis and rate limit reset overnight
    rather than leaking across days.
    """
    last_shift_alerted: float = 0.0
    alerts_sent_today: int = 0
    day: str = ""
    recovered_sent: bool = False


def decide_alert(plan: Plan, commute: Commute, state: AlertState,
                 now: datetime, max_per_day: int = 3) -> Alert | None:
    """Whether to interrupt the user, and with what.

    Five gates, in order of cheapness. Each one exists because of a specific
    way notification systems become worthless:

    1. **Action window.** Telling someone at 06:00 about their 08:00 train is
       noise; the situation will have changed twice by then.
    2. **Already left.** Once the adjusted departure time has passed, an alert
       is not advice, it is a reproach.
    3. **Actionability.** A shift under ~4 minutes cannot be acted on.
    4. **Hysteresis.** After alerting, require materially worse conditions
       before alerting again -- otherwise a delay hovering at the threshold
       produces a stream of notifications.
    5. **Daily cap.** A hard backstop. If the system wants to send five alerts
       for one commute, the system is wrong, not the network.

    Recovery notifications ("you can leave at the normal time after all") are
    sent once, and are arguably the most valuable output here: they give back
    time the user had already written off, and no transit app does it.
    """
    today = now.date().isoformat()
    if state.day != today:
        state.day, state.last_shift_alerted = today, 0.0
        state.alerts_sent_today, state.recovered_sent = 0, False

    if not commute.runs_on(now):
        return None

    minutes_to_normal = (plan.normal_leave_at - now).total_seconds() / 60.0

    # 1. outside the window
    if minutes_to_normal > ACTION_WINDOW_MIN:
        return None

    # 2. departure time already passed
    if minutes_to_normal < -5:
        return None

    # recovery: previously told to leave early, now no longer necessary
    if (state.last_shift_alerted >= MIN_ACTIONABLE_SHIFT_MIN
            and plan.shift_minutes < MIN_ACTIONABLE_SHIFT_MIN
            and not state.recovered_sent):
        state.recovered_sent = True
        state.last_shift_alerted = 0.0
        return Alert(
            kind=AlertKind.RECOVERED,
            commute_id=commute.id,
            title=f"{commute.line} is back to normal",
            body=(f"You can leave at the usual time "
                  f"({plan.normal_leave_at.strftime('%H:%M')})."),
            shift_minutes=0.0,
            leave_at=plan.normal_leave_at,
            context=plan.explanation,
        )

    # 3. not actionable
    if not plan.actionable:
        return None

    # 4. hysteresis
    if plan.shift_minutes < state.last_shift_alerted + HYSTERESIS_MIN:
        return None

    # 5. daily cap
    if state.alerts_sent_today >= max_per_day:
        return None

    state.last_shift_alerted = plan.shift_minutes
    state.alerts_sent_today += 1
    state.recovered_sent = False

    kind = AlertKind.CANCELLED if "cancelled" in plan.explanation else AlertKind.LEAVE_EARLIER
    return Alert(
        kind=kind,
        commute_id=commute.id,
        title=f"Leave {plan.shift_minutes:.0f} min early — {commute.line}",
        body=(f"Leave at {plan.adjusted_leave_at.strftime('%H:%M')} "
              f"instead of {plan.normal_leave_at.strftime('%H:%M')}."),
        shift_minutes=plan.shift_minutes,
        leave_at=plan.adjusted_leave_at,
        context=plan.explanation,
    )


# ---------------------------------------------------------------------------
# multi-leg alerting
# ---------------------------------------------------------------------------
def decide_alert_for_journey(plan, journey, state: AlertState,
                             now: datetime, max_per_day: int = 3):
    """Alert decision for a multi-leg journey.

    Same five gates as the single-leg version, with one addition: a broken
    connection is escalated regardless of the raw shift. Missing a connection is
    qualitatively different from being generally late -- it strands you on a
    platform for a full headway, and the traveller can often avoid it entirely
    by leaving a few minutes earlier and catching the previous service.
    """
    from bahnwatch.models import Alert, AlertKind

    today = now.date().isoformat()
    if state.day != today:
        state.day, state.last_shift_alerted = today, 0.0
        state.alerts_sent_today, state.recovered_sent = 0, False

    if not journey.runs_on(plan.normal_leave_at):
        return None

    minutes_to_normal = (plan.normal_leave_at - now).total_seconds() / 60.0
    if minutes_to_normal > ACTION_WINDOW_MIN or minutes_to_normal < -5:
        return None

    broken = [leg for leg in plan.legs if leg.connection_missed]

    # recovery
    if (state.last_shift_alerted >= MIN_ACTIONABLE_SHIFT_MIN
            and plan.shift_minutes < MIN_ACTIONABLE_SHIFT_MIN
            and not broken
            and not state.recovered_sent):
        state.recovered_sent = True
        state.last_shift_alerted = 0.0
        return Alert(
            kind=AlertKind.RECOVERED,
            commute_id=journey.id,
            title="Connection is safe again",
            body=f"Leave at the usual time ({plan.normal_leave_at.strftime('%H:%M')}).",
            shift_minutes=0.0,
            leave_at=plan.normal_leave_at,
            context=plan.explanation,
        )

    if not broken and not plan.actionable:
        return None
    if plan.shift_minutes < state.last_shift_alerted + HYSTERESIS_MIN and not broken:
        return None
    if state.alerts_sent_today >= max_per_day:
        return None

    state.last_shift_alerted = max(plan.shift_minutes, MIN_ACTIONABLE_SHIFT_MIN)
    state.alerts_sent_today += 1
    state.recovered_sent = False

    if broken:
        leg = broken[0]
        title = f"You'll miss the {leg.line} connection"
        body = (f"Leave at {plan.adjusted_leave_at.strftime('%H:%M')} "
                f"instead of {plan.normal_leave_at.strftime('%H:%M')}.")
    else:
        title = f"Leave {plan.shift_minutes:.0f} min early"
        body = (f"Leave at {plan.adjusted_leave_at.strftime('%H:%M')} "
                f"instead of {plan.normal_leave_at.strftime('%H:%M')}.")

    return Alert(
        kind=AlertKind.LEAVE_EARLIER,
        commute_id=journey.id,
        title=title,
        body=body,
        shift_minutes=plan.shift_minutes,
        leave_at=plan.adjusted_leave_at,
        context=plan.explanation,
    )
