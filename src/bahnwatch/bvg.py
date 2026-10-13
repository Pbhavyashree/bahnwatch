"""Live departure data.

Two implementations behind one interface: the real BVG feed, and a simulator.

The simulator is not a testing shortcut. A demo that shows a blank screen
because an upstream API is slow, rate-limiting, or down is worse than no demo,
and this is a public-facing page. It also means the entire test suite runs
offline with no network and no flakiness.
"""

from __future__ import annotations

import asyncio
import math
import random
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import ClassVar

from bahnwatch.models import BERLIN, Departure, Mode, now_berlin

# Community-run HAFAS wrapper. No key, no registration, generous rate limits.
BVG_BASE = "https://v6.bvg.transport.rest"

_MODE_MAP = {
    "suburban": Mode.SBAHN, "subway": Mode.UBAHN, "tram": Mode.TRAM,
    "bus": Mode.BUS, "regional": Mode.REGIONAL, "ferry": Mode.FERRY,
    "express": Mode.EXPRESS,
}


class DepartureSource(ABC):
    @abstractmethod
    async def departures(self, stop_id: str, minutes: int = 60) -> list[Departure]:
        ...


class BvgClient(DepartureSource):
    """Real feed.

    Failures return an empty list rather than raising. A commute alerter that
    crashes when the transit API hiccups is useless at exactly the moment it
    matters -- during disruption, when that API is also under load. Callers
    treat "no data" as a distinct state, not as "everything is fine".
    """

    def __init__(self, base_url: str = BVG_BASE, timeout: float = 10.0,
                 max_retries: int = 2):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.last_error: str | None = None

    async def departures(self, stop_id: str, minutes: int = 60) -> list[Departure]:
        import httpx

        params = {"duration": minutes, "results": 60, "remarks": "false"}
        url = f"{self.base_url}/stops/{stop_id}/departures"

        for attempt in range(self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.get(url, params=params)
                if resp.status_code == 429 and attempt < self.max_retries:
                    await asyncio.sleep(random.uniform(0.5, 2.0 * (attempt + 1)))
                    continue
                resp.raise_for_status()
                self.last_error = None
                return self._parse(resp.json())
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
                if attempt >= self.max_retries:
                    return []
                await asyncio.sleep(random.uniform(0.3, 1.5 * (attempt + 1)))
        return []

    @staticmethod
    def _parse(payload: dict | list) -> list[Departure]:
        """Parse the HAFAS-shaped response.

        Defensive throughout: this is third-party data with optional fields that
        genuinely go missing. `when` is null for cancelled services, `delay` is
        null when no realtime data exists (which is not the same as zero delay,
        and conflating them makes a line with no live feed look perfectly
        punctual).
        """
        rows = payload.get("departures", []) if isinstance(payload, dict) else payload
        out: list[Departure] = []

        for r in rows or []:
            try:
                planned = r.get("plannedWhen") or r.get("when")
                if not planned:
                    continue
                scheduled = datetime.fromisoformat(planned).astimezone(BERLIN)

                line = r.get("line") or {}
                stop = r.get("stop") or {}
                delay = r.get("delay")

                out.append(Departure(
                    trip_id=r.get("tripId", ""),
                    line=line.get("name", "?"),
                    mode=_MODE_MAP.get(line.get("product", ""), Mode.BUS),
                    direction=r.get("direction") or "",
                    stop_id=str(stop.get("id", "")),
                    stop_name=stop.get("name", ""),
                    scheduled=scheduled,
                    delay_seconds=int(delay) if delay is not None else 0,
                    cancelled=bool(r.get("cancelled", False)),
                    platform=r.get("platform"),
                ))
            except (ValueError, TypeError, AttributeError):
                # One malformed row must not lose the other fifty-nine.
                continue
        return out


class SimulatedSource(DepartureSource):
    """Offline generator producing plausible Berlin departures.

    Delays are lognormal, not normal: they are bounded below by zero, right-
    skewed, and occasionally very large. A normal distribution generates
    negative delays, which then have to be clipped -- and clipping is where the
    model stops describing anything real.

    Rush hours and a controllable disruption make the alerting logic
    demonstrable on demand rather than only when Berlin happens to misbehave.

    Known blind spot: every line here runs in exactly one direction. The real
    S Blankenburg alternates S2s between Bernau and Blankenfelde, which meant
    the planner's line-only matching picked the wrong train about half the time
    and nothing in this simulator could show it. The fix is in
    `planner._direction_matches`; making the simulator bidirectional is the
    better fix and is still outstanding.
    """

    # Minutes past the hour at which the first service of each hour departs.
    # S2 from Blankenburg at :17/:37/:57; S7 from Friedrichstr. at :04/:24/:44 --
    # a scheduled 22-minute ride plus a 5-minute change lands exactly on the
    # next S7, which is the connection this project exists to protect.
    SCHEDULE_PHASE: ClassVar[dict[str, float]] = {"S2": 17.0, "S7": 4.0}

    LINES: ClassVar[dict[str, tuple[Mode, float]]] = {
        "S1": (Mode.SBAHN, 10.0), "S2": (Mode.SBAHN, 20.0),
        "S7": (Mode.SBAHN, 20.0), "S41": (Mode.SBAHN, 5.0),
        "U2": (Mode.UBAHN, 4.0), "U6": (Mode.UBAHN, 4.0), "U8": (Mode.UBAHN, 5.0),
        "M10": (Mode.TRAM, 7.0), "M4": (Mode.TRAM, 5.0),
        "142": (Mode.BUS, 12.0), "M29": (Mode.BUS, 10.0),
    }

    def __init__(self, seed: int = 42, disruption_line: str | None = None,
                 disruption_minutes: float = 0.0):
        self._rng = random.Random(seed)
        self.disruption_line = disruption_line
        self.disruption_minutes = disruption_minutes

    def set_disruption(self, line: str | None, minutes: float = 0.0) -> None:
        self.disruption_line = line
        self.disruption_minutes = minutes

    async def departures(self, stop_id: str, minutes: int = 60) -> list[Departure]:
        now = now_berlin()
        out: list[Departure] = []

        # Departures sit on an ABSOLUTE grid anchored to midnight, not on
        # offsets from `now`. Generating them relative to the current time gives
        # every departure a fresh identity on every poll, so the same train is
        # recorded dozens of times and the baselines it feeds are meaningless.
        # Real timetables are absolute; the simulator has to be too.
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        minutes_since_midnight = (now - midnight).total_seconds() / 60.0

        for line, (mode, headway) in self.LINES.items():
            # Real timetable phases for the lines in the configured journey, so
            # the S2 -> S7 connection at Friedrichstrasse exists in simulated
            # data as it does in reality. Without this the two lines are phased
            # randomly against each other, the five-minute change is sometimes
            # zero minutes, and the connection logic appears to be broken when
            # it is the fake timetable that is wrong.
            if line in self.SCHEDULE_PHASE:
                offset = self.SCHEDULE_PHASE[line]
            else:
                offset = (abs(hash(line)) % int(headway * 60)) / 60.0
            # First grid slot at or after now.
            k = math.ceil((minutes_since_midnight - offset) / headway)
            t = offset + k * headway - minutes_since_midnight
            while t < minutes:
                scheduled = (midnight + timedelta(minutes=minutes_since_midnight + t)
                             ).replace(second=0, microsecond=0)
                delay = self._delay_for(line, scheduled)
                cancelled = self._rng.random() < 0.004

                out.append(Departure(
                    trip_id=f"sim-{line}-{int(scheduled.timestamp())}",
                    line=line, mode=mode,
                    direction=self._direction(line),
                    stop_id=stop_id, stop_name="Simulated Stop",
                    scheduled=scheduled,
                    delay_seconds=int(delay * 60),
                    cancelled=cancelled,
                ))
                t += headway
        return sorted(out, key=lambda d: d.scheduled)

    def _delay_for(self, line: str, when: datetime) -> float:
        hour = when.hour
        # Rush hours: delays roughly double.
        rush = 1.0
        if 7 <= hour <= 9 or 16 <= hour <= 19:
            rush = 2.1
        elif 23 <= hour or hour <= 4:
            rush = 0.6

        # Buses sit in the same traffic as everyone else; U-Bahn does not.
        mode_factor = {"S": 1.2, "U": 0.8, "M": 1.4}.get(line[0], 1.6)

        base = self._rng.lognormvariate(math.log(0.9), 0.85) * rush * mode_factor
        if self.disruption_line and line == self.disruption_line:
            base += self.disruption_minutes * self._rng.uniform(0.7, 1.3)
        return max(0.0, base)

    def _direction(self, line: str) -> str:
        dests = {
            "S1": "Wannsee", "S2": "Blankenfelde", "S7": "Potsdam Hbf", "S41": "Ring",
            "U2": "Pankow", "U6": "Alt-Tegel", "U8": "Wittenau",
            "M10": "Warschauer Str.", "M4": "Falkenberg",
            "142": "Ostbahnhof", "M29": "Grunewald",
        }
        return dests.get(line, "Zentrum")
