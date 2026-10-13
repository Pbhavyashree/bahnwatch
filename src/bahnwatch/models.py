"""Domain types.

Times are always timezone-aware and always Europe/Berlin. Naive datetimes are
the single most common source of bugs in a system like this: a departure "at
07:42" is a different instant in March than in July, and the DST transition
happens at 02:00 on a Sunday when the U-Bahn is still running.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")


def now_berlin() -> datetime:
    """Current Berlin time, optionally overridden for demos.

    An evening commute alerter is awkward to develop at midday, so the clock can
    be moved:

        BAHNWATCH_DEMO_TIME=21:40    # pretend it is 21:40 today
        BAHNWATCH_CLOCK_OFFSET_MIN=90

    DEMO_TIME is the one to reach for. An offset requires the user to work out
    the difference between now and the time they want, which is arithmetic at
    the exact moment they are trying to look at something else -- and getting it
    wrong produces a page that looks broken rather than shifted.
    """
    import os

    real = datetime.now(BERLIN)

    demo = os.environ.get("BAHNWATCH_DEMO_TIME", "").strip()
    if demo:
        try:
            hh, mm = (int(x) for x in demo.split(":"))
            return real.replace(hour=hh, minute=mm, second=0, microsecond=0)
        except (ValueError, TypeError):
            pass  # malformed value falls through to real time rather than crashing

    offset = float(os.environ.get("BAHNWATCH_CLOCK_OFFSET_MIN", "0") or 0)
    return real + timedelta(minutes=offset)


class Mode(str, Enum):
    SBAHN = "suburban"
    UBAHN = "subway"
    TRAM = "tram"
    BUS = "bus"
    REGIONAL = "regional"
    FERRY = "ferry"
    EXPRESS = "express"


@dataclass(frozen=True)
class Departure:
    """One scheduled departure, with whatever the live feed says about it."""

    trip_id: str
    line: str
    mode: Mode
    direction: str
    stop_id: str
    stop_name: str
    scheduled: datetime
    delay_seconds: int = 0
    cancelled: bool = False
    platform: str | None = None
    # Whether the feed actually carried a realtime prediction for this service.
    # Without it, "no live data" and "predicted exactly on time" are both
    # delay_seconds == 0, an unmonitored line scores as the most punctual on
    # the network, and that fiction is then fed into its own baseline.
    has_realtime: bool = True
    observed_at: datetime = field(default_factory=now_berlin)

    @property
    def actual(self) -> datetime:
        return self.scheduled + timedelta(seconds=self.delay_seconds)

    @property
    def delay_minutes(self) -> float:
        return self.delay_seconds / 60.0

    @property
    def is_late(self) -> bool:
        # Under two minutes is not a delay anyone experiences as one, and the
        # feed itself is noisy at that resolution.
        return self.delay_seconds >= 120


@dataclass(frozen=True)
class Commute:
    """A journey the user actually makes, and when they need to arrive."""

    id: str
    label: str
    origin_stop_id: str
    origin_stop_name: str
    line: str
    direction: str
    # Minutes from door to platform. The single most-underestimated number in
    # any journey planner.
    walk_minutes: float
    # Scheduled in-vehicle time, door to destination.
    ride_minutes: float
    # How late the user is willing to risk being. Separate from walk time
    # because it is a risk preference, not a fact about geography.
    buffer_minutes: float
    # Target arrival, local clock time.
    arrive_by_hour: int
    arrive_by_minute: int
    # Days this commute runs. 0 = Monday.
    days: tuple[int, ...] = (0, 1, 2, 3, 4)

    def target_arrival(self, on: datetime) -> datetime:
        return on.astimezone(BERLIN).replace(
            hour=self.arrive_by_hour, minute=self.arrive_by_minute,
            second=0, microsecond=0,
        )

    def baseline_departure(self, on: datetime) -> datetime:
        """When to leave the door assuming everything runs on time."""
        return self.target_arrival(on) - timedelta(
            minutes=self.walk_minutes + self.ride_minutes + self.buffer_minutes
        )

    def runs_on(self, when: datetime) -> bool:
        return when.astimezone(BERLIN).weekday() in self.days


@dataclass(frozen=True)
class Leg:
    """One vehicle on a multi-leg journey."""

    line: str
    origin_stop_id: str
    origin_stop_name: str
    direction: str
    ride_minutes: float
    # Minutes needed to change platforms at the END of this leg. Zero on the
    # final leg. Friedrichstrasse S-Bahn platform changes are short; a change
    # involving a different level or building is not.
    transfer_minutes: float = 0.0


@dataclass(frozen=True)
class Journey:
    """A commute with one or more legs and a connection between them.

    A connection changes the problem qualitatively. On a single leg, a delay
    costs you at most one headway -- you take the next train. On a connection,
    a delay either still lets you make it (costing nothing) or does not
    (costing the full wait for the next onward service). The cost function is a
    step, not a ramp, and the step is where the whole risk sits.
    """

    id: str
    label: str
    legs: tuple[Leg, ...]
    walk_minutes: float           # door to first platform
    buffer_minutes: float         # personal risk tolerance
    depart_hour: int              # planned door-departure, local clock
    depart_minute: int
    arrive_by_hour: int
    arrive_by_minute: int
    days: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)

    @property
    def first_leg(self) -> Leg:
        return self.legs[0]

    def planned_departure(self, on: datetime) -> datetime:
        """When the user normally walks out of the door."""
        return on.astimezone(BERLIN).replace(
            hour=self.depart_hour, minute=self.depart_minute,
            second=0, microsecond=0,
        )

    def target_arrival(self, on: datetime) -> datetime:
        base = on.astimezone(BERLIN).replace(
            hour=self.arrive_by_hour, minute=self.arrive_by_minute,
            second=0, microsecond=0,
        )
        # A journey that departs late evening and arrives after midnight lands
        # on the following calendar day. Without this the target arrival is 23
        # hours in the past and every calculation is nonsense.
        if base < self.planned_departure(on):
            base += timedelta(days=1)
        return base

    def next_run(self, now: datetime) -> datetime:
        """The next occurrence of this journey, today or on a later day.

        Without this, a 21:50 commute viewed at 23:00 reports a departure 70
        minutes in the past. Rolling forward to the next valid day is what makes
        the display sane at any hour.
        """
        now = now.astimezone(BERLIN)
        candidate = self.planned_departure(now)
        # Allow a short grace period so the journey does not vanish the second
        # the planned departure passes -- the user may still be running for it.
        if candidate < now - timedelta(minutes=20):
            candidate += timedelta(days=1)
        for _ in range(8):
            if candidate.weekday() in self.days:
                return candidate
            candidate += timedelta(days=1)
        return candidate

    def runs_on(self, when: datetime) -> bool:
        return when.astimezone(BERLIN).weekday() in self.days


class AlertKind(str, Enum):
    LEAVE_EARLIER = "leave_earlier"
    RECOVERED = "recovered"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Alert:
    kind: AlertKind
    commute_id: str
    title: str
    body: str
    # How much earlier (positive) or later (negative) the user must now leave.
    shift_minutes: float
    leave_at: datetime
    issued_at: datetime = field(default_factory=now_berlin)
    context: str = ""

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "commute_id": self.commute_id,
            "title": self.title,
            "body": self.body,
            "shift_minutes": round(self.shift_minutes, 1),
            "leave_at": self.leave_at.isoformat(),
            "issued_at": self.issued_at.isoformat(),
            "context": self.context,
        }


@dataclass(frozen=True)
class Baseline:
    """What 'normal' looks like for one line, in one hour, on one type of day."""

    line: str
    hour: int
    day_type: str          # "weekday" | "saturday" | "sunday"
    n_observations: int
    median_delay: float    # minutes
    p75_delay: float
    p90_delay: float
    iqr: float
    # Typical gap between departures. Determines whether a delay actually costs
    # the passenger anything -- see commute.py.
    headway_minutes: float

    @property
    def is_reliable(self) -> bool:
        """Whether there is enough history to trust this baseline.

        30 is not arbitrary: below roughly that, the p90 of a skewed
        distribution is being estimated from three or four points and moves
        wildly between refreshes. Under this threshold the system falls back to
        fixed thresholds rather than pretending to know the distribution.
        """
        return self.n_observations >= 30
