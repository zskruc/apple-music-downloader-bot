import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import word_lyrics
from word_lyrics_automation.source_worker import SourceWorker
from word_lyrics_automation.character_timing import TT

NATIVE=f'<tt xmlns="{TT}" xmlns:itunes="http://music.apple.com/lyric-ttml-internal" itunes:timing="Word"><head/><body><div begin="10" end="15"><p begin="10" end="15"><span begin="10" end="11">在夜</span><span begin="11" end="13">里微</span><span begin="13" end="15">亮</span></p></div></body></tt>'

class CharacterIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.root=Path(self.directory.name)
        self.audio=self.root/'song.m4a'
        self.audio.write_bytes(b'unchanged audio fixture')
        self.sidecar=self.audio.with_suffix('.ttml')
        self.sidecar.write_text(NATIVE)
        self.service=object.__new__(word_lyrics.WordLyricsService)
        self.service.state_dir=self.root/'state'
        self.service.audit=word_lyrics.AuditStore(self.service.state_dir)
        self.environment=patch.dict(os.environ,{'AMDL_WORD_LYRICS_ENABLED':'true','AMDL_HAN_CHARACTER_TIMING_ENABLED':'true'})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    def test_download_postprocessing_backup_hash_and_idempotence(self):
        initial=hashlib.sha256(self.audio.read_bytes()).hexdigest()
        with patch.object(word_lyrics,'service_from_environment',return_value=self.service), patch.object(word_lyrics,'inspect_track',side_effect=lambda p,seed:seed):
            first=word_lyrics.sync_word_lyrics_for_paths([self.audio])
            second=word_lyrics.sync_word_lyrics_for_paths([self.audio])
        self.assertEqual(first['character_normalized'],1)
        self.assertEqual(first['character_added_cues'],2)
        self.assertNotIn('character_normalized',second)
        self.assertEqual(hashlib.sha256(self.audio.read_bytes()).hexdigest(),initial)
        self.assertEqual(word_lyrics.parse_ttml(self.sidecar.read_text()).timed_spans,5)
        self.assertEqual(next((self.root/'state/backups').rglob('*.ttml')).read_text(),NATIVE)
        evidence=list((self.root/'state/automation/character-timing').glob('*.json'))
        self.assertEqual(len(evidence),1)

    def test_background_source_keeps_provider_and_mixed_lyric_metrics(self):
        worker=object.__new__(SourceWorker)
        worker.gateway=SimpleNamespace(service=self.service)
        result=worker.character_result(self.audio,{'provider':'kugou_reference_fusion','candidate_sha256':'native','metrics':{'line_timed_nonlexical_lines':12}})
        self.assertEqual(result['provider'],'kugou_reference_fusion')
        self.assertEqual(result['metrics']['line_timed_nonlexical_lines'],12)
        self.assertEqual(result['metrics']['timed_words'],5)
        self.assertTrue(result['metrics']['character_internal_boundaries_estimated'])
        self.assertEqual(result['candidate_sha256'],hashlib.sha256(self.sidecar.read_bytes()).hexdigest())

    def test_audio_change_rolls_back_only_derived_lyric(self):
        with patch('word_lyrics_automation.fingerprint.media_fingerprint',side_effect=[{'sha256':'before'},{'sha256':'after'}]):
            with self.assertRaisesRegex(RuntimeError,'audio changed'):
                self.service.normalize_character_sidecar(self.audio)
        self.assertEqual(self.sidecar.read_text(),NATIVE)

    def test_line_timing_untouched_and_feature_can_be_disabled(self):
        with patch.dict(os.environ,{'AMDL_HAN_CHARACTER_TIMING_ENABLED':'false'}):
            self.assertIsNone(self.service.normalize_character_sidecar(self.audio))
        self.assertEqual(self.sidecar.read_text(),NATIVE)
        self.sidecar.write_text(NATIVE.replace('timing="Word"','timing="Line"').replace('<span begin="10" end="11">在夜</span>','在夜'))
        # A line document with remaining timed spans still has word timing;
        # use a true native line document for the no-fabrication check.
        line=f'<tt xmlns="{TT}"><body><p begin="10" end="15">在夜里微亮 Du Na La</p></body></tt>'
        self.sidecar.write_text(line)
        self.assertIsNone(self.service.normalize_character_sidecar(self.audio))
        self.assertEqual(self.sidecar.read_text(),line)

if __name__=='__main__':
    unittest.main()
