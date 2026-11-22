# DE4 parallel gRPC/TLS test — 2026-10-31

## Live result

- Host: `2120c218-cf25-4310-b91f-26e41e66968e`, `🇩🇪 Германия • gRPC 443 • ТЕСТ`.
- Node: `f87b14cf-438a-4c61-acc1-7522491c05ae`, `194.48.250.102`.
- Existing profile: `60714af0-bc08-44e5-bbb3-9aa0d2dedb53`.
- Added inbound: `7fe71969-4d33-4ed7-a1df-d3d8648a883c`, tag `ordinary-grpc-tls-de4-test`.
- Client: trusted TLS/h2 at `deeee1.wayspn.com:443`, gRPC service `grpc-de4-tls-test`.
- Nginx exact route `/grpc-de4-tls-test/Tun` forwards unchanged to `grpc://127.0.0.1:10089`.
- Existing three profile inbounds and three DE4 host IDs are retained. Only the new inbound was added to the node and the same `SPN-Squad`, `bypass`, `regular`, `DE` squads as the source gRPC host.
- No users or subscriptions were accessed or created; no payments, balances, CDN resources, DNS or certificates were changed.

The user enabled this host independently and reported that it works better than the old hosts. Read-only verification confirmed enabled state, expected host/profile settings, connected node and all four squad memberships. Keyless external checks returned HTTP/2 200 for the new gRPC service and the existing `/health`, with valid TLS. These checks do not independently authenticate a VPN session; that confirmation came from the user's client test. Long-term/carrier stability remains unproven.

## State and audit caveat

- Original private state: `/private/tmp/spn-grpc-tls-de4-20261031`.
- Corrected private state: `/private/tmp/spn-grpc-tls-de4-20261031-r2`.
- R2 preserves the original preimage/profile/node intents and records the rejected host-payload revision. `mihomoX25519` is a non-nullable bool; the corrected POST uses `false` and omits the return-only `mapper` field.
- One node PATCH response was lost. The node's unchanged baseline was confirmed by fresh reads and absence of the new listener before one explicitly recorded idempotent retry. The resulting binding was verified.
- There is deliberately **no agent enable intent**: the user enabled the host. Do not fabricate an intent or toggle the host off/on to satisfy the helper.
- Three unrelated Finnish hosts (`952a72a3-cf97-449a-9f1a-0a54c5bb8e9b`, `9ac73bc4-09c8-43b6-a5df-8fe4fc514443`, `ad9348e2-de1a-4d7a-bb29-7f96922fc3aa`) also differ from the initial whole-panel hashes. Their changes were not part of these scripted writes and were not reverted. Therefore the strict original whole-panel helper audit is **not claimed as passing**. Reconcile external changes before reusing a mutating stage.

## Nginx backup and rollback

- Site: `/etc/nginx/sites-available/deeee1.wayspn.com`.
- Backup: `/var/backups/spn-grpc-tls-de4-20261031` (0700; saved files 0600).
- Original SHA256: `2ec71132478467a7653130cd4c95ea73880238f58078da5fdefc7578f5af8a23`.
- Candidate SHA256: `c0fd52446edd8c429e2e1ade7da88f1fef30aed5158ea95ff1ca7bc2df61a40b`.
- `nginx_patch.py --rollback` requires the original SHA and refuses unrelated later site edits.
- Panel rollback must remove only this new host/inbound and its added memberships, after reconciling the independently enabled state. Existing hosts/profiles/nodes must not be deleted or restored from a stale whole-panel snapshot.

Local focused tests: 83 offline tests reported passing after the host DTO correction. No server-wide restart or dependency upgrade was performed.
