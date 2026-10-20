from __future__ import annotations

import struct
import json
import tempfile
import unittest
from pathlib import Path

from word_lyrics_automation.fingerprint import mp4_audio_identity
from word_lyrics_automation.notifications import NotificationStore
from word_lyrics_automation.queue import QueueStore


def atom(name, payload):
    return struct.pack(">I4s", len(payload) + 8, name) + payload


def mp4(samples=b"same compressed audio", title=b"Song", gain=b"first gain", identity_atoms=None):
    tables = b"".join(atom(name, b"stable table") for name in [b"stsd", b"stts", b"stsc", b"stsz"])
    mdia = (atom(b"hdlr", b"\0" * 8 + b"soun") + atom(b"mdhd", b"stable duration")
            + atom(b"minf", atom(b"stbl", tables)))
    tags = (identity_atoms if identity_atoms is not None else atom(b"\xa9nam", atom(b"data", title)))
    tags += atom(b"\xa9cmt", atom(b"data", gain))
    moov = atom(b"trak", atom(b"mdia", mdia)) + atom(b"udta", atom(b"meta", b"\0" * 4 + atom(b"ilst", tags)))
    return atom(b"ftyp", b"M4A ") + atom(b"moov", moov) + atom(b"mdat", samples)


class TagSourceContinuityTests(unittest.TestCase):
    def test_gain_tag_change_keeps_wait_schedule_history_and_notice_but_new_audio_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);media = root / "song.m4a";media.write_bytes(mp4())
            now = [1000.0]
            q = QueueStore(root / "queue.sqlite3", clock=lambda: now[0])
            job = q.enqueue(media, metadata={"title": "Song", "notification": {"source": "telegram", "chat_id": "test"}}).job
            claim = q.claim("source", owner="source")
            q.wait_for_source(job.id, claim.lease_token, error="no_trusted_lyric_reference",
                diagnostic_payload={"audio_identity": job.payload["audio_identity"], "source_diagnostics_history": [{"reason": "no_trusted_lyric_reference"}]})
            original = q.get(job.id)
            n = NotificationStore(root / "queue.sqlite3", clock=lambda: now[0])
            notice = n.claim(owner="bot");n.delivered(job.id, notice.lease_token)
            media.write_bytes(mp4(gain=b"different gain tag"));now[0] += 20
            new = q.refresh_changed_active_jobs()[0].job
            self.assertEqual(q.get(job.id).status, "superseded")
            self.assertEqual(new.status, "waiting_source")
            self.assertEqual(new.next_attempt_at, original.next_attempt_at)
            self.assertEqual(new.payload["source_recovery"], original.payload["source_recovery"])
            self.assertEqual(new.payload["source_diagnostics_history"], original.payload["source_diagnostics_history"])
            self.assertNotEqual(new.fingerprint_data["sha256"], original.fingerprint_data["sha256"])
            self.assertIsNone(n.claim(owner="bot"))
            media.write_bytes(mp4(samples=b"a different recording"));now[0] += 20
            replacement = q.refresh_changed_active_jobs()[0].job
            self.assertEqual(replacement.status, "queued_source")
            self.assertNotIn("source_recovery", replacement.payload)

    def test_identity_tag_or_audio_table_change_is_not_a_cosmetic_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "song.m4a";path.write_bytes(mp4())
            before = mp4_audio_identity(path)
            path.write_bytes(mp4(title=b"Another Song"))
            self.assertNotEqual(mp4_audio_identity(path), before)
            path.write_bytes(mp4().replace(b"stable duration", b"change duration"))
            self.assertNotEqual(mp4_audio_identity(path), before)
            path.write_bytes(b"invalid MP4")
            self.assertIsNone(mp4_audio_identity(path))

    def test_tag_storage_order_preserves_identity_but_values_still_matter(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "song.m4a"
            title = atom(b"\xa9nam", atom(b"data", b"Song"))
            artist = atom(b"\xa9ART", atom(b"data", b"Artist"))
            album = atom(b"\xa9alb", atom(b"data", b"Album"))
            path.write_bytes(mp4(identity_atoms=artist + album + title))
            before = mp4_audio_identity(path)
            path.write_bytes(mp4(identity_atoms=title + artist + album, gain=b"rewritten gain"))
            self.assertEqual(mp4_audio_identity(path), before)
            path.write_bytes(mp4(identity_atoms=title + artist + atom(b"\xa9alb", atom(b"data", b"Other Album"))))
            self.assertNotEqual(mp4_audio_identity(path), before)

    def test_duplicate_identity_atoms_are_preserved_in_the_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "song.m4a"
            title = atom(b"\xa9nam", atom(b"data", b"Song"))
            path.write_bytes(mp4(identity_atoms=title))
            before = mp4_audio_identity(path)
            path.write_bytes(mp4(identity_atoms=title + title))
            self.assertNotEqual(mp4_audio_identity(path), before)

    def test_old_identity_is_upgraded_only_while_full_file_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);media = root / "song.m4a";media.write_bytes(mp4())
            q = QueueStore(root / "queue.sqlite3", clock=lambda: 1000)
            job = q.enqueue(media, metadata={"title": "Song"}).job
            claim = q.claim("source", owner="source")
            q.wait_for_source(job.id, claim.lease_token, error="no_trusted_lyric_reference",
                             diagnostic_payload={"audio_identity": {"method": "mp4_samples_and_identity_tags_v1"}})
            old = q.get(job.id)
            self.assertEqual(q.refresh_changed_active_jobs(), [])
            upgraded = q.get(job.id)
            self.assertEqual(upgraded.payload["audio_identity"], mp4_audio_identity(media))
            self.assertEqual(upgraded.next_attempt_at, old.next_attempt_at)
            self.assertEqual(upgraded.payload["source_recovery"], old.payload["source_recovery"])
            media.write_bytes(mp4(gain=b"gain tag changed"))
            replacement = q.refresh_changed_active_jobs()[0].job
            self.assertEqual(replacement.status, "waiting_source")
            self.assertEqual(replacement.next_attempt_at, old.next_attempt_at)
            with q.connect() as db:
                payload = replacement.payload.copy()
                payload["audio_identity"] = {"method": "mp4_samples_and_identity_tags_v1"}
                db.execute("update jobs set payload_json=? where id=?", (json.dumps(payload), replacement.id))
            media.write_bytes(mp4(samples=b"new recording"))
            changed = q.refresh_changed_active_jobs()[0].job
            self.assertEqual(changed.status, "queued_source")


if __name__ == "__main__":
    unittest.main()
