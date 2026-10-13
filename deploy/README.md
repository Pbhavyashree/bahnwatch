# Deploying to an Oracle Cloud always-free instance

Takes about twenty minutes. Assumes Ubuntu; the Oracle Linux differences are
noted at each step that has one.

The result: bahnwatch at `https://bahnwatch.duckdns.org`, started on boot,
restarted on crash, with its observation history surviving deploys.

---

## 0. Why HTTPS is not optional here

The page asks for browser Notification permission, and the Notification API is
only available in a **secure context**. `localhost` is exempt — which is exactly
why it works on your laptop and would silently fail over plain HTTP on a real
hostname. The prompt simply never appears and the button does nothing.

Caddy is in these steps because it gets a certificate without being asked
twice, not because a reverse proxy is otherwise interesting.

## 1. A DuckDNS subdomain

In your DuckDNS account, add `bahnwatch` pointing at the same public IP as
`energy-hub`. One A record, nothing else.

Confirm it resolves before going further — Let's Encrypt will fail on a name
that does not:

```bash
dig +short bahnwatch.duckdns.org
```

## 2. Open the ports — in both places

This is the Oracle Cloud trap. There are two firewalls and closing either one
produces the same symptom: a connection that hangs rather than refuses.

**a. The VCN security list**, in the web console:
Networking → Virtual Cloud Networks → your VCN → Security Lists → Default →
Add Ingress Rules. Source `0.0.0.0/0`, TCP, destination ports **80** and **443**.

**b. The host firewall.** Oracle's Ubuntu images ship with iptables rules that
drop almost everything, and they persist across reboots:

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

On Oracle Linux it is firewalld instead:

```bash
sudo firewall-cmd --permanent --add-service=http --add-service=https
sudo firewall-cmd --reload
```

## 3. User, code, virtualenv

A dedicated service account with no login shell. The app reads one API and
writes one SQLite file; it has no business being able to log in.

```bash
sudo useradd --system --home /opt/bahnwatch --shell /usr/sbin/nologin bahnwatch
sudo mkdir -p /opt/bahnwatch
sudo chown bahnwatch:bahnwatch /opt/bahnwatch

sudo -u bahnwatch git clone https://github.com/Pbhavyashree/bahnwatch.git /opt/bahnwatch
cd /opt/bahnwatch
sudo -u bahnwatch python3 -m venv .venv
sudo -u bahnwatch .venv/bin/pip install -e .
```

`pip install -e .` without `[dev]`: the test tooling has no place on the server.

## 4. The key

```bash
sudo install -D -m 600 -o root -g root \
     /opt/bahnwatch/deploy/bahnwatch.env.example /etc/bahnwatch/bahnwatch.env
sudo nano /etc/bahnwatch/bahnwatch.env      # paste VBB_ACCESS_ID
```

Root-owned and `0600`. systemd reads it as root before dropping to the service
user, so the service never needs read access to the file itself.

## 5. The service

```bash
sudo cp /opt/bahnwatch/deploy/bahnwatch.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bahnwatch
sudo systemctl status bahnwatch --no-pager
```

It binds to `127.0.0.1:8000` deliberately — nothing reaches uvicorn from
outside the box. Check it before putting a proxy in front:

```bash
curl -s localhost:8000/api/state | head -c 300
```

You want `"source": "vbb"` and `"feed_error": null`. A `feed_error` here means
the key or the base URL did not survive the copy; run
`sudo -u bahnwatch /opt/bahnwatch/.venv/bin/python /opt/bahnwatch/scripts/probe_vbb.py 900142001`
with the env loaded and read what VBB says.

## 6. Caddy

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

Append the block from `Caddyfile.example` to `/etc/caddy/Caddyfile` — keep
whatever energy-hub already has in there — then:

```bash
sudo systemctl reload caddy
```

Open `https://bahnwatch.duckdns.org`. The padlock is what makes the
notification button work.

## 7. Updating later

```bash
cd /opt/bahnwatch
sudo -u bahnwatch git pull
sudo -u bahnwatch .venv/bin/pip install -e .
sudo systemctl restart bahnwatch
```

The database is in `/var/lib/bahnwatch`, not in the repo, so a deploy never
takes the observation history with it. That history is the thing you cannot
rebuild: baselines need roughly 30 observations per line, hour and day type,
which is weeks of evenings.

---

## Operating it

```bash
sudo journalctl -u bahnwatch -f              # live logs
sudo systemctl restart bahnwatch             # after a config change
curl -s localhost:8000/api/state | jq .feed_error
```

**Request budget.** The poller runs at `BAHNWATCH_POLL_SECONDS` within
`BAHNWATCH_ACTIVE_WINDOW_MIN` of a departure and at
`BAHNWATCH_IDLE_POLL_SECONDS` the rest of the time. With the defaults and two
stops that is roughly 500 requests a day rather than 2,900. If VBB tells you a
quota, set these from it rather than guessing.

**This runs against VBB's test system.** It carries no availability guarantee,
and VBB grants production access separately once development is finished and
the terms are accepted. Fine for personal use; not something to advertise as a
public service until that conversation has happened.

**If the feed dies.** The client returns an empty list rather than raising, the
page shows the error in orange, and the service stays up. That is deliberate:
an alerter that crashes when the transit API hiccups is useless at precisely the
moment it matters, during disruption, when that API is also under load.
