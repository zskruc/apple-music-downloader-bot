"""Release verification must reject unsafe inputs before publishing source."""
import hashlib
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location('amdl_bootstrap', Path(__file__).resolve().parents[1] / 'install.py')
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


class ReleaseBootstrapTests(unittest.TestCase):
    def archive(self, root, *, extra=None):
        path = root / 'source.tar.gz'
        with tarfile.open(path, 'w:gz') as archive:
            info = tarfile.TarInfo(bootstrap.PREFIX + '/deploy/installer.py')
            content = b'"""synthetic release installer"""\n'
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
            if extra is not None:
                data = b'synthetic extra'
                if extra.isfile():
                    extra.size = len(data)
                archive.addfile(extra, io.BytesIO(data) if extra.isfile() else None)
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    def test_verified_source_is_published_atomically(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            archive, digest = self.archive(root)
            bootstrap.unpack(archive, root / 'source', digest)
            self.assertTrue((root / 'source/deploy/installer.py').is_file())
            self.assertTrue((root / 'source/.release-source.json').is_file())

    def test_bad_digest_leaves_no_published_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            archive, _ = self.archive(root)
            with self.assertRaises(bootstrap.BootstrapError):
                bootstrap.unpack(archive, root / 'source', '0' * 64)
            self.assertFalse((root / 'source').exists())

    def test_traversal_links_private_env_and_duplicate_paths_are_rejected(self):
        entries = [tarfile.TarInfo('../escaped'), tarfile.TarInfo('/escaped'),
                   tarfile.TarInfo(bootstrap.PREFIX + '/.env'),
                   tarfile.TarInfo(bootstrap.PREFIX + '/deploy/installer.py')]
        link = tarfile.TarInfo(bootstrap.PREFIX + '/outside')
        link.type, link.linkname = tarfile.SYMTYPE, '/etc'
        entries.append(link)
        hardlink = tarfile.TarInfo(bootstrap.PREFIX + '/hardlink')
        hardlink.type, hardlink.linkname = tarfile.LNKTYPE, '/etc/passwd'
        entries.append(hardlink)
        for entry in entries:
            with self.subTest(name=entry.name), tempfile.TemporaryDirectory() as folder:
                root = Path(folder).resolve()
                archive, digest = self.archive(root, extra=entry)
                with self.assertRaises(bootstrap.BootstrapError):
                    bootstrap.unpack(archive, root / 'source', digest)
                self.assertFalse((root / 'source').exists())
                self.assertFalse(list(root.glob('.source-stage-*')))

    def test_existing_source_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            archive, digest = self.archive(root)
            source = root / 'source'
            source.mkdir()
            marker = source / 'keep'
            marker.write_text('existing')
            with self.assertRaises(bootstrap.BootstrapError):
                bootstrap.unpack(archive, source, digest)
            self.assertEqual(marker.read_text(), 'existing')

    def test_private_config_arguments_are_forwarded_without_shell_interpretation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve() / 'deployment'
            source = root / 'source/deploy'
            source.mkdir(parents=True)
            (source / 'installer.py').write_text('# synthetic installer')
            config = str(root.parent / 'private config $literal.json')
            with patch.object(bootstrap, 'dispatch', return_value=0) as dispatch:
                result = bootstrap.main(['--root', str(root), '--config', config, '--with-media-servers'])
            self.assertEqual(result, 0)
            self.assertEqual(dispatch.call_args.args[3], ['--config', config, '--with-media-servers'])

    def test_shared_and_symlink_roots_are_refused(self):
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.checked_root(Path('/srv'))
        for path in ['/Users/example/Documents/project/data', '/Users/example/Desktop/new-nas',
                     '/Users/example/Library/CloudStorage/drive/new-nas']:
            with self.subTest(path=path), self.assertRaises(bootstrap.BootstrapError):
                bootstrap.checked_root(Path(path))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            (root / 'link').symlink_to(root, target_is_directory=True)
            with self.assertRaises(bootstrap.BootstrapError):
                bootstrap.checked_root(root / 'link/deployment')


if __name__ == '__main__':
    unittest.main()
