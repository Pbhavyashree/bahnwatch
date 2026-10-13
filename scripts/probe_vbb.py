"""Confirm the VBB key and endpoint before anything depends on them.

    export VBB_ACCESS_ID=...
    python scripts/probe_vbb.py 900142001          # everything at this stop
    python scripts/probe_vbb.py 900100001 S7       # one line, with its headways

Prints the raw response, then the same rows through the parser. Run this first.
VBB has moved the ReST path between releases, so the base URL is a guess until
something answers — and a parser written against a guessed payload fails in the
least useful way possible: quietly, at 21:50, with an empty board.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import sys

import httpx

from bahnwatch.vbb import VbbClient


class HafasRefused(Exception):
    """A HAFAS server answered us with an error.

    Which means this host is the right one, whatever the error says — a wrong
    host returns a 404 page, not a HAFAS error body. So any errorCode stops the
    search. The first version of this script only stopped on auth failures, so a
    parameter rejection on the correct host sent it on to the next candidate,
    which then failed auth, and it confidently named the wrong host as the one
    that worked.
    """

    def __init__(self, base: str, code: str, detail: str):
        super().__init__(detail)
        self.base, self.code, self.detail = base, code, detail


# The test system first: that is what VBB hands out on registration. The
# production host answers too, with API_AUTH, which is why the auth path below
# reports the base URL it reached rather than silently moving on.
CANDIDATES = [
    os.environ.get("VBB_API_BASE", "").rstrip("/"),
    "https://vbb.demo.hafas.cloud/api/fahrinfo/latest",
    "https://fahrinfo.vbb.de/restproxy/latest",
    "https://demo.hafas.de/openapi/vbb-proxy",
]


async def try_base(base: str, access_id: str, stop_id: str) -> dict | None:
    params = {"accessId": access_id, "id": stop_id, "format": "json",
              "duration": 60, "rtMode": os.environ.get("VBB_RT_MODE", "SERVER_DEFAULT")}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{base}/departureBoard", params=params)
    except Exception as exc:  # noqa: BLE001
        print(f"  {base}\n    unreachable — {type(exc).__name__}: {exc}")
        return None

    print(f"  {base}\n    HTTP {resp.status_code}")

    body = None
    try:
        body = resp.json()
    except ValueError:
        pass

    # A HAFAS-shaped error body is proof the URL is right, whatever the status
    # code says. Treating a 401 as "this base URL is wrong" sends you hunting
    # for an endpoint you already found, when the only problem is the key --
    # which is the diagnosis the response has spelled out for you.
    if isinstance(body, dict) and (body.get("errorCode") or body.get("serverVersion")):
        code = body.get("errorCode")
        if code:
            print(f"    VBB answered: {code} — {body.get('errorText', '')}")
            raise HafasRefused(base, code, body.get("errorText", ""))
        return body

    if resp.status_code != 200:
        print(f"    {resp.text[:200]}")
        return None
    if body is None:
        print(f"    not JSON — first bytes: {resp.text[:160]!r}")
        return None
    return body


async def main(stop_id: str, line_filter: str = "") -> int:
    access_id = os.environ.get("VBB_ACCESS_ID", "").strip()
    if not access_id:
        print("VBB_ACCESS_ID is not set.\n  export VBB_ACCESS_ID=your-key")
        return 2

    if access_id in ("your-key-here", "your-key", "paste-the-real-key"):
        print(f"VBB_ACCESS_ID is still the placeholder {access_id!r}.\n"
              "  export VBB_ACCESS_ID='<the key from your VBB registration mail>'")
        return 2

    print(f"stop {stop_id}, key ending {access_id[-4:]}\n")
    payload = None
    for base in [b for b in dict.fromkeys(CANDIDATES) if b]:
        try:
            payload = await try_base(base, access_id, stop_id)
        except HafasRefused as refused:
            print(f"\nEndpoint confirmed: {refused.base}")
            if refused.code == "API_AUTH":
                print("The key was refused on this host. VBB issues keys against "
                      "the test system; a test key on the production host fails "
                      "exactly like this.\n"
                      "  VBB_API_BASE=https://vbb.demo.hafas.cloud/api/fahrinfo/latest")
            elif refused.code == "API_PARAM":
                print(f"The key was accepted — it got as far as parameter "
                      f"validation. This host rejects a parameter we sent:\n"
                      f"  {refused.detail}\n"
                      "Set VBB_RT_MODE to one of the values it names, or report it.")
            else:
                print(f"{refused.code}: {refused.detail}")
            print(f"\n  VBB_API_BASE={refused.base}")
            return 1
        if payload is not None:
            working = base
            break
    else:
        print("\nNo candidate answered. Check the base URL in your VBB "
              "registration mail and set VBB_API_BASE to it.")
        return 1

    rows = payload.get("Departure") or []
    if isinstance(rows, dict):
        rows = [rows]
    parsed = VbbClient.parse(payload)

    if not line_filter:
        print(f"\n--- raw: {len(rows)} departures, first one ---")
        print(json.dumps(rows[0] if rows else payload, indent=2,
                         ensure_ascii=False)[:2200])

    # A big interchange returns a hundred departures across twenty lines, and an
    # unfiltered head of that list answers no question anyone actually has. The
    # question is always "when is MY line, going MY way".
    shown = [d for d in parsed
             if not line_filter or d.line.lower() == line_filter.lower()]
    limit = 30 if line_filter else 12

    label = f" matching {line_filter}" if line_filter else ""
    print(f"\n--- parsed: {len(parsed)} departures, {len(shown)}{label} ---")
    live = 0
    for d in shown[:limit]:
        live += d.has_realtime
        flag = "" if d.has_realtime else "  (no realtime)"
        cancel = "  CANCELLED" if d.cancelled else ""
        print(f"  {d.scheduled:%H:%M}  {d.line:<6} {d.mode.value:<9} "
              f"{d.delay_minutes:+5.1f}m  -> {d.direction[:34]}{flag}{cancel}")

    if line_filter and shown:
        # Headway per destination, because the terminus rotates on many lines
        # and the per-direction gap is what the cost model actually needs.
        by_dest: dict[str, list] = {}
        for d in shown:
            by_dest.setdefault(d.direction, []).append(d.scheduled)
        print("\n  headway by destination:")
        for dest, times in sorted(by_dest.items(), key=lambda kv: -len(kv[1])):
            times.sort()
            gaps = [(b - a).total_seconds() / 60.0 for a, b in itertools.pairwise(times)]
            gap = f"every ~{sum(gaps) / len(gaps):.0f} min" if gaps else "one departure"
            print(f"    {dest[:38]:<38} {len(times):>2}  {gap}")

    if shown:
        print(f"\n{live}/{len(shown[:limit])} shown rows carry realtime data.")
    if len(parsed) != len(rows):
        print(f"WARNING: {len(rows) - len(parsed)} row(s) failed to parse.")
    print(f"\nWorking base URL:\n  export VBB_API_BASE={working}")
    return 0


if __name__ == "__main__":
    stop = sys.argv[1] if len(sys.argv) > 1 else "900142001"
    line = sys.argv[2] if len(sys.argv) > 2 else ""
    raise SystemExit(asyncio.run(main(stop, line)))
