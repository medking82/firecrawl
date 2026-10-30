import copy
import unittest
from upgrade_state import stable_summary


def sample():
    return {'db': {'id': 'container', 'image': 'image', 'mounts': [], 'state': {
        'Running': True, 'Status': 'running', 'StartedAt': 'start', 'FinishedAt': 'finish',
        'ExitCode': 0, 'OOMKilled': False, 'Restarting': False,
        'Health': {'Status': 'healthy', 'FailingStreak': 0, 'Log': [{'End': 'old'}]}}}}


class StableSummaryTests(unittest.TestCase):
    def test_health_log_changes_are_ignored(self):
        old = sample(); new = copy.deepcopy(old)
        new['db']['state']['Health']['Log'] = [{'End': 'new'}]
        self.assertEqual(stable_summary(old), stable_summary(new))

    def test_failing_streak_and_unhealthy_rejected(self):
        for update in ({'FailingStreak': 1}, {'Status': 'unhealthy'}, {'Status': 'starting'}, {'FailingStreak': None}):
            with self.subTest(update=update):
                row = sample(); row['db']['state']['Health'].update(update)
                with self.assertRaises(ValueError): stable_summary(row)

    def test_stable_state_identity_and_mounts_are_compared(self):
        old = sample()
        for key, value in [('Running', False), ('Status', 'exited'), ('StartedAt', 'other'),
                           ('FinishedAt', 'other'), ('ExitCode', 1), ('OOMKilled', True), ('Restarting', True)]:
            with self.subTest(key=key):
                new = copy.deepcopy(old); new['db']['state'][key] = value
                self.assertNotEqual(stable_summary(old), stable_summary(new))
        for key, value in [('id', 'different'), ('image', 'different'),
                           ('mounts', [{'Destination': '/data', 'Name': 'different'}])]:
            new = copy.deepcopy(old); new['db'][key] = value
            self.assertNotEqual(stable_summary(old), stable_summary(new))

    def test_missing_state_and_health_streak_rejected(self):
        row = sample(); del row['db']['state']['FinishedAt']
        with self.assertRaises(ValueError): stable_summary(row)
        row = sample(); del row['db']['state']['Health']['FailingStreak']
        with self.assertRaises(ValueError): stable_summary(row)

    def test_no_healthcheck_is_supported(self):
        row = sample(); del row['db']['state']['Health']
        self.assertNotIn('Health', stable_summary(row)['db']['state'])


if __name__ == '__main__': unittest.main()
