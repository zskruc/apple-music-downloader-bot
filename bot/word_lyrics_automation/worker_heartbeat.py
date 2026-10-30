"""Finite job heartbeat; business progress is evaluated independently."""
from contextlib import contextmanager
import json
import threading
import time

from .io import atomic_write_json


@contextmanager
def job_heartbeat(path, *, stage, job_id, interval_seconds=20):
    stopped=threading.Event()
    def beat():
        while not stopped.wait(interval_seconds):
            try:
                value=json.loads(path.read_text())
                value.update(updated_at=time.time(),stage=stage,job_id=job_id)
                atomic_write_json(path,value)
            except (OSError,ValueError):
                # Never turn a heartbeat write failure into a fake success.
                # The independent healthcheck will detect the stale heartbeat.
                pass
    thread=threading.Thread(target=beat,name='lyrics-job-heartbeat',daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set();thread.join(timeout=2)
