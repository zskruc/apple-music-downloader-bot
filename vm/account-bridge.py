#!/usr/bin/env python3
"""Expose the selected external wrapper session only on the private stack network."""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path('/opt/apple-wrapper/data/wrapper/rootfs/data/instances')


class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        pass  # Never log account tokens or request bodies.

    def do_GET(self) -> None:
        instances = sorted(p for p in ROOT.glob('*') if p.is_dir())
        payload: dict[str, str] = {'error': 'exactly one authenticated instance is required'}
        status = 503
        if len(instances) == 1:
            try:
                music = (instances[0] / 'MUSIC_TOKEN').read_text().strip()
                dev_path = instances[0] / 'DEV_TOKEN'
                dev = dev_path.read_text().strip() if dev_path.exists() else ''
                if music:
                    status, payload = 200, {'music_token': music, 'dev_token': dev}
            except OSError:
                pass
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('content-type', 'application/json')
        self.send_header('cache-control', 'no-store')
        self.send_header('content-length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', 30020), Handler).serve_forever()
