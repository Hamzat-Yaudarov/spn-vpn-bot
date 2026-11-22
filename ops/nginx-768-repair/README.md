# Scoped Nginx connection limit repair

The helper accepts only `191.217.245.225` (Poland) and `191.217.252.188`
(Finland), verifies the requested address is assigned locally, and refuses an
unexpected existing connection limit. Its default is a plan only:

```sh
python3 ops/nginx-768-repair/nginx_limits.py --target 191.217.245.225
python3 -m unittest discover -s ops/nginx-768-repair -p 'test_*.py'
```

After explicit approval, stream the script over an already authenticated,
strictly pinned SSH master. For Poland, `191.217.245.62` is the **old pin's
alias only**; the connection target must be `191.217.245.225`:

```sh
ssh -T -F /dev/null -S "$MASTER" -o ControlMaster=no \
  -o ProxyCommand=/usr/bin/false -o BatchMode=yes \
  -o HostKeyAlias=191.217.245.62 -o StrictHostKeyChecking=yes \
  root@191.217.245.225 'python3 - --target 191.217.245.225 --apply' \
  < ops/nginx-768-repair/nginx_limits.py
```

The script backs up the original and candidate at mode 0600 in a private
`/var/backups/spn-nginx-768-<IP>-*` directory, validates the original and staged
configuration, installs atomically, runs `nginx -t` again and gracefully
reloads. It checks that the replacement workers actually have the expected
file limit, and only restores its own unchanged candidate on failure. No
server reboot, Xray, firewall, route, DNS, account or panel edits.
