# TLS for internal services — the actual plan (UCG-based, 2026-07-15)

> Replaces the previous plan (`TLS_DEPLOYMENT_PLAN_2026-07-15.md`). That plan assumed AdGuard was the LAN DNS and the rewrites would go there. **You use the UCG for DNS, not AdGuard.** New plan, same goal: every internal service reachable as `https://<name>.pancakefarts.xyz` from anywhere with a valid public-CA cert, no browser warnings.

## Critical: the dual-controller gotcha (verified 2026-07-15)

You have **two Unifi controllers**:
- **UCG-Fiber at 192.168.1.1** (the main gateway) — `deviceState: "setup"` (half-state, see unifi-controller-admin skill). Its network-app API is **gated** — all `/proxy/network/api/s/default/...` calls return 401 with the UCG key, even though the UCG is the actual gateway. The UCG key (`K2UYvy...`) only works on `/api/system` (Unifi OS shell), not on the network-app paths.
- **Unifi OS Server at 192.168.1.89:11443** (the AP controller) — `deviceState: "setup"` too, but per the unifi-controller-admin skill the network-app endpoints work anyway when probed directly. **The UOS VM is the working surface for network config writes.** Use the UOS key (`rFQfxM...`) against `https://192.168.1.89:11443/proxy/network/api/s/default/...`.

**Rule of thumb for any script in this plan:**
- **Read/write network config (networkconf, wlanconf, firewallgroup, portforward, setting/super_*)** → `https://192.168.1.89:11443` + UOS key
- **Read only — the gateway's view** (interfaces, ARP, nftables) → `https://192.168.1.1/api/system` + UCG key (just the system endpoint; everything else 401s)

Both keys are at `/home/hermeswebui/.hermes/memories/UNIFI_API_KEY.md` (symlink to profile dir).

---

## TL;DR

**Architecture (verified against the live UOS API):**

```
LAN client (phone, laptop, AppleTV)               Public client (phone on cellular)
        │                                                     │
        ▼                                                     ▼
   UCG (192.168.1.1)                            Cloudflare DNS
   - DHCP server (currently relaying to dead .254)         │
   - DNS server (currently upstream-only)                  ▼
   - Has `dhcpd_dns_server_1` field per-network   173.181.99.119 (UCG WAN)
     (this is the split-horizon slot)                       │
        │                                                    │
        ▼                                                    │
   AdGuard (192.168.1.3)                                  ...same path...
   - Currently NOT in the DNS chain
   - Could be the split-horizon DNS if we want
                                              OR ↓ ↓ ↓
   npmplus on Unraid:443 (192.168.1.104)
   wildcard *.pancakefarts.xyz cert (LE, DNS-01)
   proxy_host per service → backend:port
        │
        ▼
   Backend container (192.168.1.x:port) or
   backend on TrueNAS (.122:port)
```

**Same hostname, two DNS answers. The "answer" comes from one of two places:**

1. **UCG itself** — UCG has per-network `dhcpd_dns_server_1/2/3` fields (currently unset). We point the LAN at AdGuard (or a small dnsmasq) for split-horizon.
2. **AdGuard at 192.168.1.3** — already running, has `rewrites: []` (empty). If we route LAN DNS to it, we get filter + split-horizon for free.

**Decision needed:** which DNS resolver does the LAN use? UCG passes through to upstream (no split-horizon). AdGuard on .3 has the split-horizon slot (rewrites) and adds ad-blocking. **My recommendation: route LAN DNS through AdGuard** — it's the right tool for this and already running.

---

## The current state (verified 2026-07-15)

Read from UOS at 192.168.1.89:11443 with the UOS API key:

| Item | Value | Implication |
|---|---|---|
| UCG/UOS `deviceState` | `setup` | UOS is in half-state; the UCG's network-app API is gated but UOS API works (per the unifi-controller-admin skill) |
| Default LAN (`63b0d766652205010897d27e`) | `192.168.1.1/24`, `dhcpd_enabled: false`, `dhcp_relay_enabled: true`, `dhcp_relay_servers: ['192.168.1.254']` | **Stale relay target** (.254 is unreachable). LAN clients are getting DHCP from somewhere else (probably the UCG's own DHCP, despite the API saying it's off — or they're on static IPs) |
| Default LAN `dhcpd_dns_enabled` | `false` | The field the 2026-07-04 IoT DNS fix turned on, but on the Default network |
| Default LAN `dhcpd_dns_server_1` | unset | **This is the split-horizon slot — needs to be set to AdGuard (192.168.1.3)** |
| Default LAN `domain_name` | `localdomain` | Cosmetic; doesn't affect split-horizon |
| IoT VLAN 30 (`6a015216222f682c917104af`) | `192.168.30.1/24`, `dhcpd_enabled: false`, `dhcp_relay_enabled: true`, **`dhcpd_dns_enabled` field absent** | Same stale relay pattern; `dhcpd_dns_server_*` would need to be added |
| IPTV VLAN 20 (`694110528a4af646fcaccb34`) | VLAN-only, no IP subnet | mDNS only |
| 117 services on Unraid + TrueNAS | reachable by IP:port only | No hostnames → cert warnings on browsers that get a self-signed cert from the service |

**Two findings worth flagging now (not part of this plan to fix, but worth knowing):**
1. **DHCP relay target is dead** (192.168.1.254) — LAN clients are getting IPs from somewhere; needs investigation later
2. **`dhcpd_dns_enabled: false` on the Default LAN** — same class of bug as the 2026-07-04 IoT DNS issue, but on the LAN. Won't bite us if we set `dhcpd_dns_server_1: 192.168.1.3` directly (which overrides the boolean)

---

## The 4 decisions I need from you

Before I touch anything, these need answering:

1. **Where does LAN DNS go: UCG directly, or AdGuard at 192.168.1.3?**
   - **AdGuard (recommended):** set `dhcpd_dns_server_1: 192.168.1.3` on the Default and IoT networks. AdGuard's `rewrites` field becomes the split-horizon slot. You get ad-blocking as a side effect.
   - **UCG directly:** UCG has its own static-DNS-records feature, but it's not exposed via the API. Would require webUI work, no automation.

2. **Wildcard cert approach OK?** One `*.pancakefarts.xyz` LE cert (via Cloudflare DNS-01) covers all 12 existing + 25 new services. Saves 25 individual certs.

3. **What scope? Full sweep (~50 services) or a 3-service pilot first?**
   - Full sweep: same script for all of them, takes ~2 hours
   - Pilot: 3 services (jellyfin, homepage, adminer), validate end-to-end, then sweep

4. **Cloudflare API token: do you have one already, or do we need to create one?**
   - Needed: a token with `Zone:DNS:Edit` scope on `pancakefarts.xyz` only
   - Save it in 1Password; never on a server file

---

## The proposed hostname list (unchanged from before, but now concrete)

Same ~50 services as the previous plan. **Strike what you don't use, add what I missed.** Grouped by tier (who uses them):

### Tier 1 — wife/family daily use (cert warnings are visible pain)

| Service | Container | Port | Hostname | Backend |
|---|---|---|---|---|
| Jellyfin | `jellyfin` (Unraid) | 8096 | `jellyfin.pancakefarts.xyz` | 192.168.1.104 |
| Overseerr | `seerr` | 5055 | `seerr.pancakefarts.xyz` | 192.168.1.104 |
| Sonarr | `sonarr` | 8989 | `sonarr.pancakefarts.xyz` | 192.168.1.104 |
| Radarr | `radarr` | 7878 | `radarr.pancakefarts.xyz` | 192.168.1.104 |
| Lidarr | `binhex-lidarr` | 8686 | `lidarr.pancakefarts.xyz` | 192.168.1.104 |
| Readarr | `binhex-readarr` | 8787 | `readarr.pancakefarts.xyz` | 192.168.1.104 |
| Bazarr | `bazarr` | 6767 | `bazarr.pancakefarts.xyz` | 192.168.1.104 |
| Tautulli | `tautulli` | 8189 | `tautulli.pancakefarts.xyz` | 192.168.1.104 |
| Homebridge | `homebridge` | n/a (mDNS) | `homebridge.pancakefarts.xyz` | 192.168.1.104 |

### Tier 2 — your daily admin

| Service | Container | Port | Hostname | Backend |
|---|---|---|---|---|
| Homepage | `homepage` | 3030 | `homepage.pancakefarts.xyz` | 192.168.1.104 |
| Adminer | `adminer` | 4545 | `adminer.pancakefarts.xyz` | 192.168.1.104 |
| Apprise | `apprise` | 8010 | `apprise.pancakefarts.xyz` | 192.168.1.104 |
| Glances | `glances` | 61208 | `glances.pancakefarts.xyz` | 192.168.1.104 |
| Dockmon | `dockmon` | 8001 | `dockmon.pancakefarts.xyz` | 192.168.1.104 |
| Dockwatch | `dockwatch` | 9999 | `dockwatch.pancakefarts.xyz` | 192.168.1.104 |
| PeaNUT | `peanut` | 9500 | `peanut.pancakefarts.xyz` | 192.168.1.104 |
| Hermes WebUI | `hermes-webui` | 18787 | `hermes.pancakefarts.xyz` | 192.168.1.104 |

### Tier 3 — occasional admin tools

| Service | Container | Port | Hostname | Backend |
|---|---|---|---|---|
| Scrutiny | `scrutiny` | 5756 | `scrutiny.pancakefarts.xyz` | 192.168.1.104 |
| QDirStat | `qdirstat` | 7815 | `qdirstat.pancakefarts.xyz` | 192.168.1.104 |
| Scanopy | `scanopy-server-1` | 60072 | `scanopy.pancakefarts.xyz` | 192.168.1.104 |
| Profilarr | `profilarr` | 6868 | `profilarr.pancakefarts.xyz` | 192.168.1.104 |
| UnraidConfigGuardian | `unraidconfigguardian` | 7842 | `unraid-cfg.pancakefarts.xyz` | 192.168.1.104 |
| changedetection.io | `changedetection.io` | 5051 | `changedetection.pancakefarts.xyz` | 192.168.1.104 |
| Databasus | `databasus` | 4005 | `databasus.pancakefarts.xyz` | 192.168.1.104 |
| Calibre-Web | `calibre-web-automated` | 8083 | `calibre.pancakefarts.xyz` | 192.168.1.104 |
| Actual Budget | `actualserver` | 5006 | `actual.pancakefarts.xyz` | 192.168.1.104 |
| Bookshelf | `bookshelf` | 8587 | `bookshelf.pancakefarts.xyz` | 192.168.1.104 |
| Paperless-AI | `paperless-ai` | 3321 | `paperless.pancakefarts.xyz` | 192.168.1.104 |
| OpenBooks | `openbooks` | 8035 | `openbooks.pancakefarts.xyz` | 192.168.1.104 |
| LubeLogger | `lubelogger` | 8780 | `lubelogger.pancakefarts.xyz` | 192.168.1.104 |
| Shelfmark | `shelfmark` | 8084 | `shelfmark.pancakefarts.xyz` | 192.168.1.104 |
| SoulSync | `soulsync` | 8008 | `soulsync.pancakefarts.xyz` | 192.168.1.104 |
| Termix | `termix` | 30001 | `termix.pancakefarts.xyz` | 192.168.1.104 |
| Maintainerr | `maintainerr` | 6246 | `maintainerr.pancakefarts.xyz` | 192.168.1.104 |
| Hound | `hound-server` | 2323 | `hound.pancakefarts.xyz` | 192.168.1.104 |
| Houndarr | `houndarr` | 2635 | `houndarr.pancakefarts.xyz` | 192.168.1.104 |
| Mediamanager | `mediamanager-mediamanager-1` | 3060 | `mediamanager.pancakefarts.xyz` | 192.168.1.104 |
| Plex2Letterboxd | `plex2letterboxd-frontend` | 5670 | `p2l.pancakefarts.xyz` | 192.168.1.104 |
| YACReader | `yacreaderlibraryserver` | 8761 | `yacreader.pancakefarts.xyz` | 192.168.1.104 |
| EbookBuddy | `ebookbuddy` | 5110 | `ebookbuddy.pancakefarts.xyz` | 192.168.1.104 |
| Beets | `beets` | 8337 | `beets.pancakefarts.xyz` | 192.168.1.104 |
| Kapowarr | `kapowarr` | 5656 | `kapowarr.pancakefarts.xyz` | 192.168.1.104 |
| Explo | `explo` | 7288 | `explo.pancakefarts.xyz` | 192.168.1.104 |
| Cleanarr | `cleanarr` | 5915 | `cleanarr.pancakefarts.xyz` | 192.168.1.104 |
| LAZYLIBRARIAN | `lazylibrarian` | 5299 | `lazylibrarian.pancakefarts.xyz` | 192.168.1.104 |
| Mylar3 | `mylar3` | 8090 | `mylar3.pancakefarts.xyz` | 192.168.1.104 |
| PlexPosterUpdater | `plexposterupdater` | 5234 | `plexposters.pancakefarts.xyz` | 192.168.1.104 |
| Tdarr | `tdarr` | 8264 | `tdarr.pancakefarts.xyz` | 192.168.1.104 |
| Watchstate | `watchstate` | 4243 | `watchstate.pancakefarts.xyz` | 192.168.1.104 |
| AdGuard Home | `adguard-home` | 3000 | `adguard.pancakefarts.xyz` | 192.168.1.3 |
| Free-Games-Claimer | `free-games-claimer` | 6080 | `freegames.pancakefarts.xyz` | 192.168.1.104 |
| Plex-Media-Server | (TrueNAS) | 32400 | (already on `plex.pancakefarts.xyz` if set) | 192.168.1.122 |

**Already on `.xyz` with valid certs (12 services, no action):** auth, nextcloud, immich, owu, mesh, borg, oauth2proxy, ssh, tug, ha, searxng, yacy, epic (some have access_list but all are working). Plus 3 apex rows for `pancakefarts.xyz`.

---

## The execution plan (after the 4 decisions above)

### Step 1 — Set LAN DNS to route through AdGuard (10 min, the UOS API)

```python
# /tmp/split_horizon_setup.py
import urllib.request, json, ssl
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
UOS_BASE = "https://192.168.1.89:11443"
KEY = "rFQfxMKoL6UdegdxViGOK8_jrHL9ki1D"

def put(path, body):
    req = urllib.request.Request(UOS_BASE + path,
        data=json.dumps(body).encode(),
        headers={"X-API-Key": KEY, "Content-Type": "application/json"},
        method="PUT")
    with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
        return json.loads(r.read())

# Set LAN DNS to AdGuard (the split-horizon slot)
put("/proxy/network/api/s/default/rest/networkconf/63b0d766652205010897d27e", {
    "dhcpd_dns_enabled": True,
    "dhcpd_dns_server_1": "192.168.1.3",
    "wan_dns_preference": "auto"
})

# Set IoT DNS to AdGuard too (per the 2026-07-04 pattern, but properly)
put("/proxy/network/api/s/default/rest/networkconf/6a015216222f682c917104af", {
    "dhcpd_dns_enabled": True,
    "dhcpd_dns_server_1": "192.168.1.3",
    "wan_dns_preference": "auto"
})
```

**Verify:** `dig @192.168.1.3 pancakefarts.xyz` → still 173.181.99.119 (no rewrite yet, but resolver is reachable). `dig @192.168.1.1 pancakefarts.xyz` → still 173.181.99.119 (UCG passes through).

### Step 2 — Populate AdGuard's rewrites (10 min, the AdGuard REST API)

```python
# AdGuard at 192.168.1.3:3000 (or via docker exec)
# Use the AdGuard API: POST /control/rewrite/add
import urllib.request, json
AG_BASE = "http://192.168.1.3:3000"
# Need AdGuard admin creds — saved in 1Password? Or set up.

rewrites = [
    ("homepage.pancakefarts.xyz",   "192.168.1.104"),
    ("jellyfin.pancakefarts.xyz",   "192.168.1.104"),
    ("seerr.pancakefarts.xyz",      "192.168.1.104"),
    # ... 50+ entries
]
for domain, answer in rewrites:
    # POST /control/rewrite/add with body {"domain": domain, "answer": answer}
    ...
```

**Verify:** `dig +short @192.168.1.3 homepage.pancakefarts.xyz` → should return 192.168.1.104. `dig +short @1.1.1.1 homepage.pancakefarts.xyz` → should return 173.181.99.119 (public IP). **Same hostname, two answers = split-horizon working.**

### Step 3 — Add wildcard LE cert to npmplus (10 min, webUI)

In npmplus UI: Settings → SSL Certificates → Add → Let's Encrypt → DNS Challenge with Cloudflare API token → Domains: `*.pancakefarts.xyz, pancakefarts.xyz` → Request. Should issue in 30s.

### Step 4 — Add Cloudflare DNS A records (one curl per hostname, 10 min)

```bash
CF_TOKEN="<zone-scoped token from 1Password>"
ZONE_ID="<the pancakefarts.xyz zone id>"
for hostname in $(cat /tmp/hostname_list.txt); do
  curl -X POST "https://api.cloudflare.com/client/v4/zones/$ZONE_ID/dns_records" \
    -H "Authorization: Bearer $CF_TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"type\":\"A\",\"name\":\"$hostname\",\"content\":\"173.181.99.119\",\"proxied\":false}" \
    | python3 -c "import sys, json; d=json.load(sys.stdin); print(f'{d.get(\"result\",{}).get(\"name\",\"?\")}: {\"OK\" if d.get(\"success\") else d.get(\"errors\")}')"
done
```

`-proxied:false` (grey cloud) — same as existing records.

### Step 5 — Bulk-add proxy_hosts in npmplus (20 min, scripted SQL)

For each hostname in the agreed list, insert a row in `proxy_host` pointing at the wildcard cert and the right backend. Restart npmplus.

### Step 6 — Migrate existing 12 `.xyz` proxy_hosts to the wildcard cert (10 min, one UPDATE)

```sql
UPDATE proxy_host SET certificate_id = (SELECT id FROM certificate WHERE nice_name LIKE '%pancakefarts%' AND domain_names LIKE '%*.pancakefarts.xyz%')
WHERE enabled = 1 AND domain_names LIKE '%pancakefarts.xyz%';
-- Then hard-delete the now-orphaned per-hostname certs (with backup)
```

### Step 7 — End-to-end verify (10 min)

From a laptop on Wi-Fi:
- `dig homepage.pancakefarts.xyz` → 192.168.1.104 (the AdGuard answer)
- `curl -I https://homepage.pancakefarts.xyz` → 200, cert shows `*.pancakefarts.xyz` SAN
- Open in browser → no cert warning
- From phone on cellular: same checks, just the IP differs

### Step 8 — Watchdogs (30 min)

- **Cert expiry check:** daily cron, alert if wildcard cert < 21 days to expiry
- **DNS drift check:** daily cron, alert if `dig @192.168.1.3 <hostname>` returns wrong IP for any service
- **AdGuard uptime check:** existing pattern (Unraid `restart: unless-stopped`)

---

## Cost / risk

- **Time:** 2-3 hours end-to-end, mostly wait time
- **Money:** $0 (LE free, Cloudflare free tier, npmplus already running, AdGuard already running)
- **Risk:** low. Each step is reversible with the snapshot taken before. The only shared-resource change is migrating the existing 12 proxy_hosts to the wildcard cert (DB UPDATE; we have a snapshot).
- **The split-horizon DNS change in Step 1 is the most user-visible thing** — until LAN clients renew their DHCP lease (default 24h, most clients renew at 12h), they keep using the old DNS. To force immediately: `ipconfig /renew` (Windows), `dhclient -r && dhclient` (Linux/Mac), or just reboot the device.

---

## What's intentionally NOT in this plan

- **Not touching the 4 broken Miniflux/SearXNG hostnames** (those are in the reconciliation script — separate work)
- **Not migrating the 6 npmplus proxy_hosts with no auth_request** (the OIDC enforcement check from the reconciliation doc — separate work)
- **Not fixing the DHCP relay to .254** (a real bug, but not in scope of TLS)
- **Not setting `dhcpd_dns_enabled: true` on the IoT network** (it's set as part of Step 1)
- **Not creating a Cloudflare API token** (you do this, save in 1Password)
- **Not migrating the existing 3-row `pancakefarts.xyz` apex** (those are intentional, leave alone)
