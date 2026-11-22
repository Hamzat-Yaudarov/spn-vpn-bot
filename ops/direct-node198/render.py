"""Render new-VPS artifacts from the reviewed Finland deployment."""
import runpy
import sys
from pathlib import Path

ops = Path(__file__).resolve().parents[1]
name, = sys.argv[1:]
assert name in {'nginx.conf', 'renew-nginx.sh', 'finish.sh', 'docker-compose.yml'}
raw = (ops / 'direct-fi' / name).read_bytes()
if name == 'nginx.conf':
    raw = runpy.run_path(str(ops / 'yandex-fi' / 'origin_patch.py'))['render'](raw)
raw = raw.replace(b'fi103.wayspn.com', b'node198.wayspn.com').replace(b'92.42.102.103', b'179.236.145.198')
raw = raw.replace(b'direct-fi', b'direct-node198').replace(b'DIRECT_FI', b'DIRECT_NODE198')
sys.stdout.buffer.write(raw)
