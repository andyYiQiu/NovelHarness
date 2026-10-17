"""正式78暴露的前置容错与上轮校验范围反例。"""
from types import SimpleNamespace
import unittest

from pydantic import ValidationError

from common.novel_extract.NovelExtractSchema import (
    CandidateCheck,
    Change,
    Event,
    Evidence,
    Foundation,
    TimeNode,
    World,
    fact_dependencies,
    information_dependency,
    time_comparisons,
    timeline_errors,
)


def event(key, chunk, node, order):
    return Event(
        id=key,
        description=key + ' 发生',
        actor_ids=['ENT_1'],
        source_time='原文时间锚点',
        time_node_id=node,
        event_order=order,
        temporal_basis='依据原文比较开始',
        core_reason='主线事件',
        evidence=[Evidence(chunk=chunk, passage_ids=[f'C{chunk}_P1'])],
        epistemic_status='事实',
        occurrence_layer='实际发生',
        occurrence_signature={'actual_subjects': ['ENT_1'], 'occurrence': key + ' 发生',
                              'start_anchor': '原文时间锚点', 'layer': '实际发生'},
    )


class SchemaRegressionTests(unittest.TestCase):
    def test_candidate_check_duplicate_event_ids_are_normalized(self):
        check = CandidateCheck(
            candidate_id='C1_K1', event_ids=['E1', 'E1', 'E2'], reason='原文支持',
            disposition='mapped',
            signature={
                'actual_subjects': ['ENT_1'], 'occurrence': '甲出发',
                'start_anchor': '原文时间锚点', 'layer': '实际发生',
            },
        )
        self.assertEqual(check.event_ids, ['E1', 'E2'])

    def test_foundation_moves_unambiguous_root_catalog_field(self):
        value = Foundation.model_validate({
            'catalog': {'entities': {}, 'locations': {}, 'objects': {}},
            'world_traits': {'WORLD_1': '鲁镇年终祝福'},
            'information': {},
        })
        self.assertEqual(value.catalog.world_traits, {'WORLD_1': '鲁镇年终祝福'})

    def test_foundation_does_not_guess_conflicting_catalog_field(self):
        with self.assertRaisesRegex(ValidationError, '内容冲突'):
            Foundation.model_validate({
                'catalog': {'entities': {}, 'locations': {}, 'objects': {},
                            'world_traits': {'WORLD_1': '已保存规则'}},
                'world_traits': {'WORLD_1': '冲突规则'},
            })

    def test_foundation_still_rejects_genuinely_missing_catalog_field(self):
        with self.assertRaises(ValidationError):
            Foundation.model_validate({'catalog': {'entities': {}, 'locations': {}, 'objects': {}}})

    def test_unreferenced_world_trait_does_not_invalidate_every_chunk(self):
        first = event('E1', 1, 'N1', 1)
        second = event('E2', 2, 'N2', 1)
        value = World(
            entities={'ENT_1': {'name': '甲'}}, locations={}, objects={},
            world_traits={'WORLD_1': '被引用', 'WORLD_2': '无关说明'},
            events=[first, second],
            changes=[Change(event_id='E1', subject_id='WORLD_1', change='规则生效', basis='原文')],
            time_nodes=[TimeNode(id='N1', name='早期', order=1),
                        TimeNode(id='N2', name='后期', order=2)],
        )
        chunks = [SimpleNamespace(index=1), SimpleNamespace(index=2)]
        before = fact_dependencies(value, chunks)
        unrelated = value.model_copy(deep=True)
        unrelated.world_traits['WORLD_2'] = '只改说明'
        self.assertEqual(before, fact_dependencies(unrelated, chunks))
        self.assertNotEqual(information_dependency(value, {}), information_dependency(unrelated, {}))
        related = value.model_copy(deep=True)
        related.world_traits['WORLD_1'] = '实际规则已改'
        after = fact_dependencies(related, chunks)
        self.assertNotEqual(before[1], after[1])
        self.assertEqual(before[2], after[2])

    def test_time_comparisons_use_a_transitive_chain_without_losing_order_coverage(self):
        value = World(
            entities={'ENT_1': {'name': '甲'}}, locations={}, objects={}, world_traits={},
            events=[event('A1', 1, 'N1', 1), event('A2', 1, 'N1', 2),
                    event('B1', 2, 'N2', 1), event('B2', 2, 'N2', None)],
            time_nodes=[TimeNode(id='N1', name='早期', order=1),
                        TimeNode(id='N2', name='后期', order=2)],
        )
        pairs = time_comparisons(value)
        between = {(row['left'], row['right']) for row in pairs if row['scope'] == 'between_nodes'}
        self.assertEqual(between, {('A2', 'B1')})
        self.assertIn(('B1', 'B2'),
                      {(row['left'], row['right']) for row in pairs if row['scope'] == 'within_node'})
        self.assertEqual(len(pairs), len(value.events) - 1)

    def test_unknown_or_duplicate_node_order_creates_no_fake_adjacency(self):
        for orders in ((None, None), (1, 1)):
            with self.subTest(orders=orders):
                value = World(
                    entities={'ENT_1': {'name': '甲'}}, locations={}, objects={}, world_traits={},
                    events=[event('A', 1, 'N1', 1), event('B', 2, 'N2', 1)],
                    time_nodes=[TimeNode(id='N1', name='节点一', order=orders[0]),
                                TimeNode(id='N2', name='节点二', order=orders[1])],
                )
                self.assertFalse(any(row['scope'] == 'between_nodes'
                                     for row in time_comparisons(value)))

    def test_two_level_orders_must_use_dense_sequence_numbers(self):
        value = World(
            entities={'ENT_1': {'name': '甲'}}, locations={}, objects={}, world_traits={},
            events=[event('A', 1, 'N1', 1), event('B', 1, 'N1', 3), event('C', 2, 'N2', 1)],
            time_nodes=[TimeNode(id='N1', name='早期', order=1),
                        TimeNode(id='N2', name='后期', order=3)],
        )
        errors = timeline_errors(value)
        self.assertTrue(any('nodes.order 须从1连续编号' in error for error in errors))
        self.assertTrue(any('N1内events.order须从1连续编号' in error for error in errors))

        incomplete = value.model_copy(deep=True)
        incomplete.time_nodes[1].order = None
        incomplete.events[2].event_order = None
        complete_errors = timeline_errors(incomplete, require_complete=True)
        self.assertTrue(any('时间节点缺少实际开始顺序' in error for error in complete_errors))
        self.assertTrue(any('C 缺少节点内开始顺序' in error for error in complete_errors))


if __name__ == '__main__':
    unittest.main()
