"""离线回归：真实 AutoGen + 固定 HTTP 交付；任何真实网络访问立即失败。"""
import asyncio
from contextlib import closing, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import httpx

from common.novel_extract.NovelExtractReader import chunk_text, compact_evidence
from common.novel_extract.NovelExtractSchema import (
    CandidateCheck, Change, ChangeRemoval, CoreCandidate, CoreChainCheck, Correction, Entities, Entity, EventMerge, Event, EventPass,
    Evidence, Foundation, FoundationReview, OccurrenceSignature, Reading, Relation, RelationRemoval, RepositoryInformation, Review, StoryLink,
    GroupedTimeline, TimeCheck, TimeNode, TimePlacement, Timeline, TimelinePlan, World, apply_correction, apply_information, apply_timeline, evidence_errors,
    fact_dependencies, information_dependency, time_comparisons, time_dependencies, canonical_keys, normalize_evidence, normalize_references, information_errors, register_errors, timeline_errors, topology_errors,
)
from common.novel_extract.NovelExtractRuntime import ROOT, digest
from model.ModelClient import CALL_SCOPE
from storage.novel_extract.NovelExtractStorage import NovelExtractStorage
from exporters.NovelExtractExporter import NovelExtractExporter
from workflows.NovelExtractWorkflow import NovelExtractWorkflow, ReviewState


def event(index=1, **updates):
    data = dict(id=f'EVT_C{index}_E1', description='甲出发', actor_ids=['ENT_1'],
                core_reason='故事起点', temporal_basis='出发时', epistemic_status='事实',
                occurrence_layer='实际发生',
                evidence=[Evidence(chunk=index, passage_ids=[f'C{index}_P1'])])
    data.update(updates)
    if 'occurrence_signature' not in updates:
        data['occurrence_signature'] = {
            **occurrence_signature(index), 'actual_subjects': list(data['actor_ids']),
            'layer': data['occurrence_layer'],
        }
    return Event(**data)


def world(events):
    return World(entities={'ENT_1': {'name': '甲'}}, locations={}, objects={}, world_traits={}, events=events)


def information():
    return RepositoryInformation(
        story_overview='甲出发并推动了之后的行程',
        story_spine={
            'endings': ['甲已经出发'], 'turning_points': ['甲决定出发'],
            'causal_chains': [[story_link('甲决定出发', 1), story_link('甲实际出发', 2)]],
        },
        characters={'ENT_1': {
            'activity': '主角', 'identity': ['出发者'], 'profile': '实际实施出发的人物',
        }},
        locations={}, background={
            'description': '原文仅给出出发这一故事背景',
            'detail': '故事在出发与行程的现实环境中展开',
        },
    )


def occurrence_signature(index=1):
    return {
        'actual_subjects': ['甲'], 'occurrence': '实际出发',
        'start_anchor': f'故事阶段{index}', 'layer': '实际发生',
    }


def story_link(text, index=1):
    return {
        'link': text,
        'signature': occurrence_signature(index),
        'core_reason': '删除该环节会使后续行程或故事结局无法成立',
    }


def information_audit(entity_ids=('ENT_1',), location_ids=()):
    return {
        'checked_entity_ids': list(entity_ids), 'checked_location_ids': list(location_ids),
        'background_checked': True,
        'relation_findings': {key: '已逐段核对原文明示关系；无记录时原文未明示可建立关系' for key in entity_ids},
        'settings_checked': True, 'overview_checked': True, 'unresolved': [],
    }


def context_information_audit(context):
    """固定回复要审计当前世界版本，不能继续回传只覆盖 ENT_1 的旧清单。"""
    catalog = context.get('world') or context.get('current', {}).get('catalog', {})
    return information_audit(sorted(catalog.get('entities', {})), sorted(catalog.get('locations', {})))


def format36_information(value):
    """为只读历史回复补上格式36新增的故事骨架，不改写历史库。"""
    data = deepcopy(value)
    overview = str(data.get('story_overview') or '历史回放中的故事主干').strip()
    data.setdefault('story_spine', {
        'endings': [overview],
        'turning_points': [overview],
        'causal_chains': [[story_link(overview, 1), story_link('上述主干导致故事结局', 2)]],
    })
    return data


def format36_world(value):
    """历史事件没有发生层字段；回放事实补丁时显式标成实际发生。"""
    result = value.model_copy(deep=True) if isinstance(value, World) else World.model_validate(value)
    for item in result.events:
        if item.occurrence_layer == '未分类':
            item.occurrence_layer = '实际发生'
        if item.occurrence_signature is None:
            item.occurrence_signature = OccurrenceSignature.model_validate({
                'actual_subjects': item.actor_ids, 'occurrence': item.description,
                'start_anchor': item.source_time or item.temporal_basis, 'layer': item.occurrence_layer,
            })
    return result


def format36_review(value):
    """只迁移历史修订中新建事件；旧式时间判断保留为协议反例。"""
    if not isinstance(value, Review):
        value = deepcopy(value)
        for removal in value.get('correction', {}).get('removals', []):
            removal.setdefault('signature', {
                'actual_subjects': [], 'occurrence': removal.get('reason') or '历史删除',
                'start_anchor': '历史删除未结构化', 'layer': '未分类'})
    result = value.model_copy(deep=True) if isinstance(value, Review) else Review.model_validate(value)
    for item in result.correction.events:
        if item.occurrence_layer == '未分类':
            item.occurrence_layer = '实际发生'
        if item.occurrence_signature is None:
            item.occurrence_signature = OccurrenceSignature.model_validate({
                'actual_subjects': item.actor_ids, 'occurrence': item.description,
                'start_anchor': item.source_time or item.temporal_basis, 'layer': item.occurrence_layer,
            })
    for fields in result.correction.event_updates.values():
        if fields.get('occurrence_layer') == '未分类':
            fields['occurrence_layer'] = '实际发生'
    return result


def time_check(key, comparisons=None, verdict='confirmed', reason='已逐对核对实际开始'):
    comparisons = comparisons or {}
    return TimeCheck(
        id=key, verdict=verdict, reason=reason, comparisons=comparisons,
        comparison_reasons={other: '原文的阶段、相对词或因果前提支持该判断'
                            for other in comparisons},
        comparison_sources={other: [Evidence(chunk=1, passage_ids=['C1_P1'])]
                            for other in comparisons},
    )


class FixtureServer:
    """只在验证中提供固定候选，不分析原文、不替代生产模型。"""
    def __init__(self, mode='normal'):
        self.mode, self.requests, self.active, self.peak = mode, [], 0, 0
        self.contexts = {}
        self.source_occurrences = 2
        self.source_event_count = 2

    async def handle(self, request):
        body = json.loads(request.content)
        job, attempt = CALL_SCOPE.get()
        context = json.JSONDecoder().raw_decode(body['messages'][-1]['content'])[0]
        if 'previous_candidate' in context and 'errors' in context:
            context = self.contexts[job]
        else:
            self.contexts[job] = context
        self.requests.append((job, attempt, body))
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(.002)
        self.active -= 1
        if self.mode == 'retry' and len(self.requests) == 1:
            return httpx.Response(429, json={'error': 'temporary'})
        result = self.reply(job, attempt, context)
        raw = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': raw}}],
                                         'usage': {'prompt_tokens': 10, 'completion_tokens': 5}})

    def reply(self, job, attempt, context):
        if job == 'foundation.extract':
            self.source_occurrences = context['novel_source'].count('甲出发。')
            self.source_event_count = max(2, self.source_occurrences)
            details = information().model_dump(mode='json')
            if self.source_event_count > 2:
                details['story_spine']['causal_chains'] = [[
                    story_link(f'甲的核心行程环节{i}', i) for i in range(1, self.source_event_count + 1)
                ]]
            return {'catalog': world([]).model_dump(exclude={'events', 'relations', 'changes', 'time_nodes'}),
                    'information': details}
        if job == 'foundation.check':
            return {'summary': '独立核对身份与资料，无遗漏', 'issues': [],
                    'audit': information_audit()}
        if job.startswith('information.supplement.'):
            return {'characters': {key: {
                        'activity': 'npc', 'identity': ['原文补建人物'], 'profile': '原文中参与核心发生的人物',
                    } for key in context['missing']['characters']},
                    'locations': {key: {'description': '原文中核心发生的地点'}
                                  for key in context['missing']['locations']}}
        if job.startswith('chunk.'):
            index = int(job.split('.')[1])
            if job.endswith('reading'):
                candidate = CoreCandidate(
                    description='甲出发', evidence=event(index).evidence,
                    signature=occurrence_signature(index),
                    core_reason='这是后续行程成立的直接起点',
                )
                return Reading(who='甲', what='出发', where='未知', when='未知', why='未知', how='未知',
                               core_candidates=[candidate]).model_dump(mode='json')
            if job.endswith('first_pass'):
                if self.mode == 'format' and attempt == 0: return '{broken'
                if self.mode in ('failed', 'rescued') and index == 1: return '{broken'
                events = [event(index, time_node_id=f'TN_C{index}_1', event_order=1)]
                if self.source_occurrences == 1 and index == 1 and context['chunk']['passages'].get('C1_P1') \
                        and context['chunk']['passages']['C1_P1'].count('甲出发') == 1:
                    events.append(event(index, id='EVT_C1_E2', description='甲继续行程',
                                        occurrence_signature=occurrence_signature(2),
                                        time_node_id=f'TN_C{index}_1', event_order=2))
                return EventPass(events=events,
                    time_nodes=[TimeNode(id=f'TN_C{index}_1', name='出发阶段', order=1)],
                    coverage='已收录').model_dump(mode='json')
            if job.endswith('second_pass'): return EventPass(coverage='无新增').model_dump(mode='json')
            return {'changes': [], 'coverage': '无其他状态变化'}
        round_index = int(job.split('.')[-1])
        if job.startswith('global.timeline.'):
            return TimelinePlan(summary='按实际开始归纳阶段', time=self.timeline(context['required_event_ids'])).model_dump(mode='json')
        if self.mode == 'failed':
            return Review(is_valid=False, summary='保留失败', issues=['无法补齐'],
                          covered_chunks=[c['index'] for c in context['source_chunks']]).model_dump(mode='json')
        correction = Correction()
        if self.mode == 'rescued' and round_index == 0:
            correction.events = [event(1)]
        if self.mode == 'late_facts' and round_index < 3:
            correction.world_traits = {f'WORLD_{round_index}': f'事实 {round_index}'}
        if self.mode == 'revise_after_time' and round_index == 0:
            correction.event_updates = {context['required_event_ids'][0]: {'description': '甲正式出发'}}
        if self.mode == 'oscillate':
            correction.world_traits = {'WORLD_1': str(round_index % 2)}
        candidates = context.get('core_candidates', {})
        checks = [CandidateCheck(
            candidate_id=k, event_ids=[f'EVT_C{k.split("_")[0][1:]}_E1'], reason='原文支持',
            disposition='mapped', signature=candidates[k]['signature'])
                  for k in candidates]
        resolved = [f['job'] for f in context['failed_jobs']]
        ids = sorted([row['id'] for row in context['world']['events']] + [e.id for e in correction.events])
        changed_ids = set(correction.event_updates) | {row.id for row in correction.events}
        check_ids = [key for key in context.get('time_confirmation', {}).get('check_now', [])
                     if key not in changed_ids]
        requirements = context.get('time_confirmation', {}).get('required_comparisons', {})
        audit = context_information_audit(context)
        information_patch = None
        current_entities = sorted(context['world']['entities'])
        if not context.get('information'):
            information_patch = information().model_dump(mode='json')
            audit = information_audit()
        elif len(current_entities) > 1 and not context['information'].get('relations'):
            information_patch = {'relations': [{
                'owner': current_entities[0], 'other': current_entities[1],
                'relation': ['原文中的相关人物'],
                'detail': '两人在原文同一核心行程中发生直接关联',
            }]}
            audit = None
        # 事实修订会使本事件以及直接比较对端的时间依赖失效；
        # 它们必须留到下一轮独立确认，不能在修订同轮申明 confirmed。
        if correction.events:
            check_ids = []
        elif changed_ids:
            affected = set(changed_ids)
            for key, peers in requirements.items():
                for other in peers:
                    if key in changed_ids or other in changed_ids:
                        affected.update((key, other))
            check_ids = [key for key in check_ids if key not in affected]
        delivered_information = information_patch if not context.get('information') else context['information']
        chains = (delivered_information.get('story_spine') or {}).get('causal_chains') or []
        chain = chains[0] if chains else [story_link(f'甲的核心行程环节{i}', i) for i in range(1, len(ids) + 1)]
        return Review(is_valid=self.mode != 'oscillate', time_valid=True,
            issues=['尚有问题'] if self.mode == 'oscillate' else [],
            time_checks=[TimeCheck(id=key, verdict='confirmed', reason='依据原文再次比较',
                comparisons=requirements.get(key, {}),
                comparison_reasons={other: '原文开始锚点支持该相对顺序'
                                    for other in requirements.get(key, {})},
                comparison_sources={other: [Evidence(chunk=1, passage_ids=['C1_P1'])]
                                    for other in requirements.get(key, {})}) for key in check_ids],
            summary='跨块核实', correction=correction, candidate_checks=checks, resolved_jobs=resolved,
            information=information_patch, information_audit=audit,
            core_chain_checks=[{
                'chain_index': 1,
                'links': [{
                    'link': link['link'], 'event_id': event_id, 'signature': link['signature'],
                    'reason': '该事件实际发生并完成对应的故事主干环节',
                } for link, event_id in zip(chain, ids)],
                'missing_links': [],
                'reason': '固定夹具的事件按实际开始先后覆盖故事主干',
            }],
            covered_chunks=[c['index'] for c in context['source_chunks']],
            time=self.timeline(ids)).model_dump(mode='json')

    def timeline(self, ids):
        if self.mode == 'bad_time': ids = ids[:-1]
        placements, nodes = [], {}
        for i, key in enumerate(ids, 1):
            number = 1 if self.mode == 'simultaneous' else ((1 if i <= 3 else 2) if self.mode == 'hierarchy' else i)
            node_id = f'N{number}'
            nodes[node_id] = TimeNode(id=node_id, name=f'阶段{number}',
                order=None if self.mode == 'unknown' else number, relative='原文故事阶段')
            order = ((1 if i in (1, 4) else 2) if self.mode == 'hierarchy' else 1)
            placements.append(TimePlacement(id=key, node_id=node_id,
                order=None if self.mode == 'unknown' else order, basis='依据原文'))
        return Timeline(nodes=list(nodes.values()), timeline=placements)


class GroupedFixtureServer(FixtureServer):
    """使用生产公开的嵌套协议；原平面固定回复继续用于历史兼容与反例。"""
    def reply(self, job, attempt, context):
        result = super().reply(job, attempt, context)
        if isinstance(result, dict) and job.startswith('global.') and result.get('time'):
            old = result['time']
            result['time'] = {'nodes': [
                {k: v for k, v in n.items() if k != 'order'} | {'events': [
                    {k: v for k, v in e.items() if k != 'node_id'}
                    for e in old['timeline'] if e['node_id'] == n['id']]}
                for n in old['nodes']]}
        return result


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='novel-harness-', dir=Path(tempfile.gettempdir()).resolve())
        self.directory = Path(self.temp.name)
        self.network = patch.object(socket.socket, 'connect', side_effect=AssertionError('禁止真实网络'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.addCleanup(self.temp.cleanup)

    def run_pipeline(self, server, chunks=2, max_calls=1000, hook=None):
        source = self.directory / 'source.txt'
        if not source.exists():
            source.write_text(('甲出发。\n\n' + '路旁有树。' * 190 + '\n\n') * chunks)
            (self.directory / 'config.json').write_text(json.dumps({
                'api_url': 'https://offline.invalid/v1/chat/completions', 'model': 'fixture',
                'api_key_env': 'NOVEL_HARNESS_OFFLINE_KEY', 'timeout_seconds': 1,
                'max_retries': 1, 'max_calls': max_calls}))
        self.workspace = self.directory / 'work.sqlite3'
        self.output = self.directory / 'repository'
        args = ['--input', str(source), '--config', str(self.directory / 'config.json'),
                '--workspace', str(self.workspace), '--output', str(self.output),
                '--log-dir', str(self.directory / 'logs'), '--log-terminal', 'quiet', '--concurrency', '3']
        original = httpx.AsyncClient
        def client(**kwargs):
            return original(**kwargs, transport=httpx.MockTransport(server.handle))
        with patch.dict('os.environ', {'NOVEL_HARNESS_OFFLINE_KEY': 'offline-only'}), \
             patch('httpx.AsyncClient', client), redirect_stdout(io.StringIO()):
            if hook:
                with hook: return NovelExtractWorkflow().run(args)
            return NovelExtractWorkflow().run(args)

    def snapshots(self):
        with closing(sqlite3.connect(self.workspace.as_uri() + '?mode=ro', uri=True)) as db:
            return {s: json.loads(v) for s, v in db.execute('select stage,payload from snapshots order by id')}

    def test_descriptions_do_not_revoke_time_but_actual_anchor_and_identity_do(self):
        w, chunks = self.timed_world()
        w = World.model_validate({**w.model_dump(), 'locations': {'LOC_1': {'name': '屋子'}},
                                 'objects': {'OBJ_1': {'name': '物品'}}})
        for e in w.events: e.at_location_id, e.object_ids = 'LOC_1', ['OBJ_1']
        w.relations = [Relation(source=w.events[0].id, target=w.events[1].id, kind='before', basis='原文')]
        state = self.confirmed_state(w); changed = w.model_copy(deep=True)
        changed.entities['ENT_1'].description = '档案补充'
        changed.locations['LOC_1'].description = '换一种说明'
        changed.objects['OBJ_1'].description = '补充物品介绍'
        changed.time_nodes[0].name, changed.time_nodes[0].relative = '阶段说明', '同一开始的解释'
        changed.relations[0].basis = '同一先后的解释'
        check = Review(is_valid=True, summary='描述修订', issues=[], covered_chunks=[1])
        NovelExtractWorkflow().update_time_confirmations(state, check, w, changed)
        self.assertEqual(set(state.confirmed_times), {e.id for e in w.events})
        self.assertNotEqual(fact_dependencies(w, chunks), fact_dependencies(changed, chunks))
        for field, mutate in (
                ('anchor', lambda x: setattr(x.events[0], 'source_time', '次年')),
                ('identity', lambda x: setattr(x.entities['ENT_1'], 'name', '乙')),
                ('evidence', lambda x: setattr(x.events[0].evidence[0], 'quote', '不同发生')),
                ('relation', lambda x: setattr(x.relations[0], 'kind', 'enables'))):
            with self.subTest(field=field):
                target = w.model_copy(deep=True); mutate(target); state = self.confirmed_state(w)
                NovelExtractWorkflow().update_time_confirmations(state, check.model_copy(deep=True), w, target)
                self.assertNotIn(w.events[0].id, state.confirmed_times)

    @unittest.skip("Legacy proof-table protocol retired; see reports/novel_extract/shadowloom_validation_reset_20261016/retired_tests.md")
    def test_unknown_time_warning_does_not_force_a_repair(self):
        w, chunks = self.timed_world()
        storage = NovelExtractStorage(self.directory/'check.sqlite3'); self.addCleanup(storage.close)
        storage.snapshot('information', information().model_dump(mode='json'))
        workflow = NovelExtractWorkflow(); workflow.storage, workflow.failures = storage, []
        nodes = [n.model_copy(deep=True) for n in w.time_nodes]; nodes[0].relative = '换一种说法'
        required = workflow.time_check_requirements(w, {w.events[0].id})[w.events[0].id]
        rejected = dict(required)
        peer = next(iter(rejected))
        rejected[peer] = 'unknown'
        review = Review(is_valid=False, summary='仍待核实', issues=[], covered_chunks=[1], time=Timeline(nodes=nodes),
            time_checks=[time_check(w.events[0].id, rejected, verdict='unknown', reason='没有日期')])
        errors = workflow.validate_review_delivery(review, w, chunks, {w.events[0].id})
        self.assertFalse(errors)

    @unittest.skip("Legacy proof-table protocol retired; see reports/novel_extract/shadowloom_validation_reset_20261016/retired_tests.md")
    def test_same_round_review_accepts_changed_fact_without_repeating_coverage(self):
        class Focused(FixtureServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job == 'global.review.0':
                    self.focus = context
                    result['correction']['event_updates'] = {'EVT_C1_E1': {'description': '甲正式出发'}}
                    result['time_checks'] = []
                return result
        server = Focused()
        self.assertEqual(self.run_pipeline(server, chunks=2), 0)
        self.assertEqual(server.focus['review_scope']['fact_chunks'], [1, 2])
        self.assertEqual(server.focus['review_scope']['retained_fact_chunks'], [])
        self.assertEqual(server.focus['candidate_check_now'], ['C1_K1', 'C2_K1'])
        self.assertEqual(len(server.focus['source_chunks']), 2)  # 比较需要的全文仍可取得。
        self.assertEqual([(j,a) for j,a,_ in server.requests if j.startswith('global.')],
                         [('global.timeline.0',0),('global.review.0',0)])
        state = NovelExtractStorage.read_status(self.workspace)['review_state']
        self.assertEqual(set(state['checked_facts']), {'1','2'})
        self.assertEqual(set(state['candidate_checks']), {'C1_K1','C2_K1'})

    def test_incomplete_first_source_check_cannot_pass_or_fabricate_coverage(self):
        class SkipsSource(FixtureServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job.startswith('global.review.'):
                    result['covered_chunks'] = [1]
                return result
        self.assertEqual(self.run_pipeline(SkipsSource(), chunks=2), 2)
        state = NovelExtractStorage.read_status(self.workspace)['review_state']
        self.assertEqual(set(state['checked_facts']), {'1'})
        self.assertFalse(state['accepted'])

    def test_fact_dependency_rechecks_both_relation_endpoints_not_other_chunk(self):
        chunks = chunk_text(('甲出发。\n\n' + '路旁有树。'*190+'\n\n')*3)
        w = world([event(i) for i in range(1,4)])
        w.relations = [Relation(source=w.events[0].id, target=w.events[1].id, kind='before', basis='先后')]
        changed = w.model_copy(deep=True); changed.events[0].description = '甲返家'
        before, after = fact_dependencies(w, chunks), fact_dependencies(changed, chunks)
        self.assertEqual([i for i in before if before[i] != after[i]], [1,2])
        changed = w.model_copy(deep=True); changed.events[0].event_order = 2
        self.assertEqual(fact_dependencies(w, chunks), fact_dependencies(changed, chunks))

    @unittest.skip("Legacy proof-table protocol retired; see reports/novel_extract/shadowloom_validation_reset_20261016/retired_tests.md")
    def test_deleted_event_clears_candidate_mapping_but_same_review_covers_new_fact_version(self):
        w, chunks = self.timed_world()
        storage = NovelExtractStorage(self.directory/'coverage.sqlite3'); self.addCleanup(storage.close)
        storage.snapshot('job:chunk.1.reading', {'result': {'core_candidates': [
            CoreCandidate(description='甲出发', evidence=event().evidence,
                          signature=occurrence_signature(),
                          core_reason='这是后续行程成立的直接起点').model_dump(mode='json')]}})
        workflow = NovelExtractWorkflow(); workflow.storage = storage
        state = ReviewState(checked_facts={1:digest(fact_dependencies(w,chunks)[1])},
                            candidate_checks={'C1_K1':CandidateCheck(
                                candidate_id='C1_K1', event_ids=[w.events[0].id], reason='原文',
                                disposition='mapped', signature=occurrence_signature())})
        changed = w.model_copy(deep=True); changed.events.pop(0)
        review = Review(is_valid=True, summary='删除', issues=[], covered_chunks=[1])
        workflow.advance_fact_review(state, review, w, changed, chunks)
        self.assertEqual(set(state.checked_facts), {1})
        self.assertFalse(state.candidate_checks)

    @unittest.skip("Legacy proof-table protocol retired; see reports/novel_extract/shadowloom_validation_reset_20261016/retired_tests.md")
    def test_coverage_and_candidate_progress_restore_atomically(self):
        original, fired = NovelExtractStorage.set, []
        def stop(storage,key,value):
            original(storage,key,value)
            if key == 'review_state' and value['checked_facts'] and not fired:
                fired.append(True); raise KeyboardInterrupt()
        server=FixtureServer('revise_after_time')
        self.assertEqual(self.run_pipeline(server,hook=patch.object(NovelExtractStorage,'set',stop)),130)
        count=len(server.requests)
        self.assertEqual(NovelExtractStorage.read_status(self.workspace)['review_state']['checked_facts'], {})
        self.assertEqual(self.run_pipeline(server),0)
        self.assertEqual(len(server.requests),count)
        state=NovelExtractStorage.read_status(self.workspace)['review_state']
        self.assertEqual(set(state['checked_facts']),{'1','2'})
        self.assertEqual(set(state['candidate_checks']),{'C1_K1','C2_K1'})

    def test_record_deletion_is_precise_idempotent_and_can_replace_same_key(self):
        w, chunks = self.timed_world()
        edge = Relation(source=w.events[0].id,target=w.events[1].id,kind='before',basis='原文')
        w.relations=[edge]
        review=Review(is_valid=False,summary='删除错误边',issues=[],covered_chunks=[1],
                      correction=Correction(relation_removals=[RelationRemoval(**edge.model_dump(),reason='误录')]))
        once=apply_correction(w,review,chunks)
        self.assertEqual(apply_correction(once,review,chunks),once)
        review.correction.relations=[edge.model_copy(update={'basis':'修正依据'})]
        replacement=apply_correction(w,review,chunks)
        self.assertEqual(len(replacement.relations),1)
        self.assertEqual(replacement.relations[0].basis,'修正依据')
        review.correction.relations=[]
        review.correction.relation_removals[0].source='UNKNOWN'
        with self.assertRaisesRegex(ValueError,'未知引用.*UNKNOWN'):apply_correction(w,review,chunks)
        self.assertEqual(w.relations,[edge])

    def test_wrong_deletion_direction_and_state_wording_report_current_values(self):
        w,chunks=self.timed_world()
        edge=Relation(source=w.events[0].id,target=w.events[1].id,kind='before',basis='原文')
        w.relations=[edge]
        review=Review(is_valid=False,summary='修复',issues=[],covered_chunks=[1],correction=Correction(
            relation_removals=[RelationRemoval(source=edge.target,target=edge.source,kind='before',basis='原文',reason='方向写反')]))
        with self.assertRaisesRegex(ValueError,'相关已保存记录') as caught:apply_correction(w,review,chunks)
        self.assertIn(edge.source,str(caught.exception)); self.assertIn(edge.target,str(caught.exception))
        w.changes=[Change(event_id=edge.source,subject_id='ENT_1',change='开始远行',basis='原文')]
        review.correction=Correction(change_removals=[ChangeRemoval(event_id=edge.source,subject_id='ENT_1',change='远行开始',basis='原文',reason='改写了旧键')])
        with self.assertRaisesRegex(ValueError,'开始远行'):apply_correction(w,review,chunks)
        self.assertEqual(w.changes[0].change,'开始远行')

    def test_time_comparisons_expose_interleaved_groups_and_simultaneous_members(self):
        w,chunks=self.timed_world()
        w.events[0].description='出发'; w.events[1].description='抵达'; w.events[2].description='途中遇险'
        w.events[1].time_node_id='N1'; w.events[1].event_order=2
        w.time_nodes=[n for n in w.time_nodes if n.id!='N2']
        pairs=time_comparisons(w)
        self.assertEqual(pairs,[{'left':w.events[0].id,'right':w.events[1].id,
                                'scope':'within_node','draft':'before'}])
        # N1→N3 中间有缺失节点，不得把尚未修复的编号空洞伪装成相邻边界。
        next(node for node in w.time_nodes if node.id == 'N3').order = 2
        pairs=time_comparisons(w)
        self.assertEqual(pairs,[{'left':w.events[0].id,'right':w.events[1].id,'scope':'within_node','draft':'before'},
                                {'left':w.events[1].id,'right':w.events[2].id,'scope':'between_nodes','draft':'before'}])
        # 节点内链已覆盖 E1→E2，跨节点只比较前节点最晚组与后节点最早组，
        # 传递性已证明 E1→E3，不再做重复全对比较。
        # 这是需要模型反驳的错误草稿边界，不是程序当作真值的 before 边。
        self.assertEqual(w.relations,[])
        w.events[1].event_order=1
        self.assertEqual(time_comparisons(w)[0]['draft'],'simultaneous')

    def test_formal77_last_description_changes_keep_independent_time_confirmations(self):
        database=ROOT/'.novel_agent_work/祝福_gpt4o_正式77.sqlite3'
        if not database.exists():self.skipTest('正式77历史库不在当前环境')
        with closing(sqlite3.connect(database.as_uri()+'?mode=ro&immutable=1',uri=True)) as db:
            meta={k:json.loads(v) for k,v in db.execute('select key,value from meta')}
            rows=[(i,s,json.loads(p)) for i,s,p in db.execute('select id,stage,payload from snapshots order by id')]
        boundary,_,raw=next(row for row in reversed(rows) if row[1]=='job:global.review.6')
        latest={s:p for i,s,p in rows if i<=boundary}
        storage=NovelExtractStorage(self.directory/'formal77.sqlite3');self.addCleanup(storage.close)
        storage.snapshot('information', format36_information(latest['information']))
        storage.snapshot('world', format36_world(latest['world']).model_dump(mode='json'))
        workflow=NovelExtractWorkflow();workflow.storage,workflow.failures=storage,[]
        chunks=chunk_text(meta['input_text'])
        before=format36_world(latest['world']); review=format36_review(raw['result'])
        after,_=workflow.evaluate_review(review,before,chunks)
        state=self.confirmed_state(before)
        workflow.update_time_confirmations(state,review,before,after,chunks)
        # 正式77旧回复中的笼统判断不会撤销已有确认；真正的事实与结构问题仍会被识别。
        self.assertEqual(set(state.confirmed_times), {event.id for event in before.events})
        self.assertFalse(review.is_valid)
        self.assertEqual(len(after.events),26)
        pairs={(p['left'],p['right']) for p in time_comparisons(after)}
        self.assertIn(('EVT_C5_E3','EVT_C6_E1'),pairs)  # 返鲁/阿毛遇难的错误组间边界。
        self.assertIn(('EVT_C8_E3','EVT_C8_E4'),pairs)  # 精神恶化/祭祀被拒的错误组内顺序。

    def test_grouped_delivery_preserves_two_orders_and_terminal_replay(self):
        server = GroupedFixtureServer('hierarchy')
        self.assertEqual(self.run_pipeline(server, chunks=4), 0)
        w = self.snapshots()['world']
        self.assertEqual([n['order'] for n in w['time_nodes']], [1, 2])
        self.assertEqual([e['event_order'] for e in w['events']], [1, 2, 2, 1])
        first = json.loads((self.output / 'timeline/N1_阶段1/todolist.json').read_text())
        self.assertEqual([e['order'] for e in first['todolist']], [1, 2, 2])
        count = len(server.requests)
        self.assertEqual(self.run_pipeline(server, chunks=4), 0)
        self.assertEqual(len(server.requests), count)
        # 公开协议只有节点包含事件；历史平面对象不再要求模型维护两份映射。
        schema = TimelinePlan.model_json_schema()
        self.assertNotIn('Timeline', schema['$defs'])
        self.assertNotIn('order', schema['$defs']['TimeGroup']['properties'])
        self.assertIn('order', schema['$defs']['GroupedEvent']['properties'])

    def test_group_list_order_is_not_node_id_or_narrative_order(self):
        chunks = chunk_text('先写晚年，回忆早年。')
        w = world([event(id='LATE', description='晚年归乡'), event(id='EARLY', description='少年出走')])
        data = {'nodes': [
            {'id': 'N90', 'name': '少年', 'relative': '回忆中的早年，早于归乡',
             'events': [{'id': 'EARLY', 'order': 1, 'basis': '少年出走'}]},
            {'id': 'N1', 'name': '晚年', 'relative': '多年以后归乡',
             'events': [{'id': 'LATE', 'order': 1, 'basis': '晚年归乡'}]}]}
        plan = TimelinePlan.model_validate({'summary': '出走后归乡', 'time': data})
        result = NovelExtractWorkflow.apply_time_plan(plan, w, chunks)
        self.assertEqual([(n.id, n.order) for n in result.time_nodes], [('N90', 1), ('N1', 2)])
        self.assertEqual([e.event_order for e in result.events], [1, 1])
        data['nodes'].reverse()
        self.assertEqual(TimelinePlan.model_validate({'summary': '模型改排', 'time': data}).time.nodes[0].id, 'N1')
        # 程序忠实编码模型列表；它不能判断这次反转语义错误，也不能自行改回。

    def test_complete_grouping_cannot_drop_or_duplicate_events(self):
        w, chunks = self.timed_world()
        data = {'nodes': [{'id': 'N', 'name': '阶段', 'relative': '原文阶段',
            'events': [{'id': w.events[0].id, 'order': 1, 'basis': '原文'}]}]}
        plan = TimelinePlan.model_validate({'summary': '分组', 'time': data})
        with self.assertRaisesRegex(ValueError, '完整分组缺少事件'):
            NovelExtractWorkflow.apply_time_plan(plan, w, chunks)
        data['nodes'][0]['events'] *= 2
        plan = TimelinePlan.model_validate({'summary': '分组', 'time': data})
        with self.assertRaisesRegex(ValueError, '重复事件编号'):
            NovelExtractWorkflow.apply_time_plan(plan, w, chunks)
        self.assertEqual(len(w.events), 3)

    def test_malformed_nested_time_cannot_fall_back_to_empty_flat_time(self):
        invalid = {'nodes': [{'id': 'N1', 'name': '阶段', 'relative': '原文', 'events': []}]}
        with self.assertRaises(ValueError): TimelinePlan.model_validate({'summary': '分组', 'time': invalid})
        invalid['nodes'][0]['events'] = [{'id': 'E', 'order': True, 'basis': '原文'}]
        with self.assertRaises(ValueError): TimelinePlan.model_validate({'summary': '分组', 'time': invalid})
        invalid['nodes'][0]['events'][0]['order'] = 1
        invalid['nodes'][0]['order'] = 2
        with self.assertRaisesRegex(ValueError, '冗余节点order'):
            TimelinePlan.model_validate({'summary': '分组', 'time': invalid})
        invalid['nodes'][0]['order'] = '1'
        self.assertEqual(TimelinePlan.model_validate({'summary': '分组', 'time': invalid}).time.nodes[0].order, 1)
        invalid['nodes'][0]['events'][0]['node_id'] = 'WRONG'
        with self.assertRaisesRegex(ValueError, 'node_id与所属分组冲突'):
            TimelinePlan.model_validate({'summary': '分组', 'time': invalid})

    def test_unresolved_group_order_preserves_draft_without_bypassing_edges(self):
        w, chunks = self.timed_world()
        plan = TimelinePlan(summary='尚无法比较两个阶段，保留草稿', time=None, issues=['缺少相对先后线索'])
        workflow = NovelExtractWorkflow()
        self.assertEqual(workflow.apply_time_plan(plan, w, chunks), w)
        plan.relations = [Relation(source='UNKNOWN', target=w.events[0].id, kind='before', basis='错误编号')]
        with self.assertRaisesRegex(ValueError, '非法事件引用'):
            workflow.apply_time_plan(plan, w, chunks)

    def test_review_patch_error_repaired_before_next_global_round(self):
        class RepairPatch(GroupedFixtureServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job == 'global.review.0' and attempt == 0:
                    result['correction']['removals'] = [{'id': 'MISSING', 'reason': '错误删除编号'}]
                return result
        server = RepairPatch()
        result = self.run_pipeline(server, chunks=1)
        self.assertEqual(result, 0, self.snapshots().get('failures'))
        self.assertEqual([(j, a) for j, a, _ in server.requests if j.startswith('global.')],
                         [('global.timeline.0', 0), ('global.review.0', 0), ('global.review.0', 1)])
        followup = next(b['messages'][-1]['content'] for j, a, b in server.requests
                        if j == 'global.review.0' and a == 1)
        self.assertIn('$.correction.removals[0].signature', followup)
        self.assertIn('补充协议要求的必填值', followup)

    @unittest.skip("Legacy proof-table protocol retired; see reports/novel_extract/shadowloom_validation_reset_20261016/retired_tests.md")
    def test_review_mapping_error_repaired_in_the_same_task(self):
        class RepairMapping(FixtureServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job == 'global.review.0' and attempt == 0:
                    result['candidate_checks'].append(dict(result['candidate_checks'][0]))
                    result['issues'] = ['candidate_checks编号重复：C1_K1']
                return result

        server = RepairMapping()
        self.assertEqual(self.run_pipeline(server, chunks=1), 0)
        self.assertEqual([(job, attempt) for job, attempt, _ in server.requests
                          if job.startswith('global.')],
                         [('global.timeline.0', 0), ('global.review.0', 0),
                          ('global.review.0', 1)])
        feedback = next(body['messages'][-1]['content'] for job, attempt, body in server.requests
                        if job == 'global.review.0' and attempt == 1)
        self.assertIn('candidate_checks编号重复', feedback)

    @unittest.skip("Legacy proof-table protocol retired; see reports/novel_extract/shadowloom_validation_reset_20261016/retired_tests.md")
    def test_vague_time_rejection_gets_actionable_repair(self):
        class VagueCheck(GroupedFixtureServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job == 'global.review.0' and attempt == 0:
                    result['time'] = None
                    result['time_checks'][0].update(
                        verdict='unknown', reason='没有日期，需要进一步核实')
                    result['time_checks'][0]['comparisons'] = {}
                    result['time_checks'][0]['comparison_reasons'] = {}
                return result
        server = VagueCheck()
        self.assertEqual(self.run_pipeline(server, chunks=2), 0)
        reviews = [(j, a, b) for j, a, b in server.requests if j.startswith('global.review.')]
        self.assertEqual([(j, a) for j, a, _ in reviews],
                         [('global.review.0', 0), ('global.review.1', 0)])
        # 时间含义缺口必须回到带原文的下一轮，而不是在无原文的同任务格式重试里猜。
        followup = json.loads(reviews[1][2]['messages'][-1]['content'])
        self.assertTrue(any('verdict=unknown' in error
                            for error in followup['last_delivery']['review']['program_feedback']))
        self.assertEqual(followup['candidate_index'], {})
        self.assertEqual(followup['proposed_changes'], [])
        self.assertTrue(all(set(chunk) == {'index', 'passages'}
                            for chunk in followup['source_chunks']))
        self.assertEqual(NovelExtractStorage.read_status(self.workspace)['review_state']['rounds'], 3)

    @unittest.skip("Legacy proof-table protocol retired; see reports/novel_extract/shadowloom_validation_reset_20261016/retired_tests.md")
    def test_unknown_time_is_recorded_without_discarding_best_order(self):
        class Uncertain(GroupedFixtureServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job == 'global.review.0' and attempt == 0:
                    result['time'] = None
                    result['is_valid'] = True
                    result['issues'] = []
                    check = result['time_checks'][0]
                    peer = next(key for key in context['required_event_ids'] if key != check['id'])
                    check.update(
                        verdict='unknown', reason='两段只分别写出发，未比较日期或前后',
                        comparisons={peer: 'unknown'},
                    )
                return result
        server = Uncertain()
        self.assertEqual(self.run_pipeline(server, chunks=2), 0)
        self.assertEqual([(j, a) for j, a, _ in server.requests if j.startswith('global.review.')],
                         [('global.review.0', 0)])
        state = NovelExtractStorage.read_status(self.workspace)['review_state']
        self.assertTrue(state['time_doubts'])
        self.assertEqual(set(state['confirmed_times']), {'EVT_C1_E1', 'EVT_C2_E1'})

    def test_grouped_delivery_atomic_recovery_keeps_same_identity(self):
        original, stopped = NovelExtractStorage.set, []
        def stop(storage, key, value):
            original(storage, key, value)
            if key == 'review_state' and value['rounds'] == 1 and not stopped:
                stopped.append(True)
                raise KeyboardInterrupt()
        server = GroupedFixtureServer()
        self.assertEqual(self.run_pipeline(server, hook=patch.object(NovelExtractStorage, 'set', stop)), 130)
        calls = sum(j == 'global.timeline.0' for j, _, _ in server.requests)
        self.assertEqual(self.run_pipeline(server), 0)
        self.assertEqual(sum(j == 'global.timeline.0' for j, _, _ in server.requests), calls)

    @unittest.skip("Legacy proof-table protocol retired; see reports/novel_extract/shadowloom_validation_reset_20261016/retired_tests.md")
    def test_formal76_vague_checks_and_fact_errors_replayed_read_only(self):
        database = ROOT / '.novel_agent_work/祝福_gpt4o_正式76.sqlite3'
        if not database.exists(): self.skipTest('正式76历史库不在当前环境')
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
            meta = {k: json.loads(v) for k, v in db.execute('select key,value from meta')}
            rows = [(i,s,json.loads(p)) for i,s,p in db.execute('select id,stage,payload from snapshots order by id')]
        workflow = NovelExtractWorkflow()
        storage = NovelExtractStorage(self.directory / 'replay.sqlite3'); self.addCleanup(storage.close)
        workflow.storage, workflow.failures = storage, []
        chunks = chunk_text(meta['input_text'])
        for job in ('global.review.1', 'global.review.4'):
            boundary, _, raw = next(row for row in reversed(rows) if row[1] == 'job:' + job)
            latest = {s:p for i,s,p in rows if i <= boundary}
            storage.snapshot('information', format36_information(latest['information']))
            previous = format36_world(latest['world'])
            storage.snapshot('world', previous.model_dump(mode='json'))
            review = format36_review(raw['result'])
            if job.endswith('.1'):
                protocol_errors = workflow.validate_review_delivery(review, previous, chunks, [])
                self.assertFalse(any('目录修订未接收' in error or '时间修订未接收' in error
                                     for error in protocol_errors))
                workflow.evaluate_review(review, previous, chunks)
                self.assertTrue(any('目录修订未接收' in e and '非法事件引用 EVT_C3_E1' in e
                                    for e in review.issues))
                self.assertTrue(any('时间修订未接收' in e for e in review.issues))
            else:
                required = next(([check.id] for check in review.time_checks
                                 if workflow.time_check_requirements(previous, [check.id])[check.id]), None)
                self.assertIsNotNone(required)
                errors = workflow.validate_time_checks(review, previous, chunks, required)
                self.assertTrue(any('verdict=unknown须指出' in e for e in errors))
        goals = event(goals={'leave': '离开'})
        errors = topology_errors(world([goals]), world([goals]), chunk_text('甲出发。'))
        self.assertTrue(any('不是目的名称' in e and 'ENT_1' in e for e in errors))

    def test_timeline_task_is_focused_and_immediately_checked(self):
        server = FixtureServer()
        self.assertEqual(self.run_pipeline(server, chunks=2), 0)
        requests = [(job, json.JSONDecoder().raw_decode(body['messages'][-1]['content'])[0], body)
                    for job, _, body in server.requests if job.startswith('global.')]
        self.assertEqual([job for job, _, _ in requests], ['global.timeline.0', 'global.review.0'])
        _, plan, request = requests[0]
        self.assertNotIn('information', plan)
        self.assertNotIn('changes', plan['world'])
        self.assertNotIn('core_reason', plan['world']['events'][0])
        self.assertEqual(len(plan['required_event_ids']), 2)
        self.assertEqual(plan['source_chunks'], requests[1][1]['source_chunks'])
        self.assertNotIn('event_updates', request['messages'][0]['content'])
        self.assertEqual(len(requests[1][1]['world']['events']), 2)
        self.assertNotIn('review_history', requests[1][1])

    def test_duplicate_nodes_repair_inside_time_task_before_check(self):
        class DuplicateOrder(FixtureServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job == 'global.timeline.0' and attempt == 0:
                    for n in result['time']['nodes']: n['order'] = 1
                return result
        server = DuplicateOrder()
        self.assertEqual(self.run_pipeline(server, chunks=2), 0)
        jobs = [(job, attempt) for job, attempt, _ in server.requests if job.startswith('global.')]
        self.assertEqual(jobs, [('global.timeline.0', 0), ('global.timeline.0', 1), ('global.review.0', 0)])
        repair = next(body['messages'][-1]['content'] for job, attempt, body in server.requests
                      if job == 'global.timeline.0' and attempt == 1)
        self.assertIn('冲突为', repair)
        self.assertIn('N1', repair)
        self.assertIn('N2', repair)

    def test_correct_time_needs_no_per_event_confirmation_rows(self):
        class MissingCheck(FixtureServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job == 'global.review.0' and attempt == 0: result['time_checks'] = []
                return result
        server = MissingCheck()
        self.assertEqual(self.run_pipeline(server, chunks=2), 0)
        self.assertEqual([attempt for job, attempt, _ in server.requests if job == 'global.review.0'], [0])
        self.assertEqual(NovelExtractStorage.read_status(self.workspace)['review_state']['rounds'], 2)

    def test_failed_planner_keeps_check_opportunity_and_candidate(self):
        class FailedPlan(FixtureServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job.startswith('global.timeline.'):
                    for n in result['time']['nodes']: n['order'] = 1
                return result
        server = FailedPlan()
        self.assertEqual(self.run_pipeline(server, chunks=2), 0)
        saved = self.snapshots()
        self.assertEqual(saved['job:global.timeline.0']['status'], 'failed')
        self.assertTrue(saved['job:global.timeline.0']['candidate']['time'])
        self.assertEqual(saved['failures'], [])
        self.assertEqual(len([1 for job, _, _ in server.requests if job == 'global.timeline.0']), 2)
        self.assertTrue(NovelExtractStorage.read_status(self.workspace)['review_state']['confirmed_times'])

    def test_rewording_issues_cannot_extend_no_progress(self):
        class RewordedIssues(FixtureServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job.startswith('global.review.'):
                    result['is_valid'] = False
                    result['issues'] = ['同一未解决疑点换一种说法：' + job]
                return result
        self.assertEqual(self.run_pipeline(RewordedIssues(), chunks=1), 2)
        state = NovelExtractStorage.read_status(self.workspace)['review_state']
        self.assertEqual(state['rounds'], 3)
        self.assertTrue(state['confirmed_times'])
        self.assertIn('停止空转', self.snapshots()['failures'][-1]['error'])

    def test_flashback_conflict_is_repaired_and_checked_in_one_review(self):
        # 固定回复只验证错误排序→实际修订的交接，不宣称模型自动理解倒叙。
        self.directory.joinpath('source.txt').write_text('甲先出发，后来到达。小说先写到达，再回忆出发。')
        self.directory.joinpath('config.json').write_text(json.dumps({
            'api_url': 'https://offline.invalid/v1/chat/completions', 'model': 'fixture',
            'api_key_env': 'NOVEL_HARNESS_OFFLINE_KEY', 'timeout_seconds': 1, 'max_retries': 1, 'max_calls': 1000}))
        class Flashback(FixtureServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job == 'chunk.1.first_pass':
                    result['events'] = [event(id='EVT_C1_E1', description='甲到达',
                                              occurrence_signature=occurrence_signature(2),
                                              time_node_id='TN_C1_1', event_order=1).model_dump(mode='json'),
                                        event(id='EVT_C1_E2', description='甲出发',
                                              occurrence_signature=occurrence_signature(1),
                                              time_node_id='TN_C1_2', event_order=1).model_dump(mode='json')]
                    result['time_nodes'] = [TimeNode(id=f'TN_C1_{i}', name=name, order=i).model_dump(mode='json')
                                            for i, name in [(1, '到达'), (2, '出发')]]
                if job.startswith('global.review.'):
                    for n in result['time']['nodes']: n['order'] = 3-n['order']
                    result['correction']['relations'] = [{'source': 'EVT_C1_E2', 'target': 'EVT_C1_E1', 'kind': 'before', 'basis': '先出发后来到达'}]
                    if job == 'global.review.0':
                        result['is_valid'] = True
                        result['issues'] = []
                return result
        server = Flashback()
        result = self.run_pipeline(server, chunks=1)
        self.assertEqual(result, 0, self.snapshots().get('failures'))
        jobs = [job for job, _, _ in server.requests if job.startswith('global.')]
        self.assertEqual(jobs, ['global.timeline.0', 'global.review.0', 'global.review.1'])
        saved = self.snapshots(); nodes = {n['id']: n['order'] for n in saved['world']['time_nodes']}
        positions = {e['description']: nodes[e['time_node_id']] for e in saved['world']['events']}
        self.assertLess(positions['甲出发'], positions['甲到达'])
        self.assertEqual(len(NovelExtractStorage.read_status(self.workspace)['review_state']['confirmed_times']), 2)

    def test_basis_paraphrase_does_not_revoke_unchanged_time(self):
        w, _ = self.timed_world(); state = self.confirmed_state(w)
        changed = w.model_copy(deep=True)
        changed.events[0].temporal_basis = '同一原文依据的另一种措辞'
        review = Review(is_valid=False, summary='解释换词', issues=[], covered_chunks=[1])
        NovelExtractWorkflow().update_time_confirmations(state, review, w, changed)
        self.assertEqual(len(state.confirmed_times), 3)
        changed.events[0].source_time = '与原来不同的开始锚点'
        NovelExtractWorkflow().update_time_confirmations(state, review, w, changed)
        self.assertNotIn(changed.events[0].id, state.confirmed_times)

    def test_time_edges_can_be_repaired_despite_unrelated_bad_state_change(self):
        w, chunks = self.timed_world()
        w.changes = [Change(event_id='MISSING', subject_id='MISSING', change='待查', basis='坏候选')]
        wrong = Relation(source=w.events[1].id, target=w.events[0].id, kind='before', basis='误读')
        w.relations = [wrong]
        plan = TimelinePlan(summary='纠正倒叙关系', time=Timeline(),
            relation_removals=[RelationRemoval(**wrong.model_dump(), reason='原文方向相反')],
            relations=[Relation(source=w.events[0].id, target=w.events[1].id, kind='before', basis='原文')])
        updated = NovelExtractWorkflow.apply_time_plan(plan, w, chunks)
        self.assertEqual(updated.relations[0].source, w.events[0].id)
        self.assertEqual(updated.changes, w.changes)

    def test_formal75_duplicate_reply_gets_specific_repair_feedback(self):
        database = ROOT / '.novel_agent_work/祝福_gpt4o_正式75.sqlite3'
        if not database.exists(): self.skipTest('正式75历史库不在当前环境')
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
            meta = {k: json.loads(v) for k, v in db.execute('select key,value from meta')}
            initial = World.model_validate(json.loads(db.execute("select payload from snapshots where stage='world' order by id limit 1").fetchone()[0]))
            raw = json.loads(db.execute("select payload from snapshots where stage='job:global.review.0' order by id desc limit 1").fetchone()[0])['result']
        review = Review.model_validate(raw)
        plan = TimelinePlan(summary=review.summary, time=review.time, relations=review.correction.relations or [])
        with self.assertRaisesRegex(ValueError, 'TN_C1_1') as caught:
            NovelExtractWorkflow.apply_time_plan(plan, initial, chunk_text(meta['input_text']))
        self.assertIn('TN_C2_1', str(caught.exception))
        self.assertIn('节点order,事件order', str(caught.exception))

    def test_complete_time_delivery_is_checked_once_and_accepted(self):
        server = FixtureServer()
        self.assertEqual(self.run_pipeline(server, chunks=2), 0)
        state = NovelExtractStorage.read_status(self.workspace)['review_state']
        self.assertEqual(state['rounds'], 2)
        self.assertTrue(state['accepted'])
        self.assertEqual([row['task'] for row in state['history']], ['timeline', 'review'])

    def test_timeline_semantic_error_gets_full_context_review_not_compact_retry(self):
        class MissingPlacementOnce(FixtureServer):
            def reply(self, job, attempt, context):
                if job == 'global.timeline.0':
                    ids = context['required_event_ids'][:-1]
                    return TimelinePlan(
                        summary='遗漏一个事件的位置', time=self.timeline(ids)).model_dump(mode='json')
                return super().reply(job, attempt, context)

        server = MissingPlacementOnce()
        self.assertEqual(self.run_pipeline(server, chunks=1), 0)
        self.assertEqual([(job, attempt) for job, attempt, _ in server.requests
                          if job == 'global.timeline.0'], [('global.timeline.0', 0)])
        review_context = server.contexts['global.review.0']
        feedback = review_context['last_delivery']['timeline']['program_feedback']
        self.assertTrue(any('时间修订未接收' in error for error in feedback))
        self.assertTrue(review_context['source_chunks'])
        self.assertIn('world', review_context)

    def test_model_reported_issue_is_preserved_for_full_context_followup(self):
        class IssueOnce(FixtureServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job == 'global.review.0':
                    result['is_valid'] = True
                    result['issues'] = ['人物关系方向仍错误']
                return result

        server = IssueOnce()
        self.assertEqual(self.run_pipeline(server, chunks=1), 0)
        self.assertEqual([(job, attempt) for job, attempt, _ in server.requests
                          if job == 'global.review.0'], [('global.review.0', 0)])
        next_context = server.contexts['global.review.1']
        self.assertIn('人物关系方向仍错误',
                      next_context['last_delivery']['review']['model_issues'])
        self.assertEqual(len(next_context['source_chunks']), 1)
        self.assertIn('information', next_context)
        self.assertEqual(next_context['review_scope']['fact_chunks'], [])

    @unittest.skip("Legacy TimeCheck conflict table retired; specific time errors now use Review.issues and a time patch")
    def test_new_program_feedback_gets_one_full_context_repair_round(self):
        class ConflictAfterConfirmation(FixtureServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job == 'global.review.0':
                    result['is_valid'] = False
                    result['issues'] = ['关系说明仍待核对']
                elif job == 'global.review.1':
                    first, second = context['required_event_ids'][:2]
                    result['is_valid'] = True
                    result['issues'] = []
                    result['time'] = None
                    result['time_checks'] = [{
                        'id': first, 'verdict': 'conflict',
                        'reason': '发现具体顺序冲突', 'comparisons': {second: 'after'},
                    }]
                return result

        server = ConflictAfterConfirmation()
        self.assertEqual(self.run_pipeline(server, chunks=1), 0)
        review_jobs = [job for job, _, _ in server.requests if job.startswith('global.review.')]
        self.assertEqual(review_jobs, ['global.review.0', 'global.review.1', 'global.review.2'])
        feedback = server.contexts['global.review.2']['last_delivery']['review']['program_feedback']
        self.assertTrue(any('判定conflict却没有修正' in error for error in feedback))

    @unittest.skip("Legacy proof-table protocol retired; see reports/novel_extract/shadowloom_validation_reset_20261016/retired_tests.md")
    def test_unchanged_core_chain_confirmation_is_reused(self):
        class CoreChainOnce(FixtureServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job == 'global.review.0':
                    result['information_audit'] = None
                elif job == 'global.review.1':
                    self.assert_no_chain_recheck = context['core_chain_confirmation']['check_now']
                    result['core_chain_checks'] = []
                return result

        server = CoreChainOnce()
        self.assertEqual(self.run_pipeline(server, chunks=1), 0)
        self.assertEqual(server.assert_no_chain_recheck, [])
        state = NovelExtractStorage.read_status(self.workspace)['review_state']
        self.assertEqual(set(state['core_chain_checks']), {'1'})

    @unittest.skip("Legacy proof-table protocol retired; see reports/novel_extract/shadowloom_validation_reset_20261016/retired_tests.md")
    def test_core_chain_confirmations_are_saved_per_chain(self):
        details = information()
        details.story_spine.causal_chains.append([
            StoryLink.model_validate(story_link('甲开始第二段行程', 3)),
            StoryLink.model_validate(story_link('甲完成第二段行程', 4))])
        events = [event(i, id=f'E{i}', time_node_id=f'N{i}', event_order=1)
                  for i in range(1, 5)]
        current = world(events)
        current.time_nodes = [TimeNode(id=f'N{i}', name=f'阶段{i}', order=i)
                              for i in range(1, 5)]
        review = Review(is_valid=False, summary='逐链核对', issues=['第二条链映射错误'],
            covered_chunks=[1], core_chain_checks=[
                CoreChainCheck(chain_index=1,
                    links=[{'event_id': 'E1'}, {'event_id': 'E2'}], reason='第一条链完整'),
                CoreChainCheck(chain_index=2,
                    links=[{'event_id': 'E3'}, {'event_id': 'E1'}], reason='第二条链仍有误'),
            ])
        state = ReviewState()

        NovelExtractWorkflow().update_core_chain_confirmation(
            state, review, details.model_dump(mode='json'), current)

        self.assertEqual(set(state.core_chain_checks), {1})
        self.assertEqual(set(state.core_chain_versions), {1})

    @unittest.skip("Legacy proof-table protocol retired; see reports/novel_extract/shadowloom_validation_reset_20261016/retired_tests.md")
    def test_reconcile_retains_a_valid_chain_while_another_chain_is_unresolved(self):
        class PartlyWrongChains(FixtureServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job == 'foundation.extract':
                    chain = result['information']['story_spine']['causal_chains'][0]
                    result['information']['story_spine']['causal_chains'] = [chain[:2], chain[2:]]
                elif job == 'global.review.0':
                    ids = sorted(context['required_event_ids'])
                    result['is_valid'] = False
                    result['issues'] = ['第二条核心链映射仍有错误']
                    result['core_chain_checks'] = [
                        {'chain_index': 1, 'links': [{'event_id': key} for key in ids[:2]],
                         'missing_links': [], 'reason': '第一条链已核对'},
                        {'chain_index': 2, 'links': [{'event_id': ids[2]}, {'event_id': ids[0]}],
                         'missing_links': [], 'reason': '第二条链仍有错误'},
                    ]
                elif job == 'global.review.1':
                    self.retained_at_second_review = context['core_chain_confirmation']
                    ids = sorted(context['required_event_ids'])
                    result['core_chain_checks'] = [{
                        'chain_index': 2, 'links': [{'event_id': key} for key in ids[2:]],
                        'missing_links': [], 'reason': '第二条链已修正'}]
                return result

        server = PartlyWrongChains()
        self.assertEqual(self.run_pipeline(server, chunks=4), 0)
        self.assertEqual(set(server.retained_at_second_review['retained']), {'1'})
        self.assertEqual(server.retained_at_second_review['check_now'], [2])

    @unittest.skip("Legacy proof-table protocol retired; see reports/novel_extract/shadowloom_validation_reset_20261016/retired_tests.md")
    def test_core_chain_confirmation_expires_when_alias_resolution_changes(self):
        details = information()
        for link in details.story_spine.causal_chains[0]:
            link.signature.actual_subjects = ['甲别称']
        events = [event(i, id=f'E{i}', time_node_id=f'N{i}', event_order=1)
                  for i in (1, 2)]
        current = world(events)
        current.entities['ENT_1'].aliases = ['甲别称']
        current.time_nodes = [TimeNode(id=f'N{i}', name=f'阶段{i}', order=i) for i in (1, 2)]
        row = CoreChainCheck(chain_index=1,
            links=[{'event_id': 'E1'}, {'event_id': 'E2'}], reason='别名解析后对应同一人物')
        state = ReviewState()
        workflow = NovelExtractWorkflow()
        workflow.update_core_chain_confirmation(state,
            Review(is_valid=True, summary='核对', issues=[], covered_chunks=[1],
                   core_chain_checks=[row]), details.model_dump(mode='json'), current)
        self.assertEqual(set(state.core_chain_checks), {1})

        changed = current.model_copy(deep=True)
        changed.entities['ENT_1'].aliases = []
        self.assertTrue(workflow.core_chain_check_errors(
            [row], details.model_dump(mode='json'), changed, require_complete=False))
        self.assertEqual(workflow.retained_core_chain_checks(
            state, details.model_dump(mode='json'), changed), {})

    def test_normal_and_terminal_replay(self):
        server = FixtureServer()
        self.assertEqual(self.run_pipeline(server), 0)
        self.assertGreaterEqual(server.peak, 2)
        count = len(server.requests)
        self.assertEqual(self.run_pipeline(server), 0)
        self.assertEqual(len(server.requests), count)
        timeline = json.loads((self.output / 'timeline/timeline.json').read_text())
        orders = [n['order'] for n in timeline['nodes']]
        self.assertEqual(len(orders), len(set(orders)))

        args = ['--input', str(self.directory / 'source.txt'),
                '--config', str(self.directory / 'config.json'),
                '--workspace', str(self.workspace), '--output', str(self.output),
                '--log-dir', str(self.directory / 'logs'), '--log-terminal', 'quiet',
                '--concurrency', '3']
        with patch.dict('os.environ', {'NOVEL_HARNESS_OFFLINE_KEY': ''}), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(NovelExtractWorkflow().run(args), 0)
        self.assertEqual(len(server.requests), count)

    def test_time_is_inferred_in_chunks_before_global_facts(self):
        server = FixtureServer('late_facts')
        self.assertEqual(self.run_pipeline(server), 0)
        saved = self.snapshots()
        self.assertTrue(saved['chunk.1.topology']['time_nodes'])
        self.assertTrue(saved['chunk.1.topology']['events'][0]['time_node_id'])
        global_requests = [body for job, _, body in server.requests if job.startswith('global.')]
        first = json.loads(global_requests[0]['messages'][-1]['content'])
        self.assertEqual(saved['chunk.1.topology']['time_nodes'][0]['order'], 1)
        self.assertNotIn('time_nodes', first['world'])
        self.assertEqual(first['current_time_groups'], [])
        self.assertTrue(all('time_node_id' not in e for e in first['world']['events']))
        # 局部同为1不当成跨块同时，且不需要事实稳定才整理时间。
        self.assertEqual(len(global_requests), 2)
        self.assertIsNotNone(saved['job:global.timeline.0']['result']['time'])
        check = json.loads(global_requests[1]['messages'][-1]['content'])
        self.assertEqual(len(check['world']['events']), 2)

    def test_fact_and_time_change_in_one_atomic_review(self):
        server = FixtureServer('revise_after_time')
        self.assertEqual(self.run_pipeline(server), 0)
        state = NovelExtractStorage.read_status(self.workspace)['review_state']
        self.assertEqual(state['rounds'], 3)
        self.assertTrue(state['accepted'])
        e = self.snapshots()['world']['events'][0]
        self.assertEqual(e['description'], '甲正式出发')
        self.assertEqual(e['time_node_id'], 'N1')

    def test_oscillation_stops_without_deleting_events(self):
        server = FixtureServer('oscillate')
        self.assertEqual(self.run_pipeline(server), 2)
        state = NovelExtractStorage.read_status(self.workspace)['review_state']
        self.assertLessEqual(state['rounds'], 6)
        self.assertFalse(state['accepted'])
        self.assertEqual(len(self.snapshots()['world']['events']), 2)

    def test_revision_and_time_resume_atomically(self):
        original, fired = NovelExtractStorage.set, []
        def stop(storage, key, value):
            original(storage, key, value)
            if key == 'review_state' and value['rounds'] == 2 and not fired:
                fired.append(True)
                self.assertEqual(storage.latest('world')['events'][0]['description'], '甲正式出发')
                self.assertTrue(storage.latest('world')['time_nodes'])
                raise KeyboardInterrupt()
        server = FixtureServer('revise_after_time')
        self.assertEqual(self.run_pipeline(server, chunks=1, hook=patch.object(NovelExtractStorage, 'set', stop)), 130)
        self.assertEqual(NovelExtractStorage.read_status(self.workspace)['review_state']['rounds'], 1)
        self.assertEqual(self.snapshots()['world']['events'][0]['description'], '甲出发')
        self.assertEqual(self.run_pipeline(server, chunks=1), 0)
        self.assertEqual(len(server.requests), 9)

    def test_failed_chunk_does_not_stop_others(self):
        server = FixtureServer('failed')
        self.assertEqual(self.run_pipeline(server), 2)
        saved = self.snapshots()
        self.assertEqual(saved['job:chunk.2.consequences']['status'], 'complete')
        self.assertEqual({f['job'] for f in saved['failures'] if f['job'].startswith('chunk.')},
                         {'chunk.1.first_pass', 'chunk.1.second_pass', 'chunk.1.consequences'})
        self.assertTrue(Path(str(self.output) + '.incomplete.json').is_file())

    def test_rescue_requires_full_handoff(self):
        server = FixtureServer('rescued')
        self.assertEqual(self.run_pipeline(server), 0)
        self.assertEqual(self.snapshots()['failures'], [])
        self.assertEqual(len(self.snapshots()['world']['events']), 2)

    def test_format_repairs_bound_history(self):
        server = FixtureServer('format')
        self.assertEqual(self.run_pipeline(server), 0)
        for _, _, body in server.requests:
            self.assertLessEqual(len(body['messages']), 2)
        self.assertTrue(any(attempt == 1 for _, attempt, _ in server.requests))

    def test_partial_time_with_incomplete_broad_review_keeps_orders_unconfirmed(self):
        server = FixtureServer('bad_time')
        self.assertEqual(self.run_pipeline(server), 2)
        timeline = json.loads((self.output / 'timeline/timeline.json').read_text())
        self.assertEqual(len(timeline['nodes']), 2)
        self.assertEqual([n['order'] for n in timeline['nodes']], [None, None])

    def test_unknown_and_simultaneous_are_different(self):
        # 真正未知的开始顺序保留 partial，不用“同时”伪造完整时间轴。
        self.assertEqual(self.run_pipeline(FixtureServer('unknown')), 2)
        self.assertEqual(len(json.loads((self.output / 'timeline/timeline.json').read_text())['nodes']), 2)
        state = NovelExtractStorage.read_status(self.workspace)['review_state']
        self.assertFalse(state['time_reviewed'])

    def test_simultaneous_groups_confirmed_starts(self):
        self.assertEqual(self.run_pipeline(FixtureServer('simultaneous')), 0)
        self.assertEqual(len(json.loads((self.output / 'timeline/timeline.json').read_text())['nodes']), 1)

    def test_two_levels_preserved_in_repository(self):
        server = FixtureServer('hierarchy')
        self.assertEqual(self.run_pipeline(server, chunks=4), 0)
        self.assertEqual(server.peak, 3)
        nodes = json.loads((self.output / 'timeline/timeline.json').read_text())['nodes']
        self.assertEqual([n['order'] for n in nodes], [1, 2])
        tasks = [json.loads((self.output / 'timeline' / n['path']).read_text())['todolist'] for n in nodes]
        self.assertEqual([[e['order'] for e in rows] for rows in tasks], [[1, 2, 2], [1]])

    def test_request_budget_is_global(self):
        server = FixtureServer()
        self.assertEqual(self.run_pipeline(server, max_calls=9), 75)
        self.assertEqual(len(server.requests), 9)
        self.assertEqual(NovelExtractStorage.read_status(self.workspace)['status'], 'working')

    def test_missing_key_does_not_create_a_false_partial_workspace(self):
        source = self.directory / 'missing-key.txt'
        source.write_text('甲出发。')
        config = self.directory / 'missing-key-config.json'
        config.write_text(json.dumps({
            'api_url': 'https://offline.invalid/v1/chat/completions', 'model': 'fixture',
            'api_key_env': 'NOVEL_HARNESS_MISSING_KEY', 'timeout_seconds': 1,
            'max_retries': 0, 'max_calls': 10,
        }))
        workspace = self.directory / 'missing-key.sqlite3'
        args = ['--input', str(source), '--config', str(config),
                '--workspace', str(workspace), '--output', str(self.directory / 'missing-output'),
                '--log-dir', str(self.directory / 'missing-logs')]

        with patch.dict('os.environ', {'NOVEL_HARNESS_MISSING_KEY': ''}), redirect_stdout(io.StringIO()):
            self.assertEqual(NovelExtractWorkflow().run(args), 75)

        self.assertFalse(workspace.exists())

    def test_http_retry(self):
        server = FixtureServer('retry')
        self.assertEqual(self.run_pipeline(server), 0)
        self.assertEqual(len([r for r in server.requests if r[0] == 'foundation.extract']), 2)

    def test_interrupt_after_response_replays_without_extra_request(self):
        original, fired = NovelExtractStorage.received, []
        def stop(storage, *args):
            original(storage, *args)
            if not fired: fired.append(True); raise KeyboardInterrupt()
        server = FixtureServer()
        self.assertEqual(self.run_pipeline(server, chunks=1, hook=patch.object(NovelExtractStorage, 'received', stop)), 130)
        self.assertEqual(self.run_pipeline(server, chunks=1), 0)
        self.assertEqual(len(server.requests), 8)

    def test_response_lost_before_persistence_keeps_unknown(self):
        original, fired = NovelExtractStorage.received, []
        def stop(storage, *args):
            if not fired: fired.append(True); raise KeyboardInterrupt()
            original(storage, *args)
        server = FixtureServer()
        self.assertEqual(self.run_pipeline(server, chunks=1, hook=patch.object(NovelExtractStorage, 'received', stop)), 130)
        self.assertEqual(self.run_pipeline(server, chunks=1), 0)
        self.assertEqual(len(server.requests), 9)
        self.assertEqual(NovelExtractStorage.read_status(self.workspace)['unknown_requests'], 1)

    def test_interrupt_after_agent_state(self):
        original, fired = NovelExtractStorage.snapshot, []
        def stop(storage, stage, payload):
            original(storage, stage, payload)
            if stage == 'job:foundation.extract' and payload.get('status') == 'received' and not fired:
                fired.append(True); raise KeyboardInterrupt()
        server = FixtureServer()
        self.assertEqual(self.run_pipeline(server, chunks=1, hook=patch.object(NovelExtractStorage, 'snapshot', stop)), 130)
        self.assertEqual(self.run_pipeline(server, chunks=1), 0)
        self.assertEqual(len(server.requests), 8)

    def test_interrupt_inside_global_transaction(self):
        original, fired = NovelExtractStorage.set, []
        def stop(storage, key, value):
            original(storage, key, value)
            if key == 'review_state' and not fired: fired.append(True); raise KeyboardInterrupt()
        server = FixtureServer()
        self.assertEqual(self.run_pipeline(server, chunks=1, hook=patch.object(NovelExtractStorage, 'set', stop)), 130)
        self.assertEqual(self.run_pipeline(server, chunks=1), 0)
        self.assertEqual(len(server.requests), 8)

    def test_interrupt_before_publish(self):
        server = FixtureServer()
        with patch.object(NovelExtractExporter, 'publish', side_effect=KeyboardInterrupt()):
            self.assertEqual(self.run_pipeline(server, chunks=1), 130)
        count = len(server.requests)
        self.assertEqual(self.run_pipeline(server, chunks=1), 0)
        self.assertEqual(len(server.requests), count)

    def test_tampered_output_is_not_overwritten(self):
        server = FixtureServer()
        self.assertEqual(self.run_pipeline(server), 0)
        file = self.output / 'timeline/timeline.json'
        file.write_text('user changed')
        count = len(server.requests)
        self.assertEqual(self.run_pipeline(server), 1)
        self.assertEqual(file.read_text(), 'user changed')
        self.assertEqual(len(server.requests), count)

    def test_passage_references_preserve_discontinuous_evidence(self):
        chunks = chunk_text('甲出发。\n\n乙问话。\n\n甲到达。')
        row = event(evidence=[Evidence(chunk=1, passage_ids=['C1_P1', 'C1_P3'])])
        normalize_evidence([row], chunks)
        self.assertEqual([q.quote for q in row.evidence], ['甲出发。', '甲到达。'])
        self.assertEqual(evidence_errors([row], chunks), [])
        invalid = event(evidence=[Evidence(chunk=1, quote='甲出发。甲到达。')])
        self.assertTrue(evidence_errors([invalid], chunks))

    def test_unknown_passage_and_word_change_rejected(self):
        chunks = chunk_text('甲出发。')
        with self.assertRaises(ValueError): normalize_evidence([event(evidence=[Evidence(chunk=1, passage_ids=['C1_P9'])])], chunks)
        row = event(evidence=[Evidence(chunk=1, quote='乙出发。')])
        normalize_evidence([row], chunks)
        self.assertTrue(evidence_errors([row], chunks))

    def test_incremental_edges_never_clear_old_records(self):
        old = world([event(), event(2)])
        old.relations = [Relation(source='EVT_C1_E1', target='EVT_C2_E1', kind='before', basis='先后')]
        old.changes = [Change(event_id='EVT_C1_E1', subject_id='ENT_1', change='离家', basis='原文')]
        review = Review(is_valid=False, summary='增量', issues=['待查'], covered_chunks=[1, 2],
                        correction=Correction(relations=[], changes=[]))
        revised = apply_correction(old, review)
        self.assertEqual(revised.relations, old.relations)
        self.assertEqual(revised.changes, old.changes)
        review.correction.relation_removals = [RelationRemoval(**old.relations[0].model_dump(), reason='原文纠错')]
        review.correction.change_removals = [ChangeRemoval(**old.changes[0].model_dump(), reason='误录')]
        revised = apply_correction(old, review)
        self.assertFalse(revised.relations)
        self.assertFalse(revised.changes)
        self.assertEqual(len(old.relations), 1)

    def test_time_constraints(self):
        chunks = chunk_text('甲出发。')
        a = event(); b = event(id='EVT_C1_E2')
        normalize_evidence([a, b], chunks)
        old = world([a, b])
        old.relations = [Relation(source=a.id, target=b.id, kind='before', basis='先后')]
        reply = Timeline(is_valid=True, summary='时间', issues=[], timeline=[
            TimePlacement(id=a.id, node_id='N1', order=1, basis='依据'),
            TimePlacement(id=b.id, node_id='N1', order=1, basis='依据')],
            nodes=[TimeNode(id='N1', name='出发阶段', order=1)])
        with self.assertRaisesRegex(ValueError, '矛盾'): apply_timeline(old, reply, chunks)
        reply.timeline.pop()
        partial = apply_timeline(old, reply, chunks)
        self.assertIsNone(partial.events[1].time_node_id)
        self.assertTrue(timeline_errors(partial))
        old.relations.append(Relation(source=b.id, target=a.id, kind='before', basis='错误'))
        self.assertTrue(any('循环' in e for e in topology_errors(old, old, chunks, check_time=True)))

    def test_node_orders_are_unique_but_event_orders_are_local(self):
        chunks = chunk_text('甲出发。')
        a, b = event(), event(id='EVT_C1_E2')
        normalize_evidence([a, b], chunks)
        old = world([a, b])
        old.relations = [Relation(source=a.id, target=b.id, kind='before', basis='前后阶段')]
        reply = Timeline(is_valid=True, summary='两阶段', issues=[], nodes=[
            TimeNode(id='N1', name='早期', order=1), TimeNode(id='N2', name='后来', order=2)], timeline=[
            TimePlacement(id=a.id, node_id='N1', order=1, basis='早期唯一一事'),
            TimePlacement(id=b.id, node_id='N2', order=1, basis='后来第一事')])
        apply_timeline(old, reply, chunks)
        reply.nodes[1].order = 1
        with self.assertRaisesRegex(ValueError, '不得重号'): apply_timeline(old, reply, chunks)
        reply.nodes[1].order = 2
        old.relations[0].kind = 'simultaneous'
        with self.assertRaisesRegex(ValueError, '不同时间节点'): apply_timeline(old, reply, chunks)
        with self.assertRaises(ValueError): TimeNode(id='N1', name='阶段', order=True)
        with self.assertRaises(ValueError): TimePlacement(id=a.id, node_id='N1', order=0, basis='不能从零开始')

    @unittest.skip("Legacy proof-table protocol retired; see reports/novel_extract/shadowloom_validation_reset_20261016/retired_tests.md")
    def test_candidate_disposition_and_failed_step_scope(self):
        chunks = chunk_text('甲出发。')
        storage = NovelExtractStorage(self.directory / 'check.sqlite3')
        self.addCleanup(storage.close)
        workflow = NovelExtractWorkflow(); workflow.storage = storage
        workflow.failures = [{'job': 'chunk.1.first_pass'}, {'job': 'chunk.1.second_pass'}]
        storage.snapshot('information', information().model_dump(mode='json'))
        storage.snapshot('job:chunk.1.reading', {'result': {'core_candidates': [
            CoreCandidate(description='甲出发', evidence=event().evidence,
                          signature=occurrence_signature(),
                          core_reason='这是后续行程成立的直接起点').model_dump(mode='json')]}})
        e = event(); normalize_evidence([e], chunks)
        review = Review(is_valid=True, issues=[], summary='完成', covered_chunks=[1], resolved_jobs=['chunk.1.first_pass'])
        workflow.evaluate_review(review, world([e]), chunks)
        self.assertFalse(review.is_valid)
        self.assertTrue(any('尚未补齐' in s for s in review.issues))
        review = Review(is_valid=True, issues=[], summary='完成', covered_chunks=[1], resolved_jobs=['chunk.1'])
        workflow.evaluate_review(review, world([e]), chunks)
        self.assertTrue(any('candidate_checks' in s for s in review.issues))
        review.is_valid, review.issues, review.resolved_jobs = True, [], ['chunk.1']
        second = event(id='EVT_C1_E2', description='甲实际出发',
                       occurrence_signature=occurrence_signature(2))
        normalize_evidence([second], chunks)
        review.candidate_checks = [CandidateCheck(
            candidate_id='C1_K1', event_ids=[e.id], reason='收录',
            disposition='mapped', signature=occurrence_signature())]
        review.core_chain_checks = [CoreChainCheck(
            chain_index=1, links=[
                {'link': '甲决定出发', 'event_id': e.id,
                 'signature': occurrence_signature(1),
                 'reason': '该事件对应决定出发'},
                {'link': '甲实际出发', 'event_id': second.id,
                 'signature': occurrence_signature(2),
                 'reason': '该事件对应实际出发'},
            ], missing_links=[], reason='两条事件依次覆盖决定与实际出发')]
        review.time = Timeline(nodes=[
                TimeNode(id='N1', name='决定出发', order=1),
                TimeNode(id='N2', name='实际出发', order=2),
            ], timeline=[
                TimePlacement(id=e.id, node_id='N1', order=1, basis='原文'),
                TimePlacement(id=second.id, node_id='N2', order=1, basis='原文'),
            ])
        workflow.evaluate_review(review, world([e, second]), chunks)
        self.assertEqual(review.resolved_jobs, ['chunk.1.first_pass', 'chunk.1.second_pass'])

    def test_partial_keeps_model_groups_without_confirming_orders(self):
        storage = NovelExtractStorage(self.directory / 'partial.sqlite3')
        self.addCleanup(storage.close)
        storage.snapshot('information', information().model_dump(mode='json'))
        storage.set('source', '离线')
        e = event(time_node_id='N1', event_order=1)
        other = event(id='EVT_C1_E2', time_node_id='N1', event_order=1)
        w = world([e, other]); w.time_nodes = [TimeNode(id='N1', name='未确认分组', order=1)]
        files, excluded = NovelExtractExporter(storage).project(w)
        self.assertFalse(excluded)
        self.assertEqual(len(files['timeline/timeline.json']['nodes']), 1)
        self.assertTrue(all(n['order'] is None for n in files['timeline/timeline.json']['nodes']))
        node = files['timeline/timeline.json']['nodes'][0]
        self.assertEqual(node['node_id'], 'N1')
        tasks = files['timeline/' + node['path']]['todolist']
        self.assertEqual(len(tasks), 2)
        self.assertTrue(all(row['order'] is None for row in tasks))

    def test_old_format_read_only(self):
        path = self.directory / 'old.sqlite3'
        with closing(sqlite3.connect(path)) as db:
            db.execute('create table meta(key text,value text)')
            db.execute('insert into meta values(?,?)', ('format', '28'))
            db.commit()
        self.assertEqual(NovelExtractStorage.read_status(path)['format'], 28)
        with self.assertRaisesRegex(ValueError, '历史工作区'): NovelExtractStorage(path)

    def test_workspace_lock(self):
        path = self.directory / 'lock.sqlite3'
        storage = NovelExtractStorage(path)
        try:
            storage.set('format', NovelExtractStorage.FORMAT_VERSION)
            with self.assertRaises(BlockingIOError): NovelExtractStorage(path)
        finally: storage.close()

    def test_quote_conflicts_keep_source_and_require_verification(self):
        chunks = chunk_text('甲出发。\n\n乙到达。')
        matching = event(evidence=[Evidence(chunk=1, passage_ids=['C1_P1'], quote='甲出发。')])
        normalize_evidence([matching], chunks)
        self.assertEqual(matching.evidence, [Evidence(chunk=1, quote='甲出发。')])
        for quote in ('乙到达。', '甲出发。乙到达。', '甲没有出发。'):
            with self.subTest(quote=quote):
                row = event(evidence=[Evidence(chunk=1, passage_ids=['C1_P1'], quote=quote)])
                normalize_evidence([row], chunks)
                self.assertEqual(row.evidence[0].quote, '甲出发。')
                self.assertEqual(row.evidence[0].unverified_quote, quote)

    def test_exact_names_resolve_without_guessing_ambiguous_roles(self):
        w = world([event(actor_ids=['甲'])])
        info = information()
        info.characters = {'甲': info.characters['ENT_1']}
        normalize_references(w, w)
        self.assertEqual(w.events[0].actor_ids, ['ENT_1'])
        self.assertEqual(information_errors(info, w), [])
        w.entities['ENT_2'] = Entity(name='乙', aliases=['甲'])
        w.events[0].actor_ids = ['甲']
        normalize_references(w, w)
        self.assertEqual(w.events[0].actor_ids, ['甲'])
        self.assertTrue(any('未知人物' in e for e in topology_errors(w, w, chunk_text('甲出发。'))))

    def test_partial_event_patch_preserves_fields_and_unrelated_time(self):
        chunks = chunk_text('甲出发。')
        a, b = event(time_node_id='N1', event_order=1), event(id='EVT_C1_E2', time_node_id='N1', event_order=2)
        normalize_evidence([a, b], chunks)
        old = world([a, b]); old.time_nodes = [TimeNode(id='N1', name='旅程', order=1)]
        review = Review(is_valid=False, summary='修订', issues=['待确认'], covered_chunks=[1])
        self.assertEqual(apply_correction(old, review, chunks), old)
        review.correction.event_updates = {a.id: {'description': '甲实际出发'}}
        updated = apply_correction(old, review, chunks)
        self.assertEqual(updated.events[0].actor_ids, a.actor_ids)
        self.assertEqual(updated.events[0].evidence, a.evidence)
        self.assertIsNone(updated.events[0].time_node_id)
        self.assertEqual(updated.events[1], b)
        self.assertEqual(updated.time_nodes, old.time_nodes)
        self.assertEqual(old.events[0].description, '甲出发')
        review.correction.event_updates = {a.id: {'evidence': [{'chunk': 1, 'passage_ids': ['C1_P1']}]}}
        self.assertEqual(apply_correction(old, review, chunks), old)
        review.correction.event_updates = {a.id: {'invented_field': 'oops'}}
        with self.assertRaisesRegex(ValueError, '非法字段'): apply_correction(old, review, chunks)

    def test_chunk_can_add_missing_people_without_substituting_existing(self):
        class MissingPerson(FixtureServer):
            def reply(self, job, attempt, context):
                reply = super().reply(job, attempt, context)
                if job == 'chunk.1.first_pass':
                    reply['entities'] = {'ENT_C1_2': {'name': '乙', 'description': '原文中缺漏的人物'}}
                    reply['events'][0]['target_ids'] = ['ENT_C1_2']
                return reply
        server = MissingPerson()
        self.assertEqual(self.run_pipeline(server), 0)
        w = self.snapshots()['world']
        self.assertEqual(w['entities']['ENT_1']['name'], '甲')
        self.assertEqual(w['entities']['ENT_C1_2']['name'], '乙')
        self.assertEqual(w['events'][0]['target_ids'], ['ENT_C1_2'])
        self.assertTrue((self.output / 'roles/ENT_C1_2/role_info.json').exists())

    def test_time_and_reversed_relation_can_be_repaired_together(self):
        chunks = chunk_text('甲出发。')
        a, b = event(), event(id='EVT_C1_E2')
        normalize_evidence([a, b], chunks)
        w = world([a, b])
        wrong = Relation(source=b.id, target=a.id, kind='before', basis='误读倒叙')
        w.relations = [wrong]
        placement = Timeline(nodes=[TimeNode(id='N1', name='行程', order=1)], timeline=[
            TimePlacement(id=a.id, node_id='N1', order=1, basis='先出发'),
            TimePlacement(id=b.id, node_id='N1', order=2, basis='后到达')])
        with self.assertRaisesRegex(ValueError, '矛盾'): apply_timeline(w, placement, chunks)
        review = Review(is_valid=False, summary='纠正倒叙误读', issues=['待独立确认'], covered_chunks=[1],
            time=placement, correction=Correction(
                relation_removals=[RelationRemoval(**wrong.model_dump(), reason='方向反了')],
                relations=[Relation(source=a.id, target=b.id, kind='before', basis='原文先后')]))
        corrected = apply_timeline(apply_correction(w, review, chunks), review.time, chunks)
        self.assertEqual(topology_errors(corrected, corrected, chunks, check_time=True), [])
        self.assertEqual(w.relations, [wrong])

    def test_explicit_merge_remaps_graph_and_information_without_source_loss(self):
        a, b = event(), event(id='EVT_C1_E2', evidence=[Evidence(chunk=1, quote='甲到了。')])
        w = world([a, b]); w.entities['ENT_2'] = Entity(name='甲的别称')
        review = Review(is_valid=False, issues=['核实'], summary='同一事件', covered_chunks=[1],
            correction=Correction(merges=[EventMerge(source=b.id, target=a.id, reason='同次')],
                event_updates={a.id: {'description': '甲出发后到达'}},
                entities={'ENT_1': Entity(name='甲', aliases=['甲的别称'], description='')},
                entity_merges=[EventMerge(source='ENT_2', target='ENT_1', reason='同人')]),
            information={'characters': {'ENT_1': {
                'activity': '主角', 'nickname': None, 'profile': None, 'gender': None,
                'identity': None, 'age': None, 'personality': None, 'biography': '合并后的档案'}}})
        before = review.model_dump()
        corrected = apply_correction(w, review)
        self.assertEqual(len(corrected.events), 1)
        self.assertEqual(len(corrected.events[0].evidence), 2)
        self.assertEqual(review.model_dump(), before)
        info = information().model_dump()
        info['characters']['ENT_2'] = {'activity': 'npc'}
        info['relations'] = [{'owner': 'ENT_1', 'other': 'ENT_2', 'relation': ['同人'], 'detail': None}]
        details = apply_information(info, review, corrected)
        self.assertEqual(set(details.characters), {'ENT_1'})
        self.assertEqual(details.relations, [])
        self.assertIn('ENT_2', info['characters'])

    def test_already_resolved_jobs_are_idempotent(self):
        class RepeatResolution(FixtureServer):
            def reply(self, job, attempt, context):
                reply = super().reply(job, attempt, context)
                if job.startswith('global.'):
                    reply['resolved_jobs'] = ['chunk.1.second_pass', 'chunk.1.first_pass', 'chunk.1.consequences']
                return reply
        self.assertEqual(self.run_pipeline(RepeatResolution('rescued')), 0)
        self.assertEqual(self.snapshots()['failures'], [])

    def test_evidence_context_compaction_is_lossless(self):
        chunks = chunk_text('甲出发。\n\n乙到达。')
        value = {'source_chunks': [c.context() for c in chunks],
                 'records': [event(evidence=[Evidence(chunk=1, quote='甲出发。')]).model_dump(mode='json')]}
        compact = compact_evidence(value, chunks)
        self.assertEqual(compact['source_chunks'], value['source_chunks'])
        self.assertEqual(value['records'][0]['evidence'][0]['quote'], '甲出发。')
        restored = [Event.model_validate(row) for row in compact['records']]
        normalize_evidence(restored, chunks)
        self.assertEqual(restored[0].model_dump(mode='json'), value['records'][0])
        repeated = chunk_text('甲出发。\n\n甲出发。')
        self.assertEqual(compact_evidence(value['records'], repeated), value['records'])

    def test_empty_optional_collections_normalize_without_dropping_data(self):
        patch = Correction(entities=None, event_updates=[], relations=[], removals={})
        self.assertEqual(patch.entities, {})
        self.assertEqual(patch.event_updates, {})
        self.assertEqual(patch.removals, [])
        with self.assertRaises(ValueError): Correction(event_updates=['非空错误形状'])
        self.assertEqual(Timeline(nodes=None, timeline=[]).nodes, [])

    def test_archived_formal73_six_evidence_replies(self):
        path = ROOT / '.novel_agent_work/祝福_gpt4o_正式73.sqlite3'
        if not path.exists(): self.skipTest('本机历史实验不在此副本')
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
            text = json.loads(db.execute("select value from meta where key='input_text'").fetchone()[0])
            rows = [json.loads(row[0]) for row in db.execute(
                "select payload from snapshots where stage='job:chunk.5.second_pass' order by id")]
        chunks = chunk_text(text); count = 0
        for row in rows:
            if row['status'] != 'received': continue
            candidate = EventPass.model_validate_json(row['response'])
            normalize_evidence(candidate.events, chunks)
            self.assertEqual(evidence_errors(candidate.events, chunks), [])
            count += 1
        self.assertEqual(count, 6)

    def test_archived_formal73_name_instead_of_id(self):
        path = ROOT / '.novel_agent_work/祝福_gpt4o_正式73.sqlite3'
        if not path.exists(): self.skipTest('本机历史实验不在此副本')
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
            register = World.model_validate(json.loads(db.execute(
                "select payload from snapshots where stage='register' order by id desc limit 1").fetchone()[0]))
            for job in ('ontology.entities_check', 'information'):
                received = next(json.loads(row[0]) for row in db.execute(
                    'select payload from snapshots where stage=? order by id', ('job:' + job,))
                    if json.loads(row[0])['status'] == 'received')
                if job == 'ontology.entities_check':
                    reply = Entities.model_validate_json(received['response'])
                    reply.entities = canonical_keys(reply.entities, register.entities)
                    self.assertEqual(register_errors(register.model_copy(update={'entities': reply.entities})), [])
                else:
                    reply = RepositoryInformation.model_validate(
                        format36_information(json.loads(received['response'])))
                    self.assertEqual(information_errors(reply, register), [])

    def test_archived_formal72_edge_loss(self):
        path = ROOT / '.novel_agent_work/祝福_gpt4o_正式72.sqlite3'
        if not path.exists(): self.skipTest('本机历史实验不在此副本')
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
            assembled = World.model_validate(json.loads(db.execute(
                "select payload from snapshots where stage='world' order by id limit 1").fetchone()[0]))
            review = Review.model_validate(json.loads(db.execute(
                "select payload from snapshots where stage='validation.facts' order by id limit 1").fetchone()[0]))
        updated = apply_correction(assembled, review)
        self.assertEqual(len(assembled.relations), 12)
        self.assertEqual(len(updated.relations), 14)
        self.assertEqual(len(updated.changes), len(assembled.changes))

    def test_unverified_source_cannot_complete_without_explicit_repair(self):
        class SourceConflict(FixtureServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job == 'chunk.1.first_pass':
                    result['events'][0]['evidence'][0]['quote'] = '甲没有出发。'
                return result
        self.assertEqual(self.run_pipeline(SourceConflict(), chunks=1), 2)
        saved = self.snapshots()
        self.assertEqual(saved['job:chunk.1.first_pass']['status'], 'complete')
        self.assertTrue(saved['world']['events'][0]['evidence'][0]['unverified_quote'])
        self.assertTrue(any('复抄引文' in s for s in saved['validation.review']['issues']))
        self.assertFalse(NovelExtractStorage.read_status(self.workspace)['review_state']['accepted'])

    def test_source_repair_preserves_group_and_is_rechecked_in_same_review(self):
        class SourceRepair(FixtureServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job == 'chunk.1.first_pass':
                    result['events'][0]['evidence'][0]['quote'] = '甲没有出发。'
                if job == 'global.review.0':
                    result['correction']['event_updates'] = {'EVT_C1_E1': {
                        'evidence': [{'chunk': 1, 'passage_ids': ['C1_P1']}]}}
                    result['time_checks'] = []
                return result
        server = SourceRepair()
        self.assertEqual(self.run_pipeline(server, chunks=1), 0)
        self.assertEqual(len([job for job, _, _ in server.requests if job.startswith('global.')]), 3)
        self.assertFalse(self.snapshots()['world']['events'][0]['evidence'][0]['unverified_quote'])
        properties = EventPass.model_json_schema()['$defs']['Evidence']['properties']
        self.assertNotIn('quote', properties)
        self.assertNotIn('unverified_quote', properties)
        self.assertEqual(properties['passage_ids']['minItems'], 1)
        self.assertIn('passage_ids', EventPass.model_json_schema()['$defs']['Evidence']['required'])

    @unittest.skip("Legacy proof-table protocol retired; see reports/novel_extract/shadowloom_validation_reset_20261016/retired_tests.md")
    def test_explicit_pending_patch_recovers_only_selected_candidate_and_dependencies(self):
        class PendingRepair(FixtureServer):
            def reply(self, job, attempt, context):
                if job == 'chunk.1.reading':
                    candidate = CoreCandidate(
                        description='乙出发', evidence=event().evidence,
                        signature={**occurrence_signature(), 'actual_subjects': ['乙']},
                        core_reason='这是后续行程成立的直接起点')
                    return Reading(who='乙', what='出发', where='未知', when='未知', why='未知', how='未知',
                                   core_candidates=[candidate]).model_dump(mode='json')
                if job == 'chunk.1.first_pass':
                    broken = event(actor_ids=['ENT_C1_2'], description='乙出发', time_node_id='TN_C1_1', event_order=1,
                                   evidence=[Evidence(chunk=1, passage_ids=['C1_P999'])])
                    return EventPass(entities={'ENT_C1_2': Entity(name='乙')}, events=[broken,
                        event(id='EVT_C1_UNSELECTED', time_node_id='TN_C1_1', event_order=2)],
                        time_nodes=[TimeNode(id='TN_C1_1', name='旅程', order=1)], coverage='待修').model_dump(mode='json')
                result = super().reply(job, attempt, context)
                if job == 'global.review.0':
                    self.pending = context['pending_candidates']
                    result['correction']['events'] = [event(
                        id='EVT_C1_CHAIN2', actor_ids=['ENT_C1_2'],
                        description='乙继续行程', time_node_id=None,
                        event_order=None, core_reason='乙出发后继续行程，完成主干链的后续环节',
                        occurrence_signature={**occurrence_signature(2), 'actual_subjects': ['ENT_C1_2']},
                    ).model_dump(mode='json')]
                    result['correction']['event_updates'] = {'EVT_C1_E1': {
                        'evidence': [{'chunk': 1, 'passage_ids': ['C1_P1']}]}}
                    result['core_chain_checks'][0]['links'] = [{
                        'link': '甲决定出发', 'event_id': 'EVT_C1_E1',
                        'reason': '乙实际出发是主干起点',
                    }, {
                        'link': '甲实际出发', 'event_id': 'EVT_C1_CHAIN2',
                        'reason': '乙继续行程是后续主干环节',
                    }]
                    result['information'] = {
                        'relations': [{
                            'owner': 'ENT_1', 'other': 'ENT_C1_2', 'relation': ['同行程人物'],
                            'detail': '甲与乙同属原文行程中的相关人物',
                        }],
                        'story_spine': {
                            'endings': ['乙继续行程'], 'turning_points': ['乙实际出发'],
                            'causal_chains': [[
                                {**story_link('甲决定出发', 1),
                                 'signature': {**occurrence_signature(1), 'actual_subjects': ['乙']}},
                                {**story_link('甲实际出发', 2),
                                 'signature': {**occurrence_signature(2), 'actual_subjects': ['乙']}},
                            ]],
                        },
                    }
                    result['information_audit'] = None
                    result['time_checks'] = []
                    result['is_valid'] = False
                    result['issues'] = ['新增主干链后续环节，待下轮独立复核']
                elif job.startswith('global.review.'):
                    result['time'] = self.timeline(['EVT_C1_E1', 'EVT_C1_CHAIN2']).model_dump(mode='json')
                    result['core_chain_checks'][0]['links'] = [{
                        'link': '甲决定出发', 'event_id': 'EVT_C1_E1',
                        'reason': '乙实际出发是主干起点',
                    }, {
                        'link': '甲实际出发', 'event_id': 'EVT_C1_CHAIN2',
                        'reason': '乙继续行程是后续主干环节',
                    }]
                return result
        server = PendingRepair('rescued')
        result = self.run_pipeline(server, chunks=1)
        self.assertEqual(result, 0, self.snapshots().get('failures'))
        saved = self.snapshots()
        self.assertEqual(len(saved['world']['events']), 2)
        by_id = {row['id']: row for row in saved['world']['events']}
        self.assertEqual(by_id['EVT_C1_E1']['actor_ids'], ['ENT_C1_2'])
        self.assertIn('EVT_C1_CHAIN2', by_id)
        self.assertNotIn('EVT_C1_UNSELECTED', by_id)
        self.assertEqual(saved['world']['entities']['ENT_C1_2']['name'], '乙')
        self.assertTrue((self.output / 'roles/ENT_C1_2/role_info.json').exists())
        self.assertEqual(saved['failures'], [])
        self.assertEqual(len(server.pending['events']), 2)
        self.assertTrue(any(job.startswith('information.supplement.') for job, _, _ in server.requests))

    def test_invalid_fact_patch_does_not_discard_valid_time(self):
        class InvalidFact(FixtureServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job == 'global.review.0':
                    result['correction']['event_updates'] = {'EVT_UNKNOWN': {'description': '未知对象'}}
                if job == 'global.review.1':
                    self.saved_context = context
                return result
        server = InvalidFact()
        self.assertEqual(self.run_pipeline(server, chunks=1), 0)
        self.assertEqual(server.saved_context['world']['events'][0]['time_node_id'], 'N1')
        self.assertTrue(any('事实修订未接收' in s for s in server.saved_context['last_delivery']['review']['program_feedback']))

    def test_invalid_time_does_not_discard_valid_fact_patch(self):
        class InvalidTime(FixtureServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job == 'global.review.0':
                    result['correction']['event_updates'] = {'EVT_C1_E1': {'description': '甲正式出发'}}
                    result['time']['timeline'][0]['node_id'] = 'MISSING_NODE'
                if job == 'global.review.1': self.saved_context = context
                return result
        server = InvalidTime()
        self.assertEqual(self.run_pipeline(server, chunks=1), 0)
        first = server.saved_context['world']['events'][0]
        self.assertEqual(first['description'], '甲正式出发')
        # 修订事实后旧位置失效，独立时间任务先重建，再交下一轮检查。
        self.assertEqual(first['time_node_id'], 'N1')
        self.assertTrue(any('时间修订未接收' in s for s in server.saved_context['last_delivery']['review']['program_feedback']))
        self.assertEqual(NovelExtractStorage.read_status(self.workspace)['review_state']['rounds'], 4)

    def test_incremental_global_time_requires_all_events_and_keeps_old_positions(self):
        class IncrementalTime(FixtureServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job == 'global.timeline.0':
                    result['time']['nodes'] = result['time']['nodes'][:1]
                    result['time']['timeline'] = result['time']['timeline'][:1]
                elif job == 'global.review.0':
                    self.before = context
                    result['time']['nodes'] = result['time']['nodes'][1:]
                    result['time']['timeline'] = result['time']['timeline'][1:]
                elif job.startswith('global.'):
                    result['time'] = None
                return result
        server = IncrementalTime()
        self.assertEqual(self.run_pipeline(server, chunks=3), 0)
        self.assertEqual(server.before['required_event_ids'],
                         ['EVT_C1_E1', 'EVT_C2_E1', 'EVT_C3_E1'])
        self.assertFalse(any(f['job'] == 'global.timeline.0' for f in server.before['failed_jobs']))
        state = NovelExtractStorage.read_status(self.workspace)['review_state']
        self.assertEqual(state['rounds'], 3)
        self.assertEqual(len(state['time_placements']), 3)
        self.assertEqual([e['time_node_id'] for e in self.snapshots()['world']['events']], ['N1', 'N2', 'N3'])

    def test_redundant_time_field_normalizes_but_conflict_does_not(self):
        chunks = chunk_text('甲出发。'); e = event(); normalize_evidence([e], chunks)
        w = world([e])
        review = Review(is_valid=False, issues=[], summary='修格式', covered_chunks=[1],
            correction=Correction(event_updates={e.id: {'event_order': '2'}}),
            time=Timeline(nodes=[TimeNode(id='N1', name='出发', order=1)],
                          timeline=[TimePlacement(id=e.id, node_id='N1', order=2, basis='原文顺序')]))
        self.assertEqual(apply_correction(w, review, chunks), w)
        review.correction.event_updates[e.id]['event_order'] = 1
        with self.assertRaisesRegex(ValueError, '非法字段'): apply_correction(w, review, chunks)
        review.time.timeline[0].order = 1
        review.correction.event_updates[e.id]['event_order'] = True
        with self.assertRaisesRegex(ValueError, '非法字段'): apply_correction(w, review, chunks)

    def test_unplaced_event_stays_in_manifest_not_fabricated_node(self):
        storage = NovelExtractStorage(self.directory / 'unplaced.sqlite3'); self.addCleanup(storage.close)
        storage.set('source', '离线'); storage.set('status', 'partial')
        storage.snapshot('information', information().model_dump(mode='json'))
        target = self.directory / 'unplaced-repository'
        NovelExtractExporter(storage).export(target, world([event()]))
        self.assertEqual(json.loads((target / 'timeline/timeline.json').read_text())['nodes'], [])
        report = json.loads(Path(str(target) + '.incomplete.json').read_text())
        self.assertEqual(len(report['saved_events']), 1)
        self.assertEqual(report['not_exported_records'][0]['kind'], 'event')
        self.assertIn('时间节点未交付', report['not_exported_records'][0]['reason'])

    def test_archived_formal74_candidates_and_pending_patch(self):
        path = ROOT / '.novel_agent_work/祝福_gpt4o_正式74.sqlite3'
        if not path.exists(): self.skipTest('本机历史实验不在此副本')
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
            text = json.loads(db.execute("select value from meta where key='input_text'").fetchone()[0])
            saved = {k: json.loads(v) for k, v in db.execute('select stage,payload from snapshots order by id')}
            replies = [json.loads(v) for v, in db.execute(
                "select payload from snapshots where stage='job:global.review.0' order by id")
                if json.loads(v)['status'] == 'received']
        chunks = chunk_text(text)
        for job, expected in [('chunk.6.first_pass', 5), ('chunk.7.second_pass', 3)]:
            candidate = EventPass.model_validate_json(saved['job:' + job]['response'])
            normalize_evidence(candidate.events, chunks)
            self.assertEqual(len(candidate.events), expected)
            self.assertEqual(evidence_errors(candidate.events, chunks), [])
            self.assertTrue(any(q.unverified_quote for e in candidate.events for q in e.evidence))
        storage = NovelExtractStorage(self.directory / 'replay74.sqlite3'); self.addCleanup(storage.close)
        storage.set('source', '祝福_鲁迅')
        saved['information'] = format36_information(saved['information'])
        saved['world'] = format36_world(saved['world']).model_dump(mode='json')
        for job in ('chunk.6.first_pass', 'chunk.7.second_pass'):
            payload = saved['job:' + job]
            candidate = json.loads(payload['response'])
            for item in candidate.get('events', []):
                item['occurrence_layer'] = '实际发生'
                item['occurrence_signature'] = {
                    'actual_subjects': item.get('actor_ids', []),
                    'occurrence': item.get('description') or '历史事件',
                    'start_anchor': item.get('source_time') or item.get('temporal_basis') or '历史阶段',
                    'layer': '实际发生',
                }
            payload['response'] = json.dumps(candidate, ensure_ascii=False)
        for key, value in saved.items(): storage.snapshot(key, value)
        workflow = NovelExtractWorkflow(); workflow.storage = storage; workflow.failures = saved['failures'][:-1]
        w = format36_world(saved['world'])
        for reply in replies:
            review = format36_review(json.loads(reply['response']))
            updated, details = workflow.evaluate_review(review, w, chunks)
            self.assertFalse(review.is_valid)
            self.assertTrue(review.issues)
            self.assertGreaterEqual(len(updated.events), len(w.events))
        last = format36_review(json.loads(replies[-1]['response']))
        updated, _ = workflow.evaluate_review(last, w, chunks)
        self.assertIn('EVT_C6_E1', {e.id for e in updated.events})
        self.assertFalse(any('event_updates 只能修改' in s for s in last.issues))
        # 原始引文、32条已保存事件和18个局部节点都保留，历史库不写入。
        self.assertEqual(len(w.events), 32)
        files, _ = NovelExtractExporter(storage).project(w)
        self.assertEqual(len(files['timeline/timeline.json']['nodes']), 18)
        self.assertFalse(any(n['node_id'].startswith('N_UNKNOWN') for n in files['timeline/timeline.json']['nodes']))

    def test_resume_after_partial_fact_commit_does_not_replay_global_request(self):
        class PartialFact(FixtureServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job == 'global.review.0':
                    result['correction']['event_updates'] = {'EVT_C1_E1': {'description': '甲正式出发'}}
                    result['time']['timeline'][0]['node_id'] = 'MISSING'
                return result
        original, fired = NovelExtractStorage.set, []
        def stop(storage, key, value):
            if key == 'stage' and storage.get('review_state', {}).get('rounds') == 2 and not fired:
                fired.append(True)
                raise KeyboardInterrupt()
            original(storage, key, value)
        server = PartialFact()
        self.assertEqual(self.run_pipeline(server, chunks=1, hook=patch.object(NovelExtractStorage, 'set', stop)), 130)
        saved = self.snapshots()['world']['events'][0]
        self.assertEqual(saved['description'], '甲正式出发')
        self.assertIsNone(saved['time_node_id'])
        prior_requests = len([job for job, _, _ in server.requests if job == 'global.review.0'])
        self.assertEqual(self.run_pipeline(server, chunks=1), 0)
        self.assertEqual(len([job for job, _, _ in server.requests if job == 'global.review.0']), prior_requests)

    def test_resume_after_profile_supplement_reuses_saved_delivery(self):
        class ExtraPerson(FixtureServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job == 'chunk.1.first_pass':
                    result['entities'] = {'ENT_C1_2': {'name': '乙'}}
                    result['events'][0]['target_ids'] = ['ENT_C1_2']
                return result
        original, fired = NovelExtractStorage.snapshot, []
        def stop(storage, stage, value):
            original(storage, stage, value)
            if stage == 'information' and 'ENT_C1_2' in value['characters'] and not fired:
                fired.append(True)
                raise KeyboardInterrupt()
        server = ExtraPerson()
        self.assertEqual(self.run_pipeline(server, chunks=1, hook=patch.object(NovelExtractStorage, 'snapshot', stop)), 130)
        self.assertEqual(self.run_pipeline(server, chunks=1), 0)
        self.assertEqual(len([job for job, _, _ in server.requests if job.startswith('information.supplement.')]), 1)

    def test_entity_merge_cannot_leave_unsynchronized_profiles(self):
        storage = NovelExtractStorage(self.directory / 'merge.sqlite3'); self.addCleanup(storage.close)
        current = information().model_dump(mode='json'); current['characters']['ENT_2'] = {'activity': 'npc'}
        storage.snapshot('information', current)
        engine = NovelExtractWorkflow(); engine.storage = storage; engine.failures = []
        chunks = chunk_text('甲出发。'); e = event(); normalize_evidence([e], chunks)
        w = world([e]); w.entities['ENT_2'] = Entity(name='甲别称')
        review = Review(is_valid=False, summary='合并但档案未交', issues=[], covered_chunks=[1],
                        correction=Correction(entities={'ENT_1': Entity(name='甲', aliases=['甲别称'])},
                            entity_merges=[EventMerge(source='ENT_2', target='ENT_1', reason='同人')]))
        candidate, details = engine.evaluate_review(review, w, chunks)
        self.assertEqual(candidate, w)
        self.assertEqual(set(details['characters']), {'ENT_1', 'ENT_2'})
        self.assertTrue(any('档案修订未接收' in s for s in review.issues))

    def test_programming_fault_is_not_downgraded_to_partial(self):
        server = FixtureServer()
        with self.assertRaisesRegex(RuntimeError, 'program fault'):
            self.run_pipeline(server, chunks=1, hook=patch.object(
                NovelExtractWorkflow, 'evaluate_review', side_effect=RuntimeError('program fault')))
        self.assertEqual(NovelExtractStorage.read_status(self.workspace)['status'], 'working')

    def test_failed_supplement_survives_unrelated_entity_revision(self):
        class FailedSupplement(FixtureServer):
            def reply(self, job, attempt, context):
                if job.startswith('information.supplement.'): return '{broken'
                result = super().reply(job, attempt, context)
                if job == 'chunk.1.first_pass':
                    result['entities'] = {'ENT_C1_2': {'name': '乙'}}
                    result['events'][0]['target_ids'] = ['ENT_C1_2']
                if job == 'global.review.0':
                    result['correction']['entities'] = {'ENT_1': {'name': '甲', 'aliases': ['甲君']}}
                    result['information_audit'] = None
                if job == 'global.review.1':
                    information_patch = result.get('information') or {}
                    result['information'] = information_patch
                    information_patch['characters'] = {'ENT_C1_2': {
                        'activity': 'npc', 'identity': ['事件的承受者'],
                        'profile': '原文中与甲出发有关的人物',
                    }}
                    result['information_audit'] = None
                return result
        server = FailedSupplement()
        self.assertEqual(self.run_pipeline(server, chunks=1), 0)
        self.assertEqual(len([job for job, _, _ in server.requests if job.startswith('information.supplement.')]), 2)
        self.assertEqual(self.snapshots()['failures'], [])

    def timed_world(self):
        chunks = chunk_text('甲出发。')
        events = [event(id=f'EVT_C1_E{i}', time_node_id=f'N{i}', event_order=1) for i in range(1, 4)]
        normalize_evidence(events, chunks)
        w = world(events)
        w.time_nodes = [TimeNode(id=f'N{i}', name=f'阶段{i}', order=i) for i in range(1, 4)]
        return w, chunks

    def confirmed_state(self, w):
        return ReviewState(time_placements=[e.id for e in w.events],
                           confirmed_times={key: digest(value) for key, value in time_dependencies(w).items()})

    def test_profile_failure_does_not_block_time_confirmation_or_partial_export(self):
        class MissingProfile(FixtureServer):
            def reply(self, job, attempt, context):
                if job == 'foundation.extract':
                    result = super().reply(job, attempt, context)
                    result['information'] = {'broken': True}
                    return result
                if job == 'foundation.check': return '{broken'
                result = super().reply(job, attempt, context)
                if job.startswith('global.'):
                    result['is_valid'] = False
                    result['issues'] = ['人物档案未完成']
                    result['resolved_jobs'] = []
                    result['information'] = None
                    result['information_audit'] = None
                return result
        server = MissingProfile()
        self.assertEqual(self.run_pipeline(server, chunks=2), 2)
        state = NovelExtractStorage.read_status(self.workspace)['review_state']
        self.assertEqual(len(state['confirmed_times']), 2)
        self.assertFalse(state['accepted'])
        self.assertEqual(len(self.snapshots()['world']['events']), 2)
        # 档案缺失仍遵守导出引用契约，不因此清除内部时间确认。
        report = json.loads(Path(str(self.output) + '.incomplete.json').read_text())
        self.assertTrue(any('人物档案' in r['reason'] for r in report['not_exported_records']))

    def test_unrelated_open_issue_keeps_verified_orders_and_terminal_replay(self):
        class OpenIssue(FixtureServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job.startswith('global.'):
                    result['is_valid'], result['issues'] = False, ['背景习俗仍待核实']
                return result
        server = OpenIssue()
        self.assertEqual(self.run_pipeline(server), 2)
        timeline = json.loads((self.output / 'timeline/timeline.json').read_text())
        self.assertEqual([n['order'] for n in timeline['nodes']], [1, 2])
        for node in timeline['nodes']:
            tasks = json.loads((self.output / 'timeline' / node['path']).read_text())['todolist']
            self.assertEqual([t['order'] for t in tasks], [1])
        requests = len(server.requests)
        self.assertEqual(self.run_pipeline(server), 2)
        self.assertEqual(len(server.requests), requests)

    def test_complete_broad_review_needs_no_per_event_confirmation_rows(self):
        class NoConfirmation(FixtureServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job.startswith('global.'): result['time_checks'] = []
                return result
        self.assertEqual(self.run_pipeline(NoConfirmation(), chunks=1), 0)
        state = NovelExtractStorage.read_status(self.workspace)['review_state']
        self.assertTrue(state['time_reviewed'])
        self.assertTrue(state['confirmed_times'])
        self.assertTrue(state['accepted'])

    def test_time_fact_change_invalidates_only_explicit_dependencies(self):
        w, chunks = self.timed_world()
        w.relations = [Relation(source=w.events[0].id, target=w.events[1].id, kind='before', basis='原文先后')]
        state = self.confirmed_state(w)
        review = Review(is_valid=False, summary='修改发生', issues=[], covered_chunks=[1],
            correction=Correction(event_updates={w.events[0].id: {'description': '甲到达'}}))
        updated = apply_correction(w, review, chunks)
        NovelExtractWorkflow().update_time_confirmations(state, review, w, updated)
        self.assertEqual(set(state.confirmed_times), {w.events[2].id})
        # 已失效的确认即使内容再改回，也不能自动复活。
        review.correction = Correction()
        NovelExtractWorkflow().update_time_confirmations(state, review, updated, w)
        self.assertEqual(set(state.confirmed_times), {w.events[2].id})

    def test_collection_reason_and_goal_do_not_revoke_time(self):
        w, chunks = self.timed_world(); state = self.confirmed_state(w)
        review = Review(is_valid=False, summary='补充说明', issues=[], covered_chunks=[1],
            correction=Correction(event_updates={w.events[0].id: {'core_reason': '构成关键起点',
                                                               'goals': {'ENT_1': '启程'}}}))
        updated = apply_correction(w, review, chunks)
        self.assertEqual(updated.events[0].time_node_id, 'N1')
        NovelExtractWorkflow().update_time_confirmations(state, review, w, updated)
        self.assertEqual(len(state.confirmed_times), 3)

    @unittest.skip("Legacy proof-table protocol retired; see reports/novel_extract/shadowloom_validation_reset_20261016/retired_tests.md")
    def test_information_change_can_be_confirmed_by_the_same_complete_audit(self):
        w, chunks = self.timed_world()
        before = information().model_dump(mode='json')
        after = deepcopy(before)
        after['story_overview'] = '修订后的故事概览'
        state = ReviewState(
            checked_facts={1: digest(fact_dependencies(w, chunks)[1])},
            confirmed_information=digest(information_dependency(w, before)),
        )
        same_round = Review(is_valid=True, summary='修订并完成核实', issues=[], covered_chunks=[1],
                            information_audit=information_audit())

        NovelExtractWorkflow.update_information_confirmation(state, same_round, w, before, w, after)

        self.assertEqual(state.confirmed_information, digest(information_dependency(w, after)))
        self.assertTrue(same_round.is_valid)
        self.assertEqual(set(state.checked_facts), {1})

    @unittest.skip("Legacy proof-table protocol retired; see reports/novel_extract/shadowloom_validation_reset_20261016/retired_tests.md")
    def test_rejected_information_candidate_cannot_confirm_old_profile(self):
        class RejectedInformation(FixtureServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job.startswith('global.review.'):
                    result['information'] = {'characters': []}
                    result['information_audit'] = information_audit()
                return result

        server = RejectedInformation()
        self.assertEqual(self.run_pipeline(server, chunks=1), 2)
        saved = self.snapshots()
        # 结构合法但语义不可接收的档案是一次已完成的审计结果；它进入下一轮
        # 全文定向修正，不消耗同一job的无原文格式重试。
        self.assertEqual(saved['job:global.review.0']['status'], 'complete')
        self.assertEqual([attempt for job, attempt, _ in server.requests
                          if job == 'global.review.0'], [0])
        self.assertTrue(any(issue.startswith('档案修订未接收：')
                            for issue in saved['validation.review']['issues']))
        state = NovelExtractStorage.read_status(self.workspace)['review_state']
        self.assertIsNone(state['confirmed_information'])

    def test_entity_identity_and_node_order_invalidate_confirmations(self):
        w, _ = self.timed_world(); state = self.confirmed_state(w)
        review = Review(is_valid=False, summary='修主体', issues=[], covered_chunks=[1])
        changed = w.model_copy(deep=True); changed.entities['ENT_1'].name = '乙'
        NovelExtractWorkflow().update_time_confirmations(state, review, w, changed)
        self.assertFalse(state.confirmed_times)
        state = self.confirmed_state(w); changed = w.model_copy(deep=True)
        changed.time_nodes[0].order, changed.time_nodes[1].order = 2, 1
        NovelExtractWorkflow().update_time_confirmations(state, review, w, changed)
        # 交换前两个节点也改变了第三个节点的相邻边界；三者都须重新核实。
        self.assertFalse(state.confirmed_times)

    @unittest.skip("Legacy proof-table protocol retired; see reports/novel_extract/shadowloom_validation_reset_20261016/retired_tests.md")
    def test_explicit_time_doubt_is_warning_until_a_complete_repair_replaces_it(self):
        w, chunks = self.timed_world()
        w.events[0].event_order = None
        w.events[1].time_node_id = 'N1'; w.events[1].event_order = 2
        w.time_nodes = [n for n in w.time_nodes if n.id != 'N2']
        distant = event(id='EVT_C1_E4', time_node_id='N4', event_order=1)
        w.events.append(distant); w.time_nodes.append(TimeNode(id='N4', name='远处阶段', order=4))
        state = self.confirmed_state(w)
        workflow = NovelExtractWorkflow()
        required = workflow.time_check_requirements(w, {w.events[0].id, w.events[1].id})
        review = Review(is_valid=True, summary='发现时间疑点', issues=[], covered_chunks=[1],
            time_checks=[time_check(w.events[0].id, required[w.events[0].id], 'unknown', '原文无法确定组内开始先后'),
                         time_check(w.events[1].id, required[w.events[1].id])])
        workflow.update_time_confirmations(state, review, w, w)
        self.assertIn(w.events[0].id, state.confirmed_times)
        self.assertIn(distant.id, state.confirmed_times)
        self.assertTrue(review.is_valid)
        self.assertIn(w.events[0].id, state.time_doubts)
        # 后续没有新证据时保留当前最佳顺序和疑点记录。
        review.time_checks = [time_check(w.events[1].id, required[w.events[1].id])]
        workflow.update_time_confirmations(state, review, w, w)
        self.assertIn(w.events[0].id, state.confirmed_times)
        self.assertIn(w.events[0].id, state.time_doubts)
        repaired = w.model_copy(deep=True)
        repaired.events[0].event_order = 1
        for order, node in enumerate(sorted(repaired.time_nodes, key=lambda item: item.order), 1):
            node.order = order
        workflow.update_time_confirmations(state, Review(
            is_valid=False, summary='修复未知顺序', issues=[], covered_chunks=[1]), w, repaired)
        final_review = Review(is_valid=True, summary='完整复核未发现时间冲突',
                              issues=[], covered_chunks=[1])
        workflow.update_time_confirmations(state, final_review, repaired, repaired, chunks)
        self.assertEqual(len(state.confirmed_times), 4)
        self.assertFalse(state.time_doubts)

    def test_invalid_state_change_does_not_block_time_draft(self):
        w, chunks = self.timed_world()
        w.changes = [Change(event_id='MISSING', subject_id='MISSING', change='错误状态', basis='待修')]
        updated = apply_timeline(w, Timeline(nodes=[TimeNode(id='N1', name='出发阶段', order=1)]), chunks)
        self.assertEqual(updated.time_nodes[0].name, '出发阶段')
        self.assertTrue(topology_errors(updated, updated, chunks))

    @unittest.skip("Legacy proof-table protocol retired; see reports/novel_extract/shadowloom_validation_reset_20261016/retired_tests.md")
    def test_duplicate_or_unknown_time_checks_cannot_confirm(self):
        w, _ = self.timed_world(); state = self.confirmed_state(w)
        review = Review(is_valid=True, summary='错误确认', issues=[], covered_chunks=[1],
            time_checks=[time_check(w.events[0].id),
                         time_check(w.events[0].id, reason='重复'),
                         time_check('MISSING', reason='不存在')])
        NovelExtractWorkflow().update_time_confirmations(state, review, w, w)
        # 新交付的重复/未知编号不能冒充确认，但也不应撤销先前独立有效的确认。
        self.assertIn(w.events[0].id, state.confirmed_times)
        self.assertFalse(review.is_valid)

    def test_export_rejects_stale_confirmation_even_with_accepted_flag(self):
        w, _ = self.timed_world()
        storage = NovelExtractStorage(self.directory / 'stale.sqlite3'); self.addCleanup(storage.close)
        storage.set('source', '离线'); storage.snapshot('information', information().model_dump(mode='json'))
        state = self.confirmed_state(w); state.accepted = True
        storage.set('review_state', state.model_dump(mode='json'))
        w.events[0].source_time = '改为次年'
        files, _ = NovelExtractExporter(storage).project(w)
        orders = {n['node_id']: n['order'] for n in files['timeline/timeline.json']['nodes']}
        self.assertIsNone(orders['N1']); self.assertIsNone(orders['N2']); self.assertEqual(orders['N3'], 3)
        storage.set('status', 'complete')
        with self.assertRaisesRegex(ValueError, '时间确认缺失或依赖已变化'):
            NovelExtractExporter(storage).export(self.directory / 'stale-output', w)

    def test_time_confirmation_and_world_commit_resume_together(self):
        original, fired = NovelExtractStorage.set, []
        def stop(storage, key, value):
            original(storage, key, value)
            if key == 'review_state' and value['confirmed_times'] and not fired:
                fired.append(True)
                raise KeyboardInterrupt()
        server = FixtureServer()
        self.assertEqual(self.run_pipeline(server, chunks=1, hook=patch.object(NovelExtractStorage, 'set', stop)), 130)
        state = NovelExtractStorage.read_status(self.workspace)['review_state']
        self.assertFalse(state['confirmed_times'])
        requests = len(server.requests)
        self.assertEqual(self.run_pipeline(server, chunks=1), 0)
        self.assertEqual(len(server.requests), requests)
        self.assertTrue(NovelExtractStorage.read_status(self.workspace)['review_state']['confirmed_times'])

    def test_full_text_foundation_runs_once_with_one_delta_check(self):
        server = FixtureServer()
        self.assertEqual(self.run_pipeline(server), 0)
        jobs = [job for job, _, _ in server.requests]
        self.assertEqual(jobs[:2], ['foundation.extract', 'foundation.check'])
        self.assertIn('catalog', Foundation.model_json_schema()['required'])
        self.assertEqual(jobs.count('foundation.extract'), 1)
        self.assertEqual(jobs.count('foundation.check'), 1)
        self.assertNotIn('information', jobs)
        self.assertFalse(any(job.startswith('ontology.') for job in jobs))
        for _, _, request in server.requests[:2]:
            context = json.loads(request['messages'][-1]['content'])
            self.assertEqual(context['novel_source'], (self.directory / 'source.txt').read_text())
        self.assertEqual(self.snapshots()['information'], information().model_dump(mode='json'))

    def test_second_pass_same_occurrence_is_a_review_proposal(self):
        class DuplicateSecondPass(FixtureServer):
            def reply(self, job, attempt, context):
                if job == 'chunk.1.second_pass':
                    duplicate = event(1, id='EVT_C1_S1', description='甲在清晨实际出发',
                                      time_node_id='TN_C1_1', event_order=1)
                    return EventPass(events=[duplicate], coverage='补充同一发生的描述').model_dump(mode='json')
                if job == 'global.review.0':
                    self.proposals = context.get('second_pass_proposals', [])
                return super().reply(job, attempt, context)

        server = DuplicateSecondPass()
        self.assertEqual(self.run_pipeline(server, chunks=1), 0)
        saved = self.snapshots()
        self.assertNotIn('EVT_C1_S1', {row['id'] for row in saved['world']['events']})
        self.assertEqual(len(server.proposals), 1)
        self.assertEqual(server.proposals[0]['original_id'], 'EVT_C1_E1')
        self.assertEqual(server.proposals[0]['proposed']['id'], 'EVT_C1_S1')

    def test_second_pass_can_fix_old_event_time_without_rewriting_its_fact(self):
        original = event(1, time_node_id='TN_C1_1', event_order=1)
        prior = world([original])
        prior.time_nodes = [TimeNode(id='TN_C1_1', name='同一阶段', order=1)]
        earlier = event(1, id='EVT_C1_S1', description='甲先作决定',
                        occurrence_signature={**occurrence_signature(1), 'occurrence': '甲作决定'},
                        time_node_id='TN_C1_1', event_order=1)
        revised = original.model_copy(update={'description': '未经核对的改写', 'event_order': 2})
        incoming = EventPass(events=[earlier, revised],
                             relations=[Relation(source=earlier.id, target=original.id,
                                                 kind='before', basis='先决定再出发')], coverage='补充先后')

        update, proposals = NovelExtractWorkflow.second_pass_update(prior, incoming)
        combined = NovelExtractWorkflow.combine(prior, update)
        saved = {item.id: item for item in combined.events}
        self.assertEqual(saved[original.id].event_order, 2)
        self.assertEqual(saved[original.id].description, original.description)
        self.assertEqual(len(proposals), 1)
        chunks = chunk_text('甲出发。')
        normalize_evidence(combined.events, chunks)
        self.assertEqual(timeline_errors(combined) + topology_errors(
            combined, combined, chunks, check_time=True), [])

        wrong_identity = revised.model_copy(update={
            'occurrence_signature': earlier.occurrence_signature, 'event_order': 3})
        unsafe, _ = NovelExtractWorkflow.second_pass_update(
            prior, EventPass(events=[wrong_identity], coverage='身份不同'))
        self.assertEqual(unsafe.events, [])

    def test_last_round_new_fact_gets_one_bounded_time_handoff(self):
        class FinalFacts(FixtureServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job.startswith('global.review.'):
                    index = int(job.rsplit('.', 1)[-1])
                    if index < 4:
                        result['is_valid'] = False
                        result['issues'] = ['继续核对']
                        result['correction']['world_traits'] = {f'WORLD_{index}': f'事实{index}'}
                    elif index == 4:
                        result['correction']['events'] = [event(
                            1, id='EVT_C1_LATE', description='甲到达',
                            occurrence_signature={**occurrence_signature(1), 'occurrence': '甲到达'},
                            time_node_id=None, event_order=None).model_dump(mode='json')]
                        result['correction']['event_updates'] = {'EVT_C1_E1': {
                            'occurrence_signature': {**occurrence_signature(1), 'occurrence': '甲未出发'}}}
                return result

        server = FinalFacts()
        original_set, interrupted = NovelExtractStorage.set, []
        def stop_after_last_regular_round(storage, key, value):
            original_set(storage, key, value)
            if key == 'review_state' and value['rounds'] == 6 and not interrupted:
                interrupted.append(True)
                raise KeyboardInterrupt()
        self.assertEqual(self.run_pipeline(server, chunks=1, hook=patch.object(
            NovelExtractStorage, 'set', stop_after_last_regular_round)), 130)
        self.assertEqual(self.run_pipeline(server, chunks=1), 0)
        jobs = [job for job, _, _ in server.requests if job.startswith('global.')]
        self.assertEqual(jobs[-3:], ['global.review.4', 'global.timeline.1', 'global.review.5'])
        self.assertEqual(jobs.count('global.review.4'), 1)
        saved = {item['id']: item for item in self.snapshots()['world']['events']}
        self.assertIsNotNone(saved['EVT_C1_LATE']['time_node_id'])
        self.assertEqual(saved['EVT_C1_E1']['description'], '甲出发')
        self.assertTrue(NovelExtractStorage.read_status(self.workspace)['review_state']['accepted'])

    def test_penultimate_round_new_fact_gets_review_after_final_timeline(self):
        class PenultimateFacts(FixtureServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job.startswith('global.review.'):
                    index = int(job.rsplit('.', 1)[-1])
                    if index < 3:
                        result['is_valid'] = False
                        result['issues'] = ['继续核对']
                        result['correction']['world_traits'] = {f'WORLD_{index}': f'事实{index}'}
                    elif index == 3:
                        result['correction']['events'] = [event(
                            1, id='EVT_C1_LATE', description='甲到达',
                            occurrence_signature={**occurrence_signature(1), 'occurrence': '甲到达'},
                            time_node_id=None, event_order=None).model_dump(mode='json')]
                return result

        server = PenultimateFacts()
        self.assertEqual(self.run_pipeline(server, chunks=1), 0)
        jobs = [job for job, _, _ in server.requests if job.startswith('global.')]
        self.assertEqual(jobs[-3:], ['global.review.3', 'global.timeline.1', 'global.review.4'])
        saved = {item['id']: item for item in self.snapshots()['world']['events']}
        self.assertIsNotNone(saved['EVT_C1_LATE']['time_node_id'])
        self.assertTrue(NovelExtractStorage.read_status(self.workspace)['review_state']['accepted'])

    def test_second_pass_keeps_distinct_or_ambiguous_occurrences(self):
        first = event(1)
        distinct = event(1, id='EVT_C1_S1', description='甲实际抵达',
                         occurrence_signature={**occurrence_signature(1), 'occurrence': '实际抵达'})
        additions, proposals = NovelExtractWorkflow.separate_second_pass([first], [distinct])
        self.assertEqual(additions, [distinct])
        self.assertEqual(proposals, [])

        ambiguous = event(1, id='EVT_C1_E2', description='另一件可能误签名的事')
        same_signature = event(1, id='EVT_C1_S2', description='尚不能确定对应哪个旧事件')
        additions, proposals = NovelExtractWorkflow.separate_second_pass([first, ambiguous], [same_signature])
        self.assertEqual(additions, [same_signature])
        self.assertEqual(proposals, [])

        other_source = event(1, id='EVT_C1_S3', evidence=[Evidence(chunk=1, passage_ids=['C1_P2'])])
        additions, proposals = NovelExtractWorkflow.separate_second_pass([first], [other_source])
        self.assertEqual(additions, [other_source])
        self.assertEqual(proposals, [])

    def test_foundation_delta_adds_missing_identity_and_profile_together(self):
        class MissingIdentity(FixtureServer):
            def reply(self, job, attempt, context):
                if job == 'foundation.check':
                    return {'summary': '补齐无名参与者与地点', 'issues': [],
                        'correction': {'entities': {'ENT_2': {'name': '乙'}},
                                       'locations': {'LOC_1': {'name': '村口'}}},
                        'information': {
                            'characters': {'ENT_2': {
                                'activity': 'npc', 'identity': ['行程参与者'],
                                'profile': '原文参与者',
                            }},
                            'locations': {'LOC_1': {'description': '原文地点'}},
                            'relations': [{
                                'owner': 'ENT_1', 'other': 'ENT_2',
                                'relation': ['同行程人物'],
                                'detail': '甲与乙在原文中共同参与出发行程',
                            }],
                        },
                        'audit': information_audit(('ENT_1', 'ENT_2'), ('LOC_1',))}
                return super().reply(job, attempt, context)
        server = MissingIdentity()
        self.assertEqual(self.run_pipeline(server), 0)
        saved = self.snapshots()
        self.assertEqual(set(saved['register']['entities']), {'ENT_1', 'ENT_2'})
        self.assertEqual(saved['information']['characters']['ENT_1'], information().characters['ENT_1'].model_dump())
        self.assertIn('ENT_2', saved['information']['characters'])
        self.assertIn('LOC_1', saved['information']['locations'])
        self.assertFalse(any(job.startswith('information.supplement.') for job, _, _ in server.requests))

    def test_malformed_foundation_information_is_repaired_without_reextracting_catalog(self):
        class BrokenDetails(FixtureServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job == 'foundation.extract':
                    result['information']['characters'] = ['误用列表']
                    result['information']['background'] = '误用字符串'
                    result['information']['relations'] = [{'other': 'ENT_1'}]
                elif job == 'foundation.check':
                    self.errors = context['structural_errors']
                    if attempt == 0: return result
                    result['information'] = information().model_dump(mode='json')
                return result
        server = BrokenDetails()
        self.assertEqual(self.run_pipeline(server), 0)
        self.assertTrue(server.errors)
        self.assertEqual(sum(job == 'foundation.check' for job, _, _ in server.requests), 2)
        self.assertEqual(sum(job == 'foundation.extract' for job, _, _ in server.requests), 1)
        self.assertEqual(self.snapshots()['information'], information().model_dump(mode='json'))

    def test_missing_foundation_information_requires_complete_check(self):
        class MissingDetails(FixtureServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job == 'foundation.extract':
                    result['information'] = {}
                elif job == 'foundation.check':
                    result['information'] = information().model_dump(mode='json')
                return result

        server = MissingDetails()
        self.assertEqual(self.run_pipeline(server, chunks=1), 0)
        check_request = next(body for job, _, body in server.requests if job == 'foundation.check')
        self.assertIn('RepositoryInformation', check_request['messages'][0]['content'])
        self.assertEqual(self.snapshots()['information'], information().model_dump(mode='json'))

    def test_failed_foundation_check_keeps_valid_catalog_and_continues_chunks(self):
        class FailedCheck(FixtureServer):
            def reply(self, job, attempt, context):
                if job == 'foundation.check': return '{broken'
                if job == 'global.review.0': self.failed_jobs = context['failed_jobs']
                return super().reply(job, attempt, context)
        server = FailedCheck()
        self.assertEqual(self.run_pipeline(server), 0)
        self.assertTrue(any(f['job'] == 'foundation.check' for f in server.failed_jobs))
        self.assertTrue(any(job == 'chunk.2.first_pass' for job, _, _ in server.requests))
        self.assertEqual(sum(job == 'foundation.check' for job, _, _ in server.requests), 2)
        self.assertEqual(self.snapshots()['failures'], [])

    def test_foundation_profile_gap_is_repaired_by_delta_check(self):
        class ProfileGap(FixtureServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job == 'foundation.extract': result['information']['characters'] = {}
                if job == 'foundation.check':
                    self.errors = context['structural_errors']
                    result['information'] = {'characters': information().model_dump(mode='json')['characters']}
                return result
        server = ProfileGap()
        self.assertEqual(self.run_pipeline(server), 0)
        self.assertTrue(any('ENT_1' in error for error in server.errors))
        self.assertEqual(sum(job.startswith('information.supplement.') for job, _, _ in server.requests), 0)
        self.assertEqual(sum(job == 'foundation.check' for job, _, _ in server.requests), 1)
        self.assertEqual(self.snapshots()['information']['characters'],
                         information().model_dump(mode='json')['characters'])
        self.assertNotIn('information', [job for job, _, _ in server.requests])

    def test_foundation_semantic_issue_does_not_trigger_compact_format_retry(self):
        class SemanticIssue(FixtureServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job == 'foundation.check':
                    result['issues'] = ['人物关系时期仍需结合后文核对']
                    result['audit']['unresolved'] = ['人物关系时期仍需结合后文核对']
                return result

        server = SemanticIssue()
        self.assertEqual(self.run_pipeline(server, chunks=1), 0)
        self.assertEqual([attempt for job, attempt, _ in server.requests
                          if job == 'foundation.check'], [0])
        self.assertTrue(any(job == 'global.review.0' for job, _, _ in server.requests))

    def test_foundation_handoff_is_atomic_and_reuses_saved_check(self):
        original, fired = NovelExtractStorage.set, []
        def stop(storage, key, value):
            original(storage, key, value)
            if key == 'foundation_complete' and not fired:
                fired.append(True)
                raise KeyboardInterrupt()
        server = FixtureServer()
        self.assertEqual(self.run_pipeline(server, hook=patch.object(NovelExtractStorage, 'set', stop)), 130)
        with closing(sqlite3.connect(self.workspace.as_uri() + '?mode=ro', uri=True)) as db:
            self.assertIsNone(db.execute("select value from meta where key='foundation_complete'").fetchone())
        self.assertEqual(self.run_pipeline(server), 0)
        self.assertEqual(sum(job == 'foundation.extract' for job, _, _ in server.requests), 1)
        self.assertEqual(sum(job == 'foundation.check' for job, _, _ in server.requests), 1)

    def test_resume_after_foundation_failure_preserves_unresolved_responsibility(self):
        class FailedCheck(FixtureServer):
            def reply(self, job, attempt, context):
                if job == 'foundation.check': return '{broken'
                if job == 'global.review.0': self.failed_jobs = context['failed_jobs']
                return super().reply(job, attempt, context)
        original, fired = NovelExtractWorkflow.stage, []
        def stop(engine, name, **data):
            original(engine, name, **data)
            if name == '文本分块' and not fired:
                fired.append(True)
                raise KeyboardInterrupt()
        server = FailedCheck()
        self.assertEqual(self.run_pipeline(server, hook=patch.object(NovelExtractWorkflow, 'stage', stop)), 130)
        requests = len(server.requests)
        self.assertEqual(requests, 3)
        self.assertEqual(self.run_pipeline(server), 0)
        self.assertTrue(any(f['job'] == 'foundation.check' for f in server.failed_jobs))
        self.assertEqual(sum(job.startswith('foundation.') for job, _, _ in server.requests), requests)

    def test_foundation_merge_migrates_profile_and_relations(self):
        class MergeIdentity(FixtureServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job == 'foundation.extract':
                    result['catalog']['entities']['ENT_2'] = {'name': '甲别称'}
                    result['information']['characters']['ENT_2'] = {'activity': 'npc'}
                    result['information']['relations'] = [{'owner': 'ENT_1', 'other': 'ENT_2',
                                                            'relation': ['误认为他人']}]
                if job == 'foundation.check':
                    result['correction'] = {'entities': {'ENT_1': {
                        'name': '甲', 'aliases': ['甲别称'], 'description': ''}},
                        'entity_merges': [{'source': 'ENT_2', 'target': 'ENT_1', 'reason': '原文明确同人'}]}
                    result['information'] = {'characters': {'ENT_1': {
                        'activity': '主角', 'nickname': '甲别称', 'profile': '原文中实际出发的人物',
                        'identity': ['出发者'], 'gender': None, 'age': None,
                        'personality': None, 'biography': None}}}
                return result
        self.assertEqual(self.run_pipeline(MergeIdentity()), 0)
        saved = self.snapshots()
        self.assertEqual(set(saved['register']['entities']), {'ENT_1'})
        self.assertEqual(set(saved['information']['characters']), {'ENT_1'})
        self.assertEqual(saved['information']['characters']['ENT_1']['nickname'], '甲别称')
        self.assertEqual(saved['information']['relations'], [])


if __name__ == '__main__':
    unittest.main()
