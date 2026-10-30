"""One bounded policy check invoked by the existing NAS maintenance timer."""
import json
from pathlib import Path
import httpx
import yaml

from lyrics_network_policy import ensure_policy, controller_callbacks


def main():
    config=Path('/policy/config.yaml')
    # This trusted one-shot container receives no Telegram/account secrets.
    # The controller secret remains only in memory and private config backups.
    document=yaml.safe_load(config.read_text())
    transport=httpx.HTTPTransport(uds='/var/run/docker.sock')
    with httpx.Client(transport=transport,base_url='http://docker',timeout=35) as docker:
        def validate(candidate):
            relative=candidate.relative_to('/policy')
            response=docker.post('/containers/mihomo/exec',json={
                'AttachStdout':True,'AttachStderr':True,
                'Cmd':['/mihomo','-t','-d','/root/.config/mihomo',
                       '-f','/root/.config/mihomo/'+str(relative)]})
            response.raise_for_status();identity=response.json()['Id']
            response=docker.post('/exec/'+identity+'/start',json={'Detach':False,'Tty':False})
            response.raise_for_status()
            response=docker.get('/exec/'+identity+'/json');response.raise_for_status()
            if response.json().get('ExitCode')!=0:raise RuntimeError('candidate validation failed')
        reload_config,flush=controller_callbacks('http://127.0.0.1:9090',
            '/root/.config/mihomo/config.yaml',str(document.get('secret') or ''),timeout_seconds=10)
        result=ensure_policy(config,'/policy/maintenance/lyrics-network-policy',mode='ensure',
            validate=validate,reload_config=reload_config,flush_dns=flush)
    print(json.dumps(result,sort_keys=True))
    return 0 if result.get('passed') else 1


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        # Exception strings may contain control URLs. Report only fixed codes.
        print(json.dumps({'passed':False,'code':getattr(exc,'code','policy_check_failed'),
                          'error_type':type(exc).__name__}))
        raise SystemExit(1)
