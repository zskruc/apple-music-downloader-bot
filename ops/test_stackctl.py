"""Offline tests for the maintenance script's mutation boundaries."""
import argparse
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('stackctl', Path(__file__).with_name('stackctl.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class MaintenanceContracts(unittest.TestCase):
    def stack(self, *, profiles=(), heal=False):
        stack = module.Stack.__new__(module.Stack)
        stack.args = argparse.Namespace(profile=list(profiles), self_heal=heal)
        stack.project = 'test-stack'
        stack.base = ['docker', 'compose', '-p', 'test-stack']
        stack.command = stack.base
        stack.config = {'services': {'bot': {'environment': {}, 'volumes': []}}}
        return stack

    def test_profiles_expand_default_service_set(self):
        stack = self.stack(profiles=['lyrics', 'auth'], heal=True)
        self.assertEqual(set(stack.selected([])), module.SERVICES)

    def test_unknown_service_is_rejected(self):
        with self.assertRaises(RuntimeError):
            self.stack().selected(['unrelated-nas-service'])

    def test_tokens_require_allowlists(self):
        stack = self.stack()
        settings = stack.config['services']['bot']['environment']
        settings['TELEGRAM_BOT_TOKEN'] = 'example-token'
        with self.assertRaisesRegex(RuntimeError, 'TELEGRAM_ALLOWED_CHAT_IDS'):
            stack.validate_access()
        settings['TELEGRAM_ALLOWED_CHAT_IDS'] = '12345'
        stack.validate_access()
        settings.update(SYNOLOGY_CHAT_ENABLED='true', SYNOLOGY_CHAT_OUTGOING_TOKEN='example')
        with self.assertRaisesRegex(RuntimeError, 'SYNOLOGY_CHAT_ALLOWED_USER_IDS'):
            stack.validate_access()

    def test_stop_all_uses_inventory_and_stops_watchdogs_first(self):
        stack = self.stack()
        rows = {name: {'id': name + '-id', 'running': True}
                for name in ['bot', 'word-lyrics-resolver', 'bot-watchdog', 'wrapper-watchdog']}
        with patch.object(stack, 'inventory', return_value=rows), patch.object(module, 'run') as run:
            stack.stop([])
        ids = [call.args[0][-1] for call in run.call_args_list]
        self.assertEqual(ids[:2], ['bot-watchdog-id', 'wrapper-watchdog-id'])
        self.assertEqual(set(ids), {row['id'] for row in rows.values()})

    def test_stopping_watched_target_alone_is_rejected(self):
        stack = self.stack()
        rows = {'bot': {'running': True}, 'bot-watchdog': {'running': True}}
        with patch.object(stack, 'inventory', return_value=rows), patch.object(module, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'bot-watchdog'):
                stack.stop(['bot'])
            run.assert_not_called()

    def test_foreign_container_is_rejected_before_mutation(self):
        stack = self.stack()
        foreign = {'Config': {'Labels': {'com.docker.compose.project': 'other-project'}}}
        with patch.object(module, 'run', side_effect=['foreign-id', json.dumps([foreign])]):
            with self.assertRaisesRegex(RuntimeError, 'outside this Compose project'):
                stack.inventory()

    def test_active_work_blocks_backup(self):
        stack = self.stack()
        for counts in [{'bot': {'running': 1}}, {'lyrics': {'leased_scan': 1}}]:
            with patch.object(stack, 'task_counts', return_value=counts):
                with self.assertRaisesRegex(RuntimeError, 'Work is active'):
                    stack.require_idle()

    def test_repository_backup_destination_is_rejected(self):
        stack = self.stack()
        with self.assertRaisesRegex(RuntimeError, 'outside the Git checkout'):
            stack.backup(module.ROOT / 'private-backup')

    def test_failed_backup_restarts_original_services(self):
        stack = self.stack()
        rows = {'bot': {'running': True}, 'bot-watchdog': {'running': True}}
        stack.config['services']['bot-watchdog'] = {'volumes': []}
        with tempfile.TemporaryDirectory() as folder:
            stack.active = Path(folder) / 'missing'
            with patch.object(stack, 'inventory', return_value=rows), \
                    patch.object(stack, 'require_idle'), \
                    patch.object(stack, 'compose') as compose, \
                    patch.object(module.tarfile, 'open', side_effect=OSError('simulated archive failure')):
                with self.assertRaisesRegex(OSError, 'simulated archive failure'):
                    stack.backup(Path(folder) / 'snapshot')
                operations = [call.args for call in compose.call_args_list]
                self.assertEqual(operations[0], ('stop', '--timeout', '90', 'bot-watchdog'))
                self.assertEqual(operations[-2:], [('start', 'bot'), ('start', 'bot-watchdog')])

    def test_rollback_only_changes_target_and_never_restores_data(self):
        stack = self.stack()
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            stack.active = directory / 'active.json'
            module.write_json(stack.active, {'services': {'bot': {'image': 'new'}, 'wrapper': {'image': 'keep'}}})
            module.write_json(directory / 'upgrade.json', {'project': stack.project, 'service': 'bot',
                              'rollback_image': 'retained:old', 'previously_running': False})
            with patch.object(module, 'run'), patch.object(stack, 'compose') as compose:
                stack.rollback(directory)
            changed = json.loads(stack.active.read_text())
            self.assertEqual(changed['services']['wrapper']['image'], 'keep')
            self.assertEqual(changed['services']['bot']['image'], 'retained:old')
            compose.assert_called_once_with('up', '--no-start', '--no-deps', '--no-build', '--pull', 'never', 'bot')

    def test_upgrade_keeps_writers_stopped_until_candidate_is_created(self):
        stack = self.stack()
        rows = {'bot': {'image_id': 'sha256:old', 'running': True}}
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            stack.active = directory / 'state/ops/images.compose.json'
            events = []

            def backup(destination, *, restart=True):
                self.assertFalse(restart)
                self.assertTrue(destination.is_absolute())
                destination.mkdir()
                events.append('backup-stopped')
                return {'running_services': ['bot']}

            def compose(*args, **kwargs):
                self.assertTrue((directory / 'snapshot/upgrade.json').exists())
                self.assertIn('--no-start', args)
                events.append('candidate-created')

            with patch.object(stack, 'inventory', return_value=rows), \
                    patch.object(stack, 'backup', side_effect=backup), \
                    patch.object(stack, 'compose', side_effect=compose), \
                    patch.object(stack, 'resume', side_effect=lambda services: events.append('resumed')), \
                    patch.object(module, 'run', side_effect=[json.dumps([{'Id': 'sha256:new'}]), '']):
                stack.upgrade('bot', 'candidate:reviewed', directory / 'snapshot')
            self.assertEqual(events, ['backup-stopped', 'candidate-created', 'resumed'])

    def test_upgrade_creation_failure_restores_old_image_before_resume(self):
        stack = self.stack()
        rows = {'bot': {'image_id': 'sha256:old', 'running': True}}
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            snapshot = directory / 'snapshot'
            snapshot.mkdir()
            stack.active = directory / 'state/ops/images.compose.json'
            with patch.object(stack, 'inventory', return_value=rows), \
                    patch.object(stack, 'backup', return_value={'running_services': ['bot']}), \
                    patch.object(stack, 'compose', side_effect=[RuntimeError('simulated create failure'), '']) as compose, \
                    patch.object(stack, 'resume') as resume, \
                    patch.object(module, 'run', side_effect=[json.dumps([{'Id': 'sha256:new'}]), '']):
                with self.assertRaisesRegex(RuntimeError, 'simulated create failure'):
                    stack.upgrade('bot', 'candidate:reviewed', snapshot)
            self.assertEqual(compose.call_count, 2)
            self.assertTrue(json.loads(stack.active.read_text())['services']['bot']['image'].startswith('amdl-rollback:'))
            resume.assert_called_once_with(['bot'])


if __name__ == '__main__':
    unittest.main()
