#!/usr/bin/env python3
"""SSH stdio transport on one physical Mac interface; does not alter routes."""
import argparse
import ipaddress
import os
import select
import socket
import sys
import re


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--ip', required=True)
    p.add_argument('--interface', required=True)
    args = p.parse_args()
    address = ipaddress.IPv4Address(args.ip)
    if (sys.platform != 'darwin' or str(address) != args.ip or not address.is_global
            or not re.fullmatch('[A-Za-z][A-Za-z0-9_.:-]{0,15}', args.interface)
            or args.interface.lower().startswith(('lo', 'utun', 'tun', 'tap'))):
        raise ValueError('Invalid physical SSH endpoint')
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
        stream.setsockopt(socket.IPPROTO_IP, 25, socket.if_nametoindex(args.interface))
        stream.settimeout(10)
        stream.connect((args.ip, 22))
        stream.settimeout(None)
        sources = [stream, sys.stdin.buffer]
        while stream in sources:
            readable, _, _ = select.select(sources, [], [], 30)
            for source in readable:
                if source is stream:
                    data = stream.recv(65536)
                    if not data:
                        return
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                else:
                    data = os.read(sys.stdin.fileno(), 65536)
                    if not data:
                        sources.remove(sys.stdin.buffer)
                        stream.shutdown(socket.SHUT_WR)
                    else:
                        stream.sendall(data)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyboardInterrupt):
        print('Physical SSH transport did not connect.', file=sys.stderr)
        raise SystemExit(2)
