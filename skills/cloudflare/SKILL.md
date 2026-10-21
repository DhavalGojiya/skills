---
name: cloudflare
description: >-
  Cloudflare DNS zones and nameservers: how a zone gets its nameserver pair, how the zone that answers is chosen when one domain is in several accounts, activation, DNSSEC and TTLs while moving DNS to Cloudflare, and proxied versus DNS-only records. Use when adding a domain to Cloudflare or moving its DNS there, when a zone stays in "Pending Nameserver Update" or turns "Moved", when the assigned nameservers differ from other zones or from the ones preset at the registrar, when SERVFAIL starts after changing nameservers (stale DS record), when the same domain is in two Cloudflare accounts, when adding a domain fails with 1049, 1099, 1105, 1097 or "subject to a hold", when records answer at Cloudflare's nameservers but users still get old ones, when SSH or mail breaks or the origin IP leaks after proxying, or when asking what the registrar still does once the nameservers point to Cloudflare.
---

# Cloudflare DNS zones and nameservers

Verified against Cloudflare's docs source at [`ef83b95`](https://github.com/cloudflare/cloudflare-docs/tree/ef83b95d4849c750001782e70ec04aee900e695c) (2026-10-21), the API v4 schema as generated into [cloudflare-python 5.9.0](https://github.com/cloudflare/cloudflare-python/tree/v5.9.0), and live queries to all of Cloudflare's standard nameservers on 2026-10-21 with `scripts/check_delegation.py` (dnspython 2.9.0) and dig 9.18.39. Cloudflare is a hosted service; recheck the pool size and timings against the docs when they matter.

```text
you ---> registrar ---EPP---> registry: the TLD zone publishes  example.com NS adam, gina  (+ DS)
resolver: root -> TLD -> referral to adam, gina -> query sent to one of adam's or gina's IPs
Cloudflare picks the zone by (query name, nameserver IP the query arrived on):
  IPs of adam or gina  -> the zone that was assigned adam + gina, pending or active
  any other NS IP      -> the active zone of that name, if there is one, else REFUSED
```

## Rules

### Nameserver assignment

1. Nameservers belong to the zone, not to the account. Take the pair from that zone (its Overview page, or `name_servers` in `GET /zones/{zone_id}`) and set exactly those two at the registrar; never reuse a pair from another zone, account or earlier attempt.
2. Expect a different pair when the same domain is or recently was active in another account, when the zone was deleted and re-added, or when a parent or child zone in the account holds the preferred pair. Support cannot change an assigned pair, and deleting and re-adding gives yet another one. For one pair across all zones, use account custom nameservers or nameserver sets (Business and Enterprise).
3. Create the zone first, then change the registrar. Cloudflare never gives a new zone the nameservers its domain is already delegated to, so a pair preset at the registrar, or left there from a deleted zone, is replaced and the zone stays pending until the registrar gets the new pair. This is what stops anyone from claiming a domain whose delegation still points at Cloudflare.
4. The standard pool is 899 names (`<name>.ns.cloudflare.com`, counted by PTR on 2026-10-21). Cloudflare's 2013 [post on the names](https://blog.cloudflare.com/whats-the-story-behind-the-names-of-cloudflares-name-servers/) described 101 names and 2,550 pairs. Each name has its own three IPv4 and three IPv6 addresses, and a pair almost always takes one name from each of two halves of 450 and 449 names (9,802 of 9,803 sampled zones), so do not infer the second nameserver from the first. A pair serves many unrelated zones; it only has to differ between zones with the same name.

### Which zone answers

5. A pending zone, and a deleted one until it is purged, answers only on the IPs of its own two nameservers; the active zone answers on every other Cloudflare nameserver IP; a name with no zone gets `REFUSED`. Test a zone before switching with `dig +norec @<its nameserver> www.example.com`, never through a resolver or another Cloudflare nameserver.
6. An authoritative answer from a Cloudflare nameserver proves neither delegation nor activation: anyone can add your domain to their account, and that zone answers on its own pair. Check the parent instead (rule 10).
7. When several accounts hold the domain, the zone whose pair the parent publishes activates. The others stay pending (Free plan: deleted after 28 days); a zone that was active turns `moved` and is deleted after 7 days (Free plan), and a deleted zone answers for 7 more days before it is purged. Only an Enterprise zone hold stops other accounts from adding the domain; a partial (CNAME) setup belongs to whichever account verified last.

### Activation

08. A full-setup zone activates only when the parent lists exactly its assigned nameservers. Remove every other provider's nameserver at the registrar; with extra ones the zone stays pending unless multi-provider DNS is enabled. Registries that test the new nameservers before accepting them, such as DENIC for `.de`, need the zone to answer already, so finish onboarding (choose a plan) first: an `initializing` zone answers nothing.
09. Remove the DS record at the registrar before changing nameservers, and wait out its TTL (`dig DS example.com`; 24 h for `.com`). A stale DS makes validating resolvers return `SERVFAIL` for the whole domain and blocks activation. After activation, enable DNSSEC at Cloudflare and add its DS at the registrar; Cloudflare adds it itself only for Cloudflare Registrar, `.ch` and `.cz` domains. Cloudflare's DS has key tag 2371 unless the account has dedicated Foundation DNS keys, so another key tag left at the parent after the switch is the old host's.
10. Believe the parent, not the registrar's control panel: `dig +trace +nodnssec example.com NS +noall +authority`. A registrar lock (`client update prohibited` in RDAP), a pending transfer or a redemption period rejects the change; `client hold` or `server hold` removes the delegation and turns an active zone `moved`.
11. The first activation check runs 60 s after the zone is added, later ones at growing intervals. `PUT /zones/{zone_id}/activation_check` queues an earlier one, at most once an hour on Free and every 5 minutes on paid plans.
12. Copy the nameserver names, do not type them. `ns.cloudflare.com` answers `NOERROR` without an address for names it does not use, so a misspelled pair looks valid until something tries to reach it.
13. Cloudflare adds only a registered domain one label below a public suffix that already answers `NS` and `SOA` from working nameservers. `1105` means too many attempts to add that domain; wait 3 hours.

### Switching without an outage

14. Copy every record before switching: the quick scan only guesses common names and misses names like `my-store1900` or a custom DKIM selector, and a wildcard at the old host makes it import records that never existed. Import a zone file exported from the old host, then compare the answers of the assigned nameservers (rule 5) with the old host's.
15. Keep the old host serving the same records after the switch for at least the parent's `NS` TTL, because resolvers keep using the cached delegation until it expires: 48 h for `.com`, `.net` and `.uk`, 24 h for `.de`, 1 h for `.org`, 15 min for `.in`, 5 min for `.io` (measured 2026-10-21). Only the registry sets that TTL; Cloudflare's own migration guide retires the old servers several days to a week after the switch.
16. A name that a resolver looked up while it did not exist stays `NXDOMAIN` there for the SOA minimum, 1800 s on Cloudflare unless an Enterprise account changed it.
17. Start every record DNS-only and proxy it after the zone is active, the Universal SSL certificate is issued (15 minutes to 24 hours after activation) and the origin accepts Cloudflare's IP ranges.

### Proxied and DNS-only records

18. A proxied A, AAAA or CNAME answers with Cloudflare's anycast IPs and a fixed TTL of 300 s; a DNS-only record answers with its own value and a TTL of 60 to 86400 s (from 30 s on Enterprise; `ttl: 1` in the API is Auto, 300 s). A proxied name also answers AAAA when only an A record exists (IPv6 compatibility, which only Enterprise can turn off). MX, TXT and every other type are always DNS-only, and proxied CNAMEs are answered flattened.
19. One proxied A or AAAA record on a name makes all of that name's A and AAAA records proxied, and one proxied hop in a CNAME chain proxies the chain.
20. The proxy carries only HTTP on ports 80, 8080, 8880, 2052, 2082, 2086, 2095 and HTTPS on 443, 2053, 2083, 2087, 2096, 8443. SSH, SMTP, FTP, RDP and database traffic to a proxied name fails; give those services a DNS-only name (or use Spectrum).
21. The origin IP still leaks through DNS-only records on the same address, through an MX pointing at a proxied name (Cloudflare answers with a generated `_dc-mx.<hash>` name whose A record is the origin), and during the pending period, when proxied records are served DNS-only on the assigned nameservers. When the origin must stay hidden, change its IP after activation and accept web traffic only from Cloudflare's ranges.

### The registrar

22. The registrar only forwards nameserver and DS changes to the registry; resolution never reaches it. Moving the registration to another registrar keeps the delegation, while an expired, suspended or parked domain loses it and the zone turns `moved`.
23. Cloudflare Registrar domains must use Cloudflare's nameservers, a transfer to Cloudflare Registrar needs an active full-setup zone and DNSSEC turned off at the old registrar first, and an unverified registrant email swaps in parking nameservers until it is verified.

## Debug procedure

1. Compare the parent's delegation with the zone's assigned pair; the script also checks each nameserver, the DS against the served DNSKEYs, a validating resolver with and without `+cd`, and the registry's RDAP status:
   ```bash
   uv run scripts/check_delegation.py example.com --expect adam.ns.cloudflare.com gina.ns.cloudflare.com
   ```
2. Get the assigned pair, the status and what Cloudflare saw when the zone was created (API token with `Zone:Read`):
   ```bash
   curl -s -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
     'https://api.cloudflare.com/client/v4/zones?name=example.com' \
     | jq '.result[] | {id, status, name_servers, original_name_servers, original_registrar}'
   ```
3. The parent still lists the old provider: the registrar has not published the change. Look for `client update prohibited`, `pending transfer`, `redemption period` or a hold in the RDAP part of step 1, fix it at the registrar, and rerun step 1 (rule 10).
4. The parent lists other Cloudflare names: the pair came from another account, a deleted zone or an earlier attempt. Set exactly the pair from step 2 (rules 1 to 3). To see every zone that Cloudflare holds for the domain and the pair each answers on, add `--scan-pool`.
5. The script reports a stale DS, or `SERVFAIL` that `+cd` fixes: remove the DS at the registrar, wait for the TTL step 1 printed, and rerun (rule 9).
6. Everything matches and the zone is still pending: queue a check, then wait (rule 11).
   ```bash
   curl -s -X PUT -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
     "https://api.cloudflare.com/client/v4/zones/$ZONE_ID/activation_check"
   ```
7. Users get old or missing records while Cloudflare answers correctly: compare the zone's own nameserver with a public resolver. An old answer means a cached delegation, so keep the old host serving (rule 15); `NXDOMAIN` with an SOA in the authority section means negative caching (rule 16).
   ```bash
   dig +norec @adam.ns.cloudflare.com www.example.com A
   dig @1.1.1.1 www.example.com A +noall +answer +authority
   ```
8. A zone turned `moved`: check the RDAP status and expiry in step 1, restore the assigned pair at the registrar within the 7 days before deletion (rule 7), or expect a new pair after re-adding it.

## Script

`scripts/check_delegation.py` asks the parent zone's servers directly, so caches never hide a change, and exits 1 when it finds a problem. `--scan-pool` finds the standard nameservers by PTR in `172.64.32.0/22` and asks each one for the domain's SOA, which lists every zone of that name with the pair it answers on and marks the one the delegation reaches. It needs outbound DNS (UDP and TCP 53) and HTTPS for RDAP (`--no-rdap` skips it).

```bash
uv run scripts/check_delegation.py example.com
uv run scripts/check_delegation.py example.com --no-rdap --scan-pool
```

## References

- `references/nameservers.md`: what Cloudflare's 2013 post on the nameserver names says, quoted, and what changed since; the nameserver pool and its addresses; how the zone that answers is chosen (the experiments behind rules 4 to 7); why preset pairs are replaced and the dangling-delegation takeover it prevents; and Foundation DNS and custom nameservers. Read it when nameservers are unexpected or the same domain is in several accounts.
- `references/activation.md`: registrar, registry and DNS host roles with a diagram, EPP status codes, the zone status lifecycle and its timers, the activation check, DNSSEC during the switch, the measured TTLs behind "propagation", errors when adding a domain, Cloudflare Registrar, partial setup and zone holds. Read it for a pending, moved or deleted zone, or to plan a migration.
- `references/proxy-status.md`: what proxied and DNS-only records return, TTL rules, mixed records and CNAME chains, ports and protocols, the ways the origin IP leaks, and when to turn the proxy on. Read it when a service breaks behind the proxy or the origin must stay hidden.
