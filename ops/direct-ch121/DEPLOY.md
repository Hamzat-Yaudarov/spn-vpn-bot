# Direct gRPC and WS on 179.254.109.121

Published on 2026-11-08 (Europe/Saratov). `ch121.wayspn.com` has a DNS-only A
record to `179.254.109.121`. No CDN, relay or TCP VPN endpoint was added.

- Remnawave profile `DIRECT_CH121`: `76b9408f-5a9d-4e00-89ed-f5e3109032ef`.
- Node `node-direct-ch121`: `fcc9f57f-8f66-4a75-b090-2762c0f962aa`.
- «🇨🇭 Швейцария • gRPC»: `a68764f9-662e-4598-94fc-2ba3afe0f4c8`.
- «🇨🇭 Швейцария • WS»: `b60a0566-a1e2-4142-b52a-94aa4f1e7d20`.

Both use VLESS over trusted TLS on 443; internal ports 10087/10088 are bound to
loopback. Host visibility and squad membership mirror the working FI pair
independently for each transport. Existing hosts were not edited. UFW permits
SSH/HTTP/HTTPS and restricts 2222 to the panel. The certificate renews via
Certbot; the deploy hook tests and reloads Nginx only for this lineage.

The new node was connected before publication. Public HTTPS health and gRPC
HTTP/2 passed, and both gRPC and WS carried authenticated HTTPS204 over physical
`en0` before they were enabled. `check_pair.py --run` also verifies the two
`node18253` endpoints with the protected owner-approved test export. No keys or
private configuration are committed here.
