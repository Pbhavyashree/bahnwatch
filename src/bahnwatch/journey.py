"""Multi-leg journeys.

A real commute has changes, and a change is where the interesting risk lives. A
delay on the first leg matters only if it makes you miss the second -- and if it
does, the cost is not the delay, it is the wait for the next connecting service.

Concretely: an S2 running 6 minutes late into a 5-minute connection makes you
miss an S7 that runs every 20. The 6-minute delay costs 20 minutes. A 4-minute
delay on the same journey costs nothing at all. Single-leg reasoning cannot
express that difference, and it is the difference that decides whether you need
to leave early.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from bahnwatch.models import BERLIN


@dataclass(frozen=True)
class Leg:
    """One vehicle, boarded at one stop and left at another."""

    line: str
    direction: str
    origin_stop_id: str
    origin_stop_name: str
    dest_stop_name: str
    # Scheduled in-vehicle time for this leg.
    ride_minutes: float
    # Walking time between platforms on arrival, before the next leg can be
    # boarded. Zero for the final leg. Underestimating this is the single most
    # common way a journey planner produces a connection nobody can actually
    # make -- Friedrichstraße's cross-platform changes are not all equal.
    transfer_minutes: float = 0.0
    # Typical service interval for this line at this time, used when there is
    # no learned baseline yet.
    nominal_headway: float = 20.0


@dataclass(frozen=True)
class Journey:
    """A door-to-door commute with one or more legs."""

    id: str
    label: str
    # Door to the first platform.
    walk_minutes: float
    legs: tuple[Leg, ...]
    # Risk tolerance, applied once at the end rather than per leg.
    buffer_minutes: float
    arrive_by_hour: int
    arrive_by_minute: int
    days: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)
    notes: str = ""

    @property
    def first_leg(self) -> Leg:
        return self.legs[0]

    @property
    def total_ride_minutes(self) -> float:
        return sum(leg.ride_minutes for leg in self.legs)

    @property
    def total_transfer_minutes(self) -> float:
        return sum(leg.transfer_minutes for leg in self.legs)

    @property
    def stop_ids(self) -> set[str]:
        return {leg.origin_stop_id for leg in self.legs}

    def target_arrival(self, on: datetime) -> datetime:
        """Next occurrence of the target arrival time.

        Rolls to the next day the journey runs once today's time has passed --
        otherwise the countdown reads "924 minutes ago", which is arithmetically
        correct and completely useless.
        """
        local = on.astimezone(BERLIN)
        target = local.replace(hour=self.arrive_by_hour, minute=self.arrive_by_minute,
                               second=0, microsecond=0)
        # Allow a short grace period so a journey in progress is not immediately
        # rolled to tomorrow.
        if target < local - timedelta(minutes=20):
            target += timedelta(days=1)
        # Skip forward to a day this journey actually runs.
        for _ in range(8):
            if target.weekday() in self.days:
                return target
            target += timedelta(days=1)
        return target

    def scheduled_leave(self, on: datetime) -> datetime:
        """Door departure assuming every leg runs to timetable."""
        return self.target_arrival(on) - timedelta(
            minutes=(self.walk_minutes + self.total_ride_minutes
                     + self.total_transfer_minutes + self.buffer_minutes)
        )

    def runs_on(self, when: datetime) -> bool:
        return when.astimezone(BERLIN).weekday() in self.days


@dataclass
class LegStatus:
    """What the live feed says about one leg of a planned journey."""

    line: str
    from_stop: str
    to_stop: str
    scheduled_departure: datetime | None
    current_delay_min: float
    predicted_delay_min: float
    headway_min: float
    # Only meaningful for legs after the first.
    connection_slack_min: float | None = None
    connection_missed: bool = False
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "line": self.line,
            "from_stop": self.from_stop,
            "to_stop": self.to_stop,
            "scheduled_departure": (self.scheduled_departure.isoformat()
                                    if self.scheduled_departure else None),
            "current_delay_min": round(self.current_delay_min, 1),
            "predicted_delay_min": round(self.predicted_delay_min, 1),
            "headway_min": round(self.headway_min, 1),
            "connection_slack_min": (round(self.connection_slack_min, 1)
                                     if self.connection_slack_min is not None else None),
            "connection_missed": self.connection_missed,
            "note": self.note,
        }


@dataclass
class JourneyPlan:
    """When to leave for a multi-leg journey, and why."""

    journey_id: str
    label: str
    normal_leave_at: datetime
    adjusted_leave_at: datetime
    shift_minutes: float
    total_cost_minutes: float
    legs: list[LegStatus] = field(default_factory=list)
    anomalous: bool = False
    explanation: str = ""
    minutes_until_leave: float = 0.0

    @property
    def actionable(self) -> bool:
        from bahnwatch.commute import MIN_ACTIONABLE_SHIFT_MIN
        return abs(self.shift_minutes) >= MIN_ACTIONABLE_SHIFT_MIN

    def to_dict(self) -> dict:
        return {
            "journey_id": self.journey_id,
            "label": self.label,
            "normal_leave_at": self.normal_leave_at.isoformat(),
            "adjusted_leave_at": self.adjusted_leave_at.isoformat(),
            "shift_minutes": round(self.shift_minutes, 1),
            "total_cost_minutes": round(self.total_cost_minutes, 1),
            "minutes_until_leave": round(self.minutes_until_leave, 1),
            "anomalous": self.anomalous,
            "explanation": self.explanation,
            "legs": [leg.to_dict() for leg in self.legs],
        }
