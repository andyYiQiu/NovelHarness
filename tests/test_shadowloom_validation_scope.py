"""离线验证：结构保护 + 一次广义语义审计，不要求逐项交付证明。"""
import unittest

from common.novel_extract.NovelExtractReader import chunk_text
from common.novel_extract.NovelExtractRuntime import digest
from common.novel_extract.NovelExtractSchema import (
    Event, Evidence, Entity, OccurrenceSignature, RepositoryInformation,
    Review, StorySpine, TimeNode, World, time_dependencies,
)
from workflows.NovelExtractWorkflow import NovelExtractWorkflow, ReviewState


class Storage:
    def __init__(self, information):
        self.information = information

    def latest(self, key, default=None):
        return self.information if key == 'information' else default

    def get(self, key, default=None):
        return default


class ValidationScopeTests(unittest.TestCase):
    def setUp(self):
        self.chunks = chunk_text('甲离家。后来甲返回。')
        self.information = RepositoryInformation(
            story_overview='甲离家，后来返回。',
            story_spine=StorySpine(endings=['甲返回'], turning_points=['甲离家'],
                causal_chains=[[{ 'link': '甲离家', 'signature': {
                    'actual_subjects': ['ENT_1'], 'occurrence': '甲离家',
                    'start_anchor': '早年', 'layer': '实际发生'},
                    'core_reason': '离家改变后续返回的起点'}]]),
            characters={'ENT_1': {'activity': '主角', 'profile': '离家并返回的人'}},
            locations={}, background={}, relations=[], settings=[],
        ).model_dump(mode='json')
        events = []
        for index, description in enumerate(('甲离家', '甲返回'), 1):
            events.append(Event(id=f'E{index}', description=description, actor_ids=['ENT_1'],
                source_time='早年' if index == 1 else '后来', time_node_id=f'N{index}',
                event_order=1, temporal_basis='原文先说离家，明确后来返回',
                core_reason='改变此事会改变离家与返回的故事链',
                evidence=[Evidence(chunk=1, passage_ids=['C1_P1'])], epistemic_status='事实',
                occurrence_layer='实际发生', occurrence_signature=OccurrenceSignature(
                    actual_subjects=['ENT_1'], occurrence=description,
                    start_anchor='早年' if index == 1 else '后来', layer='实际发生')))
        self.world = World(entities={'ENT_1': Entity(name='甲')}, locations={}, objects={},
            world_traits={}, events=events,
            time_nodes=[TimeNode(id=f'N{i}', name=f'阶段{i}', order=i) for i in (1, 2)])
        self.workflow = NovelExtractWorkflow()
        self.workflow.storage = Storage(self.information)
        self.workflow.failures = []

    def test_public_review_asks_for_findings_not_proof_tables(self):
        fields = Review.model_json_schema()['properties']
        self.assertEqual(set(fields), {'is_valid', 'summary', 'issues', 'time_valid',
            'correction', 'time', 'covered_chunks', 'information'})

    def test_one_link_story_spine_keeps_profiles_available_for_review(self):
        self.assertEqual(len(RepositoryInformation.model_validate(
            self.information).story_spine.causal_chains[0]), 1)

    def test_full_review_needs_no_candidate_or_chain_rows(self):
        review = Review(is_valid=True, time_valid=True, summary='全文核对完成',
                        issues=[], covered_chunks=[1])
        candidate, details = self.workflow.evaluate_review(review, self.world, self.chunks)
        self.assertTrue(review.is_valid, review.issues)
        self.assertEqual(len(candidate.events), 2)
        self.assertEqual(set(details['characters']), {'ENT_1'})

    def test_missing_source_coverage_cannot_pass(self):
        review = Review(is_valid=True, time_valid=True, summary='遗漏原文块',
                        issues=[], covered_chunks=[])
        self.workflow.evaluate_review(review, self.world, self.chunks)
        self.assertFalse(review.is_valid)
        self.assertTrue(any('原文查漏' in issue for issue in review.issues))

    def test_time_is_confirmed_by_semantic_review_not_row_count(self):
        state = ReviewState()
        review = Review(is_valid=True, time_valid=True, summary='故事时间核对',
                        issues=[], covered_chunks=[1])
        self.workflow.update_time_confirmations(state, review, self.world, self.world, self.chunks)
        self.assertEqual(set(state.confirmed_times), {'E1', 'E2'})
        self.assertEqual(state.confirmed_times['E1'], digest(time_dependencies(self.world)['E1']))

    def test_time_revision_requires_new_full_review(self):
        state = ReviewState()
        review = Review(is_valid=True, time_valid=True, summary='改了时间',
                        issues=[], covered_chunks=[1])
        changed = self.world.model_copy(deep=True)
        changed.events[1].source_time = '次年'
        self.workflow.update_time_confirmations(state, review, self.world, changed, self.chunks)
        self.assertFalse(state.confirmed_times)
        self.workflow.update_time_confirmations(state, review, changed, changed, self.chunks)
        self.assertEqual(set(state.confirmed_times), {'E1', 'E2'})


if __name__ == '__main__':
    unittest.main()
