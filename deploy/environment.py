"""Keep an explicit deployment env file authoritative over the caller's shell."""
from __future__ import annotations

import os
from pathlib import Path
import re


def compose_environment(*env_files, extra_keys=()):
    keys = set(extra_keys)
    for filename in env_files:
        path = Path(filename)
        if path.is_symlink():
            raise ValueError('Deployment environment files must not be symlinks')
        if not path.exists():
            continue
        for line in path.read_text(encoding='utf-8').splitlines():
            key, separator, _value = line.partition('=')
            key = key.strip()
            if separator and re.fullmatch(r'[A-Z][A-Z0-9_]*', key):
                keys.add(key)
    return {key: value for key, value in os.environ.items()
            if key not in keys and not key.startswith('COMPOSE_')}


def command_environment(command):
    """Return a scrubbed environment for an explicit Docker Compose command."""
    if command[:2] != ['docker', 'compose'] or '--env-file' not in command:
        return None
    files = [command[index + 1] for index, value in enumerate(command[:-1]) if value == '--env-file']
    if '--project-directory' in command:
        files.append(Path(command[command.index('--project-directory') + 1]) / '.env.example')
    return compose_environment(*files)
