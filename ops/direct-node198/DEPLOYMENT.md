# Node 198 — deployment 2026-11-03/04

Scope: one new VPS `179.236.145.198`, gRPC, WS and one Yandex CDN
XHTTP resource. No existing nodes, customer accounts or subscription templates
are replaced. The owner confirmed the ED25519 SSH fingerprint in the VPS console:
`SHA256:wk67RsjxMGWzUyBfjDRjwqF0mCK7GD92amupLrKfkio`.

## New infrastructure

- Origin: `node198.wayspn.com`, DNS-only A to the new VPS.
- Edge: `cdn-node198.wayspn.com`, DNS-only CNAME to
  `e2a9e51d3ba2edff.topology.gslb.yccdn.ru`.
- Renewal CNAME: `_acme-challenge.cdn-node198.wayspn.com` to
  `fpq3sojl2aalfjaaa7uv.cm.yandexcloud.net` (retain for automatic renewal).
- Remnawave profile: `c4d69975-2c36-4944-9da9-9bf1cc9a1786`, `DIRECT_NODE198`.
- Remnawave node: `d8385b61-667d-4a99-8c5c-642c497bc354`.
- gRPC host: `45e27593-cc52-4b4b-9fb8-f0233adff81a`.
- WS host: `2f7910ea-37cc-4c66-a9d7-769a1952323f`.
- CDN XHTTP host: `c546f062-1c03-45d4-9ee1-865967f37c06`.
- Yandex resource: `bc8r22pnbft63mgvsbau`, folder `b1gd0s9bmbop5deptg62`.
- Managed CDN certificate: `fpq3sojl2aalfjaaa7uv`, `cdn-node198-wayspn`.

The owner created exactly one paid CDN resource in the prepared UI, with the
displayed 150 RUB/month base charge and metered traffic. Logs and origin
shielding were left disabled. Origin protocol, Host and explicit SNI are HTTPS
and `node198.wayspn.com`. Managed certificate status is Issued. HTTPS redirect
enabled; CDN/browser caching, gzip and segmentation disabled. Allowed methods:
GET, HEAD, OPTIONS.

## Transports and isolation

Configuration derives from the working FI profile's three inbounds. REALITY
keys/short ID are unique to this node; no customer credentials were copied.
gRPC REALITY listens on TCP2083 with own-domain SNI and local TLS fallback.
WS TLS443 uses `/direct/ws/`, upstream loopback10088. CDN XHTTP uses
`/assets/app/`, loopback10085, packet-up GET/header `X-Transport-Data` mode,
2048-byte maximum packets and server header limit8192. Nginx forwards without
URI rewriting or buffering. No direct connection is presented as CDN.

UFW default-denies incoming traffic; public TCP22/80/443/2083; TCP2222 is
allowed only from the resolved panel address `144.31.170.147`. No upstream
10085/10088 public listeners. Remnanode image:
`remnawave/node:3.4.1@sha256:0cdf386dd49f360fc885bb34bde21132e478e40f0deac62d616086ec0fa9257e`.

Origin certificate was issued after explicit owner permission, and renews with
Certbot plus a checked Nginx reload hook. Root-owned private node configuration
is in `/opt/remnanode`. Reversible Nginx bootstrap backups and installation
artifacts are in `/root/direct-node198`.

## Verification and current stage

Direct host verification: node Connected, trusted origin health200,
WS101 upgrade and gRPC REALITY fallback TLS1.3/h2 health200. Both direct hosts
enabled with the same four squads as their corresponding FI references.
These are infrastructure/handshake checks, not authenticated VPN throughput
or Russian carrier allowlist tests. No test subscriber was created.

CDN certificate is attached and Issued. Distribution was initially mixed, so
enablement was delayed until three consecutive strict TLS health200/XHTTP400
checks passed from the new VPS, 20 seconds apart. The CDN host is now enabled
in the same three squads as its FI reference. No certificate-validation bypass
was used. `manage.py publish-cdn` also checked the live target profile, inbound
identities, node connection and bindings before adding the host or appending
target inbounds to existing squad memberships. Panel readback confirmed the
host enabled and the node Connected, with no status error.
Final post-enable checks again returned the exact origin health JSON through
trusted edge HTTPS, XHTTP400 without session data, and HTTP307 to the same
HTTPS URL. On 2026-11-04 the owner confirmed that all three new servers work
in their client. This does not establish performance or availability on every
carrier/allowlist network.

The Mac's direct route to the panel later suffered intermittent TLS timeouts.
The final panel calls used `--panel-tunnel` through a temporary localhost-only
SSH forward (`127.0.0.1:18743` to `144.31.170.147:443`). The inner TLS kept
`panel.wayspn.online` hostname/SNI and full certifi validation. Panel credentials
remained in the local process and were not installed on the VPS. No redirects
were followed and no permanent tunnel/service was installed.

Local private creation intents/plan are in `/private/tmp/wayspn-node198-panel`
(0700 directory, 0600 files). They contain secrets: do not publish or commit.
No automatic deletion of these recovery artifacts is performed.
