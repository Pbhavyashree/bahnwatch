import asyncio, httpx

CANDIDATES = {
    "vbb-v6": "https://v6.vbb.transport.rest/locations?query=Blankenburg&results=2",
    "vbb-v5": "https://v5.vbb.transport.rest/locations?query=Blankenburg&results=2",
    "bvg-v6": "https://v6.bvg.transport.rest/locations?query=Alexanderplatz&results=2",
    "bvg-v5": "https://v5.bvg.transport.rest/locations?query=Alexanderplatz&results=2",
    "db-v6":  "https://v6.db.transport.rest/locations?query=Berlin%20Friedrichstr&results=2",
    "db-v5":  "https://v5.db.transport.rest/locations?query=Berlin%20Friedrichstr&results=2",
    "local":  "http://localhost:3000/locations?query=Blankenburg&results=2",
}

async def probe(name, url):
    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as c:
            r = await c.get(url)
        if r.status_code != 200:
            return name, False, f"HTTP {r.status_code}"
        d = r.json()
        rows = d if isinstance(d, list) else d.get("locations", [])
        if not rows:
            return name, False, "empty response"
        return name, True, f"{rows[0].get('name','?')} (id {rows[0].get('id','?')})"
    except Exception as e:
        return name, False, type(e).__name__

async def main():
    print("Probing...\n")
    for name, ok, detail in await asyncio.gather(*(probe(n,u) for n,u in CANDIDATES.items())):
        print(f"  [{'OK  ' if ok else 'dead'}] {name:<8} {detail}")

asyncio.run(main())
