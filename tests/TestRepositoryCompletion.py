"""真实离线工作流到入库的完成交接；不依赖真实模型或历史产物。"""
import json
import unittest

import test_harness as fixtures
from test_dynamic_assets import DynamicServer, TEXT
from storage.novel_extract.NovelExtractStorage import NovelExtractStorage
from tools.ImportRepository import import_repository, load_repository, validate_repository
from tools.RepositoryReader import RepositoryReader


class EmptyEvents(fixtures.FixtureServer):
    def reply(self, job, attempt, context):
        if job.endswith('.reading'):
            return {'who': '甲', 'what': '未提取', 'where': '未知', 'when': '未知',
                    'why': '未知', 'how': '未知', 'core_candidates': []}
        if job.endswith(('.first_pass', '.second_pass')):
            return {'events': [], 'time_nodes': [], 'coverage': '无事件'}
        if job.startswith('global.review.'):
            return {'is_valid': True, 'time_valid': True, 'summary': '误确认空结果', 'issues': [],
                    'covered_chunks': [c['index'] for c in context['source_chunks']]}
        return super().reply(job, attempt, context)


class RepositoryCompletionTests(unittest.TestCase):
    def setUp(self):
        self.harness = fixtures.HarnessTests(methodName='runTest')
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        self.database = self.harness.directory / 'public.sqlite3'

    def import_result(self):
        return import_repository(self.harness.output, self.database, workspace=self.harness.workspace)

    def test_complete_dynamic_output_imports_and_replays_without_extra_requests(self):
        server = DynamicServer()
        self.assertEqual(self.harness.run_pipeline(server, chunks=1, text=TEXT), 0)
        self.assertEqual(len(server.requests), 8)
        self.assertEqual(self.import_result()['events'], 3)
        with RepositoryReader(self.database) as reader:
            event = reader.get_event_context('EVT_C1_E1')['event']
            self.assertEqual(event['todo'], '甲把书交给乙')
            self.assertEqual(event['epistemic_status'], '事实')
            self.assertEqual(reader.get_state_before('EVT_C1_E1'), reader.get_state_before('EVT_C1_E2'))
            self.assertEqual(reader.get_state_after_group('N1', 1), reader.get_state_before('EVT_C1_E3'))
        self.assertEqual(self.harness.run_pipeline(server, chunks=1, text=TEXT), 0)
        self.assertEqual(len(server.requests), 8)

    def test_structurally_valid_partial_is_preserved_but_never_imported(self):
        class OpenIssue(DynamicServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job.startswith('global.review.'):
                    result.update(is_valid=False, issues=['背景习俗仍待核实'])
                return result
        server = OpenIssue()
        self.assertEqual(self.harness.run_pipeline(server, chunks=1, text=TEXT), 2)
        validate_repository(load_repository(self.harness.output))
        with self.assertRaisesRegex(ValueError, "上游状态为 'partial'"):
            self.import_result()
        self.assertFalse(self.database.exists())
        requests = len(server.requests)
        self.assertEqual(self.harness.run_pipeline(server, chunks=1, text=TEXT), 2)
        self.assertEqual(len(server.requests), requests)

    def test_empty_model_result_gets_feedback_and_cannot_complete(self):
        server = EmptyEvents()
        self.assertEqual(self.harness.run_pipeline(server, chunks=1), 2)
        status = NovelExtractStorage.read_status(self.harness.workspace)
        self.assertEqual(status['status'], 'partial')
        self.assertIn('尚未提取任何核心事件', json.dumps(server.contexts, ensure_ascii=False))
        with self.assertRaisesRegex(ValueError, '仅 complete 可入库'):
            self.import_result()
        self.assertFalse(self.database.exists())

    def test_existing_review_can_repair_empty_extraction_before_import(self):
        class Repair(EmptyEvents):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job == 'global.review.1':
                    result.update(is_valid=False, time_valid=False, issues=['已补回实际发生，待排时与核对'],
                                  correction={'events': [fixtures.event().model_dump(mode='json')]})
                return result
        server = Repair()
        self.assertEqual(self.harness.run_pipeline(server, chunks=1), 0)
        self.assertIn('尚未提取任何核心事件', json.dumps(server.contexts['global.review.1'], ensure_ascii=False))
        self.assertEqual(self.import_result()['events'], 1)
        self.assertTrue(any(job.startswith('global.timeline.') for job, *_ in server.requests))


if __name__ == '__main__':
    unittest.main()
