"""Small valid GGML headers and explicit synthetic DTW observations for tests."""
import struct


def model_bytes(*, text_layers=4, suffix=b''):
    return struct.pack('<12I', 0x67676D6C, 51866, 1500, 1280, 20, 32,
                       448, 1280, 20, text_layers, 128, 2008) + suffix


def dtw_document(text, *, language='ja', start_ms=1250, step_ms=700, text_layers=4):
    tokens = [{'text': char, 'id': 1000 + index, 't_dtw': (start_ms + index * step_ms) / 10,
               'offsets': {'from': start_ms + index * step_ms,
                           'to': start_ms + (index + 1) * step_ms}, 'p': .9}
              for index, char in enumerate(text)]
    # An explicit observed punctuation boundary bounds the last lexical token.
    # Its own final group remains unknown; no offsets are used as DTW evidence.
    tokens.append({'text': '。', 'id': 1543, 't_dtw': (start_ms + len(text) * step_ms) / 10,
                   'offsets': {'from': 0, 'to': 0}, 'p': .9})
    return {'model': {'multilingual': True, 'vocab': 51866, 'mels': 128,
                      'audio': {'ctx': 1500, 'state': 1280, 'head': 20, 'layer': 32},
                      'text': {'ctx': 448, 'state': 1280, 'head': 20, 'layer': text_layers}},
            'result': {'language': language}, 'transcription': [{'tokens': tokens}]}
