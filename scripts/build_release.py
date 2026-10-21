#!/usr/bin/env python3
"""Create a deterministic, source-only release from the committed Git tree."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    if output == ROOT or ROOT in output.parents:
        parser.error('Release output must be outside the source checkout')
    if output.exists() and any(output.iterdir()):
        parser.error('Release output must be an empty directory')
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    subprocess.run(['git', 'diff', '--exit-code', 'HEAD', '--'], cwd=ROOT,
                   stdout=subprocess.DEVNULL, check=True)
    subprocess.run(['python3', str(ROOT / 'scripts/check_repo.py')], cwd=ROOT, check=True)
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    archive = output / 'source.tar.gz'
    process = subprocess.Popen(['git', 'archive', '--format=tar', '--prefix=apple-music-downloader-bot/', revision],
                               cwd=ROOT, stdout=subprocess.PIPE)
    with archive.open('xb') as destination, gzip.GzipFile(filename='', mode='wb', fileobj=destination, mtime=0) as compressed:
        shutil.copyfileobj(process.stdout, compressed)
    if process.wait() != 0:
        archive.unlink(missing_ok=True)
        raise SystemExit('Git source archive creation failed')
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (output / 'source.tar.gz.sha256').write_text(digest + '  source.tar.gz\n')
    (output / 'release.json').write_text(json.dumps({'schema': 1, 'commit': revision,
                                                   'archive_sha256': digest,
                                                   'bytes': archive.stat().st_size}, indent=2) + '\n')
    print('Source release prepared at ' + str(output))


if __name__ == '__main__':
    main()
