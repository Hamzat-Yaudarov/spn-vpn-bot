# Remnawave plan — no apply implementation

`panel_plan.py` never calls user/subscription endpoints or POST/PATCH/DELETE. Its
`snapshot` action uses only GET `/config-profiles`, `/hosts`, `/nodes`,
`/internal-squads` through the existing authenticated helper. It saves projected
metadata and config hashes, not raw server configs, keys, users or squad members.
No snapshot/API action was run as part of writing this helper.

Use separate new 0700 directories under `/private/tmp`:

```sh
python3 ops/de1-xhttp-cdn/panel_plan.py snapshot --out /private/tmp/wayspn-de1-panel-snapshot
python3 ops/de1-xhttp-cdn/panel_plan.py plan --snapshot /private/tmp/wayspn-de1-panel-snapshot/snapshot.json --cdn-domain YOUR-CDN-HOST.chislo.site --out /private/tmp/wayspn-de1-panel-plan
```

Planning refuses profile/node/address/host/CDN-endpoint collisions and requires
exactly one existing `DE` squad plus the selected DE11 source host/profile. It
preserves source ALPN/fingerprint, but never copies source credentials, template
assignments, profile/node IDs or broad injection selectors. XHTTP extras must
match the already-reviewed private origin bundle.

After separate review and CDN validation, the safe creation order is:

1. Re-GET infrastructure and check names/addresses/bindings against the snapshot.
   Do not retry a failed/ambiguous POST blindly: re-GET its dedicated selector.
2. POST only `profile.create.json`. Require exactly one returned inbound with
   tag `vless-xhttp-de1-timeweb`; obtain its actual UUID and profile UUID.
3. Privately obtain the panel node secret from GET `/keygen` (not a subscriber
   key). Write `/opt/remnanode/node.env` 0600 with `NODE_PORT=2222` and the returned
   `SECRET_KEY`; never print/paste the value. Pin/record the pulled 3.4.1 image.
4. Before starting the host-networked node, allow TCP 2222 only from panel
   `144.31.170.147`; retain all existing SSH/HTTP/TLS rules. Never allow public
   2222. Stop only the identified temporary loopback XHTTP probe to free 10085.
   Compose needs `network_mode: host`, `node.env`, log rotation; no TLS volume is
   needed because Nginx terminates TLS. Do not add plugin capabilities.
5. Resolve profile/inbound placeholders, POST the dedicated node; require
   `configProfile.activeConfigProfileUuid` and its sole `activeInbounds` UUID
   to match. Require Connected, enabled, no startup error before proceeding.
6. Resolve node/profile/inbound placeholders; POST the host **disabled**, with
   `internalSquads.mode=ALLOW_ONLY` and only the DE squad UUID. Do not set an
   existing Xray JSON template. `nodes` is a visual association, not access control.
7. Stop here for create-only work. `de-squad.later-plan.json` is a clearly marked
   later edit to the existing DE squad, not authorized/applied by this helper.
   Any test activation must append only this inbound while preserving old ones,
   leave other squads unchanged, then separately enable only this new host.

Schema gotchas: host `inbound` uses `configProfileUuid` +
`configProfileInboundUuid`; node uses `configProfile.activeConfigProfileUuid` +
`activeInbounds`. Returned profile inbound UUIDs are not the raw config tags.
`xhttpExtraParams` preserves the working DE11 host's representation (JSON string
or native object); the metadata snapshot records only its type, not its value.
The host public address/SNI/Host are the CDN delivery domain;
the origin TLS/SNI remains `de1.chislo.site`. CDN Host override to origin must be
configured independently. No REALITY, flow or client TLS insecurity is needed.

UUID/env placeholders intentionally fail real schema validation until resolved.
The optional dedicated injection fragment selects only the new host UUID; it is
not installed into any template. Broad existing `ALL`/regex selectors may still
include an enabled host, so inspect them before later activation. Disabled hosts
are filtered before normal subscriber injection.
