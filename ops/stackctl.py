#!/usr/bin/env python3
"""Local, stack-scoped maintenance; never installs a scheduler or touches other projects."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
SERVICES = frozenset({
    'legacy-wrapper', 'wrapper', 'youtube-pot', 'bot',
    'word-lyrics-resolver', 'word-lyrics-nas-aligner',
    'youtube-auth-browser', 'bilibili-auth-browser',
    'wrapper-watchdog', 'bot-watchdog',
    'lxserver', 'sqmusic-server', 'sq-mysql', 'sq-neteasecloudmusicapi',
})
PROFILE_FILES = {
    'lx': 'services/lx-music/compose.yaml',
    'sq': 'services/sq-music/compose.yaml',
}
PROFILE_SERVICES = {
    'auth': ['youtube-auth-browser', 'bilibili-auth-browser'],
    'lyrics': ['word-lyrics-resolver', 'word-lyrics-nas-aligner'],
    'lx': ['lxserver'],
    'sq': ['sq-mysql', 'sq-neteasecloudmusicapi', 'sqmusic-server'],
}
PRIVATE_TARGETS = {'/state', '/session', '/data', '/config', '/server/data', '/var/lib/mysql',
                   '/youtube-auth-browser', '/bilibili-auth-browser',
                   '/run/secrets/lx-frontend-auth', '/run/secrets/sq-auth.json'}
WRITERS = {'bot', 'wrapper', 'legacy-wrapper', 'word-lyrics-resolver',
           'word-lyrics-nas-aligner', 'youtube-auth-browser', 'bilibili-auth-browser'}
STOP_ORDER = {'wrapper-watchdog': 0, 'bot-watchdog': 0, 'bot': 1,
              'word-lyrics-resolver': 2, 'word-lyrics-nas-aligner': 2,
              'youtube-auth-browser': 3, 'bilibili-auth-browser': 3,
              'wrapper': 4, 'youtube-pot': 5, 'legacy-wrapper': 6,
              'lxserver': 4, 'sqmusic-server': 4, 'sq-neteasecloudmusicapi': 5, 'sq-mysql': 6}


def stop_order(services: list[str]) -> list[str]:
    return sorted(services, key=lambda service: (STOP_ORDER[service], service))


def compose_base(args: argparse.Namespace) -> list[str]:
    command = ['docker', 'compose', '--project-directory', str(ROOT),
               '--env-file', str(ROOT / '.env'), '-f', str(ROOT / 'compose.yaml')]
    if args.self_heal:
        command += ['-f', str(ROOT / 'compose.self-heal.yaml'), '--profile', 'self-heal']
    if getattr(args, 'auth_refresh', False):
        command += ['-f', str(ROOT / 'compose.auth-refresh.yaml')]
    for profile in dict.fromkeys(args.profile):
        if profile in PROFILE_FILES:
            command += ['-f', str(ROOT / PROFILE_FILES[profile])]
        command += ['--profile', profile]
    return command


def run(args: list[str], *, capture: bool = False) -> str:
    result = subprocess.run(args, cwd=ROOT, text=True, check=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout if capture else ''


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(payload, indent=2) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)


class Stack:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.base = compose_base(args)
        # config is deliberately captured: it contains expanded secret values.
        self.config = json.loads(run(self.base + ['config', '--format', 'json'], capture=True))
        self.project = self.config['name']
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', self.project):
            raise RuntimeError('Invalid Compose project name')
        bot_mounts = self.config['services']['bot']['volumes']
        state = next(v['source'] for v in bot_mounts if v['target'] == '/state')
        self.state = Path(state)
        self.active = self.state / 'ops' / 'images.compose.json'
        self.command = self.base + (['-f', str(self.active)] if self.active.exists() else [])

    def validate_access(self) -> None:
        settings = self.config['services']['bot']['environment']
        if settings.get('TELEGRAM_BOT_TOKEN') and not str(settings.get('TELEGRAM_ALLOWED_CHAT_IDS') or '').strip():
            raise RuntimeError('TELEGRAM_ALLOWED_CHAT_IDS must be set when TELEGRAM_BOT_TOKEN is configured')
        if str(settings.get('SYNOLOGY_CHAT_ENABLED', '')).lower() == 'true':
            for key in ('SYNOLOGY_CHAT_OUTGOING_TOKEN', 'SYNOLOGY_CHAT_ALLOWED_USER_IDS'):
                if not str(settings.get(key) or '').strip():
                    raise RuntimeError(key + ' must be set when Synology Chat is enabled')

    def task_counts(self) -> dict[str, dict[str, int]]:
        counts = {}
        for name, filename, table in [
            ('bot', self.state / 'task-cards.sqlite3', 'task_cards'),
            ('lyrics', self.state / 'word-lyrics/automation/queue.sqlite3', 'jobs'),
        ]:
            if filename.exists():
                with sqlite3.connect('file:' + quote(str(filename)) + '?mode=ro', uri=True, timeout=3) as database:
                    counts[name] = {str(status): int(count) for status, count in database.execute(
                        f'SELECT status, count(*) FROM {table} GROUP BY status')}
        return counts

    def require_idle(self) -> None:
        counts = self.task_counts()
        busy_bot = sum(count for status, count in counts.get('bot', {}).items()
                       if status in {'queued', 'running', 'stopping'})
        busy_lyrics = sum(count for status, count in counts.get('lyrics', {}).items()
                          if status.startswith('leased_'))
        if busy_bot or busy_lyrics:
            raise RuntimeError(f'Work is active: bot={busy_bot}, leased lyrics={busy_lyrics}; wait for it to finish')

    def compose(self, *args: str, capture: bool = False) -> str:
        return run(self.command + list(args), capture=capture)

    def selected(self, names: list[str], *, default: bool = True) -> list[str]:
        if not names and default:
            names = ['legacy-wrapper', 'wrapper', 'youtube-pot', 'bot']
            for profile in dict.fromkeys(self.args.profile):
                names += PROFILE_SERVICES[profile]
            if self.args.self_heal:
                names += ['wrapper-watchdog', 'bot-watchdog']
        unknown = set(names) - SERVICES
        if unknown:
            raise RuntimeError('Unsupported service: ' + ', '.join(sorted(unknown)))
        for profile in PROFILE_FILES:
            if set(names).intersection(PROFILE_SERVICES[profile]) and profile not in self.args.profile:
                raise RuntimeError('Include --profile ' + profile + ' when managing its music services')
        return names

    def inventory(self) -> dict[str, dict]:
        ids = run(['docker', 'ps', '--all', '--quiet', '--filter',
                   'label=com.docker.compose.project=' + self.project], capture=True).split()
        rows: dict[str, dict] = {}
        for container_id in ids:
            info = json.loads(run(['docker', 'inspect', container_id], capture=True))[0]
            labels = info['Config'].get('Labels') or {}
            if labels.get('com.docker.compose.project') != self.project:
                raise RuntimeError('Refusing a container outside this Compose project')
            service = labels.get('com.docker.compose.service', '')
            if service not in SERVICES:
                raise RuntimeError('Unexpected service in this project: ' + service)
            rows[service] = {
                'id': info['Id'], 'image_id': info['Image'],
                'running': bool(info['State']['Running']),
                'status': info['State']['Status'],
                'health': info['State'].get('Health', {}).get('Status', 'not-configured'),
                'restarts': info['RestartCount'],
            }
        return rows

    def stop(self, services: list[str]) -> None:
        rows = self.inventory()
        selected = self.selected(services) if services else list(rows)
        protecting = {'bot': 'bot-watchdog', 'wrapper': 'wrapper-watchdog',
                      'legacy-wrapper': 'wrapper-watchdog'}
        for service in selected:
            watcher = protecting.get(service)
            if watcher in rows and rows[watcher]['running'] and watcher not in selected:
                raise RuntimeError('Stop ' + str(watcher) + ' first, or stop the whole stack')
        for service in stop_order(selected):
            if service in rows and rows[service]['running']:
                run(['docker', 'stop', '--time', '90', rows[service]['id']])

    def status(self) -> None:
        for service, row in sorted(self.inventory().items()):
            print(f"{service}: {row['status']}; health={row['health']}; restarts={row['restarts']}")

    def probe(self) -> None:
        self.status()
        print('Task counts: ' + json.dumps(self.task_counts(), sort_keys=True))
        # Print only an allowlisted summary; never echo HTTP bodies or sessions.
        probe = (
            "import json,urllib.request; "
            "d=json.load(urllib.request.urlopen('http://127.0.0.1:30871/health',timeout=8)); "
            "print(json.dumps({k:d[k] for k in ('ok','queue') if k in d},ensure_ascii=False))"
        )
        self.compose('exec', '-T', 'bot', 'python', '-c', probe)

    def resume(self, services: list[str]) -> None:
        failed = []
        for service in reversed(stop_order(services)):
            try:
                self.compose('start', service)
            except Exception:
                failed.append(service)
        if failed:
            raise RuntimeError('Could not restart services: ' + ', '.join(failed))

    def backup(self, destination: Path, *, restart: bool = True) -> dict:
        destination = destination.expanduser().resolve()
        if destination == ROOT or ROOT in destination.parents:
            raise RuntimeError('Backups must be outside the Git checkout')
        if destination.exists():
            raise RuntimeError('Backup destination must not already exist')
        mounts: dict[str, str] = {}
        for service, config in self.config['services'].items():
            for volume in config.get('volumes', []):
                if volume.get('type') == 'bind' and volume['target'] in PRIVATE_TARGETS:
                    source = Path(volume['source']).resolve()
                    if source == Path(source.anchor):
                        raise RuntimeError('Refusing to archive a filesystem root')
                    if source == destination or source in destination.parents:
                        raise RuntimeError('Backup destination is inside data being backed up')
                    mounts[str(source)] = f'data/{len(mounts):02d}' if str(source) not in mounts else mounts[str(source)]
        rows = self.inventory()
        running = [service for service, row in rows.items() if row['running']]
        if set(running) - set(self.config['services']):
            raise RuntimeError('Running services require their Compose file/profile; include --self-heal and matching profiles')
        self.require_idle()
        # Watchdogs stop first, then every writer, including the guest VM.
        stopped = stop_order(running)
        destination.mkdir(parents=True, mode=0o700)
        destination.chmod(0o700)
        manifest = {'project': self.project, 'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                    'running_services': running, 'containers': rows,
                    'mounts': mounts, 'complete': False}
        write_json(destination / 'manifest.json', manifest)
        try:
            for service in stopped:
                self.compose('stop', '--timeout', '90', service)
            # Catch a job accepted between the first idle check and container stop.
            self.require_idle()
            filenames = ['.env', 'compose.yaml', 'compose.self-heal.yaml']
            if getattr(self.args, 'auth_refresh', False):
                filenames.append('compose.auth-refresh.yaml')
            filenames += [PROFILE_FILES[profile] for profile in dict.fromkeys(self.args.profile)
                          if profile in PROFILE_FILES]
            for filename in filenames:
                source = ROOT / filename
                if source.exists():
                    target = destination / filename
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    shutil.copyfile(source, target)
                    target.chmod(0o600)
            if self.active.exists():
                shutil.copyfile(self.active, destination / 'images.compose.json')
                (destination / 'images.compose.json').chmod(0o600)
            with tarfile.open(destination / 'private-data.tar', 'w', dereference=False) as archive:
                for source, target in mounts.items():
                    if Path(source).exists():
                        archive.add(source, arcname=target)
            (destination / 'private-data.tar').chmod(0o600)
            manifest['complete'] = True
            write_json(destination / 'manifest.json', manifest)
        finally:
            # `start` preserves each container's image and prior configuration.
            # Dependencies already existed; the recorded service set is exact.
            if restart or not manifest['complete']:
                self.resume(stopped)
        print('Private backup completed: ' + str(destination))
        return manifest

    def override(self) -> dict:
        return json.loads(self.active.read_text()) if self.active.exists() else {'services': {}}

    def upgrade(self, service: str, image: str, destination: Path) -> None:
        destination = destination.expanduser().resolve()
        self.selected([service])
        rows = self.inventory()
        if service not in rows:
            raise RuntimeError('Service must already have a container')
        # Candidate must already be pulled or built; no implicit latest upgrade.
        candidate = json.loads(run(['docker', 'image', 'inspect', image], capture=True))[0]
        if candidate['Id'] == rows[service]['image_id']:
            raise RuntimeError('Candidate equals the current image')
        manifest = self.backup(destination, restart=False)
        rollback_tag = 'amdl-rollback:' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d%H%M%S') + '-' + service
        running = manifest['running_services']
        try:
            run(['docker', 'tag', rows[service]['image_id'], rollback_tag])
            changes = self.override()
            before = json.loads(json.dumps(changes))
            upgrade = {'project': self.project, 'service': service, 'rollback_image': rollback_tag,
                       'candidate_image_id': candidate['Id'], 'previous_override': before,
                       'previously_running': rows[service]['running']}
            # Recovery metadata is durable before the active override changes.
            write_json(destination / 'upgrade.json', upgrade)
            changes['services'][service] = {'image': candidate['Id']}
            write_json(self.active, changes)
            self.command = self.base + ['-f', str(self.active)]
            try:
                # All writers remain stopped until the new container exists.
                self.compose('up', '--no-start', '--no-deps', '--no-build', '--pull', 'never', service)
            except Exception:
                changes['services'][service] = {'image': rollback_tag}
                write_json(self.active, changes)
                self.compose('up', '--no-start', '--no-deps', '--no-build', '--pull', 'never', service)
                raise
        finally:
            self.resume(running)
        print('Candidate applied to ' + service + '. Run probe; rollback is available in ' + str(destination))

    def rollback(self, destination: Path) -> None:
        data = json.loads((destination.expanduser().resolve() / 'upgrade.json').read_text())
        if data['project'] != self.project:
            raise RuntimeError('Rollback belongs to another Compose project')
        service = self.selected([data['service']])[0]
        run(['docker', 'image', 'inspect', data['rollback_image']], capture=True)
        changes = self.override()
        changes['services'][service] = {'image': data['rollback_image']}
        write_json(self.active, changes)
        self.command = self.base + ['-f', str(self.active)]
        mode = '-d' if data['previously_running'] else '--no-start'
        self.compose('up', mode, '--no-deps', '--no-build', '--pull', 'never', service)
        print('Image rollback applied. Data was preserved; restore private backups separately if needed.')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-heal', action='store_true', help='include the explicit Docker socket override')
    parser.add_argument('--auth-refresh', action='store_true', help='allow on-demand browser refresh through Docker API')
    parser.add_argument('--profile', action='append', choices=sorted(PROFILE_SERVICES), default=[])
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ['check', 'status', 'probe']:
        commands.add_parser(name)
    for name in ['build', 'up', 'stop']:
        commands.add_parser(name).add_argument('services', nargs='*')
    commands.add_parser('backup').add_argument('destination', type=Path)
    upgrade = commands.add_parser('upgrade')
    upgrade.add_argument('service', choices=sorted(SERVICES))
    upgrade.add_argument('image')
    upgrade.add_argument('destination', type=Path)
    commands.add_parser('rollback').add_argument('destination', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    stack = Stack(args)
    if args.command == 'check':
        stack.validate_access()
        stack.compose('config', '--quiet')
        print('Compose configuration is valid (expanded secrets were not printed).')
    elif args.command in {'status', 'probe'}:
        getattr(stack, args.command)()
    elif args.command in {'build', 'up', 'stop'}:
        if args.command == 'stop':
            stack.stop(args.services)
        else:
            services = stack.selected(args.services)
            if args.command == 'up' and 'bot' in services:
                stack.validate_access()
            options = {'build': [], 'up': ['-d']}[args.command]
            stack.compose(args.command, *options, *services)
    elif args.command == 'backup':
        stack.backup(args.destination)
    elif args.command == 'upgrade':
        stack.upgrade(args.service, args.image, args.destination)
    else:
        stack.rollback(args.destination)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, ValueError, sqlite3.Error, subprocess.CalledProcessError) as exc:
        # Do not echo captured Docker inspect/config output: it can contain secrets.
        print('Maintenance failed: ' + (str(exc) if not isinstance(exc, subprocess.CalledProcessError)
                                        else 'Docker command failed; review local service logs.'), file=sys.stderr)
        raise SystemExit(1)
