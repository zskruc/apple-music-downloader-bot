import unittest
import ingest_bot as bot


class CorrectLyricStatusMessages(unittest.TestCase):
    def notice(self, **changes):
        values=dict(job_id=18,terminal_status='completed',media_path='/music/Epilogue.m4a',
            metadata={'title':'Epilogue','artist':'贾斯汀 · 赫尔维茨'},
            result={'lyric_status':'not_required','metrics':{'timed_words':0}},
            last_error='',attempts=2,lease_token='test')
        return bot.format_word_lyrics_notification(bot.WordLyricNotification(**{**values,**changes}))

    def test_instrumental_notice_explains_normal_completion_without_claiming_word_timing(self):
        text=self.notice()
        self.assertIn('无词配乐已核验',text)
        self.assertIn('两处独立来源',text)
        self.assertNotIn('逐词歌词已就绪',text)
        self.assertNotIn('失败',text)
        self.assertIn('无需生成逐词歌词',text)

    def test_actual_failure_does_not_claim_a_nonexistent_original_lyric(self):
        text=self.notice(terminal_status='dead_letter',last_error='validation_failed')
        self.assertIn('歌词文件（如有）',text)
        self.assertNotIn('原有逐行歌词仍保留',text)

    def test_ordinary_word_completion_is_unchanged(self):
        text=self.notice(result={'provider':'netease_yrc','metrics':{'timed_words':25}})
        self.assertIn('逐词歌词已就绪',text)
        self.assertIn('25',text)


    def test_wait_notice_explains_stage_and_next_check_time(self):
        text=self.notice(terminal_status='waiting_source',next_attempt_at=1791153596.5237398,
            payload={'source_diagnostics_history':[{'reason':'no_trusted_lyric_reference'}]})
        self.assertIn('等待来源',text)
        self.assertIn('尚未开始声学对齐',text)
        self.assertIn('2026-10-05 06:39',text)
        self.assertIn('同一等待状态只提醒一次',text)
        self.assertNotIn('已就绪',text)

    def test_pending_instrumental_verification_is_distinct_from_source_outage(self):
        text=self.notice(terminal_status='waiting_source',next_attempt_at=1791153596,
            payload={'source_observations':[{'instrumental_flag':True}]})
        self.assertIn('还缺少独立的版本核验',text)
        text=self.notice(terminal_status='waiting_source',next_attempt_at=1791153596,
            payload={'source_diagnostics_history':[{'reason':'online_source_temporarily_unavailable'}]})
        self.assertIn('歌词来源暂时无法访问',text)

if __name__=='__main__': unittest.main()
