# Curated standalone staging package

This is a deployment input, not a service installer or production release.
Building or unpacking it never configures the runtime, connects to PostgreSQL,
registers a Telegram webhook, accepts payment, runs SQL or starts a service.
The runtime remains **disabled** without an explicit protected configuration.

## Build locally

```sh
python3 scripts/partner_package.py --output /private/tmp/spn-partner-staging.tar.gz
python3 -m unittest discover -s tests -p test_partner_package.py
```

An existing output is never overwritten. Use a new output filename for a later
build. The archive has mode 0600 and contains only regular files below the
single `spn-partner-staging/` prefix. Owners, file modes and timestamps inside
the archive are normalized; source data determines its SHA-256.

## Exact manifest and exclusions

The complete filename allowlist is `PACKAGE_FILES` in
`scripts/partner_package.py` in the source checkout. There is no wildcard or
recursive directory copy. Each archive additionally contains
`PACKAGE_MANIFEST.json`, listing every shipped source file with byte count and
SHA-256, so the actual delivered file set can be audited without the checkout.
That manifest contains no machine paths or configuration values.

The curated payload consists of:

- Nine `partner_*_web.py`/`partner_web.py` HTTP composition modules.
- The explicit runtime and interactive setup scripts, not other operator tools.
- The standalone `services/partner_*.py` modules, excluding `partner_addons.py`,
  plus `services/__init__.py` and the pure `services/telegram_auth.py` helper.
- Six partner workspace/subscription HTML, CSS and JavaScript assets.
- Explicit partner SQL migrations, disabled public JSON templates, Nginx/systemd
  **examples**, the minimal requirements below, and relevant partner ops docs.

It excludes `.env` (including examples), `.release-secrets`, `.git`, caches,
private keys, databases, uploaded brand data, main-SPN configuration/database,
main bot/handlers, other VPN operations, release artifacts and development tests.
Source symlinks, hardlinks, unsafe archive names, missing files and detectable
private keys/bot tokens/password-bearing PostgreSQL DSNs fail the build before
an output is created. Credential scanning is defense in depth, not permission
to add arbitrary files: the exact reviewed allowlist remains the boundary.

The standalone composition explicitly injects `StagingAddonCatalog` into both
quote and summary loaders. Lazy main-SPN adapters retained in shared
`partner_catalog.py` and `partner_web.py` are not used by that composition;
therefore `config.py`, `database.py`, `services/partner_addons.py`, discount and
device-addon helpers are intentionally absent. Do not call the main-SPN
adapters from the standalone package.

## After an explicitly authorized transfer

Inspect `tar -tzf` first, then unpack into a new empty deployment directory.
Keep credentials outside the package and deployment directory. Python 3.11+
is required. Install only `ops/partner-platform/requirements-staging.txt` in a
dedicated virtual environment; it is a compatibility specification, not a
fully frozen dependency lock. Dependency installation is a separate explicit
network operation and the package contains no wheels or virtual environment.

These commands are offline and do not require a configuration file:

```sh
python scripts/partner_runtime.py --check
python scripts/partner_setup.py --help
```

The first must report `mode: disabled`, `runtime_disabled` and
`production_ready: false`; it does not discover `.env`. Help does not prompt
for credentials. Interactive `partner_setup.py --config <private-new-path>` is
a **separate** operator action; it creates a disabled, owner-only JSON without
connecting to anything. Never include its output in a later archive.

Nginx/systemd files are examples only. No package command installs or enables
them. Service creation, TLS/DNS, a protected configuration, database probing and
any capability enablement each require separate operator review. Keep HTTP on
loopback, retain the manager allowlist, and do not enable sales, worker,
webhooks or provisioning merely because the package passed its offline check.

See [SECURE_CONFIG.md](SECURE_CONFIG.md) for protected configuration and
[RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md) for staging/release gates.
