"""The existing task kind must not mislabel a completed SQ download as Apple."""
import ast
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'bot'))
from task_cards import format_task_card


def text_namespace(enabled):
    tree = ast.parse((ROOT / 'bot' / 'ingest_bot.py').read_text())
    nodes = [node for node in tree.body if getattr(node, 'name', '') in {'help_text', 'register_telegram_commands'}]
    namespace = {'UNIFIED_MUSIC_ENABLED': enabled, 'YOUTUBE_ENABLED': False,
                 'BILIBILI_ENABLED': False, 'log': lambda text: None}
    namespace['telegram_api'] = lambda method, payload, **kw: namespace.update(commands=payload['commands'])
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<bot-text>', 'exec'), namespace)
    return namespace


class UnifiedCopyTests(unittest.TestCase):
    def test_help_and_commands_follow_feature_flag(self):
        for enabled in (False, True):
            ns = text_namespace(enabled)
            ns['register_telegram_commands']()
            search = next(command['description'] for command in ns['commands'] if command['command'] == 'search')
            if enabled:
                self.assertIn('统一搜索', search)
                self.assertIn('Apple → LX → SQ', ns['help_text']())
            else:
                self.assertEqual(search, '搜索 Apple Music')
                self.assertIn('发送歌名会搜索 Apple Music', ns['help_text']())

    def test_completed_source_and_root_follow_actual_unified_result(self):
        task = dict(id=1, kind='apple_music', mode='alac', status='completed',
                    phase='SQ 无损入库完成', created_at=1, finished_at=2,
                    job_json=json.dumps({'payload': {'unified_music': {'schema': 1}}}),
                    local_paths_json=json.dumps(['/music/SQ Music/Artist/Album/Song.flac']))
        card = format_task_card(task)
        self.assertIn('来源：SQ Music 无损', card)
        self.assertIn('下载根目录：/music/SQ Music', card)
        self.assertNotIn('来源：Apple Music', card)
        task.update(phase='LX 无损入库完成', local_paths_json=json.dumps(['/music/LX Music/Song.flac']))
        self.assertIn('来源：LX Music 无损', format_task_card(task))

    def test_legacy_task_copy_stays_unchanged(self):
        task = dict(id=1, kind='apple_music', mode='alac', status='completed',
                    phase='下载完成', created_at=1, finished_at=2, job_json='{}', local_paths_json='[]')
        card = format_task_card(task)
        self.assertIn('来源：Apple Music', card)
        self.assertIn('下载根目录：/music/ALAC', card)


if __name__ == '__main__':
    unittest.main()
