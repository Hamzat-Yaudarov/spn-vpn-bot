# Admin-only automatic selection pilot

This is an additive pilot, not a replacement for existing automatic hosts.

- Name in the subscription: `🧪 Авто • тест`.
- Eight explicit gRPC/WS candidates on NODE29, PRESTIZH, CHISLO and TEKST.
- `leastPing`, HTTPS GET health checks, average interval 15s, sampling 1,
  probe timeout 3s. These are test settings, not a zero-interruption guarantee.
- Explicit `block` fallback; blackhole is also the default outbound.
- Existing intentional direct-domain and bittorrent rules are retained. This is
  **not** a claim that all traffic or DNS now goes through the VPN.
- Existing templates, hosts, profiles and node bindings are never overwritten.
- A new marker squad contains the existing virtual anchor inbound and only Admin
  (ID 2). `ALLOW_ONLY` gates the pilot. DE is not private enough for this pilot.
- Other existing Admin memberships, expiry and limits remain unchanged.
- No live subscriber configuration or authentication key is fetched or copied
  to a VPS. Generated validation outbounds are keyless placeholders.

## Deployment

Run `manage.py prepare` from the repository environment. It performs only GETs
and writes a private state directory outside Git. Review its candidate list.

Run `verify_runtime.py <state>/validation-keyless.json` on the authorized host
with the existing `remnanode` container. It compiles with the installed Xray and
runs localhost-only synthetic proxies to check fastest selection, failover and
blocking. It does not verify authenticated real-VPN connections. Temporary
Xray processes are stopped; production Xray is not restarted.

Copy the verifier's successful JSON report into the private state directory as
`validation-result.json`. Only then run `manage.py apply <state>`.

`manage.py audit <state>` verifies isolation and unchanged existing settings.
`manage.py disable <state>` disables only the owned pilot host as a safe rollback;
it does not delete users, squads or existing hosts.

## Client test

Update Admin's subscription in Happ/INCY, select the test host and test actual
websites/video. The client conducts measurements from its own network. Proxy
health-check latency is not bandwidth, gaming UDP latency or VPS CPU load.
Established connections do not migrate when the best candidate changes.
Confirm the client's Xray version and behavior before any public rollout.

Unit tests: `python3 -m pytest -q -p no:cacheprovider tests/test_autoselect_pilot.py`.
