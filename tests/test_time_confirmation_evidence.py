"""故事时间由模型审读；程序只保证完整两层位置与版本一致。"""
import unittest

from common.novel_extract.NovelExtractReader import chunk_text
from common.novel_extract.NovelExtractRuntime import digest
from common.novel_extract.NovelExtractSchema import (
    Entity, Event, Evidence, Review, TimeNode, World, time_dependencies,
)
from workflows.NovelExtractWorkflow import NovelExtractWorkflow, ReviewState


def event(key, node, order, source_time):
    return Event(id=key, description=f'{key}实际发生', actor_ids=['ENT_1'],
        source_time=source_time, time_node_id=node, event_order=order,
        temporal_basis='原文开始时间线索', core_reason='改变此事会改变结局',
        evidence=[Evidence(chunk=1, passage_ids=['C1_P1'])], epistemic_status='事实',
        occurrence_layer='实际发生', occurrence_signature={
            'actual_subjects': ['ENT_1'], 'occurrence': f'{key}实际发生',
            'start_anchor': source_time, 'layer': '实际发生'})


class TimeConfirmationEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.chunks = chunk_text('甲出发。乙后来到达。丙同时到达。')
        self.world = World(entities={'ENT_1': Entity(name='甲')}, locations={},
            objects={}, world_traits={}, events=[
                event('A', 'N1', 1, '先'), event('B', 'N2', 1, '后来'),
                event('C', 'N2', 1, '与乙同时')],
            time_nodes=[TimeNode(id='N1', name='出发', order=1),
                        TimeNode(id='N2', name='抵达', order=2)])
        self.workflow = NovelExtractWorkflow()

    def test_review_schema_has_one_time_judgment_without_pair_matrix(self):
        fields = Review.model_json_schema()['properties']
        self.assertIn('time_valid', fields)
        self.assertNotIn('time_checks', fields)

    def test_full_review_can_confirm_without_per_event_rows(self):
        review = Review(is_valid=True, time_valid=True, summary='依据全文核对',
                        issues=[], covered_chunks=[1])
        state = ReviewState()
        self.workflow.update_time_confirmations(state, review, self.world, self.world, self.chunks)
        self.assertEqual(set(state.confirmed_times), {'A', 'B', 'C'})

    def test_unreviewed_or_failed_reply_cannot_confirm(self):
        for review in (
            Review(is_valid=True, summary='尚未核对时间', issues=[], covered_chunks=[1]),
            Review(is_valid=False, time_valid=True, summary='交付失败',
                   issues=['交付未通过程序校验：格式错误'], covered_chunks=[1]),
            Review(is_valid=True, time_valid=True, summary='漏读原文',
                   issues=[], covered_chunks=[]),
        ):
            with self.subTest(review=review.summary):
                state = ReviewState()
                self.workflow.update_time_confirmations(state, review, self.world, self.world, self.chunks)
                self.assertFalse(state.confirmed_times)

    def test_changed_time_needs_a_new_review(self):
        review = Review(is_valid=True, time_valid=True, summary='第一次审读',
                        issues=[], covered_chunks=[1])
        state = ReviewState()
        self.workflow.update_time_confirmations(state, review, self.world, self.world, self.chunks)
        revised = self.world.model_copy(deep=True)
        revised.events[1].source_time = '次年'
        self.workflow.update_time_confirmations(state, review, self.world, revised, self.chunks)
        self.assertNotIn('B', state.confirmed_times)
        self.workflow.update_time_confirmations(state, review, revised, revised, self.chunks)
        self.assertEqual(state.confirmed_times['B'], digest(time_dependencies(revised)['B']))

    def test_explanation_rewording_does_not_revoke_time(self):
        state = ReviewState(confirmed_times={key: digest(value)
            for key, value in time_dependencies(self.world).items()})
        revised = self.world.model_copy(deep=True)
        revised.time_nodes[0].name = '早年阶段'
        revised.events[0].temporal_basis = '说明文字改写'
        review = Review(is_valid=False, summary='未交新结论', issues=[], covered_chunks=[])
        self.workflow.update_time_confirmations(state, review, self.world, revised, self.chunks)
        self.assertEqual(set(state.confirmed_times), {'A', 'B', 'C'})


if __name__ == '__main__':
    unittest.main()
