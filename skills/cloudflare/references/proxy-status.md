# Proxied and DNS-Only Records

What a proxied (orange cloud) and a DNS-only (gray cloud) record return, which TTL each gets, which traffic the proxy can carry, and where a zone hands out the origin IP even when its records are proxied.

Verified against Cloudflare's docs source at [`ef83b95`](https://github.com/cloudflare/cloudflare-docs/tree/ef83b95d4849c750001782e70ec04aee900e695c) (2026-10-21) and the API v4 schema as generated into [cloudflare-python 5.9.0](https://github.com/cloudflare/cloudflare-python/tree/v5.9.0). Every "Source:" link points at a commit, with line numbers.

______________________________________________________________________

## Table of Contents

1. [What Each Mode Returns](#1-what-each-mode-returns)
2. [TTL](#2-ttl)
3. [Several Records on One Name, and CNAME Chains](#3-several-records-on-one-name-and-cname-chains)
4. [Traffic the Proxy Cannot Carry](#4-traffic-the-proxy-cannot-carry)
5. [Where the Origin IP Still Leaks](#5-where-the-origin-ip-still-leaks)
6. [Turning the Proxy On During a Migration](#6-turning-the-proxy-on-during-a-migration)

______________________________________________________________________

## 1. What Each Mode Returns

Only address records can be proxied; every other type is served as-is:

Source: [`src/content/docs/dns/proxy-status/index.mdx#L20`](https://github.com/cloudflare/cloudflare-docs/blob/ef83b95d4849c750001782e70ec04aee900e695c/src/content/docs/dns/proxy-status/index.mdx#L20)

```md
Only [records used for IP address resolution](/dns/manage-dns-records/reference/dns-record-types/#ip-address-resolution) — A, AAAA, and CNAME records — can be proxied. Other record types (such as MX or TXT) are always DNS-only.
```

Source: [`src/content/docs/dns/proxy-status/index.mdx#L46-L47`](https://github.com/cloudflare/cloudflare-docs/blob/ef83b95d4849c750001782e70ec04aee900e695c/src/content/docs/dns/proxy-status/index.mdx#L46-L47)

```md
- A DNS query to the proxied record `blog.example.com` will be answered with Cloudflare [anycast IP addresses](/fundamentals/concepts/cloudflare-ip-addresses/) — shared IP addresses used to route traffic through a nearby data center — instead of `192.0.2.1`. This ensures that HTTP/HTTPS requests for this name will be sent to Cloudflare's network and can be proxied, which allows the [benefits listed above](#benefits).
- A DNS query to the DNS-only record `shop.example.com` will be answered with the actual origin IP address, `192.0.2.2`. This exposes your origin IP address to anyone who queries the record, which removes a layer of protection against targeted attacks. Cloudflare also cannot provide HTTP/HTTPS analytics on those requests (only DNS analytics).
```

With every record DNS-only, Cloudflare is a plain authoritative DNS host, like the registrar's DNS was before; changing the nameservers does not move the registration, and no traffic passes through Cloudflare. The dashboard onboarding turns the proxy on by default ([`proxy-status/index.mdx#L25`](https://github.com/cloudflare/cloudflare-docs/blob/ef83b95d4849c750001782e70ec04aee900e695c/src/content/docs/dns/proxy-status/index.mdx#L25)), so records the quick scan imported can be proxied without anyone deciding it; review the proxy status of each before the switch.

A proxied name answers with both A and AAAA records even when only an A record exists: IPv6 compatibility, on by default, makes Cloudflare generate the AAAA records, and only Enterprise accounts can turn it off ([`ipv6-compatibility.mdx#L16`](https://github.com/cloudflare/cloudflare-docs/blob/ef83b95d4849c750001782e70ec04aee900e695c/src/content/docs/network/ipv6-compatibility.mdx#L16), [`#L29-L39`](https://github.com/cloudflare/cloudflare-docs/blob/ef83b95d4849c750001782e70ec04aee900e695c/src/content/docs/network/ipv6-compatibility.mdx#L29-L39)). Visitors then reach Cloudflare over IPv6 while Cloudflare reaches the origin over IPv4.

A proxied CNAME is answered with Cloudflare's addresses, not with the CNAME target, because proxied CNAMEs are always flattened ([`proxy-status/index.mdx#L124`](https://github.com/cloudflare/cloudflare-docs/blob/ef83b95d4849c750001782e70ec04aee900e695c/src/content/docs/dns/proxy-status/index.mdx#L124)). The same flattening is what allows a CNAME at the zone apex; see the [CNAME flattening blog post](https://blog.cloudflare.com/introducing-cname-flattening-rfc-compliant-cnames-at-a-domains-root/).

## 2. TTL

| Record                       | TTL                                                                     |
| ---------------------------- | ----------------------------------------------------------------------- |
| Proxied                      | Always "Auto", 300 s; not editable                                      |
| DNS-only                     | 60 s to 86400 s (30 s minimum on Enterprise); "Auto" is 300 s           |
| Apex `NS` (Cloudflare's own) | 86400 s default; Foundation DNS "Nameserver TTL" allows 30 s to 86400 s |

Source: [`src/content/docs/dns/manage-dns-records/reference/ttl.mdx#L20`](https://github.com/cloudflare/cloudflare-docs/blob/ef83b95d4849c750001782e70ec04aee900e695c/src/content/docs/dns/manage-dns-records/reference/ttl.mdx#L20), [`#L30-L32`](https://github.com/cloudflare/cloudflare-docs/blob/ef83b95d4849c750001782e70ec04aee900e695c/src/content/docs/dns/manage-dns-records/reference/ttl.mdx#L30-L32)

```md
By default, all <GlossaryTooltip term="proxy status" link="/dns/proxy-status/">proxied records</GlossaryTooltip> have a TTL of **Auto**, which is set to 300 seconds. This value cannot be edited.
```

```md
For **DNS only** records, you can choose a TTL between **30 seconds** (Enterprise) or **60 seconds** (non-Enterprise) and **1 day**.

A TTL of **Auto** is set to 300 seconds (five minutes).
```

In the API, "Auto" is the value `1`:

Source: [`src/cloudflare/types/dns/record_response.py#L454-L459`](https://github.com/cloudflare/cloudflare-python/blob/6106cdaa6bea2143c8a9322708992929812701bc/src/cloudflare/types/dns/record_response.py#L454-L459)

```python
    ttl: TTL
    """Time To Live (TTL) of the DNS record in seconds.

    Setting to 1 means 'automatic'. Value must be between 60 and 86400, with the
    minimum reduced to 30 for Enterprise zones.
    """
```

Resolvers keep a DNS-only value for the TTL it had when they fetched it, so lower the TTL at least one old TTL ahead of a planned change ([`faq.mdx#L126-L128`](https://github.com/cloudflare/cloudflare-docs/blob/ef83b95d4849c750001782e70ec04aee900e695c/src/content/docs/dns/faq.mdx#L126-L128)).

## 3. Several Records on One Name, and CNAME Chains

The proxy status of a name is all or nothing:

Source: [`src/content/docs/dns/proxy-status/index.mdx#L69`](https://github.com/cloudflare/cloudflare-docs/blob/ef83b95d4849c750001782e70ec04aee900e695c/src/content/docs/dns/proxy-status/index.mdx#L69), [`#L88`](https://github.com/cloudflare/cloudflare-docs/blob/ef83b95d4849c750001782e70ec04aee900e695c/src/content/docs/dns/proxy-status/index.mdx#L88)

```md
If you have multiple A or AAAA records on the same name and at least one of them is proxied, Cloudflare will treat all A or AAAA records on this name as being proxied.
```

```md
Cloudflare will also proxy a request if a hostname on a CNAME chain — where one CNAME record points to another — is proxied.
```

A CNAME into a zone of a different Cloudflare account is prohibited and fails with error 1014, "CNAME Cross-User Banned" ([`proxy-status/index.mdx#L116-L118`](https://github.com/cloudflare/cloudflare-docs/blob/ef83b95d4849c750001782e70ec04aee900e695c/src/content/docs/dns/proxy-status/index.mdx#L116-L118)).

## 4. Traffic the Proxy Cannot Carry

The proxy speaks HTTP and HTTPS on a fixed set of ports, 80, 8080, 8880, 2052, 2082, 2086, 2095 and 443, 2053, 2083, 2087, 2096, 8443 ([`network-ports.mdx#L15-L38`](https://github.com/cloudflare/cloudflare-docs/blob/ef83b95d4849c750001782e70ec04aee900e695c/src/content/docs/fundamentals/reference/network-ports.mdx#L15-L38)). Anything else needs a DNS-only record, or [Spectrum](https://developers.cloudflare.com/spectrum/):

Source: [`src/content/docs/dns/proxy-status/use-cases.mdx#L92`](https://github.com/cloudflare/cloudflare-docs/blob/ef83b95d4849c750001782e70ec04aee900e695c/src/content/docs/dns/proxy-status/use-cases.mdx#L92)

```md
Records used for FTP, SSH, RDP, game servers, or other non-HTTP protocols must be DNS-only. Cloudflare's proxy only handles HTTP and HTTPS traffic. Proxying these records routes the traffic to Cloudflare, which drops the non-HTTP connection.
```

So `ssh example.com` stops working the moment the apex record is proxied, while the website keeps working. Give such services their own DNS-only name (`ssh.example.com`), and note that this name publishes the origin IP.

Mail never goes through the proxy either:

Source: [`src/content/docs/dns/proxy-status/use-cases.mdx#L53-L57`](https://github.com/cloudflare/cloudflare-docs/blob/ef83b95d4849c750001782e70ec04aee900e695c/src/content/docs/dns/proxy-status/use-cases.mdx#L53-L57)

```md
MX records cannot be proxied. If an A or AAAA record is used exclusively for email (for example, `mail.example.com`), it should also be set to DNS-only.

Cloudflare does not proxy SMTP traffic on port `25` by default. Proxying a record that handles email traffic causes mail servers to connect to Cloudflare's IP addresses instead of your mail server. This prevents email delivery.

Use a dedicated hostname for email that is separate from your proxied web traffic hostname. If your MX record points to the same hostname as your website, Cloudflare [dynamically prepends](/dns/manage-dns-records/troubleshooting/unexpected-dns-records/#_dc-mx-and-dc--subdomains) `_dc-mx` to the hostname in the response for the MX record. This ensures that mail or service traffic bypasses the Cloudflare proxy and reaches your server directly.
```

Records that prove domain ownership to another service (a verification CNAME) must stay DNS-only too, and Cloudflare refuses to proxy some known DKIM and certificate-validation targets, such as `*.acm-validations.aws` ([`proxy-status/limitations.mdx#L20-L40`](https://github.com/cloudflare/cloudflare-docs/blob/ef83b95d4849c750001782e70ec04aee900e695c/src/content/docs/dns/proxy-status/limitations.mdx#L20-L40)).

## 5. Where the Origin IP Still Leaks

A proxied record hides the origin only while nothing else in the zone publishes it:

| Leak                                            | Why                                                                                                 |
| ----------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| A DNS-only record on the same address           | `mail`, `ssh`, `ftp` or `direct` records answer with the origin IP                                  |
| `MX` pointing at a proxied name                 | Cloudflare answers with `_dc-mx.<hash>.example.com`, whose `A` record is the origin IP              |
| The zone while it is still pending              | Proxied records are served DNS-only until activation                                                |
| An old zone of the same name in another account | Pending, moved and deleted zones keep answering on their own nameservers with the records they hold |

The `_dc-mx` record is generated at query time:

Source: [`src/content/docs/dns/manage-dns-records/troubleshooting/unexpected-dns-records.mdx#L62`](https://github.com/cloudflare/cloudflare-docs/blob/ef83b95d4849c750001782e70ec04aee900e695c/src/content/docs/dns/manage-dns-records/troubleshooting/unexpected-dns-records.mdx#L62)

```md
When your `MX` or `SRV` record resolves to a domain configured to [proxy](/dns/proxy-status/) through Cloudflare, Cloudflare dynamically inserts a record into DNS responses that resolves to the origin IP address. This record is added at query time to ensure that mail or service traffic bypasses the Cloudflare proxy and reaches your server directly.
```

Pending zones do not proxy, and they answer on their assigned nameservers' IPs, which anyone can query:

Source: [`src/content/docs/dns/proxy-status/limitations.mdx#L54-L58`](https://github.com/cloudflare/cloudflare-docs/blob/ef83b95d4849c750001782e70ec04aee900e695c/src/content/docs/dns/proxy-status/limitations.mdx#L54-L58)

```md
This means that DNS records — even those set to [proxy traffic through Cloudflare](#proxy-eligibility) — will be [DNS-only](/dns/proxy-status/#dns-only-records) until your zone has been activated and any requests to your DNS records will return your origin server's IP address.

If this warning is still present after 24 hours, refer to [Troubleshooting](/dns/troubleshooting/).

For enhanced security, we recommend rolling your origin IP addresses at your hosting provider after your zone has been activated. This action prevents your origin IPs from being leaked during onboarding.
```

An address that leaked once stays known, so a hidden origin also needs a firewall that accepts web traffic only from [Cloudflare's IP ranges](https://www.cloudflare.com/ips/) ([`cloudflare-ip-addresses.mdx#L53`](https://github.com/cloudflare/cloudflare-docs/blob/ef83b95d4849c750001782e70ec04aee900e695c/src/content/docs/fundamentals/concepts/cloudflare-ip-addresses.mdx#L53)).

## 6. Turning the Proxy On During a Migration

Cloudflare issues the Universal SSL certificate only after the zone is active, which takes 15 minutes to 24 hours:

Source: [`src/content/partials/ssl/universal-ssl-enable-full.mdx#L6-L8`](https://github.com/cloudflare/cloudflare-docs/blob/ef83b95d4849c750001782e70ec04aee900e695c/src/content/partials/ssl/universal-ssl-enable-full.mdx#L6-L8)

```md
For domains on a [primary setup (full)](/dns/zone-setups/full-setup/)[^1], your domain should **automatically** receive its Universal SSL certificate within **15 minutes to 24 hours** of domain activation[^2].

This certificate will cover your zone apex (`example.com`) and all first-level subdomains (`subdomain.example.com`), and is provisioned even if your records are DNS only. However, the certificate will only be presented if your domain or subdomains are [proxied](/dns/proxy-status/).
```

A record that is proxied from the first minute of activation can therefore serve HTTPS with no certificate. Cloudflare's own order is to start every record DNS-only and proxy them once the certificate is active and the origin accepts Cloudflare's IPs ([`minimize-downtime.mdx#L19-L56`](https://github.com/cloudflare/cloudflare-docs/blob/ef83b95d4849c750001782e70ec04aee900e695c/src/content/docs/fundamentals/performance/minimize-downtime.mdx#L19-L56)). The certificate covers one level of subdomains only: `dev.blog.example.com` needs a different certificate type before it can be proxied over HTTPS ([`ssltls-subdomains.mdx#L6-L8`](https://github.com/cloudflare/cloudflare-docs/blob/ef83b95d4849c750001782e70ec04aee900e695c/src/content/partials/dns/ssltls-subdomains.mdx#L6-L8)).
