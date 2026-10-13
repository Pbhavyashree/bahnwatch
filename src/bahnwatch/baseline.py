"""Historical baselines: what 'normal' looks like for a line at a given time.

This is what the BVG app cannot tell you. It shows the current delay. It does
not tell you whether that delay is routine for a Tuesday at 18:00 or genuinely
unusual -- and that distinction is the difference between information and noise.

Baselines are cut by (line, hour, day type) because delay distributions differ
sharply along all three axes. Pooling them produces an average that describes
no actual situation: the 03:00 U8 and the 08:00 U8 are different systems.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import numpy as np

from bahnwatch.models import BERLIN, Baseline, Departure


def day_type(when: datetime) -> str:
    """Weekday / Saturday / Sunday.

    Three buckets rather than seven. Monday through Friday delay distributions
    are close enough that splitting them wastes data -- with limited history,
    five thin baselines are worse than one solid one. Saturday and Sunday differ
    from weekdays and from each other (Sunday service is sparser), so they stay
    separate.
    """
    dow = when.astimezone(BERLIN).weekday()
    if dow == 5:
        return "saturday"
    if dow == 6:
        return "sunday"
    return "weekday"


class BaselineStore:
    """Observations and the baselines derived from them."""

    def __init__(self, path: str | Path = "bahnwatch.db"):
        self.path = str(path)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS observation (
                id            INTEGER PRIMARY KEY,
                trip_id       TEXT NOT NULL,
                line          TEXT NOT NULL,
                stop_id       TEXT NOT NULL,
                scheduled     TEXT NOT NULL,
                delay_seconds INTEGER NOT NULL,
                cancelled     INTEGER NOT NULL DEFAULT 0,
                hour          INTEGER NOT NULL,
                day_type      TEXT NOT NULL,
                observed_at   TEXT NOT NULL,
                -- One row per trip per stop. The poller sees the same departure
                -- repeatedly as it approaches; without this, a train sitting in
                -- the feed for 40 minutes contributes 40 observations and
                -- silently dominates the baseline it is supposed to be measured
                -- against.
                UNIQUE(trip_id, stop_id, scheduled)
            );
            CREATE INDEX IF NOT EXISTS idx_obs_line_hour
                ON observation(line, hour, day_type);
            CREATE INDEX IF NOT EXISTS idx_obs_scheduled
                ON observation(scheduled);

            CREATE TABLE IF NOT EXISTS alert_log (
                id          INTEGER PRIMARY KEY,
                commute_id  TEXT NOT NULL,
                kind        TEXT NOT NULL,
                shift_min   REAL NOT NULL,
                issued_at   TEXT NOT NULL,
                body        TEXT
            );
        """)
        self._conn.commit()

    # -- writing ------------------------------------------------------------
    def record(self, departures: list[Departure]) -> int:
        """Store observations. Returns the number of genuinely new rows.

        `INSERT OR IGNORE` against the uniqueness constraint does the
        deduplication, so the poller can run every 30 seconds without
        distorting the statistics.

        Departures with no realtime prediction are dropped rather than recorded
        as zero. A scheduled-only row is not evidence that a train ran on time;
        it is evidence that nobody was watching. Storing it biases the baseline
        toward punctuality on exactly the lines the feed covers worst, and the
        p90 that comes out is then used to decide whether to warn someone.
        """
        rows = [
            (d.trip_id, d.line, d.stop_id, d.scheduled.isoformat(),
             d.delay_seconds, int(d.cancelled),
             d.scheduled.astimezone(BERLIN).hour, day_type(d.scheduled),
             d.observed_at.isoformat())
            for d in departures if d.trip_id and (d.has_realtime or d.cancelled)
        ]
        if not rows:
            return 0
        before = self._conn.total_changes
        self._conn.executemany(
            "INSERT OR IGNORE INTO observation "
            "(trip_id, line, stop_id, scheduled, delay_seconds, cancelled, "
            " hour, day_type, observed_at) VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )
        self._conn.commit()
        return self._conn.total_changes - before

    def log_alert(self, commute_id: str, kind: str, shift: float,
                  issued_at: str, body: str) -> None:
        self._conn.execute(
            "INSERT INTO alert_log (commute_id, kind, shift_min, issued_at, body) "
            "VALUES (?,?,?,?,?)",
            (commute_id, kind, shift, issued_at, body),
        )
        self._conn.commit()

    # -- reading ------------------------------------------------------------
    def baseline(self, line: str, hour: int, dtype: str) -> Baseline | None:
        rows = self._conn.execute(
            "SELECT delay_seconds, scheduled FROM observation "
            "WHERE line = ? AND hour = ? AND day_type = ? AND cancelled = 0",
            (line, hour, dtype),
        ).fetchall()
        if not rows:
            return None

        delays = np.array([r["delay_seconds"] for r in rows], dtype=float) / 60.0
        times = sorted(datetime.fromisoformat(r["scheduled"]) for r in rows)

        q25, q50, q75, q90 = np.percentile(delays, [25, 50, 75, 90])

        return Baseline(
            line=line, hour=hour, day_type=dtype,
            n_observations=len(delays),
            median_delay=float(q50),
            p75_delay=float(q75),
            p90_delay=float(q90),
            iqr=float(q75 - q25),
            headway_minutes=_estimate_headway(times),
        )

    def line_reliability(self, min_observations: int = 30) -> list[dict]:
        """Per-line summary, ranked by how often services run materially late.

        Ranked on the proportion late rather than mean delay, because a line
        with rare catastrophic delays and one with constant small ones are
        different problems, and the mean flattens them into the same number.
        """
        rows = self._conn.execute("""
            SELECT line,
                   COUNT(*)                                    AS n,
                   AVG(delay_seconds) / 60.0                   AS mean_delay,
                   SUM(CASE WHEN delay_seconds >= 300 THEN 1 ELSE 0 END) * 1.0
                     / COUNT(*)                                AS pct_5min_late,
                   SUM(cancelled) * 1.0 / COUNT(*)             AS pct_cancelled
            FROM observation
            GROUP BY line
            HAVING n >= ?
            ORDER BY pct_5min_late DESC
        """, (min_observations,)).fetchall()
        return [dict(r) for r in rows]

    def observation_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) c FROM observation").fetchone()["c"]

    def close(self) -> None:
        self._conn.close()


def _estimate_headway(scheduled_times: list[datetime]) -> float:
    """Typical gap between consecutive departures, in minutes.

    The **median** gap, not the mean. Service gaps are heavily right-skewed --
    the overnight break between the last train and the first is hours long, and
    a single such gap drags a mean headway from 5 minutes to 40. The median is
    unaffected by however many extreme gaps exist, which is exactly the property
    needed here.
    """
    if len(scheduled_times) < 2:
        return 10.0
    gaps = np.diff([t.timestamp() for t in sorted(scheduled_times)]) / 60.0
    gaps = gaps[(gaps > 0.5) & (gaps < 120)]
    if gaps.size == 0:
        return 10.0
    return float(np.median(gaps))
