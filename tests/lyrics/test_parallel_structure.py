"""Structure-only fixtures: no fixture clock is used as acoustic evidence."""
from __future__ import annotations

import tempfile
import unittest
import xml.etree.ElementTree as ET
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

from word_lyrics_automation.matching import retime_exact_lines_to_references
from word_lyrics_automation.models import LyricLine, TimedToken
from word_lyrics_automation.nonlexical import restore_nonlexical
from word_lyrics_automation.reference_fusion import fuse_reference_words
from word_lyrics_automation.resolver import Candidate, CandidateRejected, LegacyGateway
from word_lyrics_automation.ttml import (ITUNES_NS, TTM_NS, XML_NS, LineReference,
    parse_line_references, parse_word_ttml, render_word_ttml)


def retained_native_text():
    """Load only the checked-in, wholly synthetic fixture beside this test."""
    return Path(__file__).with_name('synthetic-parallel-70.ttml').read_text()


METADATA = ('<metadata xmlns="http://www.w3.org/ns/ttml" '
    'xmlns:ttm="http://www.w3.org/ns/ttml#metadata">'
    '<ttm:agent type="person" xml:id="v1"><ttm:name type="full">Lead</ttm:name></ttm:agent>'
    '<ttm:agent type="other" xml:id="v2000" />'
    '<ttm:agent type="group" xml:id="v1000" /></metadata>')


def timed(text, begin, end, **identity):
    return LyricLine(text, begin, end, (TimedToken(text, begin, end),), **identity)


def native_rows():
    # Canonical metadata serialization matches the parser's stable form.
    metadata = ET.tostring(ET.fromstring(METADATA), encoding='unicode')
    return [
        {'text': '主唱第一句', 'begin': 10., 'end': 15., 'key': 'L5', 'agent': 'v2000', 'metadata': metadata},
        {'text': 'Backing vocals remain complete', 'begin': 9.9, 'end': 15.5,
         'key': 'L6', 'agent': 'v1', 'metadata': metadata},
        {'text': '主唱第二句', 'begin': 15.1, 'end': 20., 'key': 'L7', 'agent': 'v2000', 'metadata': metadata},
        {'text': 'Backing second line', 'begin': 15.6, 'end': 20.1,
         'key': 'L8', 'agent': 'v1', 'metadata': metadata},
    ]


def fixture(rows=None):
    rows = native_rows() if rows is None else rows
    lines = [timed(row['text'], row['begin'], row['end'], key=row.get('key'),
                   agent=row.get('agent'), metadata=row.get('metadata')) for row in rows]
    ttml = render_word_ttml(lines, duration=40, title='Structure test', artist='Lead')
    return rows, lines, ttml


class ParallelStructureTests(unittest.TestCase):
    def test_renderer_keeps_document_order_line_keys_voices_and_native_agents(self):
        rows, _, ttml = fixture()
        parsed = parse_word_ttml(ttml)
        self.assertEqual([(line.key, line.agent, line.text) for line in parsed],
                         [(row['key'], row['agent'], row['text']) for row in rows])
        self.assertLess(parsed[1].begin, parsed[0].begin)
        self.assertTrue(all(line.metadata == rows[0]['metadata'] for line in parsed))
        agents = ET.fromstring(ttml).findall('.//{%s}agent' % TTM_NS)
        self.assertEqual([(a.get('{%s}id' % XML_NS), a.get('type')) for a in agents],
                         [('v1', 'person'), ('v2000', 'other'), ('v1000', 'group')])
        LegacyGateway._validate_alignment_reference(parsed, {
            'reference_text': ''.join(row['text'] for row in rows), 'reference_lines': rows}, 40)

    def test_native_70_line_fixture_retains_all_english_and_metadata(self):
        references = parse_line_references(retained_native_text())
        self.assertEqual(len(references), 70)
        self.assertEqual(sum(any('a' <= c.casefold() <= 'z' for c in reference.text)
                             for reference in references), 16)
        lines = [timed(r.text, r.begin, r.end, key=r.key, agent=r.agent, metadata=r.metadata)
                 for r in references]
        ttml = render_word_ttml(lines, duration=240, title='Synthetic vocal timeline', artist='Fixture Ensemble')
        parsed = parse_word_ttml(ttml)
        self.assertEqual([(l.key, l.agent, l.text) for l in parsed],
                         [(r.key, r.agent, r.text) for r in references])
        self.assertEqual([l.metadata for l in parsed], [r.metadata for r in references])
        self.assertEqual({l.agent for l in parsed}, {'v1', 'v1000', 'v2000'})
        self.assertLess(parsed[5].begin, parsed[4].begin)
        LegacyGateway._validate_alignment_reference(parsed, {
            'reference_text': ''.join(r.text for r in references),
            'reference_lines': [asdict(r) for r in references]}, 240)

    def test_publication_rejects_changed_line_key_voice_metadata_or_missing_english(self):
        rows, lines, _ = fixture()
        payload = {'reference_text': ''.join(r['text'] for r in rows), 'reference_lines': rows}
        for change, reason in [({'key': 'L99'}, 'line_key'), ({'agent': 'v2000'}, 'line_agent'),
                               ({'metadata': None}, 'metadata'), ({'text': 'Backing'}, 'line_text')]:
            with self.subTest(change=change):
                damaged = list(lines)
                damaged[1] = replace(lines[1], **change)
                with self.assertRaisesRegex(CandidateRejected, reason):
                    LegacyGateway._validate_alignment_reference(damaged, payload, 40)
        with self.assertRaisesRegex(CandidateRejected, 'line_count'):
            LegacyGateway._validate_alignment_reference(lines[:1] + lines[2:], payload, 40)

    def test_publication_checks_order_and_all_nonoverlap_boundaries_per_voice(self):
        rows, lines, _ = fixture()
        payload = {'reference_text': ''.join(r['text'] for r in rows), 'reference_lines': rows}
        bad = list(lines)
        bad[2] = timed(lines[2].text, 14.9, 20, key=lines[2].key,
                       agent=lines[2].agent, metadata=lines[2].metadata)
        with self.assertRaisesRegex(CandidateRejected, 'time_window'):
            LegacyGateway._validate_alignment_reference(bad, payload, 40)
        old = {'reference_lines': [{'text': '甲', 'begin': 10, 'end': 18},
                                   {'text': '乙', 'begin': 12, 'end': 20}]}
        with self.assertRaisesRegex(CandidateRejected, 'time_window'):
            LegacyGateway._validate_alignment_reference([timed('甲', 12, 18), timed('乙', 11, 20)], old, 40)

    def test_old_native_metadata_without_vocal_agents_keeps_absent_voice(self):
        metadata = '<metadata xmlns="http://www.w3.org/ns/ttml"><title>Legacy</title></metadata>'
        line = timed('Old single voice', 10, 15, metadata=metadata)
        rendered = render_word_ttml([line], duration=40, title='T', artist='A')
        parsed, = parse_word_ttml(rendered)
        self.assertIsNone(parsed.agent)
        self.assertEqual(parsed.metadata, ET.tostring(ET.fromstring(metadata), encoding='unicode'))

    def test_renderer_refuses_duplicate_keys_and_undeclared_native_voices(self):
        _, lines, _ = fixture()
        with self.assertRaisesRegex(ValueError, 'keys are not unique'):
            render_word_ttml([lines[0], replace(lines[1], key=lines[0].key)],
                             duration=40, title='T', artist='A')
        with self.assertRaisesRegex(ValueError, 'undeclared native agent'):
            render_word_ttml([replace(lines[0], agent='v999')], duration=40, title='T', artist='A')

    def test_pretty_native_metadata_does_not_include_outer_head_whitespace(self):
        source = ('<tt xmlns="http://www.w3.org/ns/ttml">'
                  '<head>' + METADATA + '\n\t</head><body>'
                  '<p begin="10" end="15" xmlns:ttm="http://www.w3.org/ns/ttml#metadata" '
                  'ttm:agent="v1">Words</p></body></tt>')
        reference, = parse_line_references(source)
        line = timed(reference.text, reference.begin, reference.end,
                     key=reference.key, agent=reference.agent, metadata=reference.metadata)
        rendered = render_word_ttml([line], duration=40, title='T', artist='A')
        parsed, = parse_word_ttml(rendered)
        self.assertEqual(parsed.metadata, reference.metadata)

    def test_native_reference_payload_preserves_structure_and_old_missing_end(self):
        import word_lyrics as legacy
        _, _, ttml = fixture()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'native.ttml'
            path.write_text(ttml)
            document = legacy.parse_ttml(ttml)
            gateway = object.__new__(LegacyGateway)
            gateway.references = lambda path: [(Path(temporary) / 'native.ttml', document)]
            track = SimpleNamespace(path=path.with_suffix('.m4a'), title='T', artist='A', album='B',
                                    duration=40, song_id='123', isrc='')
            payload = gateway.alignment_payload(track, {'sha256': 'fixture'})
            self.assertEqual([row['key'] for row in payload['reference_lines']], ['L5', 'L6', 'L7', 'L8'])
            self.assertEqual([row['agent'] for row in payload['reference_lines']],
                             ['v2000', 'v1', 'v2000', 'v1'])
            path.write_text('<tt><body><p begin="10s">旧格式</p></body></tt>')
            rows = gateway._reference_rows(path, legacy.parse_ttml(path.read_text()))
            self.assertEqual(rows[0]['text'], '旧格式')
            self.assertIsNone(rows[0]['end'])

    def test_restoration_and_exact_retiming_keep_identity(self):
        identity = {'key': 'Native9', 'agent': 'v2000', 'metadata': native_rows()[0]['metadata']}
        references = [{'text': 'na na na', 'begin': 1, 'end': 2, **identity},
                      {'text': '春来', 'begin': 3, 'end': 5, **{**identity, 'key': 'Native10'}}]
        restored, _ = restore_nonlexical(references, [timed('春来', 3, 5)])
        self.assertEqual([(l.key, l.agent, l.metadata) for l in restored],
                         [(r['key'], r['agent'], r['metadata']) for r in references])
        reference = LineReference('春来', 10, 12, **identity)
        source = LyricLine('春来', 20, 22, (TimedToken('春', 20, 21), TimedToken('来', 21, 22)))
        result, = retime_exact_lines_to_references([reference], (source,))
        self.assertEqual((result.key, result.agent, result.metadata),
                         (identity['key'], identity['agent'], identity['metadata']))

    def test_unlabelled_provider_clock_cannot_flatten_parallel_native_voices(self):
        with self.assertRaisesRegex(ValueError, 'parallel voice identity is missing'):
            fuse_reference_words(native_rows(), [], duration=40, title='T', artist='A')

    def test_final_validation_checks_current_native_structure_even_with_valid_acoustic_report(self):
        import word_lyrics as legacy
        rows, _, ttml = fixture()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'native.ttml'
            path.write_text(ttml)
            gateway = object.__new__(LegacyGateway)
            gateway.legacy = legacy
            document = legacy.parse_ttml(ttml)
            gateway.references = lambda _: [(path, document)]
            track = SimpleNamespace(path=path.with_suffix('.m4a'), title='T', artist='A', duration=40)
            payload = {'reference_path': str(path), 'reference_text': ''.join(r['text'] for r in rows),
                       'reference_lines': rows}
            report = {'mode': 'reference_aligned', 'reference_text_similarity': 1,
                      'matched_reference_character_coverage': 1, 'raw_transcript_text_similarity': 1,
                      'unverified_line_fraction': 0}
            candidate = Candidate('mac', 'fixture', ttml, True, 'acoustic_same_file', {})
            gateway._strict_validation(track, candidate, mac_report=report, alignment_payload=payload)
            root = ET.fromstring(ttml)
            paragraphs = root.findall('.//{*}p')
            paragraphs[1].set('{%s}agent' % TTM_NS, 'v2000')
            damaged = replace(candidate, ttml=ET.tostring(root, encoding='unicode'))
            with self.assertRaisesRegex(CandidateRejected, 'line_agent'):
                gateway._strict_validation(track, damaged, mac_report=report, alignment_payload=payload)


if __name__ == '__main__':
    unittest.main()
