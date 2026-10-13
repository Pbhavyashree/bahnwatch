"""HTTP + WebSocket layer.

The browser gets a WebSocket push rather than polling. With a 20-second refresh,
polling from ten open tabs is ten times the upstream requests for identical
data; one poller broadcasting to N sockets is one. It also means an alert
reaches the page the instant it is decided rather than up to a full interval
later, which matters when the whole point is telling someone to leave now.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path

from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from bahnwatch.baseline import BaselineStore
from bahnwatch.bvg import BvgClient, SimulatedSource
from bahnwatch.commute import AlertState, decide_alert_for_journey
from bahnwatch.journey import Journey, Leg
from bahnwatch.models import now_berlin
from bahnwatch.planner import plan_journey

WEB_DIR = Path(__file__).resolve().parents[2] / "web"

# simulator | vbb | bvg.  BAHNWATCH_SIMULATE=0 is still honoured so existing
# deployments and shell aliases do not break on upgrade.
SOURCE_NAME = os.environ.get("BAHNWATCH_SOURCE", "").strip().lower()
if not SOURCE_NAME:
    SOURCE_NAME = "simulator" if os.environ.get("BAHNWATCH_SIMULATE", "1") == "1" else "bvg"
USE_SIMULATOR = SOURCE_NAME == "simulator"

# The simulator costs nothing per poll; a keyed API does. Twenty seconds against
# two stops is 8,640 requests a day, which burns a VBB quota well before the
# evening commute this exists to watch. Live mode defaults to a minute, which
# is still far finer than the resolution of the thing being measured — a train
# does not acquire a new delay every twenty seconds.
_DEFAULT_POLL = "20" if USE_SIMULATOR else "60"
POLL_SECONDS = int(os.environ.get("BAHNWATCH_POLL_SECONDS", _DEFAULT_POLL))

# Real journey: Blankenburg -> Friedrichstrasse (S2), change to S7 -> Potsdam.
#
# The connection at Friedrichstrasse is the whole point of this configuration.
# The S2 is scheduled to arrive around 22:19 and the S7 leaves at 22:24, so
# there are roughly five minutes to change platforms. At that hour the S7 to
# Potsdam runs every 20 minutes, which means a six-minute delay on the S2 costs
# twenty minutes, and a four-minute delay costs nothing. That discontinuity is
# invisible to any single-leg calculation.
#
# Stop IDs must be verified with `python scripts/find_stops.py` -- they are not
# guessable, and a wrong one silently watches the wrong platform. The default
# below was wrong for most of this project's life: S Blankenburg is 900142001,
# and the 900130002 sitting here instead resolved to a different stop entirely.
# Nothing failed. The board filled with departures and none of them were mine.
JOURNEYS = [
    Journey(
        id="potsdam-evening",
        label="Evening — Blankenburg to Potsdam",
        walk_minutes=7.0,          # Kerkowstrasse to the S Blankenburg platform
        buffer_minutes=4.0,
        arrive_by_hour=23,
        arrive_by_minute=2,
        notes="S2 21:50 -> Friedrichstr., change to S7 22:24 -> Potsdam Hbf 23:02",
        legs=(
            Leg(
                # Southbound S2s out of Blankenburg terminate at Lichtenrade,
                # Marienfelde and Blankenfelde on a rotation, and all three pass
                # Friedrichstrasse. Listing only one of them discards two thirds
                # of the service: the real southbound headway here is 10 minutes,
                # not 30. Northbound (Bernau, Buch) is what must be excluded.
                line="S2",
                direction="Blankenfelde|Lichtenrade|Marienfelde",
                origin_stop_id=os.environ.get("STOP_BLANKENBURG", "900142001"),
                origin_stop_name="S Blankenburg",
                dest_stop_name="S+U Friedrichstr.",
                ride_minutes=22.0,
                transfer_minutes=3.0,   # Friedrichstr. S-Bahn platforms are same-level
                # Measured from the live board, not assumed: southbound S2s run
                # :30 :40 :50 :00. The 20 that used to be here priced every
                # broken connection at double, since the cost of missing one
                # *is* the headway.
                nominal_headway=10.0,
            ),
            Leg(
                # Here the terminus genuinely is the direction: westbound S7s
                # run every 10 minutes but the Westkreuz and Wannsee services
                # stop short, and only the Potsdam ones reach the destination.
                line="S7",
                direction="Potsdam Hbf",
                origin_stop_id=os.environ.get("STOP_FRIEDRICHSTR", "900100001"),
                origin_stop_name="S+U Friedrichstr.",
                dest_stop_name="Potsdam Hbf",
                ride_minutes=38.0,
                transfer_minutes=0.0,
                nominal_headway=20.0,
            ),
        ),
    ),
]


class Hub:
    """Connected browsers, and the last computed state."""

    def __init__(self) -> None:
        self.sockets: set[WebSocket] = set()
        self.snapshot: dict = {"status": "starting"}
        self._lock = asyncio.Lock()

    async def join(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self.sockets.add(ws)
        # Send current state immediately: a new tab should not sit blank until
        # the next poll tick.
        with contextlib.suppress(Exception):
            await ws.send_json(self.snapshot)

    async def leave(self, ws: WebSocket) -> None:
        async with self._lock:
            self.sockets.discard(ws)

    async def broadcast(self, payload: dict) -> None:
        self.snapshot = payload
        async with self._lock:
            targets = list(self.sockets)
        dead = []
        for ws in targets:
            try:
                await ws.send_json(payload)
            except Exception:  # noqa: BLE001 - a closed socket is not an error here
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self.sockets.discard(ws)


hub = Hub()
store = BaselineStore(os.environ.get("BAHNWATCH_DB", "bahnwatch.db"))
def _build_source():
    if SOURCE_NAME == "vbb":
        from bahnwatch.vbb import VbbClient
        return VbbClient()
    if SOURCE_NAME == "bvg":
        return BvgClient()
    return SimulatedSource(seed=9)


source = _build_source()
alert_states: dict[str, AlertState] = {j.id: AlertState() for j in JOURNEYS}
recent_alerts: list[dict] = []


async def poll_once() -> dict:
    """One cycle: fetch every stop on every journey, record, plan, decide."""
    now = now_berlin()

    stop_ids: set[str] = set()
    for j in JOURNEYS:
        stop_ids |= j.stop_ids

    by_stop: dict[str, list] = {}
    all_departures = []
    for sid in stop_ids:
        deps = await source.departures(sid, minutes=90)
        by_stop[sid] = deps
        all_departures.extend(deps)

    new_rows = store.record(all_departures)

    plans, fired = [], []
    for j in JOURNEYS:
        plan = plan_journey(j, by_stop, store, now)
        plans.append(plan.to_dict())

        alert = decide_alert_for_journey(plan, j, alert_states[j.id], now)
        if alert:
            store.log_alert(alert.commute_id, alert.kind.value, alert.shift_minutes,
                            alert.issued_at.isoformat(), alert.body)
            fired.append(alert.to_dict())

    if fired:
        recent_alerts.extend(fired)
        del recent_alerts[:-20]

    # Board shows the first leg's origin, which is the stop the traveller is
    # actually standing at when this matters.
    board_stop = JOURNEYS[0].first_leg.origin_stop_id if JOURNEYS else None
    board_src = by_stop.get(board_stop, []) if board_stop else []
    board = sorted((d for d in board_src if d.actual >= now), key=lambda d: d.actual)[:22]

    return {
        "status": "ok",
        "now": now.isoformat(),
        "source": SOURCE_NAME,
        # Surfaced rather than swallowed. An alerter that silently shows an
        # empty board during an upstream outage is indistinguishable from one
        # reporting that nothing is late, which is the failure that matters.
        "feed_error": getattr(source, "last_error", None),
        "live_rows": sum(1 for d in all_departures if d.has_realtime),
        "observations": store.observation_count(),
        "new_observations": new_rows,
        "board_stop_name": (JOURNEYS[0].first_leg.origin_stop_name if JOURNEYS else ""),
        "plans": plans,
        "alerts": fired,
        "recent_alerts": recent_alerts[-6:],
        "board": [
            {
                "line": d.line, "mode": d.mode.value, "direction": d.direction,
                "scheduled": d.scheduled.strftime("%H:%M"),
                "actual": d.actual.strftime("%H:%M"),
                "delay": round(d.delay_minutes, 1),
                "cancelled": d.cancelled,
                "late": d.is_late,
            }
            for d in board
        ],
        "reliability": store.line_reliability(min_observations=20)[:8],
    }


async def poller() -> None:
    while True:
        try:
            await hub.broadcast(await poll_once())
        except Exception as exc:  # noqa: BLE001
            await hub.broadcast({"status": "error", "detail": str(exc)})
        await asyncio.sleep(POLL_SECONDS)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(poller())
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


app = FastAPI(title="bahnwatch", lifespan=lifespan)


@app.get("/api/state")
async def state() -> JSONResponse:
    return JSONResponse(hub.snapshot)


@app.get("/api/reliability")
async def reliability() -> JSONResponse:
    return JSONResponse({"lines": store.line_reliability(min_observations=10)})


@app.post("/api/simulate/disruption")
async def simulate_disruption(line: str = "S2", minutes: float = 9.0) -> JSONResponse:
    """Inject a delay, so the alerting path is demonstrable on demand.

    Without this, showing the interesting behaviour means waiting for Berlin to
    misbehave while someone watches.
    """
    if not isinstance(source, SimulatedSource):
        return JSONResponse({"error": "live mode — cannot inject"}, status_code=400)
    source.set_disruption(line, minutes)
    await hub.broadcast(await poll_once())
    return JSONResponse({"ok": True, "line": line, "minutes": minutes})


@app.post("/api/simulate/clear")
async def simulate_clear() -> JSONResponse:
    if isinstance(source, SimulatedSource):
        source.set_disruption(None, 0.0)
        await hub.broadcast(await poll_once())
    return JSONResponse({"ok": True})


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await hub.join(ws)
    try:
        while True:
            # Keeps the connection open; the client sends nothing meaningful.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await hub.leave(ws)


@app.get("/favicon.ico")
async def favicon() -> Response:
    """The page carries an inline SVG icon, so there is no file to serve.

    Answering 204 rather than letting it 404 keeps the request log readable:
    a log full of benign 404s trains you to ignore 404s.
    """
    return Response(status_code=204)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
