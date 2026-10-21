# /// script
# requires-python = ">=3.14"
# dependencies = ["dnspython>=2.9"]
# ///
"""Show a domain's delegation the way Cloudflare's activation check has to see it.

Asks the parent zone's servers directly (no resolver cache) for the NS and DS records,
then each delegated nameserver for SOA, NS and DNSKEY, then validating resolvers with
and without checking disabled, and RDAP for the registry's own record. With --expect,
compares the delegation with the nameservers a Cloudflare zone was assigned. With
--scan-pool, asks every standard Cloudflare nameserver which zone it serves for the
domain, which shows other zones of the same name held in other accounts.
"""

import argparse
import collections
import concurrent.futures
import difflib
import ipaddress
import json
import sys
import urllib.error
import urllib.request

import dns.dnssec
import dns.exception
import dns.flags
import dns.message
import dns.name
import dns.query
import dns.rcode
import dns.rdatatype
import dns.resolver
import dns.reversename

TIMEOUT = 3.0
CLOUDFLARE_KINDS = {
    "ns.cloudflare.com": "Cloudflare standard",
    "secondary.cloudflare.com": "Cloudflare secondary",
    "foundationdns.com": "Cloudflare advanced (Foundation DNS)",
    "foundationdns.net": "Cloudflare advanced (Foundation DNS)",
    "foundationdns.org": "Cloudflare advanced (Foundation DNS)",
}
# Each standard nameserver name owns one IPv4 address in this /22, and its PTR names it.
STANDARD_POOL = ipaddress.ip_network("172.64.32.0/22")
VALIDATING_RESOLVERS = ["1.1.1.1", "8.8.8.8"]
# Key tag of the DS for Cloudflare's shared signing key; Foundation DNS can give an account its own.
CLOUDFLARE_KEY_TAG = 2371
# RDAP status (RFC 8056 spelling) -> what it means for a nameserver change.
RDAP_STATUS_NOTES = {
    "client hold": "PROBLEM: the registry does not publish the delegation (RFC 5731 clientHold)",
    "server hold": "PROBLEM: the registry does not publish the delegation (RFC 5731 serverHold)",
    "client update prohibited": "nameserver changes are rejected until the registrar lifts this lock",
    "server update prohibited": "nameserver changes are rejected until the registry lifts this lock",
    "pending transfer": "a transfer is in progress; registrars usually refuse nameserver changes until it ends",
    "redemption period": "PROBLEM: the domain was deleted and is in redemption; renew it first",
    "pending delete": "PROBLEM: the domain is about to be purged by the registry",
    "inactive": "PROBLEM: the registry holds no nameservers for the domain",
}

problems: list[str] = []
resolver = dns.resolver.Resolver()
resolver.lifetime = 8.0


def problem(message: str) -> None:
    problems.append(message)
    print(f"  PROBLEM: {message}")


def hours(ttl: int) -> str:
    return f"{ttl} s = {ttl / 3600:g} h"


def ask(
    qname: str | dns.name.Name,
    rdtype: str,
    server: str,
    *,
    recurse: bool = False,
    dnssec: bool = False,
    cd: bool = False,
) -> dns.message.Message | str:
    """Send one query to server; return the response, or the error name after one retry."""
    q = dns.message.make_query(qname, rdtype, want_dnssec=dnssec)
    if not recurse:
        q.flags &= ~dns.flags.RD
    if cd:
        q.flags |= dns.flags.CD
    error = "no response"
    for _ in range(2):
        try:
            return dns.query.udp_with_fallback(q, server, timeout=TIMEOUT)[0]
        except (dns.exception.DNSException, OSError) as e:
            error = type(e).__name__
    return error


def ipv4(host: str | dns.name.Name) -> list[str]:
    try:
        return sorted(r.address for r in resolver.resolve(host, "A"))
    except dns.exception.DNSException:
        return []


def rrset(msg: dns.message.Message, name: dns.name.Name, rdtype: str):
    t = dns.rdatatype.from_text(rdtype)
    for section in (msg.answer, msg.authority):
        for rr in section:
            if rr.name == name and rr.rdtype == t:
                return rr
    return None


def kind(host: str) -> str:
    for suffix, label in CLOUDFLARE_KINDS.items():
        if host == suffix or host.endswith("." + suffix):
            return label
    registered = "".join(c for c in ".".join(host.split(".")[-2:]) if c.isalpha())
    if difflib.SequenceMatcher(None, registered, "cloudflarecom").ratio() > 0.8:
        return "MISSPELLED Cloudflare name"
    return "not Cloudflare"


def parent_view(domain: dns.name.Name) -> tuple[list[str], dict]:
    """Ask up to three of the parent zone's servers for the referral and the DS set."""
    parent = dns.resolver.zone_for_name(domain.parent(), resolver=resolver)
    servers = sorted(str(r.target).rstrip(".") for r in resolver.resolve(parent, "NS"))[
        :3
    ]
    print(
        f"Delegation of {domain.to_text(True)} in the parent zone {parent.to_text(True)}"
    )
    seen: dict[str, tuple] = {}
    result: dict = {}
    for server in servers:
        addrs = ipv4(server)
        if not addrs:
            continue
        msg = ask(domain, "NS", addrs[0])
        if isinstance(msg, str):
            print(f"  {server}: {msg}")
            continue
        rcode = dns.rcode.to_text(msg.rcode())
        ns = rrset(msg, domain, "NS")
        ds_msg = ask(domain, "DS", addrs[0], dnssec=True)
        ds = None if isinstance(ds_msg, str) else rrset(ds_msg, domain, "DS")
        names = sorted(str(r.target).rstrip(".").lower() for r in ns) if ns else []
        seen[server] = (rcode, tuple(names), tuple(sorted(map(str, ds or []))))
        if not result:
            result = {"rcode": rcode, "ns": names, "ns_ttl": ns.ttl if ns else None}
            result["ds"] = list(ds) if ds else []
            result["ds_ttl"] = ds.ttl if ds else None
    if not seen:
        problem(f"no server of {parent.to_text(True)} answered")
        return [], {}
    print(f"  asked {', '.join(seen)}")
    if len(set(seen.values())) > 1:
        problem(
            f"the servers of {parent.to_text(True)} disagree (an update still spreading): {seen}"
        )
    if result["rcode"] == "NXDOMAIN":
        problem(
            f"{parent.to_text(True)} has no delegation for {domain.to_text(True)} (NXDOMAIN): unregistered, on hold, or expired"
        )
        return [], result
    if not result["ns"]:
        problem(f"{parent.to_text(True)} returned {result['rcode']} without NS records")
        return [], result
    print(f"  NS  {'  '.join(result['ns'])}  (TTL {hours(result['ns_ttl'])})")
    if result["ds"]:
        for ds in result["ds"]:
            print(f"  DS  {ds}  (TTL {hours(result['ds_ttl'])})")
    else:
        print("  DS  none (the delegation is unsigned)")
    return result["ns"], result


def child_view(domain: dns.name.Name, parent_ns: list[str]) -> list:
    """Ask each delegated nameserver for SOA and NS; return the DNSKEY set if one answers."""
    print("Delegated nameservers")
    dnskeys = None
    for host in parent_ns:
        addrs = ipv4(host)
        label = kind(host)
        if label.startswith("MISSPELLED"):
            problem(f"{host} looks like a misspelling of a Cloudflare nameserver")
        if not addrs:
            problem(
                f"{host} has no IPv4 address (a typo, or a name Cloudflare never assigned)"
            )
            continue
        msg = ask(domain, "SOA", addrs[0])
        if isinstance(msg, str):
            problem(f"{host} ({addrs[0]}) did not answer: {msg}")
            continue
        rcode = dns.rcode.to_text(msg.rcode())
        aa = "aa" if msg.flags & dns.flags.AA else "no aa"
        soa = rrset(msg, domain, "SOA")
        detail = (
            f"SOA {soa[0].mname.to_text(True)} serial {soa[0].serial}"
            if soa
            else "no SOA"
        )
        print(f"  {host}  [{label}]  {addrs[0]}: {rcode}, {aa}, {detail}")
        if rcode != "NOERROR" or not msg.flags & dns.flags.AA:
            problem(
                f"{host} is not authoritative for the domain ({rcode}, {aa}): a lame delegation"
            )
            continue
        ns_msg = ask(domain, "NS", addrs[0])
        if not isinstance(ns_msg, str) and (ns := rrset(ns_msg, domain, "NS")):
            apex = sorted(str(r.target).rstrip(".").lower() for r in ns)
            if apex != parent_ns:
                print(f"    apex NS here: {'  '.join(apex)} (differs from the parent)")
        if dnskeys is None:
            key_msg = ask(domain, "DNSKEY", addrs[0], dnssec=True)
            if not isinstance(key_msg, str):
                dnskeys = rrset(key_msg, domain, "DNSKEY") or []
    return dnskeys or []


def dnssec_view(
    domain: dns.name.Name, ds_set: list, dnskeys: list, parent_ns: list[str]
) -> None:
    print("DNSSEC")
    if ds_set:
        matched = []
        for ds in ds_set:
            for key in dnskeys:
                try:
                    made = dns.dnssec.make_ds(
                        domain, key, ds.digest_type, validating=True
                    )
                except (dns.dnssec.UnsupportedAlgorithm, dns.dnssec.DeniedByPolicy):
                    continue
                if made == ds:
                    matched.append(ds.key_tag)
        if matched:
            print(
                f"  DS key tag(s) {sorted(set(matched))} match a DNSKEY the nameservers serve"
            )
        elif dnskeys:
            problem(
                "no DS at the parent matches a DNSKEY the nameservers serve (stale DS)"
            )
        else:
            problem(
                "the parent has a DS record but the nameservers serve no DNSKEY (stale DS)"
            )
        if any(kind(n) == "Cloudflare standard" for n in parent_ns):
            for tag in sorted({ds.key_tag for ds in ds_set} - {CLOUDFLARE_KEY_TAG}):
                print(
                    f"    key tag {tag} is not Cloudflare's {CLOUDFLARE_KEY_TAG}: the previous "
                    "host's, unless multi-signer DNSSEC or dedicated keys are set up"
                )
    else:
        print(
            f"  no DS at the parent; {len(dnskeys)} DNSKEY record(s) at the nameservers"
        )
    for server in VALIDATING_RESOLVERS:
        plain = ask(domain, "SOA", server, recurse=True, dnssec=True)
        if isinstance(plain, str):
            print(f"  {server}: {plain}")
            continue
        rcode = dns.rcode.to_text(plain.rcode())
        ad = ", ad (validated)" if plain.flags & dns.flags.AD else ""
        print(f"  {server}: {rcode}{ad}")
        if rcode == "SERVFAIL":
            unchecked = ask(domain, "SOA", server, recurse=True, dnssec=True, cd=True)
            if (
                not isinstance(unchecked, str)
                and unchecked.rcode() == dns.rcode.NOERROR
            ):
                problem(
                    f"{server} fails validation but answers with +cd: the DNSSEC chain is broken"
                )
            else:
                problem(
                    f"{server} returns SERVFAIL even with +cd: the nameservers are not answering"
                )
        break


def rdap_view(domain: dns.name.Name, parent_ns: list[str]) -> None:
    name = domain.to_text(omit_final_dot=True)
    print("Registry record (RDAP via rdap.org)")
    request = urllib.request.Request(
        f"https://rdap.org/domain/{name}", headers={"Accept": "application/rdap+json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.load(response)
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        print(f"  unavailable: {e}")
        return
    registrar = "unknown"
    for entity in data.get("entities", []):
        if "registrar" in entity.get("roles", []):
            for field in entity.get("vcardArray", [None, []])[1]:
                if field[0] == "fn":
                    registrar = field[3]
    statuses = data.get("status", [])
    rdap_ns = sorted(
        n["ldhName"].rstrip(".").lower() for n in data.get("nameservers", [])
    )
    print(f"  registrar: {registrar}")
    print(f"  status: {', '.join(statuses) or 'none'}")
    for status in statuses:
        if note := RDAP_STATUS_NOTES.get(status.lower()):
            if note.startswith("PROBLEM: "):
                problem(f"{status}: {note.removeprefix('PROBLEM: ')}")
            else:
                print(f"    {status}: {note}")
    print(f"  nameservers: {'  '.join(rdap_ns) or 'none'}")
    if rdap_ns and parent_ns and rdap_ns != parent_ns:
        print(
            "    differs from the DNS delegation: the registry has not published the change yet"
        )
    print(f"  delegationSigned: {data.get('secureDNS', {}).get('delegationSigned')}")


def expect_view(parent_ns: list[str], expected: list[str]) -> None:
    want = sorted(n.rstrip(".").lower() for n in expected)
    print("Assigned nameservers (--expect)")
    missing = [n for n in want if n not in parent_ns]
    extra = [n for n in parent_ns if n not in want]
    if not missing and not extra:
        print("  the parent lists exactly the assigned nameservers")
        return
    if len(missing) == len(want):
        problem(
            "the parent delegates to other nameservers: the registrar has not published the "
            "change, or it was made with other names (another account, a deleted zone)"
        )
        return
    if missing:
        problem(f"the parent does not list {', '.join(missing)}")
    if extra:
        problem(
            f"the parent also lists {', '.join(extra)}; a full-setup zone activates only on an "
            "exact match unless multi-provider DNS is enabled"
        )


def pool_view(domain: dns.name.Name, parent_ns: list[str], workers: int) -> None:
    """Query SOA at one IPv4 address of every standard nameserver and group the answers."""
    print(f"Standard Cloudflare nameservers answering for {domain.to_text(True)}")

    def ptr(ip: str) -> tuple[str, str | None]:
        try:
            answer = resolver.resolve(dns.reversename.from_address(ip), "PTR")
            return ip, str(answer[0].target).rstrip(".")
        except dns.exception.DNSException:
            return ip, None

    def soa(ip: str) -> tuple[str, tuple]:
        msg = ask(domain, "SOA", ip)
        if isinstance(msg, str):
            return ip, (msg, "", 0)
        record = rrset(msg, domain, "SOA") if msg.flags & dns.flags.AA else None
        if record is None:
            return ip, (dns.rcode.to_text(msg.rcode()), "", 0)
        return ip, ("NOERROR", str(record[0].mname).rstrip("."), record[0].serial)

    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        names = {ip: n for ip, n in pool.map(ptr, map(str, STANDARD_POOL)) if n}
        answers = dict(pool.map(soa, names))
    print(f"  {len(names)} nameserver names found by PTR in {STANDARD_POOL}")
    groups = collections.defaultdict(list)
    for ip, answer in answers.items():
        groups[answer].append(names[ip])
    for (rcode, mname, serial), members in sorted(
        groups.items(), key=lambda g: -len(g[1])
    ):
        if rcode != "NOERROR":
            print(f"  {len(members):4} nameservers: {rcode}")
            continue
        ip = next(i for i, a in answers.items() if a == (rcode, mname, serial))
        ns_msg = ask(domain, "NS", ip)
        ns = rrset(ns_msg, domain, "NS") if not isinstance(ns_msg, str) else None
        zone_ns = sorted(str(r.target).rstrip(".").lower() for r in ns) if ns else []
        where = (
            "every other nameserver"
            if len(members) > len(zone_ns)
            else "its own nameservers only"
        )
        mark = "  <- the delegation points here" if zone_ns == parent_ns else ""
        print(
            f"  {len(members):4} nameservers: zone with NS {' '.join(zone_ns) or '?'}, "
            f"serial {serial}, answers on {where}{mark}"
        )
        if len(members) <= len(zone_ns):
            print(f"       ({', '.join(sorted(members))})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("domain", help="apex domain, for example example.com")
    parser.add_argument(
        "--expect",
        nargs="+",
        metavar="NS",
        help="the nameservers the Cloudflare zone was assigned (zone Overview page)",
    )
    parser.add_argument("--no-rdap", action="store_true", help="skip the RDAP lookup")
    parser.add_argument(
        "--scan-pool",
        action="store_true",
        help="ask every standard Cloudflare nameserver which zone it serves for the domain",
    )
    parser.add_argument(
        "--workers", type=int, default=16, help="parallel queries for --scan-pool"
    )
    args = parser.parse_args()

    domain = dns.name.from_text(args.domain.strip().lower())
    parent_ns, parent = parent_view(domain)
    dnskeys = child_view(domain, parent_ns) if parent_ns else []
    if parent_ns:
        dnssec_view(domain, parent.get("ds", []), dnskeys, parent_ns)
    if args.expect:
        expect_view(parent_ns, args.expect)
    if not args.no_rdap:
        rdap_view(domain, parent_ns)
    if args.scan_pool:
        pool_view(domain, parent_ns, args.workers)
    print(f"\n{len(problems)} problem(s) found" if problems else "\nno problems found")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
