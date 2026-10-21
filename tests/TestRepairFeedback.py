"""修复反馈经真实工作流返回：候选、保存结果、双向引用与恢复一致。"""
from copy import deepcopy
import unittest
from unittest.mock import patch

import test_harness as fixtures
from test_dynamic_assets import dynamic_world
from common.novel_extract.NovelExtractSchema import Correction, Relation, Review
from storage.novel_extract.NovelExtractStorage import NovelExtractStorage
from workflows.NovelExtractWorkflow import NovelExtractWorkflow


class RepairFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.harness = fixtures.HarnessTests(methodName='runTest')
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)

    def test_receipt_preserves_rejected_values_and_both_relation_endpoints(self):
        world = dynamic_world()
        edge = Relation(source=world.events[0].id, target=world.events[2].id,
                        kind='before', basis='交书之后带书离开')
        world.relations = [edge]
        removed_edge = {**edge.model_dump(), 'reason': '核对旧边'}
        submitted = Review(is_valid=False, summary='修订', issues=[], covered_chunks=[], correction=Correction(
            event_updates={world.events[0].id: {'goals': {'ENT_1': '交付书'}},
                           world.events[2].id: {'source_time': None}},
            relation_removals=[removed_edge]))
        applied = Review(is_valid=False, summary='部分接收', issues=[], covered_chunks=[], correction=Correction(
            event_updates={world.events[0].id: {'goals': {'ENT_1': '交付书'}}}))
        after = world.model_copy(deep=True)
        after.events[0].goals = {'ENT_1': '交付书'}
        receipt = NovelExtractWorkflow.repair_receipt(submitted, applied, world, after)
        rejected = receipt['unapplied']['correction']
        self.assertEqual(rejected['event_updates'], {world.events[2].id: {'source_time': None}})
        self.assertEqual(rejected['relation_removals'], [removed_edge])
        self.assertEqual(set(receipt['current_references']), {world.events[0].id, world.events[2].id})
        self.assertEqual(receipt['saved_event_delta']['changed'], [world.events[0].id])
        for row in receipt['current_references'].values():
            self.assertEqual(row['relations'], [edge.model_dump(mode='json')])

    def test_catalog_receipt_includes_state_references_without_direct_participation(self):
        world = dynamic_world()
        submitted = Review(is_valid=False, summary='核对乙的资料', issues=[], covered_chunks=[],
                           correction=Correction(entities={'ENT_2': world.entities['ENT_2']}))
        receipt = NovelExtractWorkflow.repair_receipt(submitted, submitted, world, world)
        self.assertEqual(set(receipt['current_references']), {row.id for row in world.events})
        self.assertEqual(receipt['unapplied']['correction'], {})
        self.assertEqual(world.events[0].actor_ids, ['ENT_1'])
        self.assertEqual(receipt['current_references'][world.events[0].id]['state_delta']
                         ['objects'][0]['set']['holder']['id'], 'ENT_2')

    def test_rejected_deletion_is_repaired_using_received_references(self):
        class FixDeletion(fixtures.FixtureServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job.endswith('.first_pass'):
                    for field in ('description', 'occurrence_signature'):
                        result['events'][1][field] = deepcopy(result['events'][0][field])
                if job.endswith('.consequences'):
                    result['changes'] = [{'event_id': 'EVT_C1_E1', 'subject_id': 'ENT_1',
                                          'change': '已出发', 'basis': '原文实际出发'}]
                if job == 'global.review.0':
                    result.update(is_valid=False, time=None)
                    result['correction'] = {'removals': [{'id': 'EVT_C1_E1', 'reason': '离线重复记录'}]}
                if job == 'global.review.1':
                    receipt = context['last_delivery']['review']['repair_receipt']
                    self.repair_context = deepcopy(context)
                    result.update(is_valid=False, time=None)
                    result['correction'] = deepcopy(receipt['unapplied']['correction'])
                    changes = receipt['current_references']['EVT_C1_E1']['changes']
                    result['correction']['change_removals'] = [
                        {**row, 'reason': '删除重复事件的从属记录'} for row in changes]
                receipt = context.get('last_delivery', {}).get('review', {}).get('repair_receipt', {})
                if receipt.get('accepted', {}).get('removals'):
                    self.last_receipt = receipt
                return result

        server = FixDeletion()
        self.assertEqual(self.harness.run_pipeline(server, chunks=1), 0)
        feedback = server.repair_context['last_delivery']['review']
        self.assertTrue(feedback['program_feedback'])
        self.assertTrue(server.repair_context['source_chunks'])
        self.assertTrue(feedback['repair_receipt']['current_references']['EVT_C1_E1']['exists'])
        self.assertEqual(server.last_receipt['accepted']['removals'], ['EVT_C1_E1'])
        self.assertFalse(server.last_receipt['current_references']['EVT_C1_E1']['exists'])
        saved = self.harness.snapshots()['world']
        self.assertEqual([row['id'] for row in saved['events']], ['EVT_C1_E2'])
        self.assertEqual(saved['changes'], [])

    def test_time_only_rejection_keeps_failed_plan_for_next_task(self):
        class FixTime(fixtures.FixtureServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job == 'global.review.0':
                    result['time']['timeline'][0]['node_id'] = 'UNKNOWN_NODE'
                if job == 'global.timeline.1':
                    self.receipt = context['last_review_repair']
                return result

        server = FixTime()
        self.assertEqual(self.harness.run_pipeline(server, chunks=1), 0)
        self.assertEqual(server.receipt['time'], {'submitted': True, 'accepted': False})
        self.assertEqual(server.receipt['unapplied']['time']['timeline'][0]['node_id'], 'UNKNOWN_NODE')
        self.assertFalse(server.receipt['unapplied']['correction'])

    def test_saved_repair_receipt_survives_resume_without_repeating_request(self):
        class FixGoal(fixtures.FixtureServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job == 'global.review.0':
                    result.update(is_valid=False, time=None)
                    result['correction'] = {'event_updates': {'EVT_C1_E1': {'goals': {'ENT_1': '开始行程'}}}}
                if job == 'global.review.1':
                    self.receipt = context['last_delivery']['review']['repair_receipt']
                return result

        server = FixGoal()
        original, stopped = NovelExtractStorage.set, []

        def interrupt(storage, key, value):
            original(storage, key, value)
            if key == 'review_state' and value['rounds'] == 2 and not stopped:
                stopped.append(True)
                raise KeyboardInterrupt()

        self.assertEqual(self.harness.run_pipeline(server, chunks=1, hook=patch.object(
            NovelExtractStorage, 'set', interrupt)), 130)
        state = NovelExtractStorage.read_status(self.harness.workspace)['review_state']
        self.assertNotIn('review', state['feedback'])
        self.assertEqual(self.harness.run_pipeline(server, chunks=1), 0)
        self.assertEqual(server.receipt['accepted']['event_updates'], ['EVT_C1_E1'])
        self.assertEqual(server.receipt['unapplied']['correction'], {})
        jobs = [job for job, _, _ in server.requests]
        self.assertEqual(jobs.count('global.review.0'), 1)
        self.assertEqual(self.harness.snapshots()['world']['events'][0]['goals'], {'ENT_1': '开始行程'})


if __name__ == '__main__':
    unittest.main()
