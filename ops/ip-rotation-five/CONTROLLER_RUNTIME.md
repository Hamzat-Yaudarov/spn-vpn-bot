# Five-VPS automatic IPv4 replacement

Existing controller: `185.9.24.53`. No additional VPS, panel, DNS, CDN or VK
resources are included. `spn_ip_failover/five_scope.py` pins the five ordinary
host UUIDs, inbound UUIDs, nodes and hosting services. Sweden starts at the
user-confirmed `177.3.250.179`.

Shared limits: **3,000 RUB gross per Saratov calendar day**, **300 RUB/month per
IP**, at most **10 purchase attempts per VPS** until explicit operator reset.
Refunds never replenish the gross daily allowance. Unknown order results stop
further orders rather than repeat the charge. Current accepted/old addresses
are not deleted until a tested replacement is published and confirmed.

## Installation on the already prepared RF controller

Upload the current `spn_ip_failover` source package and the already used
`ops/host-health-audit/e2e.py` to `/opt/wayspn` without changing private state,
keys or credentials. Then, as root:

```sh
cd /opt/wayspn
/opt/wayspn/venv/bin/python -B -m spn_ip_failover.five_runtime install
```

Installation writes three private mode0600 runtime configs, a financial policy
and **disabled** `spn-ip-failover-five.service`. It does not create credentials,
purchase an IP, start VPN/network services or enable the controller. Repeating
it with the same inputs is harmless; a plan change after first use is rejected.
The separate existing read-only health timer remains unchanged.

Management RPC goes through the five scoped reverse SSH listeners on RF
loopback ports `23001..23005`, using their exact service aliases and RF private
keys. Outside checks use the separate, non-root `spn-ip-relay` accounts of the
selected Estonian and German VPSes, through `23002` and `23005`. They resolve
the current public IP from the panel for ownership and host-key identity; a
public SSH block does not break the reverse transport. Client subscriptions,
Xray and all private keys stay on RF.

## Readiness without enabling purchases

Use a transient service under `spn-failover` with the same three named
`LoadCredential=` entries and environment as `UNIT_TEXT` in `five_runtime.py`.
Run `/bin/sh /opt/wayspn/spn_ip_failover/deploy/run-five-controller.sh readiness`.
It refreshes only selected Admin routes, checks all five hosting/RPC bindings,
then performs fresh direct-RF and independent outside VPN probes. `unknown`
never authorizes a purchase. This command does not bypass the paid enable gate.

## Financial enable gate

Before enabling, the live one-order pilot must establish the actual 1CENT
receipt schema. The allocator must use receipt-correlated ownership, not only
an inventory delta, and only then declare
`OneCentBilling.RECEIPT_CORRELATED_OWNERSHIP = True`.

The private runtime-owned proof file is
`/var/lib/spn-ip-failover/priced-pilot-proof.json`, exact fields:

```json
{
  "version": 1,
  "deployment": "276abbcb4f334b58ab25d9b2adc9d55c",
  "service_id": "<one of the selected service UUIDs>",
  "request_id": "five-vps-pilot-20261116-001",
  "ip": "<the receipt-correlated test IPv4>",
  "response_sha256": "<SHA256 of the whole private response.json file>",
  "intent_sha256": "<SHA256 of the whole private intent.json file>",
  "dispatch_sha256": "<SHA256 of the whole private dispatch.json file>"
}
```

Artifacts live in `onecent-pilot-20261116` under the existing runtime state.
The matching order entry must contain the same `receipt_sha256`, `ip` and
`request_id`, with phase `released`; `releases[ip]` must also be `released`.
The shared budget must have the matching confirmed gross charge. No unresolved
order or reservation is accepted. Fresh hosting inventory must prove the test
IP was removed. A `ready: true` file cannot satisfy these requirements.

After the proof is verified:

```sh
cd /opt/wayspn
/opt/wayspn/venv/bin/python -B -m spn_ip_failover.five_runtime enable
```

The exact installed service performs the full financial and fresh network
preflight under its real account/credentials before its controller process
starts; only a successfully active process is enabled at boot.

## Status and stop

```sh
cd /opt/wayspn
/opt/wayspn/venv/bin/python -B -m spn_ip_failover.five_runtime status
/opt/wayspn/venv/bin/python -B -m spn_ip_failover.five_runtime stop
journalctl -u spn-ip-failover-five.service -n 30 --no-pager
```

`stop` disables boot startup too. It does not cancel an already submitted
hosting order; never erase its durable order/budget journals. The controller's
transaction journal is `/var/lib/spn-ip-failover/rotation` and cannot be reset
by reinstalling the package.
