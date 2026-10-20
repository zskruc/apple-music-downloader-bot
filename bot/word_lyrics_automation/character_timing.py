"""Subdivide existing Han word cues for character display.

Only internal boundaries are interpolated. This is a display adaptation of an
existing word clock, not new acoustic timing. Original text, word envelopes,
untimed text, other language words, and all existing single-character cues stay
unchanged. The caller must retain the original document and provenance report.
"""
from __future__ import annotations

import re
import unicodedata
import xml.etree.ElementTree as ET
from decimal import Decimal, ROUND_HALF_UP

TT = 'http://www.w3.org/ns/ttml'
TTM = 'http://www.w3.org/ns/ttml#metadata'
ITUNES = 'http://music.apple.com/lyric-ttml-internal'

ET.register_namespace('', TT)
ET.register_namespace('ttm', TTM)
ET.register_namespace('itunes', ITUNES)


def is_han(character: str) -> bool:
    code = ord(character)
    return code in {0x3005, 0x3007} or any(a <= code <= b for a, b in (
        (0x3400, 0x4dbf), (0x4e00, 0x9fff), (0xf900, 0xfaff),
        (0x20000, 0x323af),
    ))


def absolute_milliseconds(expression: str) -> Decimal:
    if not re.fullmatch(r'\d+(?:\.\d+)?(?::\d+(?:\.\d+)?){0,2}', expression):
        raise ValueError('only existing absolute clock times may be subdivided')
    seconds = Decimal(0)
    for part in expression.split(':'):
        seconds = seconds * 60 + Decimal(part)
    return seconds * 1000


def clock(milliseconds: Decimal) -> str:
    value = int(milliseconds.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    hours, value = divmod(value, 3_600_000)
    minutes, value = divmod(value, 60_000)
    seconds, fraction = divmod(value, 1000)
    return f'{hours:02}:{minutes:02}:{seconds:02}.{fraction:03}'


def character_units(text: str) -> list[str]:
    units: list[str] = []
    prefix = ''
    last_han = False
    for character in text:
        if is_han(character):
            units.append(prefix + character)
            prefix = ''
            last_han = True
        elif unicodedata.category(character)[0] in {'L', 'N'}:
            if units and not last_han:
                units[-1] += character
            else:
                units.append(prefix + character)
                prefix = ''
            last_han = False
        elif units:
            units[-1] += character
        else:
            prefix += character
    if prefix:
        if units:
            units[-1] += prefix
        else:
            return [prefix]
    return units


def subdivide_han_word_cues(document: str) -> tuple[str, dict]:
    root = ET.fromstring(document)
    language = root.get('{http://www.w3.org/XML/1998/namespace}lang', '').lower()
    body_text = ''.join(''.join(e.itertext()) for e in root.iter() if e.tag == f'{{{TT}}}p')
    kana = sum(0x3040 <= ord(c) <= 0x30ff for c in body_text)
    inferred_japanese = not language and kana >= 8
    if language == 'ja' or language.startswith('ja-') or inferred_japanese:
        return document, {'method': 'native_word_envelope_character_interpolation',
                          'internal_boundaries_estimated': False,
                          'original_word_envelopes_preserved': True,
                          'changed_groups': 0, 'added_cues': 0,
                          'groups': [], 'skipped': [],
                          'preserved_language': language or 'ja-inferred-from-kana'}
    original_rows = [''.join(e.itertext()) for e in root.iter() if e.tag == f'{{{TT}}}p']
    changes = []
    ignored = []
    for parent in list(root.iter()):
        index = 0
        while index < len(parent):
            span = parent[index]
            text = span.text or ''
            if (span.tag != f'{{{TT}}}span' or len(span)
                    or sum(is_han(c) for c in text) < 2
                    or not span.get('begin') or not span.get('end')):
                index += 1
                continue
            begin_raw, end_raw = span.attrib['begin'], span.attrib['end']
            try:
                begin, end = absolute_milliseconds(begin_raw), absolute_milliseconds(end_raw)
                units = character_units(text)
                if len(units) < 2 or end - begin < len(units):
                    raise ValueError('word clock has insufficient millisecond precision')
                boundaries = [begin] + [
                    (begin + (end - begin) * Decimal(i) / Decimal(len(units))).quantize(Decimal(1), rounding=ROUND_HALF_UP)
                    for i in range(1, len(units))
                ] + [end]
                if any(a >= b for a, b in zip(boundaries, boundaries[1:])):
                    raise ValueError('subdivision would create a nonpositive cue')
            except ValueError as exc:
                ignored.append({'text': text, 'begin': begin_raw, 'end': end_raw, 'reason': str(exc)})
                index += 1
                continue
            replacements = []
            for position, unit in enumerate(units):
                attributes = dict(span.attrib)
                attributes['begin'] = begin_raw if position == 0 else clock(boundaries[position])
                attributes['end'] = end_raw if position == len(units) - 1 else clock(boundaries[position + 1])
                part = ET.Element(span.tag, attributes)
                part.text = unit
                replacements.append(part)
            replacements[-1].tail = span.tail
            parent.remove(span)
            for position, part in enumerate(replacements):
                parent.insert(index + position, part)
            changes.append({'text': text, 'begin': begin_raw, 'end': end_raw,
                            'characters': [p.text for p in replacements],
                            'boundaries_ms': [float(b) for b in boundaries]})
            index += len(replacements)
    assert original_rows == [''.join(e.itertext()) for e in root.iter() if e.tag == f'{{{TT}}}p'], 'lyric text changed'
    report = {'method': 'native_word_envelope_character_interpolation',
              'internal_boundaries_estimated': True,
              'original_word_envelopes_preserved': True,
              'changed_groups': len(changes),
              'added_cues': sum(len(c['characters']) - 1 for c in changes),
              'groups': changes, 'skipped': ignored}
    if not changes:
        return document, report
    head = root.find(f'{{{TT}}}head')
    if head is None:
        head = ET.Element(f'{{{TT}}}head')
        root.insert(0, head)
    metadata = head.find(f'{{{TT}}}metadata')
    if metadata is None:
        metadata = ET.SubElement(head, f'{{{TT}}}metadata')
    description = ET.SubElement(metadata, f'{{{TTM}}}desc')
    description.text = ('Han character display: internal boundaries of existing word cues '
                        'are interpolated within their unchanged original envelopes. '
                        'Original single-character cue timings and lyric text are preserved.')
    result = ET.tostring(root, encoding='unicode', xml_declaration=True)
    return result + '\n', report
