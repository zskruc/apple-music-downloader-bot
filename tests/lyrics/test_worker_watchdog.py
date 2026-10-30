import unittest
from word_lyrics_automation.worker_watchdog import recovery_decision


class WatchdogTests(unittest.TestCase):
    def test_dependency_failure_and_bad_input_do_not_restart(self):
        for status in ['degraded','needs_attention']:
            action,_=recovery_decision({'liveness_ok':True,'status':status},
                {'pid':9,'queue':{'quick_check':'ok'}},{},1000,identity=lambda pid:True)
            self.assertEqual(action,'none')

    def test_confirmed_stale_worker_is_terminated_then_killed_with_cooldown(self):
        report={'liveness_ok':False,'restart_recommended':True};worker={'pid':9,'queue':{'quick_check':'ok'}}
        action,state=recovery_decision(report,worker,{},1000,identity=lambda pid:True)
        self.assertEqual(action,'confirm')
        action,state=recovery_decision(report,worker,state,1030,identity=lambda pid:True)
        self.assertEqual(action,'terminate')
        action,state=recovery_decision(report,worker,state,1060,identity=lambda pid:True)
        self.assertEqual(action,'kill')
        action,_=recovery_decision(report,worker,state,1090,identity=lambda pid:True)
        self.assertEqual(action,'cooldown')

    def test_new_heartbeat_or_pid_generation_cancels_signal(self):
        from word_lyrics_automation.worker_watchdog import signal_still_safe
        original={'pid':9,'owner':'one','updated_at':100}
        self.assertFalse(signal_still_safe(original,{'pid':9,'owner':'one','updated_at':990},now=1000,max_age=300,expected_generation='a',actual_generation='a'))
        self.assertFalse(signal_still_safe(original,original,now=1000,max_age=300,expected_generation='a',actual_generation='b'))
        self.assertFalse(signal_still_safe(original,{'pid':9,'owner':'two','updated_at':100},now=1000,max_age=300,expected_generation='a',actual_generation='a'))
        self.assertTrue(signal_still_safe(original,original,now=1000,max_age=300,expected_generation='a',actual_generation='a'))

    def test_reused_pid_and_broken_database_are_not_killed(self):
        for quick,identity in [('ok',False),('corrupt',True)]:
            action,_=recovery_decision({'liveness_ok':False,'restart_recommended':True},
                {'pid':9,'queue':{'quick_check':quick}},{'consecutive_stale':9},1000,identity=lambda pid:identity)
            self.assertEqual(action,'inspect')


if __name__=='__main__':unittest.main()
