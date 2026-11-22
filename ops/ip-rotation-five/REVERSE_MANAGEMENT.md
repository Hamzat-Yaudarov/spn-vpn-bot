# Reverse management for the five selected normal nodes

Status: local implementation and tests only. This document is not evidence of
installation, a live tunnel, permission to deploy, or permission to enable paid
IP replacement. Bypass/CDN/VK nodes are outside the scope.

## Why this is separate from the reachability probe

A VPS whose IPv4 is unreachable from the RF controller cannot be repaired via
ordinary RF-to-guest SSH. The guest can instead initiate an outbound SSH
connection to RF. It exposes its own SSH daemon only at a fixed RF loopback
port. This works only if the guest-to-RF path itself is available; bidirectional
blocking or loss of RF Internet still requires another management path.

No existing network/VPN service is restarted. The only new guest service is
`spn-ip-mgmt-client.service`. On RF, the SSH service is syntax-checked and
reloaded, never restarted. The existing root SSH policy is compared before
and after staging and reload.

## Scope and isolation

The five service UUIDs and ports 23001–23005 are fixed in
`reverse_management_model.py`; no port/address is accepted from a caller.

* RF account: `spn-ip-mgmt`; no password, sessions, PTY, shell, local forwarding,
  Unix-socket forwarding, agent, X11, tunnel device, or public listener.
* Each guest has a separate Ed25519 key. RF authorizes only that guest's one
  loopback listener with `permitlisten`. It does not pin the changing guest IP.
* Tunnel private keys are generated and remain on each guest. Only their public
  halves reach RF. Existing RPC private keys remain on RF.
* Guest client: unprivileged `spn-ip-mgmt-client`, pinned RF host key, reconnecting
  systemd service, no automatic host-key replacement.
* Existing root RPC authorization changes only from source `185.9.24.53` to
  `185.9.24.53,127.0.0.1`. `restrict` and the fixed isolated Python RPC command
  remain. Unrelated root keys are byte-for-byte preserved.
* Gateway retains its logical `{address}` argument and stable service-UUID
  host-key alias, while SSH uses `HostName=127.0.0.1` plus the fixed service port.
  Hosting-inventory and Remnawave-address checks are not bypassed.

## Reviewed deployment sequence

`install_reverse_management.py` defaults to a no-I/O plan. Its three apply roles
are root-local Linux operations, not an automatic Mac-side deployment tool.
The four adjacent source files (this installer, model, `install_eu_relays.py`,
`install_guest_keys.py`) must be staged in a root-owned, non-writable directory;
apply refuses untrusted source ancestry, symlinks, and writable source files.
Each JSON request is bounded to 16 KiB and accepts public keys only.

Before deployment, re-confirm the live panel/hosting IP and the existing guest
network journal for each service. Stop on drift. In particular, the Sweden
service must not reuse its old-primary journal without the separate reviewed
migration. Confirm real RF sshd Include/Match layout and both directions of
connectivity; effective-policy checks must not be replaced with assumptions.

1. On the selected guest, `guest-prepare`: request `service_id, apply:true`.
   Creates the dedicated owned account and guest-local key, but does not start
   a client. Output contains its public key and fixed port.
2. On RF, `rf-register`: request `service_id, public_key, apply:true`.
   Stages the scoped Match policy, verifies its effective restrictions and
   unchanged root settings, reloads SSH, then publishes the public key. Output
   includes RF's **public** host key and the registered guest-key hash.
3. On the same guest, `guest-activate`: request `service_id,
   controller_rpc_public_key, registration:<step-2-output>, apply:true`.
   Verifies the guest-key binding, pins the RF host key, validates the new unit,
   upgrades only the pre-existing fixed RPC key, enables/starts only that unit.
   The response deliberately says `tunnel_verified:false`.
4. From RF as `spn-failover`, use the existing RF-only guest RPC private key and
   pinned guest host-key file via its loopback port. Require the exact existing
   RPC `check` response `{"ok":true}` and confirm a loopback-only listener.
   A socket connection or systemd's `active` status is not sufficient.
5. Only after the per-service check succeeds, publish its `gateway-plan` SSH
   argument list into the separately reviewed gateway configuration. Run a
   read-only gateway `check` and inspect hosting/panel scope before enabling
   anything financial. No gateway application code change is required.

CLI form (payloads are passed via stdin by the reviewed setup transport):

```sh
python3 -I /root/REVIEWED-DIRECTORY/install_reverse_management.py \
  --role guest-prepare --service SERVICE_UUID --apply
```

Do not print or transfer any private key. Do not expose ports 23001–23005 in
the firewall, replace guest host-key pins, delete journals, or restart SSH,
networking, Nginx, Xray, or Docker as part of this setup.

## Recovery and compatibility

Owned key/account metadata, immutable intent/backups, fixed policy receipts,
and an RF registry fence repeats and interrupted publication. A lost reply
after authorized_keys commit but before registry commit can be resumed for the
same service/key. Another registration remains blocked until reconciliation.
Completed keys/policy that were removed or modified fail closed. A failed
`useradd` reply before its owned account receipt is ambiguous and requires
manual inspection; no existing account is silently adopted.

The old `install_guest_keys.py` accepts only the original RF-only source
restriction. **Do not rerun that installer after this migration without an
explicit compatibility update.** It must not append another authorization or
weaken/revert the loopback restriction. New client keys are not regenerated
after a completed preparation if deleted; that requires explicit re-enrollment.

The setup installer does not implement automatic uninstall. Rollback must be
reviewed against the root-only snapshots and current registry. Stop the new
client and remove only that service's owned RF line before removing any policy.
Never remove another service's key or stop the shared SSH daemon.

Tests: `python3 -m unittest discover -s tests -p 'test_ip_rotation_reverse*.py'`.
