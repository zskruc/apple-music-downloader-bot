from __future__ import annotations

import hashlib
import json
import math
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bot"))
from word_lyrics_automation.fingerprint import media_fingerprint
from word_lyrics_automation.media_integrity import CHECKER_VERSION, MediaIntegrityError, validate_media


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required")
class MediaIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="lyric-integrity-")
        self.root = Path(self.temporary.name)
        self.audio = self.root / "source.wav"
        with wave.open(str(self.audio), "wb") as handle:
            handle.setnchannels(2)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(b"".join(struct.pack("<hh", int(12000 * math.sin(i * .13)), int(9000 * math.sin(i * .17))) for i in range(4000)))
        self.cache = self.root / "cache"

    def tearDown(self):
        self.temporary.cleanup()

    def encode(self, suffix, codec, *extra):
        target = self.root / ("encoded" + suffix)
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(self.audio), *extra, "-c:a", codec, str(target)], check=True, capture_output=True)
        return target

    def validate(self, path=None, **kwargs):
        path = self.audio if path is None else path
        return validate_media(path, media_fingerprint(path), self.cache, **kwargs)

    def fake_decoder(self, body):
        tool = self.root / "fake-ffmpeg"
        # A venv can live in a path containing spaces; the kernel shebang
        # parser cannot execute that literal interpreter path.
        tool.write_text("#!/usr/bin/env python3\n" + body)
        tool.chmod(0o700)
        return tool

    def test_wav_full_decode_and_bound_cached_evidence(self):
        expected = media_fingerprint(self.audio)
        result = validate_media(self.audio, expected, self.cache)
        self.assertTrue(result["passed"])
        self.assertEqual(result["decoded_samples_per_channel"], 4000)
        self.assertEqual(result["verification_scope"], "wav_full_decode_and_declared_sample_count")
        self.assertFalse(result["upstream_completeness_verified"])
        # A validated cache hit needs no executable decoder, but still binds
        # the current bytes and the queue's full fingerprint.
        hit = validate_media(self.audio, expected, self.cache, ffmpeg="nonexistent-integrity-decoder")
        self.assertTrue(hit["cache_hit"])
        self.assertEqual(hit["source_fingerprint"], expected)

    def test_flac_native_sample_count_and_pcm_md5(self):
        audio = self.encode(".flac", "flac")
        result = self.validate(audio)
        self.assertEqual(result["decoded_samples_per_channel"], 4000)
        self.assertTrue(result["flac_sample_count_verified"])
        self.assertTrue(result["flac_pcm_md5_verified"])
        self.assertEqual(result["decoded_pcm_md5"], result["flac_streaminfo"]["pcm_md5"])

    def test_flac_24_bit_pcm_checksum_is_native_not_requantized(self):
        audio = self.encode(".flac", "flac", "-sample_fmt", "s32")
        result = self.validate(audio)
        self.assertEqual(result["decoded_pcm_bits_per_sample"], 24)
        self.assertTrue(result["flac_pcm_md5_verified"])

    def test_header_count_mismatch_rejected_despite_successful_ffmpeg(self):
        audio = self.encode(".flac", "flac")
        raw = bytearray(audio.read_bytes())
        combined = int.from_bytes(raw[18:26], "big")
        raw[18:26] = ((combined & ~((1 << 36) - 1)) | 4800).to_bytes(8, "big")
        audio.write_bytes(raw)
        probe = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(audio), "-f", "null", "-"], capture_output=True)
        self.assertEqual(probe.returncode, 0)
        with self.assertRaises(MediaIntegrityError) as raised:
            self.validate(audio)
        self.assertEqual(raised.exception.code, "media_header_mismatch")
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(raised.exception.details["decoded_samples_per_channel"], 4000)
        self.assertFalse(raised.exception.details["flac_sample_count_verified"])
        with self.assertRaises(MediaIntegrityError) as cached:
            self.validate(audio, ffmpeg="missing-decoder")
        self.assertTrue(cached.exception.details["cache_hit"])

    def test_flac_pcm_checksum_mismatch_is_rejected(self):
        audio = self.encode(".flac", "flac")
        raw = bytearray(audio.read_bytes())
        raw[26:42] = b"\x11" * 16
        audio.write_bytes(raw)
        with self.assertRaises(MediaIntegrityError) as raised:
            self.validate(audio)
        self.assertEqual(raised.exception.code, "media_checksum_mismatch")
        self.assertFalse(raised.exception.details["flac_pcm_md5_verified"])

    def test_missing_flac_checksum_is_reported_not_invented(self):
        audio = self.encode(".flac", "flac")
        raw = bytearray(audio.read_bytes())
        raw[26:42] = b"\0" * 16
        audio.write_bytes(raw)
        result = self.validate(audio)
        self.assertFalse(result["flac_pcm_md5_verified"])
        self.assertTrue(any("no PCM MD5" in item for item in result["limitations"]))

    def test_truncated_flac_is_not_accepted(self):
        audio = self.encode(".flac", "flac")
        raw = audio.read_bytes()
        audio.write_bytes(raw[:-100])
        with self.assertRaises(MediaIntegrityError) as raised:
            self.validate(audio)
        self.assertIn(raised.exception.code, {"media_corrupt", "media_header_mismatch", "media_checksum_mismatch"})

    def test_m4a_and_mp3_only_claim_checkable_decode_scope(self):
        for suffix, codec in ((".m4a", "alac"), (".mp3", "libmp3lame")):
            with self.subTest(suffix=suffix):
                audio = self.encode(suffix, codec)
                result = self.validate(audio)
                self.assertTrue(result["passed"])
                self.assertFalse(result["upstream_completeness_verified"])
                self.assertEqual(result["verification_scope"], "selected_audio_stream_full_decode")
                self.assertTrue(any("truncation" in item for item in result["limitations"]))

    def test_timeout_is_retryable_and_not_cached(self):
        tool = self.fake_decoder("import time\ntime.sleep(5)\n")
        with self.assertRaises(MediaIntegrityError) as raised:
            self.validate(ffmpeg=tool, timeout_seconds=.5)
        self.assertEqual(raised.exception.code, "media_integrity_timeout")
        self.assertTrue(raised.exception.retryable)
        self.assertFalse(list(self.cache.glob("**/*.json")))

    def test_media_changed_during_decode_is_not_bound_or_cached(self):
        expected = media_fingerprint(self.audio)
        tool = self.fake_decoder("import pathlib,sys\np=pathlib.Path(sys.argv[sys.argv.index('-i')+1]);p.write_bytes(p.read_bytes()+b'changed')\nsys.stdout.buffer.write(b'\\0'*(4000*2*4))\n")
        with self.assertRaises(MediaIntegrityError) as raised:
            validate_media(self.audio, expected, self.cache, ffmpeg=tool)
        self.assertEqual(raised.exception.code, "media_changed")
        self.assertTrue(raised.exception.retryable)
        self.assertFalse(list(self.cache.glob("**/*.json")))

    def test_stale_expected_fingerprint_rejected_before_decode(self):
        expected = media_fingerprint(self.audio)
        self.audio.write_bytes(self.audio.read_bytes() + b"new-tag")
        with self.assertRaises(MediaIntegrityError) as raised:
            validate_media(self.audio, expected, self.cache, ffmpeg="missing-decoder")
        self.assertEqual(raised.exception.code, "media_changed")

    def test_resource_limits_are_enforced_for_input_decode_and_cache(self):
        with self.assertRaises(MediaIntegrityError) as raised:
            self.validate(max_file_bytes=20)
        self.assertEqual(raised.exception.code, "media_resource_limit")
        with self.assertRaises(MediaIntegrityError) as raised:
            self.validate(max_decoded_bytes=100)
        self.assertEqual(raised.exception.code, "media_resource_limit")
        self.validate()
        with self.assertRaises(MediaIntegrityError) as raised:
            self.validate(max_duration_seconds=.1)
        self.assertEqual(raised.exception.code, "media_resource_limit")

    def test_unavailable_tool_is_retryable_and_cache_corruption_rechecks(self):
        expected = media_fingerprint(self.audio)
        with self.assertRaises(MediaIntegrityError) as raised:
            self.validate(ffprobe="missing-integrity-probe")
        self.assertEqual(raised.exception.code, "media_tool_unavailable")
        self.assertTrue(raised.exception.retryable)
        cache = self.cache / CHECKER_VERSION / (expected["sha256"] + ".json")
        cache.parent.mkdir(parents=True)
        cache.write_text("not json")
        self.assertFalse(self.validate()["cache_hit"])
        self.assertEqual(json.loads(cache.read_text())["source_sha256"], expected["sha256"])

    def test_well_formed_but_invalid_cache_rechecks(self):
        expected = media_fingerprint(self.audio)
        cache = self.cache / CHECKER_VERSION / (expected["sha256"] + ".json")
        cache.parent.mkdir(parents=True)
        for bad_document in ([], {"checker_version": CHECKER_VERSION, "source_sha256": expected["sha256"], "status": "passed", "evidence": {}}):
            with self.subTest(document=bad_document):
                cache.write_text(json.dumps(bad_document))
                self.assertFalse(self.validate()["cache_hit"])

    def test_disappearing_media_has_a_typed_retryable_error(self):
        tool = self.fake_decoder("import pathlib,sys\np=pathlib.Path(sys.argv[sys.argv.index('-i')+1]);p.unlink()\nsys.stdout.buffer.write(b'\\0'*(4000*2*4))\n")
        with self.assertRaises(MediaIntegrityError) as raised:
            self.validate(ffmpeg=tool)
        self.assertEqual(raised.exception.code, "media_io_error")
        self.assertTrue(raised.exception.retryable)
        self.assertFalse(list(self.cache.glob("**/*.json")))

    def test_unsupported_container_has_an_explicit_permanent_result(self):
        audio = self.encode(".aiff", "pcm_s16be")
        with self.assertRaises(MediaIntegrityError) as raised:
            self.validate(audio)
        self.assertEqual(raised.exception.code, "media_unsupported")
        self.assertFalse(raised.exception.retryable)

    def test_decoder_diagnostic_does_not_store_credential_urls(self):
        tool = self.fake_decoder("import sys\nsys.stderr.write('bad reference https://alice:secret@example.test/audio?token=private')\nsys.exit(1)\n")
        with self.assertRaises(MediaIntegrityError) as raised:
            self.validate(ffmpeg=tool)
        document = json.dumps(raised.exception.as_dict())
        self.assertEqual(raised.exception.code, "media_corrupt")
        self.assertIn("https://[redacted]", document)
        self.assertNotIn("secret", document)
        self.assertNotIn("token=private", document)



if __name__ == "__main__":
    unittest.main()
