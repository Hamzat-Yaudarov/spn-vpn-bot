"""Fixed SSH TCP connector bound to macOS en0, independent of a VPN route."""
import os
import select
import socket
import sys

assert sys.platform == 'darwin'
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.IPPROTO_IP, 25, socket.if_nametoindex('en0'))  # Darwin IP_BOUND_IF
sock.settimeout(10)
sock.connect(('177.3.250.75', 22))
sock.settimeout(None)
sources = [sock, sys.stdin.buffer]
while sock in sources:
    ready, _, _ = select.select(sources, [], [], 30)
    for source in ready:
        if source is sock:
            data = sock.recv(65536)
            if not data:
                sources.remove(sock)
                break
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        else:
            data = os.read(sys.stdin.fileno(), 65536)
            if not data:
                sources.remove(sys.stdin.buffer)
                sock.shutdown(socket.SHUT_WR)
            else:
                sock.sendall(data)
sock.close()
