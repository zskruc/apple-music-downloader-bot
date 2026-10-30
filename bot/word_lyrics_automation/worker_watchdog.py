"""Recover a verified stale worker, never restart for dependency/input faults."""
import fcntl
import json
import os
from pathlib import Path
import signal
import time

from .io import atomic_write_json
from .service_health import ServiceHealthMonitor


def recovery_decision(report, worker, state, now, *, identity):
    updated=dict(state)
    if report.get('liveness_ok'):
        updated.update(consecutive_stale=0,term_pid=0,term_generation='')
        return 'none',updated
    if not report.get('restart_recommended') or worker.get('queue',{}).get('quick_check')!='ok':
        return 'inspect',updated
    pid=worker.get('pid')
    if type(pid) is not int or pid<=1 or not identity(pid):return 'inspect',updated
    generation=worker.get('_process_generation','')
    if state.get('term_pid')==pid and state.get('term_generation','')!=generation:
        updated.update(consecutive_stale=0,term_pid=0,term_generation='')
        return 'inspect',updated
    if state.get('term_pid')==pid and now-float(state.get('term_at',now))>=10:
        updated.update(last_restart_at=now,term_pid=0,consecutive_stale=0)
        return 'kill',updated
    if state.get('last_restart_at') and now-float(state['last_restart_at'])<900:
        return 'cooldown',updated
    count=int(state.get('consecutive_stale',0))+1
    updated['consecutive_stale']=count
    if count<2:return 'confirm',updated
    updated.update(term_pid=pid,term_generation=generation,term_at=now,last_restart_at=now)
    return 'terminate',updated


def process_generation(pid):
    try:
        root=Path('/proc')/str(pid)
        if b'word_lyrics_automation.source_worker' not in (root/'cmdline').read_bytes().split(b'\0'):
            return ''
        return (root/'stat').read_text().rsplit(')',1)[1].split()[19]
    except (OSError,IndexError):return ''


def worker_identity(pid):return bool(process_generation(pid))


def signal_still_safe(original,latest,*,now,max_age,expected_generation,actual_generation):
    try:
        return (original.get('pid')==latest.get('pid') and original.get('owner')==latest.get('owner')
                and expected_generation and expected_generation==actual_generation
                and now-float(latest.get('updated_at') or 0)>max_age)
    except (TypeError,ValueError):return False


def main():
    root=Path(os.environ.get('AMDL_WORD_LYRICS_STATE_DIR','/state/word-lyrics'))/'automation'
    monitor=ServiceHealthMonitor.from_environment(root)
    report=monitor.update(run_probes=False)
    try:worker=json.loads((root/'source-worker-health.json').read_text())
    except (OSError,ValueError):worker={}
    with (root/'worker-watchdog.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        path=root/'worker-watchdog.json'
        try:state=json.loads(path.read_text())
        except (OSError,ValueError):state={}
        worker['_process_generation']=process_generation(worker.get('pid'))
        action,updated=recovery_decision(report,worker,state,time.time(),identity=worker_identity)
        if action in {'terminate','kill'}:
            # Recheck identity immediately before signalling; stale/reused
            # health-file PIDs must never target an unrelated process.
            try:latest=json.loads((root/'source-worker-health.json').read_text())
            except (OSError,ValueError):latest={}
            if signal_still_safe(worker,latest,now=time.time(),max_age=monitor.worker_max_age,
                                 expected_generation=worker['_process_generation'],
                                 actual_generation=process_generation(worker['pid'])):
                try:os.kill(worker['pid'],signal.SIGTERM if action=='terminate' else signal.SIGKILL)
                except ProcessLookupError:pass
            else:
                action='cancelled_after_fresh_heartbeat_or_generation_change'
                updated.update(consecutive_stale=0,term_pid=0,term_generation='')
        atomic_write_json(path,updated)
    print(json.dumps({'liveness_ok':report.get('liveness_ok'),
                      'business_status':report.get('status'),'recovery_action':action}))
    return 0 if report.get('liveness_ok') else 1


if __name__=='__main__':raise SystemExit(main())
