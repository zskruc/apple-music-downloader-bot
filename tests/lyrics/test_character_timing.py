import unittest
import xml.etree.ElementTree as ET
from word_lyrics_automation.character_timing import subdivide_han_word_cues, TT


def document(content):
    return f'<tt xmlns="{TT}"><head><metadata/></head><body><div begin="10"><p begin="10" end="15">{content}</p></div></body></tt>'


class CharacterTimingTests(unittest.TestCase):
    def test_text_word_envelopes_and_existing_cues_preserved(self):
        source = document('<span begin="10" end="10.8">在夜</span> <span begin="10.9" end="12.1">里微</span><span begin="12.1" end="15">亮</span>')
        result, report = subdivide_han_word_cues(source)
        spans = ET.fromstring(result).findall(f'.//{{{TT}}}p/{{{TT}}}span')
        self.assertEqual([s.text for s in spans], ['在', '夜', '里', '微', '亮'])
        self.assertEqual([s.get('begin') for s in spans], ['10', '00:00:10.400', '10.9', '00:00:11.500', '12.1'])
        self.assertEqual(spans[-1].attrib, {'begin': '12.1', 'end': '15'})
        self.assertEqual(''.join(ET.fromstring(result).find(f'.//{{{TT}}}p').itertext()), '在夜 里微亮')
        self.assertTrue(report['internal_boundaries_estimated'])
        self.assertEqual(report['added_cues'], 2)

    def test_cross_minute_and_mixed_clock_styles(self):
        result, _ = subdivide_han_word_cues(document('<span begin="59.5" end="1:00.5">星光</span>'))
        spans = ET.fromstring(result).findall(f'.//{{{TT}}}span')
        self.assertEqual(spans[0].get('end'), '00:01:00.000')
        self.assertEqual(spans[-1].get('end'), '1:00.5')

    def test_punctuation_spaces_and_combining_marks(self):
        source = document('<span begin="10" end="12">“你好，”</span> <span begin="12" end="14">Love you</span>')
        result, _ = subdivide_han_word_cues(source)
        self.assertEqual(''.join(ET.fromstring(result).find(f'.//{{{TT}}}p').itertext()), '“你好，” Love you')
        self.assertIn('>Love you</', result)

    def test_line_only_and_nonlexical_cues_untouched(self):
        for content in ['整行只有逐行时间', '<span begin="10" end="12">Du Na La</span>', '<span begin="10" end="12">光</span>']:
            source = document(content)
            self.assertEqual(subdivide_han_word_cues(source)[0], source)

    def test_relative_and_too_short_cues_are_not_fabricated(self):
        for content in ['<span begin="1s" end="2s">你好</span>', '<span begin="10" end="10.001">你好</span>']:
            source = document(content)
            result, report = subdivide_han_word_cues(source)
            self.assertEqual(result, source)
            self.assertEqual(len(report['skipped']), 1)

    def test_idempotence_and_nested_backing_vocals(self):
        source = document('<span><span begin="10" end="12">星光</span></span>')
        once, _ = subdivide_han_word_cues(source)
        twice, report = subdivide_han_word_cues(once)
        self.assertEqual(twice, once)
        self.assertEqual(report['changed_groups'], 0)

    def test_japanese_word_or_mora_cues_preserved(self):
        source=document('<span begin="10" end="12">明日</span>').replace('<tt ', '<tt xml:lang="ja" ')
        result, report=subdivide_han_word_cues(source)
        self.assertEqual(result, source)
        self.assertEqual(report['preserved_language'],'ja')

    def test_unlabelled_japanese_singing_preserved(self):
        source=document('<span begin="10" end="12">明日に陽が昇ってゆくから</span>')
        result, report=subdivide_han_word_cues(source)
        self.assertEqual(result, source)
        self.assertEqual(report['changed_groups'],0)


if __name__ == '__main__':
    unittest.main()
