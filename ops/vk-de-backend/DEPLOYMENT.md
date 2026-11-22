# Germany backend for rented VK TCP ingress — 2026-11-06

Scope: prepare the owner's new clean VPS `152.234.235.106`, NOT the existing
Finnish node, and connect the front supplied by the owner. No paid lease,
CDN, DNS, existing profiles/hosts, customers, subscriptions or balances were
changed. Existing bypass squads only received the new inbound; all previous
memberships were preserved.

## Backend and owner-provided front

- Backend destination for seller: **152.234.235.106:2443**.
- New profile: `076d8171-6e3c-401e-bf84-88520145dbfb`, `VK_HAPROXY_DE_235_106`.
- New node: `46ac00c9-937c-4f07-9ed4-f72956f6553c`, `node-vk-de-235.106`.
- Single VLESS RAW/TCP + REALITY inbound, tag `vk-de-235106-reality`.
- SNI and REALITY target: `login.vk.com`, `login.vk.com:443`.
- Fresh node-specific X25519 key and short ID; empty panel-managed clients.
- Owner supplied leased front `185.86.147.77:10016` forwarding to the above
  backend; screenshot says100 Mbit/s and active until2026-11-07 (UTC date only).
- New Host: `3c0cfecc-5888-4dd7-a38a-023d2edfc223`,
  `🇩🇪 Германия • ОБХОД VK`, staged disabled and then enabled after TLS checks.
- Front Host address/port are the leased endpoint, NOT backend152.234.235.106.
  Its node/control address remains152.234.235.106:2222; SNIlogin.vk.com,
  fingerprintchrome, ALPNh2, DEFAULT security (REALITY inherited from inbound).

The seller's public guide says HAProxy forwards TCP without terminating
REALITY/TLS. Thus the seller needs only the backend IP and VPN port, NOT the
Remnawave control port2222, root password or panel API key. SNI `login.vk.com`
was observed on the seller's recommended list. From the new VPS it passed
trusted TLS1.3/h2 checks; `cloud.vk.com` did not, and was not selected.

The VPS was confirmed clean Ubuntu24.04.4 with no services except SSH/resolver.
Initial ED25519 SSH key pinned on first connection:
`SHA256:DQeYwt4HgoRVIxmLnmh0QCG2xgmLTghtDbBx1FkVY7o`.

Docker is installed from Ubuntu packages. Pinned image:
`remnawave/node:3.4.1@sha256:0cdf386dd49f360fc885bb34bde21132e478e40f0deac62d616086ec0fa9257e`.
Its Xray26.7.28 accepted the exact candidate configuration. Panel readback
confirmed node Connected, not disabled, no lastStatusMessage and the expected
profile/inbound binding. Runtime TCP2443 and TCP2222 listeners and UFW rules
were read back. No authenticated VPN test or test subscriber was created.
On the VPS, strict TLS1.3/h2 through its public backend2443 successfully returned
the same302 as the direct target; aggregated recent node error counters were0.
The Mac's external keyless TLS probe timed out. Thus local/backend readiness
is confirmed, but independent reachability from the seller's forwarder and
end-to-end VPN/allowlist operation are not yet confirmed. Do not report those
as passed before receiving the front and testing the actual route.

## Network/security state

UFW default incoming deny. Public TCP22; control TCP2222 only from the resolved
panel IP `144.31.170.147`. During the controlled TLS front test, backend socket
readback showed HAProxy's actual source185.86.147.77. TCP2443 was then limited
to that source and the broad IPv4/IPv6 rule removed. Backup of prior rules:
`/root/vk-de-backend/ufw.before-front.txt`. If the provider changes its actual
egress/source address, verify and update this rule; do not guess based solely
on a new advertised front IP.

Strict TLS1.3/h2 tests from the VPS returned302/verify0 via both direct backend
and the leased front; the front test passed again after narrowing UFW2443.
Panel readback confirmed the host enabled and the node Connected/no error.
Mac probes to both endpoints timed out during TLS; this
is not an authenticated VPN success/failure result. Planned audience copies
the existing bypass host's three squads: SPN-Squad, DE, bypass. Only the new
inbound is appended, preserving other memberships; existing hosts/profiles
and subscriber records are unchanged. No automatic lease renewal was enabled.
The final panel call used a temporary localhost-only SSH forward18744 because
the Mac's direct panel TLS connection failed. Inner HTTPS domain/SNI validation
remained panel.wayspn.online and credentials stayed in the local process, not
on the VPS. The forward is closed with the temporary SSH session.

The default REALITY minimum client version gate (Xray26.3.27) is retained.
Lowering it was not approved and was not applied. Do not add minClientVer0.0.0
without separate owner approval. Older client compatibility and actual Vision
authentication must be checked once the front and intended subscription exist.

## Paths / next stage

Root-only deployment files: `/root/vk-de-backend` and `/opt/remnanode`.
Private local plan/intents/receipts: `/private/tmp/wayspn-vk-de-20261106`
(0700 directory, 0600 files; contains private keys, never publish).
Temporary SSH pin/socket directory: `/private/tmp/spn-vkde.ThYH5b`.
`ssh-session.exp` reads a hidden terminal password; no password is stored.

Next: owner client connectivity after subscription refresh, including actual
restricted-carrier testing if available.
Do not advertise bypass success on the basis of TCP or fallback TLS alone.
