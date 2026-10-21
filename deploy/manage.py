"""Take a complete private maintenance snapshot of explicitly managed projects."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

from deploy.installer import atomic_json, install_lock, read_env, validate_root
from deploy import migration

STOP_PRIORITY = {'wrapper-watchdog': 0, 'bot-watchdog': 0, 'bot': 1,
                 'word-lyrics-resolver': 2, 'word-lyrics-nas-aligner': 2,
                 'youtube-auth-browser': 3, 'bilibili-auth-browser': 3,
                 'sqmusic-server': 4, 'lxserver': 4, 'sq-neteasecloudmusicapi': 5,
                 'wrapper': 5, 'legacy-wrapper': 6, 'sq-mysql': 7}


def docker(*arguments: str) -> str:
    try:
        result = subprocess.run(['docker', *arguments], check=True, capture_output=True,
                                text=True, timeout=180)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError('Managed Docker operation failed; private data was not printed') from None
    return result.stdout


def require_idle(projects: list[dict], configs: dict | None = None) -> None:
    for project in projects:
        values = read_env(project['environment_path'])
        state = values.get('STATE_PATH')
        if not state:
            services = set((configs or {}).get(project['project_name'], {}).get('services', {}))
            if services.intersection({'bot', 'word-lyrics-resolver', 'word-lyrics-nas-aligner'}):
                raise RuntimeError('A task-writing project has no explicit state directory')
            continue
        for relative, table, active in [
            ('task-cards.sqlite3', 'task_cards', lambda status: status in {'running', 'stopping'}),
            ('word-lyrics/automation/queue.sqlite3', 'jobs', lambda status: status.startswith('leased_')),
        ]:
            path = Path(state) / relative
            if not path.exists():
                continue
            if path.is_symlink():
                raise RuntimeError('Task database must not be a symlink')
            try:
                with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=3) as database:
                    rows = list(database.execute('SELECT status, count(*) FROM ' + table + ' GROUP BY status'))
            except sqlite3.Error:
                raise RuntimeError('Cannot verify the task database; no services were stopped') from None
            if any(count and active(str(status)) for status, count in rows):
                raise RuntimeError('Active bot or lyric work must finish before the maintenance snapshot')


def backup(root: Path, destination: Path, *, include_media: bool = True,
           include_images: bool = True, restart: bool = True) -> dict:
    root = validate_root(root)
    with install_lock(root):
        descriptor, projects = migration._projects(root)
        names = {project['project_name'] for project in projects}
        configs = {project['project_name']: migration._compose_config(project) for project in projects}
        rows = [row for row in migration._containers() if row['project'] in names]
        if {row['project'] for row in rows} != names:
            raise RuntimeError('A selected project has no container evidence; no services were stopped')
        for row in rows:
            if row['service'] not in configs[row['project']]['services']:
                raise RuntimeError('Selected project contains an undeclared service; no services were stopped')
            if descriptor.get('installation_id'):
                labels = json.loads(docker('inspect', '--format', '{{json .Config.Labels}}', row['id']))
                if labels.get('io.apple-music-downloader.installation') != descriptor['installation_id']:
                    raise RuntimeError('A selected container belongs to another installation; no services were stopped')
        require_idle(projects, configs)
        running = sorted([row for row in rows if row['running']],
                         key=lambda row: (STOP_PRIORITY.get(row['service'], 4), row['project'], row['service']))
        atomic_json(root / 'state/maintenance.json', {'schema': 1, 'operation': 'backup',
                    'running_containers': [{'id': row['id'], 'project': row['project'], 'service': row['service']} for row in running],
                    'complete': False})
        complete = False
        try:
            for row in running:
                docker('stop', '--time', '90', row['id'])
            # Recheck after stopping writers: no late accepted active job may
            # disappear from a snapshot presented as an idle migration point.
            require_idle(projects, configs)
            manifest = migration.snapshot(root, destination, include_media, include_images)
            complete = True
            return manifest
        finally:
            failures = []
            if restart or not complete:
                for row in reversed(running):
                    try:
                        docker('start', row['id'])
                    except RuntimeError:
                        failures.append(row['service'])
            atomic_json(root / 'state/maintenance.json', {'schema': 1, 'operation': 'backup',
                        'complete': complete, 'restart_failures': failures,
                        'services_left_stopped': complete and not restart})
            if failures:
                raise RuntimeError('Snapshot maintenance ended, but some services need manual restart: '
                                   + ', '.join(sorted(set(failures))))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    command = actions.add_parser('backup')
    command.add_argument('--root', required=True, type=Path)
    command.add_argument('--destination', required=True, type=Path)
    command.add_argument('--without-media', action='store_true', help='Explicitly omit media; recorded in the coverage ledger')
    command.add_argument('--without-images', action='store_true', help='Require pulling/building images again on restore')
    command.add_argument('--leave-stopped', action='store_true', help='Keep this installation stopped after a successful migration snapshot')
    args = parser.parse_args(argv)
    try:
        backup(args.root, args.destination, include_media=not args.without_media,
               include_images=not args.without_images, restart=not args.leave_stopped)
    except (RuntimeError, migration.MigrationError, OSError, ValueError):
        print('Backup incomplete or refused; check status before retrying. No private contents were printed.', file=sys.stderr)
        return 1
    print('Private snapshot verified; ' + ('selected services remain stopped for migration. '
          if args.leave_stopped else 'originally running services resumed. ')
          + 'Keep this bundle out of GitHub.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
