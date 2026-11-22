#!/usr/bin/env python3
"""Read-only actual rw-core FD health, scoped to the owned remnanode container."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys


class HealthError(ValueError):
    pass


def need(condition, message):
    if not condition:
        raise HealthError(message)


def command(*args):
    result = subprocess.run(args, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=8,
                            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
    need(result.returncode == 0, 'Owned container health command failed; details suppressed')
    return result.stdout.strip()


def process_start(raw):
    # Linux /proc/PID/stat field22, allowing parentheses/spaces in comm.
    fields = raw.rsplit(')', 1)[-1].split()
    need(len(fields) >= 20 and fields[19].isdigit(), 'Cannot establish rw-core process identity')
    return fields[19]


def read_core(pid, *, proc=Path('/proc'), expected=65536):
    directory = Path(proc) / str(pid)
    try:
        first = process_start((directory / 'stat').read_text())
        need((directory / 'comm').read_text().strip() == 'rw-core', 'Selected process is not rw-core')
        limits = re.findall(r'^Max open files\s+(\d+)\s+(\d+)\s+files\s*$',
                            (directory / 'limits').read_text(), re.M)
        need(len(limits) == 1, 'Actual rw-core nofile limits are unavailable')
        soft, hard = map(int, limits[0])
        used = len(os.listdir(directory / 'fd'))
        last = process_start((directory / 'stat').read_text())
        need(first == last and (directory / 'comm').read_text().strip() == 'rw-core',
             'rw-core process changed during FD verification; retry')
    except OSError:
        raise HealthError('rw-core process disappeared or FD information is unreadable; retry') from None
    need(soft == expected and hard == expected, 'Actual rw-core soft/hard nofile limits differ from the owned configuration')
    free = soft - used
    summary = {'corePid': pid, 'fdInUse': used, 'softLimit': soft, 'hardLimit': hard,
               'fdFree': free, 'usedPercent': round(100 * used / soft, 1)}
    need(used * 5 < soft * 4 and free >= 1024,
         f'rw-core FD pressure: {used}/{soft} used, {free} free; verification failed, publication not attempted')
    return summary


def inspect(deployment, *, run=command, proc=Path('/proc'), expected=65536):
    need(re.fullmatch(r'[0-9a-f]{32}', deployment) is not None, 'Invalid deployment identity')
    owner = run('docker', 'inspect', '-f', '{{index .Config.Labels "spn.deploy.id"}}', 'remnanode')
    need(owner == deployment, 'Container does not belong to this deployment')
    rows = run('docker', 'top', 'remnanode', '-eo', 'pid,comm').splitlines()
    pids = [int(fields[0]) for row in rows if len(fields := row.split()) == 2
            and fields[0].isdigit() and fields[1] == 'rw-core']
    need(len(pids) == 1, 'Expected exactly one actual rw-core process in the owned container')
    return read_core(pids[0], proc=proc, expected=expected)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deployment', required=True)
    args = parser.parse_args(argv)
    need(sys.platform.startswith('linux') and os.geteuid() == 0, 'Run on the Linux VPS as root')
    print(json.dumps(inspect(args.deployment), sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except (HealthError, OSError, subprocess.SubprocessError) as error:
        print('Stopped: ' + str(error), file=sys.stderr)
        raise SystemExit(2)
