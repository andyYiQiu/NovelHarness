"""partial 收尾的一次六轮补救：修订、排时、核对、断点及预算边界。"""
from unittest.mock import patch
import unittest

import test_harness as fixtures
from exporters.NovelExtractExporter import NovelExtractExporter
from storage.novel_extract.NovelExtractStorage import NovelExtractStorage
from tools.ImportRepository import import_repository


class NeedsRecovery(fixtures.FixtureServer):
    def __init__(self, repair_round=None):
        super().__init__()
        self.repair_round = repair_round

    def reply(self, job, attempt, context):
        result = super().reply(job, attempt, context)
        if job.startswith('global.review.'):
            recovery = context.get('recovery_progress', {}).get('round', 0)
            if not self.repair_round or recovery <= self.repair_round:
                result.update(is_valid=False, issues=['尚有原文实际发生未收录'])
            if recovery and recovery == self.repair_round:
                result['correction']['events'] = [fixtures.event(
                    id='EVT_C1_LATE', description='甲完成行程',
                    occurrence_signature={**fixtures.occurrence_signature(), 'occurrence': '甲完成行程'},
                    time_node_id=None, event_order=None).model_dump(mode='json')]
        return result


class PartialRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.harness = fixtures.HarnessTests(methodName='runTest')
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)

    def status(self):
        return NovelExtractStorage.read_status(self.harness.workspace)

    def test_normal_complete_never_starts_recovery(self):
        server = fixtures.FixtureServer()
        self.assertEqual(self.harness.run_pipeline(server, chunks=1), 0)
        self.assertIsNone(self.status()['review_state']['recovery_start_round'])
        self.assertEqual(len(server.requests), 8)
        self.assertEqual(self.status()['policy']['partial_recovery_rounds'], 6)

    def test_added_fact_gets_time_and_review_and_stops_as_soon_as_complete(self):
        server = NeedsRecovery(repair_round=1)
        self.assertEqual(self.harness.run_pipeline(server, chunks=1), 0)
        state = self.status()['review_state']
        self.assertEqual(state['rounds'] - state['recovery_start_round'], 3)
        self.assertEqual([row['task'] for row in state['history'][-3:]], ['review', 'timeline', 'review'])
        self.assertEqual(len(state['confirmed_times']), 3)
        self.assertEqual(self.harness.snapshots()['failures'], [])
        counts = import_repository(self.harness.output, self.harness.directory / 'public.sqlite3',
                                   workspace=self.harness.workspace)
        self.assertEqual(counts['events'], 3)

    def test_sixth_recovery_round_can_finish_but_cannot_grant_more_rounds(self):
        server = NeedsRecovery(repair_round=4)
        self.assertEqual(self.harness.run_pipeline(server, chunks=1), 0)
        state = self.status()['review_state']
        self.assertEqual(state['rounds'] - state['recovery_start_round'], 6)
        self.assertTrue(state['accepted'])

    def test_sixth_round_unplaced_fact_stays_partial_without_a_seventh_round(self):
        server = NeedsRecovery(repair_round=6)
        self.assertEqual(self.harness.run_pipeline(server, chunks=1), 2)
        state = self.status()['review_state']
        self.assertEqual(state['rounds'] - state['recovery_start_round'], 6)
        self.assertEqual(len(self.harness.snapshots()['world']['events']), 3)
        self.assertFalse(state['accepted'])

    def test_recovery_starts_after_existing_normal_handoff_is_exhausted(self):
        class Changing(NeedsRecovery):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job.startswith('global.review.') and not context.get('recovery_progress'):
                    index = job.rsplit('.', 1)[-1]
                    result['correction']['world_traits'] = {f'WORLD_{index}': '待核实的事实'}
                return result
        self.assertEqual(self.harness.run_pipeline(Changing(), chunks=1), 2)
        state = self.status()['review_state']
        self.assertEqual(state['recovery_start_round'], 9)
        self.assertEqual(state['rounds'], 15)

    def test_unsolved_partial_keeps_data_and_never_restarts_recovery(self):
        server = NeedsRecovery()
        self.assertEqual(self.harness.run_pipeline(server, chunks=1), 2)
        state = self.status()['review_state']
        self.assertEqual(state['rounds'] - state['recovery_start_round'], 6)
        self.assertEqual(len(self.harness.snapshots()['world']['events']), 2)
        jobs = len(server.requests)
        self.assertEqual(self.harness.run_pipeline(server, chunks=1), 2)
        self.assertEqual(len(server.requests), jobs)
        with self.assertRaisesRegex(ValueError, '仅 complete 可入库'):
            import_repository(self.harness.output, self.harness.directory / 'public.sqlite3',
                              workspace=self.harness.workspace)

    def test_recovery_transaction_resume_preserves_six_round_budget_and_cached_reply(self):
        server = NeedsRecovery()
        original, stopped = NovelExtractStorage.set, []
        def interrupt(storage, key, value):
            original(storage, key, value)
            if (key == 'review_state' and value.get('recovery_start_round') is not None
                    and value['rounds'] - value['recovery_start_round'] == 2 and not stopped):
                stopped.append(True)
                raise KeyboardInterrupt()
        self.assertEqual(self.harness.run_pipeline(server, chunks=1, hook=patch.object(
            NovelExtractStorage, 'set', interrupt)), 130)
        state = self.status()['review_state']
        self.assertEqual(state['rounds'] - state['recovery_start_round'], 1)
        self.assertFalse(self.harness.output.exists())
        self.assertEqual(self.harness.run_pipeline(server, chunks=1), 2)
        state = self.status()['review_state']
        self.assertEqual(state['rounds'] - state['recovery_start_round'], 6)
        jobs = [job for job, _, _ in server.requests]
        self.assertEqual(len(jobs), len(set(jobs)))

    def test_request_limit_pauses_recovery_without_reset_or_partial_export(self):
        server = NeedsRecovery()
        self.assertEqual(self.harness.run_pipeline(server, chunks=1, max_calls=10), 75)
        status = self.status()
        state = status['review_state']
        self.assertEqual(status['status'], 'working')
        self.assertEqual(state['rounds'] - state['recovery_start_round'], 1)
        self.assertEqual(len(server.requests), 10)
        self.assertFalse(self.harness.output.exists())
        self.assertEqual(self.harness.run_pipeline(server, chunks=1, max_calls=10), 75)
        self.assertEqual(self.status()['review_state'], state)
        self.assertEqual(len(server.requests), 10)

    def test_export_only_failure_also_gets_exactly_one_recovery_allowance(self):
        server = fixtures.FixtureServer()
        original = NovelExtractExporter.project
        def blocked(exporter, world):
            files, excluded = original(exporter, world)
            return files, excluded + [{'kind': 'fixture', 'reason': '离线模拟未解决导出引用'}]
        with patch.object(NovelExtractExporter, 'project', blocked):
            self.assertEqual(self.harness.run_pipeline(server, chunks=1), 2)
        state = self.status()['review_state']
        self.assertEqual(state['rounds'] - state['recovery_start_round'], 6)
        self.assertEqual(len(server.requests), 14)


if __name__ == '__main__':
    unittest.main()
