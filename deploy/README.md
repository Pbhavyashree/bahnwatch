# Deploying to an Oracle Cloud always-free instance

Running at `https://bahnwatch.duckdns.org` on an Ampere instance, started on
boot, restarted on crash, with its observation history surviving deploys.

About twenty minutes. Ubuntu; the Oracle Linux differences are noted where they
exist.

---

## 0. Two things to establish before anything else

**HTTPS is load-bearing, not decoration.** The page asks for browser
Notification permission, and the Notification API is only available in a
**secure context**. `localhost` is exempt — which is exactly why it works on a
laptop and fails silently on a real hostname over plain HTTP. No prompt, no
error, the button simply does nothing.

**Find out what already owns ports 80 and 443 before installing anything.**

```bash
sudo ss -tlnp | grep -E ':80|:443'
```

If that is empty, follow **path A**. If something is already there — on this
box it was `docker-proxy`, because a containerised Caddy was already serving
another project — follow **path B**. Installing a second web server when one is
already bound to those ports fails with `address already in use`, and a host
`which caddy nginx` will not find a containerised one.

## 1. A DuckDNS subdomain

Add `bahnwatch` in your DuckDNS account, pointing at the instance's public IP.
Subdomains are not a wildcard — each is its own entry.

Confirm it resolves before going further, against a public resolver rather than
your router, which will have cached the NXDOMAIN from any earlier attempt:

```bash
nslookup bahnwatch.duckdns.org 1.1.1.1
```

Let's Encrypt cannot issue for a name that does not resolve.

## 2. Open the ports — in both places

Oracle has two firewalls and closing either produces the same symptom: a
connection that hangs rather than refuses.

**a. The VCN security list**, in the web console: Networking → Virtual Cloud
Networks → your VCN → Security Lists → Default → Add Ingress Rules. Source
`0.0.0.0/0`, TCP, destination ports **80** and **443**.

**b. The host firewall.** Oracle's Ubuntu images ship with iptables rules that
drop almost everything, and they persist across reboots:

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

Oracle Linux uses firewalld:

```bash
sudo firewall-cmd --permanent --add-service=http --add-service=https
sudo firewall-cmd --reload
```

## 3. User, code, virtualenv

A dedicated service account with no login shell. This process reads one HTTP API
and writes one SQLite file; it has no business being able to log in.

```bash
sudo apt update && sudo apt install -y git python3-venv python3-pip
sudo useradd --system --home /opt/bahnwatch --shell /usr/sbin/nologin bahnwatch
sudo mkdir -p /opt/bahnwatch && sudo chown bahnwatch:bahnwatch /opt/bahnwatch
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
curl -s localhost:8000/api/state | head -c 400
```

You want `"source": "vbb"` and `"feed_error": null`. A `feed_error` here means
the key or the base URL did not survive the copy — run the probe as the service
user and read what VBB says, rather than guessing:

```bash
sudo -u bahnwatch env $(sudo cat /etc/bahnwatch/bahnwatch.env | grep -v '^#' | xargs) \
     /opt/bahnwatch/.venv/bin/python /opt/bahnwatch/scripts/probe_vbb.py 900142001
```

---

## 6A. No existing web server: install Caddy

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

Replace `/etc/caddy/Caddyfile` with the first block from `Caddyfile.example` —
replace rather than append, because the shipped default contains a `:80 { … }`
block that claims every host on that port and makes a named site ambiguous.

```bash
sudo systemctl restart caddy
```

## 6B. A containerised reverse proxy already owns 80/443

This is the case on the energy-hub box. Do **not** install host Caddy; add a
site to the one that is running.

Find its Caddyfile on the host and the bridge gateway address:

```bash
sudo docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}'

CF=$(sudo docker inspect deploy-caddy-1 --format \
  '{{range .Mounts}}{{if eq .Destination "/etc/caddy/Caddyfile"}}{{.Source}}{{end}}{{end}}')
echo "$CF"

sudo docker network inspect $(sudo docker inspect -f \
  '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}' deploy-caddy-1) \
  -f '{{(index .IPAM.Config 0).Gateway}}'
```

**Rebind bahnwatch.** The unit binds `127.0.0.1:8000`, and inside the proxy
container that address means *the container itself*. A drop-in keeps the shipped
unit generic:

```bash
sudo mkdir -p /etc/systemd/system/bahnwatch.service.d
printf '[Service]\nExecStart=\nExecStart=/opt/bahnwatch/.venv/bin/uvicorn bahnwatch.api:app --host 0.0.0.0 --port 8000\n' \
  | sudo tee /etc/systemd/system/bahnwatch.service.d/override.conf
sudo systemctl daemon-reload && sudo systemctl restart bahnwatch
```

The empty `ExecStart=` is required — it clears the original before setting the
replacement. Binding `0.0.0.0` does not expose port 8000: the VCN security list
permits only 80 and 443, and the host firewall drops the rest.

**Add the site block** — the second block in `Caddyfile.example`, appended to
the Caddyfile that container mounts, with the gateway address from above.

**Then the rule that is easy to miss.** Traffic from the Docker bridge to the
host arrives on a different path than inbound 80/443 and is rejected by the same
ruleset, which surfaces as `dial tcp 172.18.0.1:8000: connect: no route to host`
in the proxy log and a 502 in the browser — after the certificate has been
issued, so everything looks like it worked right up to the last hop:

```bash
sudo iptables -I INPUT 6 -s 172.18.0.0/16 -p tcp --dport 8000 -j ACCEPT
sudo netfilter-persistent save
```

Scoped to the bridge subnet, so port 8000 stays unreachable from the internet.

**Reload in place**, so the other sites keep serving:

```bash
sudo docker exec deploy-caddy-1 caddy reload --config /etc/caddy/Caddyfile
sudo docker logs --tail 20 deploy-caddy-1
```

The log shows the ACME challenge and `certificate obtained successfully`.

## 7. Verify it survives a reboot

Running is not the same as deployed.

```bash
sudo reboot
# wait, reconnect
systemctl is-active bahnwatch
sudo docker ps --format '{{.Names}}\t{{.Status}}'
curl -s -o /dev/null -w '%{http_code}\n' https://bahnwatch.duckdns.org
```

`active`, the containers `Up`, and `200`.

## 8. Updating

```bash
sudo -u bahnwatch git -C /opt/bahnwatch pull
sudo -u bahnwatch /opt/bahnwatch/.venv/bin/pip install -e /opt/bahnwatch
sudo systemctl restart bahnwatch
```

The database is in `/var/lib/bahnwatch`, created and owned by `StateDirectory=`,
not in the repo — so a deploy never takes the observation history with it. That
history is the thing you cannot rebuild: baselines need roughly 30 observations
per line, hour and day type, which is weeks of evenings.

---

## Operating it

```bash
sudo journalctl -u bahnwatch -f
curl -s localhost:8000/api/state | jq .feed_error
```

**Request budget.** The poller runs at `BAHNWATCH_POLL_SECONDS` within
`BAHNWATCH_ACTIVE_WINDOW_MIN` of a departure and at
`BAHNWATCH_IDLE_POLL_SECONDS` otherwise — roughly 500 requests a day for two
stops rather than 2,900. If VBB states a quota, set these from it.

**This runs against VBB's test system**, which carries no availability
guarantee. Production access is granted separately, once development is
finished and the terms of use are accepted.

**Expect scanners.** Certificate Transparency logs are public, so a new
hostname is probed for `/wp-json/` and `/xmlrpc.php` within seconds of the
certificate being issued. There is no WordPress to find.

**If the feed dies**, the client returns an empty list rather than raising, the
page shows the error, and the service stays up. An alerter that crashes when the
transit API hiccups is useless at precisely the moment it matters — during
disruption, when that API is also under load.
