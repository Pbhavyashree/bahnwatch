"""VBB HAFAS ReST feed — the real one.

The official VBB API is a HAFAS ReST deployment, which is a different shape
from the community `transport.rest` wrapper this project started against:

    transport.rest            VBB HAFAS ReST
    --------------------      ---------------------------------------------
    ISO 8601 `plannedWhen`    separate local `date` + `time` strings
    `delay` in seconds        no delay field — you subtract rtTime from time
    `tripId`                  `JourneyDetailRef.ref`
    `line.product`            `Product.catOut` (or a *list* of Products)
    no key                    `accessId`, with a request quota

Everything above the `DepartureSource` interface is untouched by this file.
That boundary is the reason adding a second feed is one module rather than a
rewrite.

The key is read from the environment and never written to disk by this code.
It is redacted from every error string, because the usual way an API key
escapes is a stack trace pasted into a bug report.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from datetime import date as date_cls
from datetime import datetime, timedelta
from datetime import time as time_cls

from bahnwatch.bvg import DepartureSource
from bahnwatch.models import BERLIN, Departure, Mode

# VBB issues keys against a HaCon-hosted *test* system, separate from the
# production host, and the two are not interchangeable: a key for one is
# rejected by the other with API_AUTH, which reads exactly like a bad key.
# Production access is granted later, on request, against different terms --
# so this stays configurable rather than baked in.
VBB_BASE = os.environ.get(
    "VBB_API_BASE", "https://vbb.demo.hafas.cloud/api/fahrinfo/latest"
)

# catOut is the human-facing product category. VBB returns "S", "U", "Bus",
# "Tram"/"STR", "RE", "RB", "ICE", "IC"/"EC", "Fähre". Matched lowercase and
# prefix-wise so "S-Bahn", "S" and "SBahn" all land in the same place.
_CAT_PREFIXES: tuple[tuple[str, Mode], ...] = (
    ("ice", Mode.EXPRESS), ("ic", Mode.EXPRESS), ("ec", Mode.EXPRESS),
    ("re", Mode.REGIONAL), ("rb", Mode.REGIONAL), ("regional", Mode.REGIONAL),
    ("s", Mode.SBAHN),
    ("u", Mode.UBAHN),
    ("tram", Mode.TRAM), ("str", Mode.TRAM), ("m", Mode.TRAM),
    ("bus", Mode.BUS),
    ("f", Mode.FERRY), ("fäh", Mode.FERRY), ("fae", Mode.FERRY),
)


class RateLimiter:
    """Minimum spacing between upstream calls.

    The simulator had no cost per request, so the poller was free to hit every
    stop every 20 seconds. A keyed API is not free: two stops at 20 seconds is
    8,640 requests a day, which will exhaust a typical VBB quota before the
    evening commute this thing exists to watch.

    Spacing requests rather than counting them is deliberate. A daily counter
    fails closed at some unpredictable hour — likely the busy one, since that
    is when polls are densest. A floor on the interval keeps the service alive
    all day at a predictable rate.
    """

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._last = 0.0
        self._lock = asyncio.Lock()
        self.calls = 0

    async def acquire(self) -> None:
        async with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()
            self.calls += 1


def _parse_hafas_time(date_s: str, time_s: str) -> datetime:
    """Combine HAFAS local date + time into an aware Berlin datetime.

    HAFAS encodes a service running past midnight as an hour of 24 or more on
    the *departure* date: a train leaving at 00:12 on the 14th is "24:12:00" on
    the 13th. Feeding that to `time.fromisoformat` raises, and the obvious
    workaround — taking `hour % 24` — silently files the departure a day early,
    which is worse than crashing because the board just looks wrong.
    """
    hh, mm, *rest = (int(p) for p in time_s.split(":"))
    ss = rest[0] if rest else 0
    extra_days, hh = divmod(hh, 24)
    # fold=0 (the default) picks the first of the two 02:30s on the October DST
    # night. Either choice is wrong for one of them; this one is deterministic.
    combined = datetime.combine(
        date_cls.fromisoformat(date_s), time_cls(hh, mm, ss), tzinfo=BERLIN
    )
    return combined + timedelta(days=extra_days)


def _mode_from(cat: str, name: str) -> Mode:
    probe = (cat or name or "").strip().lower()
    for prefix, mode in _CAT_PREFIXES:
        if probe.startswith(prefix):
            return mode
    return Mode.BUS


def _as_product(raw) -> dict:
    """`Product` is an object on some HAFAS deployments and a list on others.

    VBB has returned both. Taking `[0]` on a dict yields a KeyError on the
    string "icon", which is an unhelpful place to discover the difference.
    """
    if isinstance(raw, list):
        return raw[0] if raw else {}
    return raw if isinstance(raw, dict) else {}


class VbbClient(DepartureSource):
    """Live VBB departures.

    Same failure contract as the rest of the project: errors return an empty
    list and set `last_error`, never raise. The caller distinguishes "no data"
    from "nothing is late" — a commute alerter that goes quiet on upstream
    failure is lying, and it lies hardest during disruption when that API is
    also under load.
    """

    def __init__(
        self,
        access_id: str | None = None,
        base_url: str = VBB_BASE,
        timeout: float = 12.0,
        max_retries: int = 2,
        min_interval: float = 1.2,
        transport=None,
        rt_mode: str | None = None,
    ):
        self.access_id = access_id or os.environ.get("VBB_ACCESS_ID", "").strip()
        self.base_url = base_url.rstrip("/")
        # HAFAS deployments disagree about this parameter. The reference
        # documents FULL; the VBB test system rejects it outright and accepts
        # only OFF or SERVER_DEFAULT. SERVER_DEFAULT is the portable choice --
        # it means "realtime as this operator configures it", which is what we
        # want anywhere. OFF would suppress realtime entirely and make every
        # train look punctual, which is the one answer worse than no answer.
        self.rt_mode = rt_mode or os.environ.get("VBB_RT_MODE", "SERVER_DEFAULT").strip()
        self.timeout = timeout
        self.max_retries = max_retries
        # Injection seam for tests. The retry, quota and auth paths are the ones
        # that only matter when something is already going wrong, so they are
        # exactly the ones that need exercising without a live key.
        self.transport = transport
        self.limiter = RateLimiter(min_interval)
        self.last_error: str | None = None
        if not self.access_id:
            self.last_error = "VBB_ACCESS_ID is not set"

    def _redact(self, text: str) -> str:
        if self.access_id and self.access_id in text:
            text = text.replace(self.access_id, "<accessId>")
        return text

    async def departures(self, stop_id: str, minutes: int = 60) -> list[Departure]:
        import httpx

        if not self.access_id:
            self.last_error = "VBB_ACCESS_ID is not set"
            return []

        params = {
            "accessId": self.access_id,
            "id": stop_id,
            "format": "json",
            "duration": min(max(int(minutes), 1), 1439),
            "passlist": 0,          # we only need this stop; passlist multiplies payload size
        }
        if self.rt_mode and self.rt_mode.upper() != "NONE":
            params["rtMode"] = self.rt_mode
        url = f"{self.base_url}/departureBoard"

        for attempt in range(self.max_retries + 1):
            try:
                await self.limiter.acquire()
                async with httpx.AsyncClient(
                    timeout=self.timeout, transport=self.transport
                ) as client:
                    resp = await client.get(url, params=params)

                # 429 is a quota signal, not a transient blip. Backing off hard
                # is the only polite response and the only one that keeps the
                # key usable for the rest of the day.
                if resp.status_code == 429 and attempt < self.max_retries:
                    await asyncio.sleep(random.uniform(2.0, 6.0) * (attempt + 1))
                    continue
                if resp.status_code in (401, 403):
                    self.last_error = f"auth rejected ({resp.status_code}) — check VBB_ACCESS_ID"
                    return []
                resp.raise_for_status()

                payload = resp.json()
                # HAFAS reports application errors inside a 200 response.
                if isinstance(payload, dict) and payload.get("errorCode"):
                    code = payload.get("errorCode")
                    text = payload.get("errorText", "")

                    # A deployment that rejects rtMode is telling us about
                    # itself, not about this request. Dropping the parameter and
                    # retrying once costs one call and keeps the service running
                    # on a host whose parameter set we guessed wrong -- which is
                    # better than failing every poll until someone reads a log.
                    if (code == "API_PARAM" and "rtMode" in text
                            and "rtMode" in params and attempt < self.max_retries):
                        params.pop("rtMode")
                        self.rt_mode = None
                        continue

                    self.last_error = self._redact(f"{code}: {text}")
                    return []

                self.last_error = None
                return self.parse(payload)

            except Exception as exc:  # noqa: BLE001
                self.last_error = self._redact(f"{type(exc).__name__}: {exc}")
                if attempt >= self.max_retries:
                    return []
                await asyncio.sleep(random.uniform(0.4, 1.8) * (attempt + 1))
        return []

    @staticmethod
    def parse(payload: dict) -> list[Departure]:
        """Parse a HAFAS ReST departureBoard response.

        The delay is computed, not read: HAFAS gives a scheduled time and a
        realtime time and leaves the subtraction to you. Which means the one
        thing that must be right here is distinguishing *no realtime data* from
        *a realtime prediction of zero delay*. They look identical downstream —
        both are `delay_seconds == 0` — and conflating them makes an unmonitored
        line look like the most punctual on the network, then feeds that fiction
        into its own baseline. `has_realtime` keeps them apart.
        """
        rows = payload.get("Departure") if isinstance(payload, dict) else payload
        if isinstance(rows, dict):     # a single departure is not wrapped in a list
            rows = [rows]
        out: list[Departure] = []

        for r in rows or []:
            try:
                date_s, time_s = r.get("date"), r.get("time")
                if not date_s or not time_s:
                    continue
                scheduled = _parse_hafas_time(date_s, time_s)

                rt_time = r.get("rtTime")
                has_rt = bool(rt_time)
                delay_seconds = 0
                if has_rt:
                    actual = _parse_hafas_time(r.get("rtDate") or date_s, rt_time)
                    delta = (actual - scheduled).total_seconds()
                    # rtDate is occasionally absent on a service that crosses
                    # midnight, which presents a 3-minute delay as -1437 minutes.
                    # Anything beyond half a day is a date problem, not a delay.
                    if delta < -43200:
                        delta += 86400
                    elif delta > 43200:
                        delta -= 86400
                    delay_seconds = int(delta)

                product = _as_product(r.get("Product"))
                name = (product.get("line") or product.get("name")
                        or r.get("name") or "?").strip()
                cat = (product.get("catOut") or product.get("catOutS") or "").strip()

                ref = (r.get("JourneyDetailRef") or {}).get("ref") or ""
                cancelled = bool(r.get("cancelled")) or r.get("JourneyStatus") == "C"

                out.append(Departure(
                    # The ref embeds the service and its operating date, so it
                    # is stable across polls of the same train — which is what
                    # the observation uniqueness constraint depends on.
                    trip_id=ref or f"{name}-{scheduled.isoformat()}",
                    line=name,
                    mode=_mode_from(cat, name),
                    direction=r.get("direction") or r.get("directionFlag") or "",
                    stop_id=str(r.get("stopExtId") or r.get("stopid") or ""),
                    stop_name=r.get("stop") or "",
                    scheduled=scheduled,
                    delay_seconds=delay_seconds,
                    cancelled=cancelled,
                    platform=r.get("rtTrack") or r.get("track"),
                    has_realtime=has_rt,
                ))
            except (ValueError, TypeError, AttributeError, KeyError):
                # One malformed row must not cost the other fifty-nine.
                continue
        return out


async def find_stops(query: str, access_id: str | None = None,
                     base_url: str = VBB_BASE, limit: int = 8) -> list[dict]:
    """Resolve a stop name to VBB stop IDs via `location.name`.

    Stop IDs are not guessable and differ between transit APIs, so a hardcoded
    one from another system silently watches the wrong platform rather than
    failing.
    """
    import httpx

    access_id = access_id or os.environ.get("VBB_ACCESS_ID", "").strip()
    if not access_id:
        raise RuntimeError("VBB_ACCESS_ID is not set")

    params = {"accessId": access_id, "input": query, "format": "json",
              "type": "S", "maxNo": limit}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(f"{base_url.rstrip('/')}/location.name", params=params)
        resp.raise_for_status()
        payload = resp.json()

    found = payload.get("stopLocationOrCoordLocation") or []
    out = []
    for entry in found:
        loc = entry.get("StopLocation") or entry.get("CoordLocation") or {}
        if not loc.get("extId"):
            continue
        out.append({"id": loc.get("extId"), "name": loc.get("name", ""),
                    "lat": loc.get("lat"), "lon": loc.get("lon")})
    return out
