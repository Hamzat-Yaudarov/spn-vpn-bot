# Guarded dedicated-object creation

`apply_panel.py` uses only infrastructure APIs. It never fetches users,
subscriptions or keys, and never runs SSH. Review the private plan before
running. Manifest directory must be a separate 0700 directory under
`/private/tmp`; its JSON files are 0600. Keep the original private plan: the
helper revalidates its exact single-origin config and unchanged random path.

Create/resume the dedicated profile and node, without creating a host or
changing any squad:

```sh
python3 ops/de1-xhttp-cdn/apply_panel.py create-core --plan-dir /private/tmp/wayspn-de1-timeweb-panel-plan --state-dir /private/tmp/wayspn-de1-timeweb-panel-state --confirm
```

Create/resume the new host **disabled**, bound only to those new objects and
restricted to the existing `DE` test squad. The technical domain is an exact
allowlist entry, not a provider wildcard. This command does not assert CDN
readiness or activate access:

```sh
python3 ops/de1-xhttp-cdn/apply_panel.py create-host --plan-dir /private/tmp/wayspn-de1-timeweb-panel-plan --state-dir /private/tmp/wayspn-de1-timeweb-panel-state --cdn-domain 1sd2ljvb74.cdn.twcstorage.ru --confirm
python3 ops/de1-xhttp-cdn/apply_panel.py audit --state-dir /private/tmp/wayspn-de1-timeweb-panel-state
```

Only after independent CDN DNS, trusted TLS and keyless origin-route checks,
and after confirming the dedicated node is healthy, the separate command may
append **only** its new inbound to `DE`, preserving every existing inbound, and
enable **only** the new host:

```sh
python3 ops/de1-xhttp-cdn/apply_panel.py enable-DE --state-dir /private/tmp/wayspn-de1-timeweb-panel-state --confirm --cdn-verified
```

`--cdn-verified` is an operator assertion; the helper does not probe the CDN.
Other squads, profiles, nodes, hosts and existing node bindings are hash-checked
before each write and must remain unchanged. Unrelated concurrent changes,
ambiguous dedicated selectors, moved bindings, or disappeared owned objects
stop the operation for review rather than being repaired automatically.

On a timeout or lost response, inspect `audit`, then rerun the **same** command
with the **same** state and plan. Fresh GETs recover already-created objects or
already-applied activation steps; no automatic blind POST retry is used. Do not
delete the state directory or regenerate the random path to retry. A stale
`manifest.next.json` intentionally stops writes; inspect both private files and
live GET state manually before recovering it. The helper does not delete or
roll back infrastructure automatically.

Offline tests do not read credentials or call live APIs:

```sh
PYTHONDONTWRITEBYTECODE=1 DATABASE_URL='' python3 -m unittest discover -s ops/de1-xhttp-cdn -p 'test_*.py'
```
