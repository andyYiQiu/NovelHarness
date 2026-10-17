"""核心事件、全文资料与故事时间的新协议；全部离线。"""
import json
from pathlib import Path
import unittest

from common.novel_extract.NovelExtractSchema import (
    BackgroundInformation, CandidateCheck, CharacterInformation, CoreChainCheck, CoreLinkCheck, Entity, Event, Evidence,
    GroupedEvent, InformationAudit, Location, LocationInformation, RelationInformation, Review,
    OccurrenceSignature, RepositoryInformation, StorySpine, TimelinePlan, TimeNode, World,
)
from common.novel_extract.NovelExtractReader import chunk_text
from workflows.NovelExtractWorkflow import NovelExtractWorkflow
from exporters.NovelExtractExporter import NovelExtractExporter


ROOT = Path(__file__).resolve().parents[1]


def detailed_information():
    return RepositoryInformation(
        story_overview='甲受乙帮助后完成目标。',
        story_spine=StorySpine(
            endings=['甲完成目标'], turning_points=['乙帮助甲'],
            causal_chains=[[story_link('甲遭遇阻碍'), story_link('乙帮助甲'), story_link('甲完成目标')]],
        ),
        characters={
            'ENT_1': CharacterInformation(activity='主角', profile='故事主角', identity=['行动者']),
            'ENT_2': CharacterInformation(activity='npc', profile='帮助甲的人', identity=['帮助者']),
        },
        locations={'LOC_1': LocationInformation(description='事件发生地')},
        background=BackgroundInformation(description='原文呈现的社会环境', detail='制度影响人物选择与结局'),
        settings=[],
        relations=[RelationInformation(owner='ENT_1', other='ENT_2', relation=['受助者'], detail='目标完成前')],
    )


def occurrence(text='核心事件', subject='ENT_1'):
    return OccurrenceSignature(
        actual_subjects=[subject], occurrence=text, start_anchor='原文阶段', layer='实际发生')


def story_link(text):
    return {'link': text, 'signature': occurrence(text).model_dump(mode='json'),
            'core_reason': '删除该环节会使后续转折或结局无法成立'}


def timed_event(key, node, order, signature=None):
    return Event(
        id=key, description=key, actor_ids=['ENT_1'], source_time='原文阶段',
        time_node_id=node, event_order=order, temporal_basis='原文前后与因果前提',
        core_reason='删除后会改变结局，因为后事以此为前提',
        evidence=[Evidence(chunk=1, quote='原文', passage_ids=[])],
        epistemic_status='事实', occurrence_layer='实际发生',
        occurrence_signature=signature or occurrence(key),
    )


class QualityContractTests(unittest.TestCase):
    def setUp(self):
        self.info = detailed_information()
        self.world = World(
            entities={'ENT_1': Entity(name='甲'), 'ENT_2': Entity(name='乙')},
            locations={'LOC_1': Location(name='某地')}, objects={}, world_traits={},
        )

    def test_active_configs_use_deepseek_flash(self):
        config_paths = [ROOT / 'model' / 'config.example.json']
        local_config = ROOT / 'model' / 'config.local.json'
        if local_config.exists():
            config_paths.append(local_config)
        for path in config_paths:
            config = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(config['model'], 'deepseek-flash')
            self.assertEqual(config['api_url'], 'https://api.deepseek.com/chat/completions')
            self.assertEqual(config['api_key_env'], 'DEEPSEEK_API_KEY')

    @unittest.skip("Legacy proof-table protocol retired; see reports/novel_extract/shadowloom_validation_reset_20261016/retired_tests.md")
    def test_event_keeps_occurrence_identity_without_repeating_it_in_review_links(self):
        event_schema = Event.model_json_schema()
        link_schema = CoreLinkCheck.model_json_schema()
        candidate_schema = CandidateCheck.model_json_schema()
        review_schema = Review.model_json_schema()
        timeline_schema = TimelinePlan.model_json_schema()
        grouped_schema = GroupedEvent.model_json_schema()
        self.assertIn('occurrence_signature', event_schema['required'])
        self.assertEqual(set(link_schema['properties']), {'event_id'})
        self.assertEqual(set(link_schema['required']), {'event_id'})
        self.assertEqual(set(candidate_schema['properties']),
                         {'candidate_id', 'disposition', 'event_ids', 'reason'})
        self.assertEqual(set(candidate_schema['required']),
                         {'candidate_id', 'disposition', 'event_ids'})
        time_schema = review_schema['$defs']['TimeCheck']
        self.assertEqual(set(time_schema['properties']['verdict']['enum']), {'conflict', 'unknown'})
        audit_schema = review_schema['$defs']['InformationAudit']
        self.assertEqual(set(audit_schema['properties']), {'checked_sections', 'unresolved'})
        for schema, field in ((event_schema, 'occurrence_signature'),
                              (timeline_schema, 'time'), (grouped_schema, 'order')):
            self.assertNotIn('null', json.dumps(schema['properties'][field]))
        self.assertNotIn('未分类', event_schema['properties']['occurrence_layer']['enum'])
        signature_schema = event_schema['$defs']['OccurrenceSignature']
        self.assertIn('actual_subjects', signature_schema['required'])
        self.assertEqual(signature_schema['properties']['actual_subjects']['minItems'], 1)
        self.assertNotIn('未分类', signature_schema['properties']['layer']['enum'])

    @unittest.skip("Legacy proof-table protocol retired; see reports/novel_extract/shadowloom_validation_reset_20261016/retired_tests.md")
    def test_information_audit_covers_all_sections_without_per_object_proof(self):
        sections = [
            'characters', 'relations', 'locations', 'objects',
            'background', 'settings', 'story_overview', 'story_spine',
        ]
        audit = InformationAudit(checked_sections=sections)
        self.assertFalse(information_audit_errors(audit, self.world))
        missing_relations = audit.model_copy(update={
            'checked_sections': [section for section in sections if section != 'relations']})
        self.assertTrue(any('relations' in error
                            for error in information_audit_errors(missing_relations, self.world)))
        unresolved = audit.model_copy(update={'unresolved': ['乙与甲的关系时期仍不确定']})
        self.assertTrue(any('unresolved' in error
                            for error in information_audit_errors(unresolved, self.world)))

    @unittest.skip("Unused profile-text heuristics retired; current review checks source-backed detail")
    def test_empty_profiles_and_world_context_do_not_count_as_detailed(self):
        weak = self.info.model_copy(deep=True)
        weak.characters['ENT_1'] = CharacterInformation(activity='主角')
        weak.background = BackgroundInformation()
        errors = information_quality_errors(weak, self.world)
        self.assertTrue(any('characters.ENT_1' in error for error in errors))
        self.assertTrue(any('background/settings' in error for error in errors))

    @unittest.skip("Unused profile-text heuristics retired; current review checks source-backed detail")
    def test_placeholder_text_does_not_count_as_detailed_information(self):
        weak = self.info.model_copy(deep=True)
        weak.characters['ENT_1'] = CharacterInformation(activity='主角', profile='xxxx', identity=['x'])
        weak.locations['LOC_1'] = LocationInformation(description='xxxx')
        weak.background = BackgroundInformation(description='xxxx')
        weak.relations[0] = RelationInformation(
            owner='ENT_1', other='ENT_2', relation=['妻'], detail='xxxx')
        errors = information_quality_errors(weak, self.world)
        self.assertTrue(any('characters.ENT_1' in error for error in errors))
        self.assertTrue(any('locations.LOC_1' in error for error in errors))
        self.assertTrue(any('relations[0]' in error for error in errors))
        self.assertTrue(any('background/settings' in error for error in errors))

        weak = self.info.model_copy(deep=True)
        weak.relations[0].relation = ['x']
        self.assertTrue(any('relations[0]' in error
                            for error in information_quality_errors(weak, self.world)))

    @unittest.skip("Unused profile-text heuristics retired; current review checks source-backed detail")
    def test_story_spine_rejects_duplicate_occurrence_identity(self):
        weak = self.info.model_copy(deep=True)
        weak.story_spine.causal_chains[0][1].signature = \
            weak.story_spine.causal_chains[0][0].signature.model_copy(deep=True)
        self.assertTrue(any('重复发生签名' in error
                            for error in information_quality_errors(weak, self.world)))

    @unittest.skip("Unused profile-text heuristics retired; current review checks source-backed detail")
    def test_story_spine_subjects_must_resolve_to_the_person_catalog(self):
        for subject, message in ((None, '缺少实际主体'), ('ENT_UNKNOWN', '未进入人物目录')):
            with self.subTest(subject=subject):
                weak = self.info.model_copy(deep=True)
                weak.story_spine.causal_chains[0][0].signature.actual_subjects = \
                    [] if subject is None else [subject]
                self.assertTrue(any(message in error
                                    for error in information_quality_errors(weak, self.world)))

    @unittest.skip("Legacy proof-table protocol retired; see reports/novel_extract/shadowloom_validation_reset_20261016/retired_tests.md")
    def test_complete_review_rejects_final_event_without_core_source(self):
        details = detailed_information()
        details.story_spine = StorySpine(
            endings=['B'], turning_points=['A'],
            causal_chains=[[story_link('A'), story_link('B')]],
        )
        events = [timed_event(key, f'N{index}', 1, occurrence(key))
                  for index, key in enumerate(('A', 'B', 'EXTRA'), 1)]
        world = World(
            entities=self.world.entities, locations=self.world.locations,
            objects={}, world_traits={}, events=events,
            time_nodes=[TimeNode(id=f'N{index}', name=key, order=index)
                        for index, key in enumerate(('A', 'B', 'EXTRA'), 1)],
        )

        class Storage:
            @staticmethod
            def latest(key, default=None):
                if key == 'information':
                    return details.model_dump(mode='json')
                if key.startswith('job:chunk.') and key.endswith('.reading'):
                    return {'result': {'core_candidates': []}}
                return default

            @staticmethod
            def get(_key, default=None):
                return default

        workflow = NovelExtractWorkflow()
        workflow.storage, workflow.failures = Storage(), []
        review = Review(
            is_valid=True, summary='完整核对', issues=[], covered_chunks=[1],
            core_chain_checks=[CoreChainCheck(
                chain_index=1,
                links=[CoreLinkCheck(link=key, event_id=key,
                                     reason='对应主干实际发生') for key in ('A', 'B')],
                reason='A导致B',
            )],
        )
        workflow.evaluate_review(review, world, chunk_text('原文'))
        self.assertTrue(any('最终事件缺少核心来源' in issue and 'EXTRA' in issue
                            for issue in review.issues))

    @unittest.skip("Legacy proof-table protocol retired; see reports/novel_extract/shadowloom_validation_reset_20261016/retired_tests.md")
    def test_core_chain_event_ids_must_follow_fabula_not_narration(self):
        world = self.world.model_copy(update={
            'events': [timed_event('CAUSE', 'LATE', 1, occurrence('甲遭遇阻碍')),
                       timed_event('MID', 'LATE', 2, occurrence('乙帮助甲')),
                       timed_event('ENDING', 'EARLY', 1, occurrence('甲完成目标'))],
            'time_nodes': [TimeNode(id='EARLY', name='早', order=1),
                           TimeNode(id='LATE', name='晚', order=2)],
        })
        checks = [CoreChainCheck(chain_index=1, links=[
            {'link': '甲遭遇阻碍', 'event_id': 'CAUSE', 'reason': '这是链首前因'},
            {'link': '乙帮助甲', 'event_id': 'MID', 'reason': '这是后续实施'},
            {'link': '甲完成目标', 'event_id': 'ENDING', 'reason': '这是故事结局'},
        ], reason='按因果链从前因到结局核对')]
        errors = NovelExtractWorkflow.core_chain_check_errors(
            checks, self.info.model_dump(mode='json'), world, require_complete=True)
        self.assertTrue(any('实际时间顺序逆转' in error for error in errors))

    @unittest.skip("Legacy proof-table protocol retired; see reports/novel_extract/shadowloom_validation_reset_20261016/retired_tests.md")
    def test_core_chain_cannot_bind_a_correct_label_to_the_wrong_event(self):
        details = self.info.model_copy(deep=True)
        details.story_spine.causal_chains[0][1].signature.actual_subjects = ['ENT_2']
        world = self.world.model_copy(update={
            'events': [timed_event('CAUSE', 'N1', 1, occurrence('甲遭遇阻碍')),
                       timed_event('MID', 'N2', 1, occurrence('乙帮助甲', 'ENT_2')),
                       timed_event('ENDING', 'N3', 1, occurrence('甲完成目标'))],
            'time_nodes': [TimeNode(id=f'N{i}', name=f'阶段{i}', order=i) for i in range(1, 4)],
        })
        checks = [CoreChainCheck(chain_index=1, links=[
            {'link': '甲遭遇阻碍', 'event_id': 'MID', 'reason': '错误映射'},
            {'link': '乙帮助甲', 'event_id': 'CAUSE', 'reason': '错误映射'},
            {'link': '甲完成目标', 'event_id': 'ENDING', 'reason': '正确映射'},
        ], reason='核对主干映射')]
        errors = NovelExtractWorkflow.core_chain_check_errors(
            checks, details.model_dump(mode='json'), world, require_complete=True)
        self.assertTrue(any('与主干环节不是同一次实际发生' in error
                            for error in errors))

        correct = checks[0].model_copy(deep=True)
        correct.links[0].link = '同义概括：遭遇阻碍'
        correct.links[0].event_id = 'CAUSE'
        correct.links[1].event_id = 'MID'
        self.assertFalse(NovelExtractWorkflow.core_chain_check_errors(
            [correct], details.model_dump(mode='json'), world, require_complete=True))

    @unittest.skip("Legacy proof-table protocol retired; see reports/novel_extract/shadowloom_validation_reset_20261016/retired_tests.md")
    def test_candidate_actual_subject_cannot_be_replaced_by_the_reporter(self):
        signature = OccurrenceSignature(
            actual_subjects=['ENT_1'], occurrence='甲死亡', start_anchor='早年', layer='实际发生')
        candidate = {'description': '甲死亡', 'evidence': [{'chunk': 1, 'quote': '原文'}],
                     'signature': signature.model_dump(mode='json'),
                     'core_reason': '删除后结局改变，因为死亡导致后续处置'}

        class Storage:
            @staticmethod
            def latest(key, default=None):
                return {'result': {'core_candidates': [candidate]}} if key == 'job:chunk.1.reading' else default

        workflow = NovelExtractWorkflow()
        workflow.storage = Storage()
        check = CandidateCheck(candidate_id='C1_K1', event_ids=['DEATH'], reason='已映射',
                               disposition='mapped')
        for label, wrong_signature in (
            ('subject', OccurrenceSignature(actual_subjects=['ENT_2'], occurrence='乙获知甲死亡',
                                             start_anchor='后来', layer='实际发生')),
            ('layer', OccurrenceSignature(actual_subjects=['ENT_1'], occurrence='甲死亡',
                                           start_anchor='早年', layer='获知')),
        ):
            with self.subTest(label=label):
                world = self.world.model_copy(update={
                    'events': [Event(
                        id='DEATH', description='错误映射', actor_ids=wrong_signature.actual_subjects,
                        temporal_basis='原文', core_reason='消息影响后续行动',
                        evidence=[Evidence(chunk=1, quote='原文')], epistemic_status='事实',
                        occurrence_layer=wrong_signature.layer,
                        occurrence_signature=wrong_signature,
                    )],
                })
                errors = workflow.candidate_check_errors(
                    [check], world, [type('Chunk', (), {'index': 1, 'passages': lambda self: {}})()])
                self.assertTrue(any('不是同一次实际发生' in error for error in errors))

    @unittest.skip("Legacy proof-table protocol retired; see reports/novel_extract/shadowloom_validation_reset_20261016/retired_tests.md")
    def test_candidate_reason_is_only_required_for_exclusion(self):
        workflow = NovelExtractWorkflow()
        candidate = {'description': '甲死亡', 'evidence': [{'chunk': 1, 'quote': '原文'}],
                     'signature': occurrence('甲死亡').model_dump(mode='json'),
                     'core_reason': '删除后结局改变'}

        class Storage:
            @staticmethod
            def latest(key, default=None):
                return {'result': {'core_candidates': [candidate]}} if key == 'job:chunk.1.reading' else default

        workflow.storage = Storage()
        world = self.world.model_copy(update={'events': [Event(
            id='DEATH', description='甲死亡', actor_ids=['ENT_1'], temporal_basis='早年',
            core_reason='删除后结局改变', evidence=[Evidence(chunk=1, quote='原文')], epistemic_status='事实',
            occurrence_layer='实际发生', occurrence_signature=occurrence('甲死亡'))]})
        mapped = CandidateCheck(candidate_id='C1_K1', disposition='mapped', event_ids=['DEATH'])
        excluded = CandidateCheck(candidate_id='C1_K1', disposition='non_core', event_ids=[])
        chunks = [type('Chunk', (), {'index': 1, 'passages': lambda self: {'C1_P1': '原文'}})()]
        self.assertFalse(workflow.candidate_check_errors([mapped], world, chunks))
        self.assertTrue(any('须说明不收录' in error
                            for error in workflow.candidate_check_errors([excluded], world, chunks)))

    def test_partial_projection_survives_missing_foundation_information(self):
        class Storage:
            @staticmethod
            def get(key, default=None):
                return {'source': '样本小说', 'review_state': {}}.get(key, default)

            @staticmethod
            def latest(key, default=None):
                return None if key == 'information' else default

        files, excluded = NovelExtractExporter(Storage()).project(
            World(entities={}, locations={}, objects={}, world_traits={}))
        self.assertIn('timeline/timeline.json', files)
        self.assertFalse(excluded)


if __name__ == '__main__':
    unittest.main()
