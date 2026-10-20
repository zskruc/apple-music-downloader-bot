import io
import unittest
from unittest.mock import patch

from word_lyrics_automation.providers.netease import _request_json, search


class NeteaseTransportTests(unittest.TestCase):
    def response(self, songs, *, cloud=False):
        return {'code': 200, 'result': {'songs': [
            {'id': index + 1, 'name': title,
             'ar' if cloud else 'artists': [{'name': artist}],
             'al' if cloud else 'album': {'name': 'Test Release'},
             'dt' if cloud else 'duration': 212000}
            for index, (title, artist) in enumerate(songs)]}}

    def test_interface_endpoint_preserves_query_and_original_schema(self):
        payload={'code':200,'result':{'songs':[{'id':445867416,'name':'Epilogue',
            'artists':[{'name':'Justin Hurwitz'}],'album':{'name':'La La Land'},'duration':459507}]}}
        with patch('word_lyrics_automation.providers.netease._request_json', return_value=payload) as request:
            songs=search('Epilogue Justin Hurwitz',limit=5)
            url=request.call_args.args[0]
            self.assertIn('interface.music.163.com/api/search/get?',url)
            self.assertIn('s=Epilogue+Justin+Hurwitz',url)
            self.assertEqual(len(songs),1)
            self.assertEqual(songs[0].duration_ms,459507)
            self.assertEqual(songs[0].artist,'Justin Hurwitz')

    def test_transient_failure_uses_native_cloudsearch_schema(self):
        payload={'code':200,'result':{'songs':[{'id':42,'name':'真实歌曲',
            'ar':[{'name':'真实艺人'}],'al':{'name':'真实专辑'},'dt':180000}]}}
        with patch('word_lyrics_automation.providers.netease._request_json',
                   side_effect=[TimeoutError('primary unavailable'),payload]) as request:
            song=search('真实歌曲 真实艺人')[0]
            self.assertEqual(request.call_count,2)
            self.assertIn('/api/cloudsearch/pc?',request.call_args.args[0])
            self.assertEqual((song.title,song.artist,song.album,song.duration_ms),('真实歌曲','真实艺人','真实专辑',180000))

    def test_legitimate_empty_result_is_not_retried_or_replaced_with_popular_songs(self):
        with patch('word_lyrics_automation.providers.netease._request_json',
                   return_value={'code':200,'result':{'songCount':0}}) as request:
            self.assertEqual(search('not-a-real-song'),[])
            self.assertEqual(request.call_count,1)

    def test_unrelated_http_200_results_try_cloudsearch_instead_of_becoming_normal_absence(self):
        unrelated = self.response([('互动游戏音乐', '张敏敏'), ('6%', '江辰'),
            ('借口搁浅', '-6'), ('猪八戒吃西瓜', '张敏敏')])
        correct = self.response([('长路漫漫伴你闯', '林子祥')], cloud=True)
        with patch('word_lyrics_automation.providers.netease._request_json',
                   side_effect=[unrelated, correct]) as request:
            songs = search('天长地久 长路漫漫伴你闯 林子祥')
        self.assertEqual(request.call_count, 2)
        self.assertIn('interface.music.163.com/api/search/get?', request.call_args_list[0].args[0])
        self.assertIn('music.163.com/api/cloudsearch/pc?', request.call_args_list[1].args[0])
        self.assertEqual([(song.title, song.artist) for song in songs], [('长路漫漫伴你闯', '林子祥')])

    def test_both_endpoints_returning_unrelated_results_raise_a_visible_failure(self):
        unrelated = self.response([('互动游戏音乐', '张敏敏'), ('借口搁浅', '-6')])
        with patch('word_lyrics_automation.providers.netease._request_json',
                   return_value=unrelated) as request:
            with self.assertRaisesRegex(RuntimeError, 'endpoints unavailable.*unrelated results') as raised:
                search('长路漫漫伴你闯 林子祥')
        self.assertEqual(request.call_count, 2)
        self.assertEqual(str(raised.exception).count('no title or artist query association'), 2)

    def test_healthy_explicit_empty_list_still_does_not_try_backup(self):
        with patch('word_lyrics_automation.providers.netease._request_json',
                   return_value={'code': 200, 'result': {'songs': [], 'songCount': 0}}) as request:
            self.assertEqual(search('真实但尚无搜索记录的歌'), [])
        self.assertEqual(request.call_count, 1)

    def test_related_titles_or_credited_artists_keep_a_nonempty_search_response(self):
        cases = [
            ('Album Epilogue Justin Hurwitz', [('Other', 'Unrelated'), ('Epilogue', 'Another Artist')]),
            ('Epilogue Justin Hurwitz', [('Other Song', 'Justin Hurwitz')]),
            ('Ｆｉｒｓｔ　Ｌｏｖｅ Artist', [('First Love', 'Someone Else')]),
            ('周杰倫 愛在西元前', [('爱在西元前', '其他艺人')]),
            ('菊花台 周杰倫', [('另一首歌', '周杰伦')]),
            ('ガラス 赤い鳥', [('ガラス', 'Other')]),
            ('Cafe\u0301 du soir Artist', [('Café du soir', 'Other')]),
        ]
        for query, songs in cases:
            with self.subTest(query=query), patch('word_lyrics_automation.providers.netease._request_json',
                    return_value=self.response(songs)) as request:
                returned = search(query)
                self.assertEqual(len(returned), len(songs))
                self.assertEqual(request.call_count, 1)

    def test_single_character_song_titles_are_preserved_as_complete_query_terms(self):
        for query, title in [('画 赵雷', '画'), ('I Artist', 'I'), ('6 Artist', '6%')]:
            with self.subTest(query=query), patch('word_lyrics_automation.providers.netease._request_json',
                    return_value=self.response([(title, 'Other')])) as request:
                self.assertEqual(search(query)[0].title, title)
                self.assertEqual(request.call_count, 1)

    def test_short_artist_and_word_or_numeric_fragments_do_not_validate_unrelated_responses(self):
        cases = [
            ('长路漫漫伴你闯 201576333 林子祥', [('借口搁浅', '-6')]),
            ('Original Song 1996 Artist', [('6%', '-6')]),
            ('Epilogue Justin Hurwitz', [('I', '-6')]),
            ('Glove Artist', [('Love', '-6')]),
            ('画心 艺人', [('画', '-6')]),
        ]
        for query, songs in cases:
            with self.subTest(query=query), patch('word_lyrics_automation.providers.netease._request_json',
                    return_value=self.response(songs)) as request:
                with self.assertRaisesRegex(RuntimeError, 'unrelated results'):
                    search(query)
                self.assertEqual(request.call_count, 2)

    def test_transport_association_does_not_filter_or_accept_recordings_for_the_resolver(self):
        # Related metadata validates the response, not every returned row.
        # Keep the original list so the resolver still applies its recording
        # identity, duration and lyric quality gates itself.
        response = self.response([('Other', 'Unrelated'), ('Test Song', 'Different Artist')])
        response['result']['songs'][1]['duration'] = 999000
        with patch('word_lyrics_automation.providers.netease._request_json', return_value=response):
            songs = search('Test Song Expected Artist')
        self.assertEqual([song.title for song in songs], ['Other', 'Test Song'])
        self.assertEqual(songs[1].artist, 'Different Artist')
        self.assertEqual(songs[1].duration_ms, 999000)

    def test_malformed_response_tries_backup_and_complete_outage_is_visible(self):
        with patch('word_lyrics_automation.providers.netease._request_json',
                   side_effect=[{'result':'invalid'},TimeoutError('backup unavailable')]):
            with self.assertRaisesRegex(RuntimeError,'endpoints unavailable.*TimeoutError'):
                search('Epilogue Justin Hurwitz')

    def test_provider_error_code_is_not_silently_treated_as_missing_lyrics(self):
        with patch('urllib.request.urlopen',return_value=io.BytesIO(b'{"code":-462,"message":"blocked"}')):
            with self.assertRaisesRegex(RuntimeError,'NetEase API error -462'):
                _request_json('https://music.163.com/api/song/lyric?id=42')


if __name__=='__main__':unittest.main()
