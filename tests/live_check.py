"""Verify a real single-port listener, including SSE, without model calls."""
import os
from pathlib import Path
import subprocess
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parent.parent
port = 18009
with (ROOT / 'tests/live-server.log').open('w', encoding='utf-8') as log:
    process = subprocess.Popen([sys.executable, str(ROOT / 'start.py'), '--port', str(port)],
                               cwd=ROOT, stdout=log, stderr=log)
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=10, trust_env=False) as client:
            for attempt in range(100):
                if process.poll() is not None:
                    raise RuntimeError('Unified server failed to start; see tests/live-server.log')
                try:
                    response = client.get('/assistant/api/health')
                    if response.status_code == 200:
                        break
                except httpx.ConnectError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError('Unified server startup timeout')
            assert response.json()['persistenceReady'] is True
            assert client.get('/').status_code == 200
            assert client.get('/api/health').status_code == 200
            assert client.get('/assistant/').status_code == 200
            with client.stream('GET', '/assistant/api/events', params={'sid': 'merge-live-check'}) as stream:
                assert stream.status_code == 200
                assert stream.headers['content-type'].startswith('text/event-stream')
                assert next(stream.iter_lines()) == 'event: hello'
            assert client.post('/assistant/api/close', json={'sid': 'merge-live-check'}).status_code == 200
            print('Single-port HTTP, both applications, startup persistence and live SSE: PASS')
    finally:
        process.terminate()
        process.wait(timeout=15)
