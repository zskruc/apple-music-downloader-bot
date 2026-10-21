"""A caller's production variables cannot redirect an explicitly managed stack."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from deploy.environment import command_environment


class DeploymentEnvironmentTests(unittest.TestCase):
    def test_private_file_wins_over_shell_but_docker_connection_and_proxy_remain(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / '.env.example').write_text('MUSIC_PATH=example\n')
            private = root / 'private.env'
            private.write_text("STATE_PATH='managed-state'\n")
            environment = {'PATH': '/bin', 'MUSIC_PATH': 'old-library', 'STATE_PATH': 'old-state',
                           'COMPOSE_PROJECT_NAME': 'old-project', 'COMPOSE_FILE': 'old.yaml',
                           'DOCKER_HOST': 'unix:///var/run/docker.sock', 'HTTPS_PROXY': 'local-proxy'}
            with mock.patch.dict(os.environ, environment, clear=True):
                result = command_environment(['docker', 'compose', '--env-file', str(private),
                                              '--project-directory', str(root), 'config'])
            self.assertEqual(result, {key: value for key, value in environment.items()
                                     if key in {'PATH', 'DOCKER_HOST', 'HTTPS_PROXY'}})
            self.assertEqual(private.read_text(), "STATE_PATH='managed-state'\n")

    def test_non_compose_commands_keep_their_existing_runtime_environment(self):
        self.assertIsNone(command_environment(['docker', 'image', 'inspect', 'synthetic']))


if __name__ == '__main__':
    unittest.main()
