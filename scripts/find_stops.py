"""Find VBB stop IDs.

    export VBB_ACCESS_ID=...
    python scripts/find_stops.py Blankenburg Friedrichstr "Potsdam Hbf"

Stop IDs are not guessable and are not interchangeable between transit APIs, so
one copied from memory or from another system does not fail — it silently
watches the wrong platform, which is the only kind of bug this project cannot
afford.

Falls back to the community transport.rest mirror if no VBB key is set. That
mirror has been unreliable; the VBB path is the one to trust.
"""

from __future__ import annotations

import asyncio
import os
import sys

import httpx

from bahnwatch.vbb import VBB_BASE, find_stops

FALLBACK_BASE = "https://v6.bvg.transport.rest"


async def lookup_fallback(query: str) -> list[dict]:
    params = {"query": query, "results": 6, "stops": "true",
              "addresses": "false", "poi": "false"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(f"{FALLBACK_BASE}/locations", params=params)
        resp.raise_for_status()
    return [{"id": r.get("id"), "name": r.get("name", "")}
            for r in resp.json() if r.get("type") in ("stop", "station")]


async def main(queries: list[str]) -> None:
    keyed = bool(os.environ.get("VBB_ACCESS_ID", "").strip())
    print(f"source: {'VBB ' + VBB_BASE if keyed else 'transport.rest (no VBB key set)'}")

    for q in queries:
        print(f"\n{'=' * 62}\n  {q}\n{'=' * 62}")
        try:
            rows = await (find_stops(q) if keyed else lookup_fallback(q))
            if not rows:
                print("  no stops matched")
            for row in rows:
                print(f"  {row.get('id', '?')!s:<14} {row.get('name', '?')}")
        except Exception as exc:  # noqa: BLE001
            print(f"  lookup failed: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    args = sys.argv[1:] or ["Blankenburg", "Friedrichstr", "Potsdam Hbf"]
    asyncio.run(main(args))
