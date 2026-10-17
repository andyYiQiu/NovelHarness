"""Foundation 失败隔离：离线固定交付，不调用网络或真实模型。"""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
import unittest

from common.novel_extract.NovelExtractReader import chunk_text
from common.novel_extract.NovelExtractRuntime import JobFailed
from common.novel_extract.NovelExtractSchema import (
    CandidateCheck, Consequences, CoreCandidate, Entity, Event, EventPass, Evidence,
    Foundation, FoundationReview, Reading, Register, RepositoryInformation, Review,
    TimeCheck, TimeNode, TimePlacement, Timeline, TimelinePlan, World,
)
from workflows.NovelExtractWorkflow import NovelExtractWorkflow


class MemoryStorage:
    def __init__(self):
        self.meta = {}
        self.snapshots = {}

    def get(self, key, default=None):
        return deepcopy(self.meta.get(key, default))

    def set(self, key, value):
        self.meta[key] = deepcopy(value)

    def latest(self, stage, default=None):
        return deepcopy(self.snapshots.get(stage, default))

    def snapshot(self, stage, payload):
        self.snapshots[stage] = deepcopy(payload)

    @contextmanager
    def transaction(self):
        yield


class MemoryLog:
    def __init__(self):
        self.rows = []

    def write(self, kind, **data):
        self.rows.append((kind, data))


class FixedAgent:
    """只交付固定结构；validate 仍由生产 Workflow 执行。"""
    def __init__(self, storage, foundation_fails=True, invalid_reference=False):
        self.storage = storage
        self.foundation_fails = foundation_fails
        self.invalid_reference = invalid_reference
        self.calls = []

    def complete(self, job, value, validate):
        errors = validate(value)
        if errors:
            self.storage.snapshot('job:' + job, {
                'status': 'failed', 'candidate': value.model_dump(mode='json'),
                'error': '\n'.join(errors),
            })
            raise JobFailed(job + ': ' + '\n'.join(errors))
        self.storage.snapshot('job:' + job, {
            'status': 'complete', 'candidate': value.model_dump(mode='json'),
            'result': value.model_dump(mode='json'),
        })
        return value

    async def run(self, job, role, instructions, context, schema, validate=lambda result: []):
        self.calls.append(job)
        if job == 'foundation.extract':
            if self.foundation_fails:
                self.storage.snapshot('job:' + job, {'status': 'failed', 'error': '固定的格式失败'})
                raise JobFailed(job + ': 固定的格式失败')
            value = Foundation(
                catalog=Register(entities={'ENT_1': Entity(name='甲')}, locations={}, objects={}, world_traits={}),
                information=RepositoryInformation(
                    story_overview='甲出发并改变了之后的行程',
                    story_spine={
                        'endings': ['甲已经出发'], 'turning_points': ['甲决定出发'],
                        'causal_chains': [[{
                            'link': text,
                            'signature': {'actual_subjects': ['ENT_1'], 'occurrence': text,
                                          'start_anchor': f'故事阶段{index}', 'layer': '实际发生'},
                            'core_reason': '删除该环节会使后续行程或结局无法成立',
                        } for index, text in enumerate(('甲决定出发', '甲实际出发'), 1)]],
                    },
                    characters={'ENT_1': {
                        'activity': '主角', 'identity': ['出发者'], 'profile': '实际实施出发的人物',
                    }},
                    locations={}, background={'description': '原文仅给出出发这一故事背景'},
                ).model_dump(mode='json'),
            )
            return self.complete(job, value, validate)
        if job == 'foundation.check':
            return self.complete(job, FoundationReview(
                summary='目录与档案已独立核对',
                audit={
                    'checked_entity_ids': ['ENT_1'], 'checked_location_ids': [],
                    'background_checked': True,
                    'relation_findings': {'ENT_1': '原文未明示与其他人物的可建立关系'},
                    'settings_checked': True, 'overview_checked': True, 'unresolved': [],
                }), validate)
        if job.endswith('.reading'):
            index = int(job.split('.')[1])
            evidence = [Evidence(chunk=index, passage_ids=[f'C{index}_P1'])]
            value = Reading(who='甲', what='出发', where='未知', when='故事开始', why='推动行程', how='实际动身',
                            core_candidates=[CoreCandidate(
                                description='甲出发', evidence=evidence,
                                signature={
                                    'actual_subjects': ['甲'], 'occurrence': '实际出发',
                                    'start_anchor': '故事开始', 'layer': '实际发生',
                                },
                                core_reason='这是后续行程成立的直接起点',
                            )])
            return self.complete(job, value, validate)
        if job.endswith('.first_pass'):
            index = int(job.split('.')[1])
            actor = 'ENT_MISSING' if self.invalid_reference else f'ENT_C{index}_1'
            entities = {} if self.invalid_reference else {actor: Entity(name='甲')}
            value = EventPass(
                entities=entities,
                events=[Event(
                    id=f'EVT_C{index}_E1', description='甲出发', actor_ids=[actor],
                    source_time='故事开始', time_node_id=f'TN_C{index}_1', event_order=1,
                    temporal_basis='原文显示甲出发', core_reason='主要因果链起点',
                    evidence=[Evidence(chunk=index, passage_ids=[f'C{index}_P1'])],
                    epistemic_status='事实',
                    occurrence_layer='实际发生',
                    occurrence_signature={
                        'actual_subjects': [actor], 'occurrence': '实际出发',
                        'start_anchor': '故事开始', 'layer': '实际发生',
                    },
                )],
                time_nodes=[TimeNode(id=f'TN_C{index}_1', name='出发阶段', order=1)],
                coverage='已核对本块核心发生',
            )
            return self.complete(job, value, validate)
        if job.endswith('.second_pass'):
            return self.complete(job, EventPass(coverage='无新增核心言语或关系'), validate)
        if job.endswith('.consequences'):
            return self.complete(job, Consequences(changes=[], coverage='无新增核心后果'), validate)
        if job.startswith('global.timeline.'):
            key = context['required_event_ids'][0]
            value = TimelinePlan(
                summary='依原文建立实际开始顺序',
                time=Timeline(
                    nodes=[TimeNode(id='N1', name='出发阶段', order=1, relative='故事开始')],
                    timeline=[TimePlacement(id=key, node_id='N1', order=1, basis='原文开始锚点')],
                ),
            )
            return self.complete(job, value, validate)
        if job.startswith('global.review.'):
            ids = [row['id'] for row in context['world']['events']]
            check_ids = context.get('time_confirmation', {}).get('check_now', [])
            requirements = context.get('time_confirmation', {}).get('required_comparisons', {})
            checks = [CandidateCheck(
                candidate_id=key, event_ids=ids, reason='原文支持', disposition='mapped',
                signature={
                    'actual_subjects': ['甲'], 'occurrence': '实际出发',
                    'start_anchor': '故事开始', 'layer': '实际发生',
                })
                      for key in context.get('core_candidates', {})]
            value = Review(
                is_valid=False, time_valid=True, summary='保留前置失败职责', issues=['全文目录与档案仍待补齐'],
                covered_chunks=[chunk['index'] for chunk in context['source_chunks']],
                candidate_checks=checks,
                time_checks=[TimeCheck(id=key, verdict='confirmed', reason='已依原文独立比较',
                    comparisons=requirements.get(key, {}),
                    comparison_reasons={other: '原文开始锚点支持该相对顺序'
                                        for other in requirements.get(key, {})}) for key in check_ids],
            )
            return self.complete(job, value, validate)
        raise AssertionError('unexpected job: ' + job)


class FoundationFailureIsolationTests(unittest.TestCase):
    def workflow(self, agent):
        workflow = NovelExtractWorkflow()
        workflow.storage = agent.storage
        workflow.log = MemoryLog()
        workflow.agent = agent
        workflow.concurrency = 2
        workflow.failures = []
        return workflow

    def test_failed_foundation_continues_chunks_and_timeline_but_stays_partial(self):
        storage = MemoryStorage()
        agent = FixedAgent(storage, foundation_fails=True)
        workflow = self.workflow(agent)

        result, accepted = asyncio.run(workflow.extract('甲出发。'))

        self.assertFalse(accepted)
        self.assertEqual(set(result.entities), {'ENT_C1_1'})
        self.assertEqual([event.id for event in result.events], ['EVT_C1_E1'])
        self.assertTrue(any(job.startswith('global.timeline.') for job in agent.calls))
        self.assertTrue(storage.get('review_state')['confirmed_times'])
        self.assertTrue(any(row['job'] == 'foundation.extract' for row in workflow.failures))
        self.assertEqual(storage.latest('register'), {
            'entities': {}, 'locations': {}, 'objects': {}, 'world_traits': {},
        })

    def test_empty_fallback_does_not_bypass_chunk_reference_validation(self):
        storage = MemoryStorage()
        agent = FixedAgent(storage, foundation_fails=True, invalid_reference=True)
        workflow = self.workflow(agent)
        chunks = chunk_text('甲出发。')
        register = asyncio.run(workflow.foundation('甲出发。'))

        result = asyncio.run(workflow.extract_chunk(chunks[0], chunks, register, None, asyncio.Semaphore(1)))

        self.assertFalse(result.events)
        self.assertTrue(any(row['job'] == 'chunk.1.first_pass' for row in workflow.failures))

    def test_valid_foundation_keeps_existing_extract_and_check_path(self):
        storage = MemoryStorage()
        agent = FixedAgent(storage, foundation_fails=False)
        workflow = self.workflow(agent)

        register = asyncio.run(workflow.foundation('甲出发。'))

        self.assertEqual(set(register.entities), {'ENT_1'})
        self.assertEqual(agent.calls[:2], ['foundation.extract', 'foundation.check'])
        self.assertFalse(workflow.failures)
        self.assertIsNotNone(storage.latest('information'))

    def test_foundation_failure_cannot_be_resolved_without_source_backed_profiles(self):
        storage = MemoryStorage()
        agent = FixedAgent(storage)
        workflow = self.workflow(agent)
        workflow.failures = [{'job': 'foundation.extract', 'error': '前置交付失败'}]
        review = Review(
            is_valid=False, summary='企图直接销项', issues=[], covered_chunks=[1],
            resolved_jobs=['foundation.extract'],
        )

        workflow.evaluate_review(
            review,
            World(entities={}, locations={}, objects={}, world_traits={}),
            chunk_text('甲出发。'),
        )

        self.assertFalse(review.resolved_jobs)
        self.assertFalse(review.is_valid)
        self.assertTrue(workflow.failures)


if __name__ == '__main__':
    unittest.main()
