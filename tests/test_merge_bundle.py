"""跨块身份合并包：一次反馈全部缺项，合法包原子改写目录、档案和引用。"""
import unittest

from common.novel_extract.NovelExtractSchema import (
    Correction,
    Change,
    Entity,
    Event,
    EventMerge,
    EventRemoval,
    Evidence,
    Foundation,
    FoundationReview,
    Location,
    Object,
    Relation,
    Register,
    RegisterCorrection,
    Review,
    TimeNode,
    TimePlacement,
    Timeline,
    World,
    apply_correction,
    apply_foundation_review,
    apply_information,
    merge_bundle_errors,
)
from common.novel_extract.NovelExtractReader import chunk_text
from common.novel_extract.NovelExtractRuntime import DeliveryStructureError
from workflows.NovelExtractWorkflow import NovelExtractWorkflow


def profile(activity='npc', **values):
    return {
        'activity': activity, 'nickname': None, 'profile': None, 'gender': None,
        'identity': None, 'age': None, 'personality': None, 'biography': None,
        **values,
    }


def event_signature(actor, occurrence, start='原文阶段'):
    return {'actual_subjects': [actor], 'occurrence': occurrence,
            'start_anchor': start, 'layer': '实际发生'}


class MergeBundleTests(unittest.TestCase):
    def test_invalid_removal_does_not_discard_independent_exact_merge(self):
        chunks = chunk_text('甲离开。\n甲离开。\n乙抵达。')
        first = Event(
            id='EVT_1', description='甲离开', actor_ids=['ENT_1'],
            time_node_id='N1', event_order=1, temporal_basis='先离开',
            core_reason='乙随后才抵达', epistemic_status='事实', occurrence_layer='实际发生',
            occurrence_signature=event_signature('ENT_1', '甲离开'),
            evidence=[Evidence(chunk=1, passage_ids=['C1_P1'])])
        duplicate = first.model_copy(update={
            'id': 'EVT_2', 'evidence': [Evidence(chunk=1, passage_ids=['C1_P2'])]}, deep=True)
        unrelated = Event(
            id='EVT_3', description='乙抵达', actor_ids=['ENT_2'],
            time_node_id='N2', event_order=1, temporal_basis='后抵达',
            core_reason='主线结果', epistemic_status='事实', occurrence_layer='实际发生',
            occurrence_signature=event_signature('ENT_2', '乙抵达'),
            evidence=[Evidence(chunk=1, passage_ids=['C1_P3'])])
        world = World(
            entities={'ENT_1': Entity(name='甲'), 'ENT_2': Entity(name='乙')},
            locations={}, objects={}, world_traits={}, events=[first, duplicate, unrelated],
            time_nodes=[TimeNode(id='N1', name='离开', order=1),
                        TimeNode(id='N2', name='抵达', order=2)],
            changes=[Change(event_id='EVT_3', subject_id='ENT_2',
                            change='乙已抵达', basis='原文')])
        review = Review(
            is_valid=False, summary='合并重复并删除普通动作', issues=[], covered_chunks=[1],
            correction=Correction(
                event_updates={'EVT_1': {'description': '甲实际离开'}},
                merges=[EventMerge(source='EVT_2', target='EVT_1', reason='同一发生')],
                removals=[EventRemoval(
                    id='EVT_3', signature=unrelated.occurrence_signature,
                    reason='误判为普通动作')]))

        class Storage:
            @staticmethod
            def latest(_stage, default=None):
                return default

            @staticmethod
            def get(_key, default=None):
                return default

        workflow = NovelExtractWorkflow()
        workflow.storage, workflow.failures = Storage(), []
        candidate, _ = workflow.evaluate_review(review, world, chunks)

        self.assertEqual({event.id for event in candidate.events}, {'EVT_1', 'EVT_3'})
        self.assertEqual(candidate.events[0].description, '甲实际离开')
        self.assertEqual({item.quote for item in candidate.events[0].evidence},
                         {'甲离开。'})
        self.assertEqual([merge.source for merge in review.correction.merges], ['EVT_2'])
        self.assertEqual(review.correction.removals, [])
        self.assertEqual([change.event_id for change in candidate.changes], ['EVT_3'])

    def test_invalid_event_update_keeps_independent_exact_merge(self):
        chunks = chunk_text('甲离开。\n乙抵达。')
        first = Event(
            id='EVT_1', description='甲离开', actor_ids=['ENT_1'],
            time_node_id='N1', event_order=1, temporal_basis='先离开',
            core_reason='乙随后才抵达', epistemic_status='事实', occurrence_layer='实际发生',
            occurrence_signature=event_signature('ENT_1', '甲离开'),
            evidence=[Evidence(chunk=1, passage_ids=['C1_P1'])])
        duplicate = first.model_copy(update={'id': 'EVT_2'}, deep=True)
        unrelated = Event(
            id='EVT_3', description='乙抵达', actor_ids=['ENT_2'],
            time_node_id='N2', event_order=1, temporal_basis='后抵达',
            core_reason='主线结果', epistemic_status='事实', occurrence_layer='实际发生',
            occurrence_signature=event_signature('ENT_2', '乙抵达'),
            evidence=[Evidence(chunk=1, passage_ids=['C1_P2'])])
        world = World(entities={'ENT_1': Entity(name='甲'), 'ENT_2': Entity(name='乙')},
                      locations={}, objects={}, world_traits={}, events=[first, duplicate, unrelated],
                      time_nodes=[TimeNode(id='N1', name='离开', order=1),
                                  TimeNode(id='N2', name='抵达', order=2)])
        review = Review(is_valid=True, time_valid=True, summary='去重并修订', issues=[], covered_chunks=[1],
                        correction=Correction(
                            event_updates={
                                'EVT_1': {'description': '甲实际离开', 'time_node_id': 'N1', 'event_order': 1},
                                'EVT_3': {'occurrence_signature': event_signature('ENT_2', '乙没抵达')}},
                            merges=[EventMerge(source='EVT_2', target='EVT_1', reason='同一发生')],
                            relations=[Relation(source='EVT_1', target='EVT_3', kind='before', basis='先后')]))

        class Storage:
            @staticmethod
            def latest(_stage, default=None):
                return default

            @staticmethod
            def get(_key, default=None):
                return default

        workflow = NovelExtractWorkflow()
        workflow.storage, workflow.failures = Storage(), []
        final_review = review.model_copy(deep=True)
        candidate, _ = workflow.evaluate_review(review, world, chunks)

        self.assertEqual({event.id for event in candidate.events}, {'EVT_1', 'EVT_3'})
        self.assertEqual(candidate.events[0].description, '甲实际离开')
        self.assertIsNone(candidate.events[0].time_node_id)
        self.assertEqual(candidate.events[1].occurrence_signature.occurrence, '乙抵达')
        self.assertEqual(candidate.relations, [])
        self.assertEqual([merge.source for merge in review.correction.merges], ['EVT_2'])
        self.assertIsNone(review.time)
        self.assertFalse(review.is_valid)

        class FinalStorage(Storage):
            @staticmethod
            def get(key, default=None):
                return {'rounds': 5} if key == 'review_state' else default

        final_workflow = NovelExtractWorkflow()
        final_workflow.storage, final_workflow.failures = FinalStorage(), []
        final_candidate, _ = final_workflow.evaluate_review(final_review, world, chunks)
        self.assertEqual(final_candidate, world)
        self.assertFalse(final_review.is_valid)

    def test_invalid_fact_update_keeps_independent_new_event(self):
        chunks = chunk_text('甲出发。\n乙到达。')
        first = Event(
            id='EVT_1', description='甲出发', actor_ids=['ENT_1'],
            time_node_id='N1', event_order=1, temporal_basis='甲先出发',
            core_reason='乙随后才能到达', epistemic_status='事实', occurrence_layer='实际发生',
            occurrence_signature=event_signature('ENT_1', '甲出发'),
            evidence=[Evidence(chunk=1, passage_ids=['C1_P1'])])
        missing = Event(
            id='EVT_2', description='乙到达', actor_ids=['ENT_2'],
            temporal_basis='甲出发之后', core_reason='故事结果改变',
            epistemic_status='事实', occurrence_layer='实际发生',
            occurrence_signature=event_signature('ENT_2', '乙到达'),
            evidence=[Evidence(chunk=1, passage_ids=['C1_P2'])])
        world = World(entities={'ENT_1': Entity(name='甲'), 'ENT_2': Entity(name='乙')},
                      locations={}, objects={}, world_traits={}, events=[first],
                      time_nodes=[TimeNode(id='N1', name='出发', order=1)])
        review = Review(is_valid=True, time_valid=True, summary='补漏并修订', issues=[],
                        covered_chunks=[1], correction=Correction(
                            events=[missing],
                            event_updates={'EVT_1': {
                                'occurrence_signature': event_signature('ENT_1', '甲停下')}}),
                        time=Timeline(nodes=[TimeNode(id='N2', name='到达', order=2)],
                            timeline=[TimePlacement(id='EVT_2', node_id='N2', order=1,
                                                    basis='甲出发之后')]))

        class Storage:
            def latest(self, _stage, default=None):
                return default

            def get(self, _key, default=None):
                return default

        workflow = NovelExtractWorkflow()
        workflow.storage, workflow.failures = Storage(), []
        candidate, _ = workflow.evaluate_review(review, world, chunks)

        self.assertEqual({event.id for event in candidate.events}, {'EVT_1', 'EVT_2'})
        self.assertEqual(candidate.events[0].occurrence_signature.occurrence, '甲出发')
        self.assertIsNone(candidate.events[1].time_node_id)
        self.assertIsNone(review.time)
        self.assertFalse(review.is_valid)
        self.assertTrue(any('已保存独立新增事件' in issue for issue in review.issues))

    def formal79_style_review(self, complete=False):
        correction = Correction(
            entities={
                'ENT_1': Entity(name='叙述者', aliases=[], description='讲述故事'),
                'ENT_2': Entity(name='祥林嫂', aliases=[], description='故事主人公'),
            } if complete else {},
            entity_merges=[
                EventMerge(source='ENT_C1_1', target='ENT_1', reason='同人'),
                EventMerge(source='ENT_C8_1', target='ENT_1', reason='同人'),
                EventMerge(source='ENT_C8_3', target='ENT_2', reason='同人'),
            ],
            locations={'LOC_1': Location(name='鲁镇', aliases=[], description='故事地点')} if complete else {},
            location_merges=[
                EventMerge(source='LOC_C8_1', target='LOC_1', reason='同地'),
            ],
        )
        information = ({
            'characters': {
                # 完整target档案显式吸收各source中需保留的信息；
                # 程序只验证原子交付，不自作主张猜测如何拼接语义。
                'ENT_1': profile('主角', profile='旧叙述者档案；块一补充；块八补充'),
                'ENT_2': profile(profile='旧祥林嫂档案；块八祥林嫂补充'),
            },
            'locations': {'LOC_1': {'description': '旧鲁镇档案；块八鲁镇补充', 'detail': None}},
        } if complete else None)
        return Review(is_valid=False, summary='合并跨块重复对象', issues=[], covered_chunks=[],
                      correction=correction, information=information)

    def test_feedback_lists_every_merge_and_both_required_paths(self):
        review = self.formal79_style_review()

        errors = merge_bundle_errors(review.correction, review.information)

        self.assertEqual(len(errors), 4)
        expected = (
            ('ENT_C1_1→ENT_1', '$.correction.entities.ENT_1', '$.information.characters.ENT_1'),
            ('ENT_C8_1→ENT_1', '$.correction.entities.ENT_1', '$.information.characters.ENT_1'),
            ('ENT_C8_3→ENT_2', '$.correction.entities.ENT_2', '$.information.characters.ENT_2'),
            ('LOC_C8_1→LOC_1', '$.correction.locations.LOC_1', '$.information.locations.LOC_1'),
        )
        for error, fragments in zip(errors, expected):
            for fragment in fragments:
                self.assertIn(fragment, error)

    def test_workflow_accepts_semantic_merge_gap_for_isolated_review(self):
        review = self.formal79_style_review()
        errors = NovelExtractWorkflow().validate_review_delivery(review, World(
            entities={}, locations={}, objects={}, world_traits={}), [], [])
        self.assertEqual(errors, [])

    def test_defaulted_fields_are_not_mistaken_for_complete_target_records(self):
        review = Review(is_valid=False, summary='尝试合并', issues=[], covered_chunks=[],
            correction=Correction(
                entities={'ENT_1': Entity(name='叙述者')},
                entity_merges=[EventMerge(source='ENT_C1_1', target='ENT_1', reason='同人')]),
            information={'characters': {'ENT_1': {'activity': '主角'}}})

        error = '\n'.join(merge_bundle_errors(review.correction, review.information))

        self.assertIn('$.correction.entities.ENT_1.aliases', error)
        self.assertIn('$.correction.entities.ENT_1.description', error)
        self.assertIn('$.information.characters.ENT_1.profile', error)
        self.assertIn('$.information.characters.ENT_1.biography', error)

    def test_foundation_conversion_preserves_explicit_target_fields(self):
        foundation = Foundation(
            catalog=Register(
                entities={'ENT_1': Entity(name='甲'), 'ENT_C1_1': Entity(name='甲')},
                locations={}, objects={}, world_traits={}),
            information={})
        check = FoundationReview(
            summary='合并全文目录重复身份',
            correction=RegisterCorrection(
                entities={'ENT_1': Entity(name='甲')},
                entity_merges=[EventMerge(source='ENT_C1_1', target='ENT_1', reason='同人')]),
            information={'characters': {'ENT_1': profile('主角', profile='甲的完整档案')}},
            audit={
                'checked_entity_ids': ['ENT_1', 'ENT_C1_1'], 'checked_location_ids': [],
                'background_checked': True,
                'settings_checked': True, 'overview_checked': True, 'unresolved': [],
            })

        with self.assertRaises(DeliveryStructureError) as caught:
            apply_foundation_review(foundation, check)

        self.assertIn('$.correction.entities.ENT_1.aliases', str(caught.exception))
        self.assertIn('$.correction.entities.ENT_1.description', str(caught.exception))

    def test_complete_batch_is_atomic_and_preserves_supplied_profiles(self):
        review = self.formal79_style_review(complete=True)
        original = World(
            entities={
                'ENT_1': Entity(name='叙述者'),
                'ENT_C1_1': Entity(name='叙述者'),
                'ENT_C8_1': Entity(name='叙述者'),
                'ENT_2': Entity(name='祥林嫂'),
                'ENT_C8_3': Entity(name='祥林嫂'),
            },
            locations={
                'LOC_1': Location(name='鲁镇'),
                'LOC_C8_1': Location(name='鲁镇'),
            },
            objects={}, world_traits={},
            events=[Event(
                id='EVT_1', description='祥林嫂遇到叙述者',
                actor_ids=['ENT_C8_3'], target_ids=['ENT_C1_1'], at_location_id='LOC_C8_1',
                temporal_basis='原文当时', core_reason='主线相遇', epistemic_status='事实',
                occurrence_layer='实际发生',
                occurrence_signature=event_signature('ENT_C8_3', '祥林嫂遇到叙述者'),
                evidence=[Evidence(chunk=1, quote='原文')],
            )],
        )
        information = {
            'story_overview': '主线',
            'story_spine': {
                'endings': ['祥林嫂与叙述者相遇'],
                'turning_points': ['祥林嫂走近叙述者'],
                'causal_chains': [[{
                    'link': '祥林嫂走近叙述者',
                    'signature': event_signature('ENT_C8_3', '祥林嫂走近叙述者'),
                    'core_reason': '删除后两人无法相遇',
                }, {
                    'link': '两人相遇',
                    'signature': event_signature('ENT_C1_1', '两人相遇'),
                    'core_reason': '删除后相遇结果不存在',
                }]],
            },
            'characters': {
                'ENT_1': profile('主角', profile='旧叙述者档案'),
                'ENT_C1_1': profile('主角', profile='块一补充'),
                'ENT_C8_1': profile('主角', profile='块八补充'),
                'ENT_2': profile(profile='旧祥林嫂档案'),
                'ENT_C8_3': profile(profile='块八祥林嫂补充'),
            },
            'locations': {
                'LOC_1': {'description': '旧鲁镇档案'},
                'LOC_C8_1': {'description': '块八鲁镇补充'},
            },
            'background': {}, 'settings': [],
            'relations': [{'owner': 'ENT_C1_1', 'other': 'ENT_C8_3', 'relation': ['相遇']}],
        }
        original_dump = original.model_dump(mode='json')

        merged = apply_correction(original, review)
        details = apply_information(information, review, merged)

        self.assertEqual(original.model_dump(mode='json'), original_dump)
        self.assertEqual(set(merged.entities), {'ENT_1', 'ENT_2'})
        self.assertEqual(set(merged.locations), {'LOC_1'})
        self.assertEqual(merged.events[0].actor_ids, ['ENT_2'])
        self.assertEqual(merged.events[0].occurrence_signature.actual_subjects, ['ENT_2'])
        self.assertEqual(merged.events[0].target_ids, ['ENT_1'])
        self.assertEqual(merged.events[0].at_location_id, 'LOC_1')
        self.assertEqual(set(details.characters), {'ENT_1', 'ENT_2'})
        self.assertEqual(set(details.locations), {'LOC_1'})
        self.assertEqual(details.characters['ENT_1'].profile, '旧叙述者档案；块一补充；块八补充')
        self.assertEqual(details.characters['ENT_2'].profile, '旧祥林嫂档案；块八祥林嫂补充')
        self.assertEqual(details.locations['LOC_1'].description, '旧鲁镇档案；块八鲁镇补充')
        self.assertEqual(details.story_spine.causal_chains[0][0].signature.actual_subjects, ['ENT_2'])
        self.assertEqual(details.story_spine.causal_chains[0][1].signature.actual_subjects, ['ENT_1'])
        self.assertEqual(details.relations[0].owner, 'ENT_1')
        self.assertEqual(details.relations[0].other, 'ENT_2')

    def test_failed_merge_recovery_keeps_unrelated_fact_and_time_revision(self):
        chunks = chunk_text('甲出发。\n\n乙到达。\n\n另一件事原本就引用正式甲。')
        first = Event(
            id='EVT_1', description='甲出发', actor_ids=['ENT_C1_1'],
            time_node_id='N1', event_order=1, temporal_basis='先出发', core_reason='主线开始',
            epistemic_status='事实', occurrence_layer='实际发生',
            occurrence_signature=event_signature('ENT_C1_1', '甲出发'),
            evidence=[Evidence(chunk=1, passage_ids=['C1_P1'])])
        second = Event(
            id='EVT_2', description='乙到达', actor_ids=['ENT_2'],
            time_node_id='N2', event_order=1, temporal_basis='后到达', core_reason='主线结果',
            epistemic_status='事实', occurrence_layer='实际发生',
            occurrence_signature=event_signature('ENT_2', '乙到达'),
            evidence=[Evidence(chunk=1, passage_ids=['C1_P2'])])
        target_event = Event(
            id='EVT_3', description='正式甲观察', actor_ids=['ENT_1'],
            time_node_id='N1', event_order=2, temporal_basis='在乙到达前', core_reason='主线观察',
            epistemic_status='事实', occurrence_layer='实际发生',
            occurrence_signature=event_signature('ENT_1', '正式甲观察'),
            evidence=[Evidence(chunk=1, passage_ids=['C1_P3'])])
        world = World(
            entities={
                'ENT_1': Entity(name='甲', aliases=[], description='甲'),
                'ENT_C1_1': Entity(name='甲', aliases=[], description='块内甲'),
                'ENT_2': Entity(name='乙', aliases=[], description='乙'),
            }, locations={}, objects={}, world_traits={}, events=[first, second, target_event],
            time_nodes=[TimeNode(id='N1', name='出发', order=1), TimeNode(id='N2', name='到达', order=2)],
        )
        information = {
            'story_overview': '甲出发，乙到达',
            'story_spine': {
                'endings': ['乙到达'], 'turning_points': ['甲出发'],
                'causal_chains': [['甲出发', '乙到达']],
            },
            'characters': {
                'ENT_1': profile('主角', profile='甲'),
                'ENT_C1_1': profile('主角', profile='块内甲'),
                'ENT_2': profile(profile='乙'),
            },
            'locations': {}, 'background': {}, 'settings': [], 'relations': [],
        }

        class Storage:
            def latest(self, stage, default=None):
                return information if stage == 'information' else default

            def get(self, _, default=None):
                return default

        workflow = NovelExtractWorkflow()
        workflow.storage, workflow.failures = Storage(), []
        review = Review(
            is_valid=False, summary='合并缺档，但乙的事实和时间可修', issues=[], covered_chunks=[],
            correction=Correction(
                entities={'ENT_1': Entity(name='甲', aliases=[], description='合并后甲')},
                entity_merges=[EventMerge(source='ENT_C1_1', target='ENT_1', reason='同人')],
                # EVT_1手工换成target是失败合并的一部分，不能单独落库；
                # EVT_2的文字修正与身份合并无关，应保留。
                event_updates={
                    'EVT_1': {'actor_ids': ['ENT_1']},
                    'EVT_2': {'description': '乙正式到达'},
                    # 已保存事件原本就引用target，这一修订不依赖合并。
                    'EVT_3': {'description': '正式甲仔细观察'},
                },
                relations=[
                    Relation(source='EVT_1', target='EVT_2', kind='causes', basis='合并后才提交'),
                    Relation(source='EVT_3', target='EVT_2', kind='before', basis='原文先观察后到达'),
                ],
                changes=[
                    Change(event_id='EVT_1', subject_id='ENT_1', change='身份改写', basis='合并候选'),
                    Change(event_id='EVT_3', subject_id='ENT_1', change='完成观察', basis='既有target事件'),
                ]),
            # 故意不交ENT_1档案：合并必须被拒绝。
            time=Timeline(
                nodes=[TimeNode(id='N1', name='出发', order=1), TimeNode(id='N2', name='到达', order=2)],
                timeline=[
                    TimePlacement(id='EVT_1', node_id='N2', order=1, basis='合并后改到到达节点'),
                    TimePlacement(id='EVT_2', node_id='N2', order=1, basis='后到达'),
                    TimePlacement(id='EVT_3', node_id='N1', order=2, basis='在乙到达前'),
                ]),
            information={'relations': [
                {'owner': 'ENT_1', 'other': 'ENT_2', 'relation': ['合并后关系']}
            ]},
        )
        complete_but_invalid = review.model_copy(deep=True)
        complete_but_invalid.information = {
            'characters': {'ENT_1': profile('主角', profile='合并后的完整甲档案')},
            # 合并包字段齐全，但同一档案补丁的另一个字段无效。
            'relations': '错误的关系表类型',
        }

        candidate, details = workflow.evaluate_review(review, world, chunks)

        self.assertIn('ENT_C1_1', candidate.entities)
        self.assertEqual(candidate.events[0].actor_ids, ['ENT_C1_1'])
        self.assertEqual(candidate.events[1].description, '乙正式到达')
        self.assertEqual(candidate.events[2].description, '正式甲仔细观察')
        self.assertEqual([(row.source, row.target) for row in candidate.relations], [('EVT_3', 'EVT_2')])
        self.assertEqual([row.event_id for row in candidate.changes], ['EVT_3'])
        self.assertEqual(candidate.events[0].time_node_id, 'N1')
        self.assertEqual(candidate.events[1].time_node_id, 'N2')
        self.assertEqual(candidate.events[1].event_order, 1)
        self.assertIn('ENT_C1_1', details['characters'])
        self.assertFalse(review.correction.entity_merges)
        self.assertNotIn('EVT_1', review.correction.event_updates)
        self.assertIn('EVT_2', review.correction.event_updates)
        self.assertIn('EVT_3', review.correction.event_updates)
        self.assertEqual([row.id for row in review.time.timeline], ['EVT_2', 'EVT_3'])
        self.assertFalse(review.time.complete)
        self.assertEqual(details['relations'][0]['owner'], 'ENT_1')
        self.assertTrue(any(issue.startswith('档案修订未接收：合并交付结构错误')
                            for issue in review.issues))

        second = NovelExtractWorkflow()
        second.storage, second.failures = Storage(), []
        candidate, details = second.evaluate_review(complete_but_invalid, world, chunks)
        self.assertIn('ENT_C1_1', candidate.entities)
        self.assertEqual(candidate.events[0].actor_ids, ['ENT_C1_1'])
        self.assertEqual(candidate.events[1].description, '乙正式到达')
        self.assertEqual(candidate.events[2].description, '正式甲仔细观察')
        self.assertEqual([(row.source, row.target) for row in candidate.relations], [('EVT_3', 'EVT_2')])
        self.assertEqual(candidate.events[1].time_node_id, 'N2')
        self.assertEqual(candidate.events[2].event_order, 2)
        self.assertEqual(details, information)
        self.assertFalse(complete_but_invalid.correction.entity_merges)
        self.assertNotIn('EVT_1', complete_but_invalid.correction.event_updates)
        self.assertIn('EVT_2', complete_but_invalid.correction.event_updates)
        self.assertEqual([row.id for row in complete_but_invalid.time.timeline], ['EVT_2', 'EVT_3'])
        self.assertTrue(any(issue.startswith('档案修订未接收：information.relations')
                            for issue in complete_but_invalid.issues))

    def test_invalid_catalog_merge_does_not_discard_unrelated_fact_or_time(self):
        chunks = chunk_text('甲出发。\n乙到达。')
        first = Event(
            id='EVT_1', description='甲出发', actor_ids=['ENT_1'], time_node_id='N1', event_order=1,
            temporal_basis='先出发', core_reason='主线开始', epistemic_status='事实',
            occurrence_layer='实际发生', occurrence_signature=event_signature('ENT_1', '甲出发'),
            evidence=[Evidence(chunk=1, passage_ids=['C1_P1'])])
        second = Event(
            id='EVT_2', description='乙到达', actor_ids=['ENT_2'], time_node_id='N2', event_order=1,
            temporal_basis='后到达', core_reason='主线结果', epistemic_status='事实',
            occurrence_layer='实际发生', occurrence_signature=event_signature('ENT_2', '乙到达'),
            evidence=[Evidence(chunk=1, passage_ids=['C1_P2'])])
        world = World(
            entities={'ENT_1': Entity(name='甲'), 'ENT_2': Entity(name='乙')},
            locations={}, objects={}, world_traits={}, events=[first, second],
            time_nodes=[TimeNode(id='N1', name='出发', order=1),
                        TimeNode(id='N2', name='到达', order=2)])
        review = Review(
            is_valid=False, summary='错误物品合并不影响乙', issues=[], covered_chunks=[],
            correction=Correction(
                objects={'OBJ_1': Object(name='物品', aliases=[], description='',
                                         location_id=None, owner_name=None)},
                object_merges=[EventMerge(source='OBJ_MISSING', target='OBJ_1', reason='错误合并')],
                event_updates={'EVT_2': {'description': '乙正式到达'}}),
            time=Timeline(timeline=[
                TimePlacement(id='EVT_2', node_id='N2', order=1, basis='后到达')]),
        )

        class Storage:
            @staticmethod
            def latest(_stage, default=None):
                return default

            @staticmethod
            def get(_key, default=None):
                return default

        workflow = NovelExtractWorkflow()
        workflow.storage, workflow.failures = Storage(), []
        candidate, _ = workflow.evaluate_review(review, world, chunks)

        self.assertEqual(candidate.events[1].description, '乙正式到达')
        self.assertEqual(candidate.events[1].time_node_id, 'N2')
        self.assertFalse(review.correction.object_merges)
        self.assertIn('EVT_2', review.correction.event_updates)
        self.assertTrue(any(issue.startswith('目录修订未接收：') for issue in review.issues))

    def test_failed_merge_keeps_unrelated_placement_in_shared_new_node(self):
        first = Event(
            id='EVT_1', description='甲出发', actor_ids=['ENT_SOURCE'],
            time_node_id='N1', event_order=1, temporal_basis='先出发', core_reason='主线开始',
            epistemic_status='事实', occurrence_layer='实际发生',
            occurrence_signature=event_signature('ENT_SOURCE', '甲出发'),
            evidence=[Evidence(chunk=1, quote='甲出发')])
        second = Event(
            id='EVT_2', description='乙到达', actor_ids=['ENT_2'],
            time_node_id='N2', event_order=1, temporal_basis='后到达', core_reason='主线结果',
            epistemic_status='事实', occurrence_layer='实际发生',
            occurrence_signature=event_signature('ENT_2', '乙到达'),
            evidence=[Evidence(chunk=1, quote='乙到达')])
        world = World(
            entities={'ENT_SOURCE': Entity(name='甲'), 'ENT_2': Entity(name='乙')},
            locations={}, objects={}, world_traits={}, events=[first, second],
            time_nodes=[TimeNode(id='N1', name='出发', order=1),
                        TimeNode(id='N2', name='到达', order=2)])
        review = Review(
            is_valid=False, summary='隔离失败合并', issues=[], covered_chunks=[],
            time=Timeline(
                nodes=[TimeNode(id='N3', name='共同新阶段', order=3)],
                timeline=[
                    TimePlacement(id='EVT_1', node_id='N3', order=1, basis='合并相关'),
                    TimePlacement(id='EVT_2', node_id='N3', order=2, basis='独立位置'),
                ]))

        NovelExtractWorkflow._remove_merge_coupled_time(
            review, world, {event.id: event for event in world.events}, {'EVT_1'})

        self.assertIsNotNone(review.time)
        self.assertEqual([node.id for node in review.time.nodes], ['N3'])
        self.assertEqual([row.id for row in review.time.timeline], ['EVT_2'])


if __name__ == '__main__':
    unittest.main()
