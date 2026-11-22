# Finland deployment: 92.42.102.103

The owner requested gRPC, WS and one Yandex CDN XHTTP resource. No cascade,
franchise/database restoration or changes to existing VPN nodes are included.
`fi1.wayspn.com` was occupied and was **not changed**.

## Direct transports

- Origin: `fi103.wayspn.com`, DNS-only A to `92.42.102.103`.
- Profile: `8811b5b8-9608-4734-89b0-725372c96037` (`DIRECT_FI_102_103`).
- Node: `5c608352-00f2-44b8-83a9-97c2bcfa255f` (`node-direct-fi-102.103`).
- gRPC host: `3d366bb6-6ebb-4105-a4a3-9a0ad60457cc`, REALITY TCP2083,
  service `grpc`, fresh node-specific key, TLS fallback to Nginx443.
- WS host: `924c98ad-43e6-4b7b-abb5-ccfd37889ae4`, TLS443,
  `/direct/ws/`, upstream `127.0.0.1:10088`.
- Direct hosts enabled after node Connected, trusted TLS, valid WS101 and
  REALITY fallback TLS1.3/h2 checks. Source squad membership was mirrored.
- Direct-stage audit passed before the third inbound was appended:
  existing infrastructure unchanged, source access mirrored.

## Yandex stage

- CDN domain: `cdn-fi103.wayspn.com`.
- Resource: `bc8r4x5hnyfxk2ntrloa`, folder `b1gd0s9bmbop5deptg62`.
- One resource only; owner performed the paid creation clicks.
- Managed certificate: `fpqe3bm3mqaotug5v8l2` (`cdn-fi103-wayspn`), Issued.
- CNAME: `cdn-fi103` to `e2a9e51d3ba2edff.topology.gslb.yccdn.ru`.
- Renewal CNAME: `_acme-challenge.cdn-fi103` to
  `fpqe3bm3mqaotug5v8l2.cm.yandexcloud.net`; retain for renewal.
- Explicit HTTPS origin, Host and SNI all `fi103.wayspn.com`.
- CDN/browser cache, gzip, segmentation, shielding and log export disabled.
- Allowed methods: GET/HEAD/OPTIONS. TLS profile: TLS1.2+.
- Additional inbound: `67783002-921b-4201-bfe8-8ec88e8a3447`,
  `vless-xhttp-yandex-fi`, loopback10085, `/assets/app/`.
- CDN host: `e3b6e73d-9c3a-449a-b8b2-e4e313e81e44`.
- XHTTP copies the reviewed working DE11 packet-up GET/header transport:
  `X-Transport-Data`, maximum 2048-byte packets, server header limit8192.
- Trusted edge HTTPS returned health200 and XHTTP400 (expected without client
  session data); HTTP redirects to HTTPS with307. CDN host enablement followed
  those checks, with all three source squads bound.

## Server safety and recovery

Ubuntu24.04 packages provide Nginx, Docker and Certbot. Node image is pinned:
`remnawave/node:3.4.1@sha256:0cdf386dd49f360fc885bb34bde21132e478e40f0deac62d616086ec0fa9257e`.

UFW default-denies incoming traffic; public TCP22/80/443/2083, TCP2222
only from panel `144.31.170.147`. Upstreams10085/10088 listen on loopback.
Certbot renews the origin certificate automatically; its deploy hook checks
Nginx before reloading. Secrets and private validation files are root0600.

Remote staging: `/root/direct-fi`; node: `/opt/remnanode`.
Nginx backups: `/root/direct-fi/nginx-default.enabled.before`,
`/root/direct-fi/nginx-bootstrap.before-https.conf`, and
`/var/backups/wayspn-yandex-fi-origin-20261030` (exact pre-XHTTP backup).
Private local API receipts/snapshots are under
`/private/tmp/wayspn-direct-fi-20261030` and
`/private/tmp/wayspn-yandex-fi-20261030`; they contain secrets and must not
be published or committed.

After XHTTP is appended, the direct helper's exact-two-inbound audit is no
longer applicable. Use `ops/yandex-fi/manage.py audit` for the combined node;
it verifies the original two inbounds and existing infrastructure unchanged.

## Verification limits

Keyless checks validate routing, certificates and handshakes, not subscriber
authentication, throughput or availability on a Russian carrier's allowlist.
The Mac network can accept TCP handshakes through an intermediary even for
blocked ports; TCP-connect alone is not proof that a loopback service is
public. Server bind addresses and UFW rules were checked separately.

Authenticated three-transport probing is supported by `auth_probe.py`, using
only an existing active owner subscription and a pinned Xray binary. No
subscriber accounts were created, reactivated or modified for this operation.
The owner confirmed that all three new transports open sites in their client,
including the CDN host after refreshing subscription.

Final read-only reconciliation found the CDN host already enabled before this
helper's enable stage could write its intent. It was preserved, not toggled or
patched again. The strict combined audit passed structural comparisons through
the final enabled-state receipt check, then stopped because `intent-enable.json`
was absent. Do not fabricate a prior write intent or blindly repeat enable;
reconcile the externally enabled state explicitly before reusing that workflow.
