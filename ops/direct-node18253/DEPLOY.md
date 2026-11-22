# Direct gRPC and WS on 48.102.182.53

Scope: `node18253.wayspn.com`, DNS-only A to `48.102.182.53`. No CDN, VK relay,
or changes to existing nodes. Pin SSH and confirm Ubuntu 24.04, no conflicting
services on 80/443/2222/10087/10088, and an SSH listener on TCP 22. If the
interface guard fails, stop and verify the host identity and private IP
before adapting it; do not disable it.

On the reviewed new VPS:

```sh
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  docker.io docker-compose-v2 nginx certbot ufw dnsutils
install -d -m 0700 /root/direct-node18253
```

Upload `common.sh`, `bootstrap.conf`, `nginx.conf`, `docker-compose.yml`,
`renew-nginx.sh`, `install-files.sh`, `firewall.sh`, `finish.sh` and the fresh
panel-generated `node.env`. Keep `node.env` root-owned 0600.

```sh
cd /root/direct-node18253
bash install-files.sh --apply
bash firewall.sh --apply
docker compose -f /opt/remnanode/docker-compose.yml pull
```

Once public DNS has converged and TCP 80 is reachable, obtain the certificate.
Registering a new account/accepting Let's Encrypt terms requires approval.
Existing accounts and unrelated lineages are left in place.

```sh
certbot certonly --non-interactive --webroot \
  -w /var/www/direct-node18253-acme --agree-tos \
  --register-unsafely-without-email -d node18253.wayspn.com
bash finish.sh --apply
```

The renewal hook checks and reloads Nginx only for this certificate.
`finish.sh` verifies UFW before starting the host-network node: SSH/80/443
public, 2222 only from the fixed panel IP. Cloud-side firewall policy must
permit the intended ingress without changing unrelated rules.

- gRPC: loopback10087, no inner TLS, single mode, `serviceName: spngrpc`;
  client TLS443 and ALPN h2. Exact Nginx route `/spngrpc/Tun`.
- WS: loopback10088, no inner TLS, `/direct/ws/`; client TLS443 and own SNI/Host.

Read back the new node's connected status, bindings, firewall and listeners
before publication. Test trusted TLS `GET /health` for HTTP 200 and the expected
JSON; check WS101 on `/direct/ws/` and HTTP/2 empty gRPC POST on `/spngrpc/Tun`
for HTTP 200 and gRPC status, not Nginx 404/502. Those checks do not prove VPN
authentication; an existing authorized client can refresh and verify both.

## Staged state — 2026-11-07

- Ubuntu packages, HTTP bootstrap, UFW, pinned image and the new node are
  installed. SSH key pinned privately. Panel node
  `68794b7f-9025-48fb-a0dc-ff0ae15c9974` connected with node3.4.1/Xray26.7.28.
- gRPC `b252f6ad-bc7e-40c7-92bc-5cd0ce6715fb` and WS
  `17f76442-50d9-47e0-8bdc-e06ae606b947` created disabled; their respective
  audience bindings were checked. No pre-existing host or user record changed.
- Direct Mac HTTP80 reached the expected bootstrap status503. On the VPS,
  Xray config test passed, loopback gRPC returned HTTP/2 200 and grpc-status0,
  loopback WS returned101, and outbound HTTPS returned204 from48.102.182.53.
- On 2026-11-08 the owner supplied the DNS-only A record and approved LE
  issuance. The certificate was installed without restarting the running node.
  Public HTTPS health and gRPC HTTP/2 passed, and both transports carried
  authenticated HTTPS204 over physical `en0`. Both hosts were then enabled.
- Existing VK185.86.147.77:10016 was not repointed. It passed an existing
  authenticated client test on en0; no separate lease mapping was confirmed.
