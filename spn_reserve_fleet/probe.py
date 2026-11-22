"""A bounded SSH banner check, without passwords, ICMP, or VPN traffic tests."""
import re
import socket
import time

from .store import public_ipv4


def check_ssh(ip, port=22, timeout=5):
    public_ipv4(ip)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('Неверный SSH-порт')
    deadline = time.monotonic() + timeout
    try:
        with socket.create_connection((ip, port), timeout=timeout) as connection:
            data, total = b'', 0
            while total < 8192:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                connection.settimeout(remaining)
                chunk = connection.recv(min(1024, 8192 - total))
                if not chunk:
                    return False
                data += chunk
                total += len(chunk)
                while b'\n' in data:
                    line, data = data.split(b'\n', 1)
                    if re.fullmatch(rb'SSH-(?:2\.0|1\.99)-[^\x00-\x1f\x7f]{1,240}\r?', line):
                        return True
                    if line.startswith(b'SSH-'):
                        return False
    except (OSError, TimeoutError):
        return False
    return False
