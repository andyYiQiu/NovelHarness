"""离线动态资产契约：状态回放、同时更新、引用合并及真实工作流交接。"""
import json
import unittest
from unittest.mock import patch

from common.novel_extract.NovelExtractReader import chunk_text, compact_evidence
from common.novel_extract.NovelExtractSchema import (
    AssetState, Correction, EventMerge, EventPass, Evidence, Location, Object,
    ObjectState, ObjectStatePatch, ReferenceState, Review, StateDelta, TimeNode,
    TimePlacement, Timeline, normalize_evidence, time_dependencies, fact_dependencies,
)
from common.novel_extract.NovelExtractRevision import apply_correction
from common.novel_extract.NovelExtractState import (
    apply_delta, asset_errors, initial_state_coverage, merge_deltas, remap_assets, replay_states,
)
import test_harness as harness_fixtures
from test_harness import FixtureServer, event, information, world
from storage.novel_extract.NovelExtractStorage import NovelExtractStorage
from workflows.NovelExtractWorkflow import NovelExtractWorkflow
from exporters.NovelExtractExporter import NovelExtractExporter
from test_formal79_non_regression import ReadOnlyStorage
from common.novel_extract.NovelExtractRuntime import digest


TEXT = '完好的书属于甲，甲在屋内持有，乙受雇于甲。\n甲把书交给乙。\n同时甲和乙解除雇佣关系。\n乙带书离开屋子到桥边。'


def evidence(passage=1):
    return [{'chunk': 1, 'passage_ids': [f'C1_P{passage}']}]


def reference(identity=None, status=None):
    return {'status': status or ('known' if identity else 'unknown'), 'id': identity}


def object_delta(**fields):
    return {'object_id': 'OBJ_1', 'set': fields, 'basis': '原文明确说明该变化', 'evidence': evidence(2)}


def initial_state():
    return AssetState.model_validate({
        'relations': [{'owner_id': 'ENT_1', 'other_id': 'ENT_2', 'relation': '雇员',
                       'status': 'active', 'detail': '乙受雇于甲', 'evidence': evidence()}],
        'objects': {'OBJ_1': {'owner': reference('ENT_1'), 'holder': reference('ENT_1'),
                             'location': reference('LOC_1'), 'condition': '完好', 'evidence': evidence()}},
    })


def dynamic_world():
    events = [event(id=f'EVT_C1_E{i}', description=text, actor_ids=['ENT_1' if i < 3 else 'ENT_2'],
                    time_node_id='N1' if i < 3 else 'N2',
                    event_order=1, object_ids=['OBJ_1'],
                    evidence=evidence(i + 1), occurrence_signature={
                        'actual_subjects': ['ENT_1' if i < 3 else 'ENT_2'], 'occurrence': text,
                        'start_anchor': '交接时' if i < 3 else '离开时', 'layer': '实际发生'})
              for i, text in enumerate(('甲把书交给乙', '甲解除与乙的雇佣关系', '乙带书离开'), 1)]
    events[0].state_delta = StateDelta(objects=[object_delta(holder=reference('ENT_2'))])
    events[1].state_delta = StateDelta(relations=[{
        'owner_id': 'ENT_1', 'other_id': 'ENT_2', 'relation': '雇员',
        'set': {'status': 'inactive', 'detail': '雇佣关系已经解除'},
        'basis': '解除雇佣', 'evidence': evidence(3)}])
    events[2].state_delta = StateDelta(objects=[object_delta(location=reference('LOC_2'))])
    events[2].state_delta.objects[0].evidence = [Evidence.model_validate(row) for row in evidence(4)]
    value = world(events)
    value.entities['ENT_2'] = type(value.entities['ENT_1'])(name='乙')
    value.locations = {'LOC_1': Location(name='屋子'), 'LOC_2': Location(name='桥边')}
    value.objects = {'OBJ_1': Object(name='书', aliases=[], description='原文中的书',
                                   function='阅读', significance='交接物', evidence=evidence())}
    value.time_nodes = [TimeNode(id='N1', name='交接', order=1), TimeNode(id='N2', name='离开', order=2)]
    value = type(value).model_validate(value.model_dump())
    normalize_evidence(value, chunk_text(TEXT))
    return value


class DynamicAssetTests(unittest.TestCase):
    def test_patch_roundtrip_preserves_omission_and_explicit_unknown(self):
        patch = ObjectStatePatch(holder=reference(status='none'), condition=None)
        encoded = json.loads(patch.model_dump_json())
        self.assertEqual(set(encoded), {'holder', 'condition'})
        self.assertEqual(ObjectStatePatch.model_validate(encoded).model_dump(), encoded)
        state = initial_state()
        result = apply_delta(state, StateDelta(objects=[object_delta(**encoded)]))
        self.assertEqual(result.objects['OBJ_1'].owner.id, 'ENT_1')
        self.assertEqual(result.objects['OBJ_1'].holder.status, 'none')
        self.assertIsNone(result.objects['OBJ_1'].condition)
        self.assertEqual(state.objects['OBJ_1'].holder.id, 'ENT_1')

    def test_invalid_reference_or_unknown_update_field_is_rejected(self):
        for row in (reference('ENT_1', 'unknown'), reference(None, 'known'), reference('', 'none')):
            with self.subTest(row=row), self.assertRaises(ValueError):
                ReferenceState.model_validate(row)
        for row in ({}, {'holder': None}, {'holder_typo': reference('ENT_1')}):
            with self.subTest(row=row), self.assertRaises(ValueError):
                ObjectStatePatch.model_validate(row)

    def test_simultaneous_deltas_share_input_and_next_snapshot(self):
        value, initial = dynamic_world(), initial_state()
        normalize_evidence(initial, chunk_text(TEXT))
        snapshots, errors = replay_states(value, initial)
        self.assertFalse(errors)
        self.assertEqual(snapshots['N1']['objects']['OBJ_1']['holder']['id'], 'ENT_1')
        self.assertEqual(snapshots['N2']['objects']['OBJ_1']['holder']['id'], 'ENT_2')
        self.assertEqual(snapshots['N2']['objects']['OBJ_1']['owner']['id'], 'ENT_1')
        self.assertEqual(snapshots['N2']['objects']['OBJ_1']['location']['id'], 'LOC_1')
        self.assertEqual(snapshots['N2']['relations'][0]['status'], 'inactive')
        self.assertEqual(snapshots['N2']['previous_node_id'], 'N1')
        value.events.reverse()
        self.assertEqual(replay_states(value, initial), (snapshots, []))
        self.assertIsNone(snapshots['N1']['previous_node_id'])

    def test_snapshot_retains_uninvolved_assets_and_all_earlier_order_updates(self):
        value, initial = dynamic_world(), initial_state()
        value.entities['ENT_3'] = type(value.entities['ENT_1'])(name='丙')
        value.objects['OBJ_2'] = Object(name='另一件物品')
        initial.objects['OBJ_2'] = initial.objects['OBJ_1'].model_copy(deep=True)
        initial.relations.append(initial.relations[0].model_copy(
            update={'other_id': 'ENT_3', 'relation': '朋友', 'detail': '没有参加这些事件的朋友'}))
        last = value.events[2].model_copy(deep=True, update={'id': 'EVT_LAST', 'state_delta': StateDelta()})
        for order, row in enumerate(value.events, 1):
            row.time_node_id, row.event_order = 'N1', order
        value.events.append(last)
        snapshots, errors = replay_states(value, initial)
        self.assertFalse(errors)
        end = snapshots['N2']
        self.assertEqual(end['objects']['OBJ_1']['holder']['id'], 'ENT_2')
        self.assertEqual(end['objects']['OBJ_1']['location']['id'], 'LOC_2')
        self.assertEqual(end['objects']['OBJ_2']['holder']['id'], 'ENT_1')
        self.assertEqual({row['relation']: row['status'] for row in end['relations']},
                         {'雇员': 'inactive', '朋友': 'active'})

    def test_missing_initial_row_is_not_an_explicit_unknown(self):
        value, details = dynamic_world(), information()
        details.initial_state = initial_state()
        value.objects['OBJ_2'] = Object(name='尚无起点状态的物品')
        errors = initial_state_coverage(details, value)
        self.assertTrue(any('OBJ_2' in row for row in errors))
        details.initial_state.objects['OBJ_2'] = ObjectState()
        self.assertFalse(initial_state_coverage(details, value))
        details.initial_state.relations = []
        self.assertTrue(any('ENT_1' in row and 'ENT_2' in row for row in initial_state_coverage(details, value)))

    def test_first_confirmed_field_does_not_invent_ownership_or_earlier_state(self):
        initial = AssetState(objects={'OBJ_1': ObjectState()})
        result = apply_delta(initial, StateDelta(objects=[object_delta(holder=reference('ENT_2'), condition='破损')]))
        self.assertEqual(result.objects['OBJ_1'].holder.id, 'ENT_2')
        self.assertEqual(result.objects['OBJ_1'].owner.status, 'unknown')
        self.assertEqual(initial.objects['OBJ_1'].holder.status, 'unknown')

    def test_conflicting_same_order_and_last_group_cannot_silently_pass(self):
        value = dynamic_world()
        value.events[1].state_delta = StateDelta(objects=[object_delta(holder=reference('ENT_1'))])
        snapshots, errors = replay_states(value, initial_state())
        self.assertIn('N1', snapshots)
        self.assertNotIn('N2', snapshots)
        self.assertIn('同时更新冲突', errors[0])
        value = dynamic_world()
        value.events[2].state_delta.objects.append(type(value.events[2].state_delta.objects[0]).model_validate(
            object_delta(location=reference('LOC_1'))))
        self.assertIn('同时更新冲突', replay_states(value, initial_state())[1][0])

    def test_missing_or_unconfirmed_order_does_not_create_fake_snapshots(self):
        value = dynamic_world()
        value.time_nodes[0].order = None
        self.assertFalse(replay_states(value, initial_state())[0])
        value = dynamic_world()
        snapshots, errors = replay_states(value, initial_state(), {e.id for e in value.events[:2]})
        self.assertEqual(set(snapshots), {'N1'})
        self.assertTrue(errors)
        value.events[0].state_delta = None
        snapshots, errors = replay_states(value, initial_state())
        self.assertNotIn('N2', snapshots)
        self.assertIn('缺少state_delta', errors[0])
        self.assertFalse(replay_states(value, None)[0])

    def test_bad_references_and_sources_are_reported(self):
        value, initial = dynamic_world(), initial_state()
        normalize_evidence(initial, chunk_text(TEXT))
        self.assertFalse(asset_errors(value, value, chunk_text(TEXT), initial, require_complete=True))
        value.events[0].state_delta.objects[0].set.holder.id = 'ENT_MISSING'
        self.assertTrue(any('ENT_MISSING' in error for error in asset_errors(value, value, chunk_text(TEXT))))
        initial.objects['OBJ_1'].evidence = []
        self.assertTrue(any('初始状态' in error for error in asset_errors(value, value, chunk_text(TEXT), initial)))

    def test_nested_evidence_remains_compact_in_model_context(self):
        value = dynamic_world().model_dump(mode='json')
        compact = compact_evidence(value, chunk_text(TEXT))
        source = compact['events'][0]['state_delta']['objects'][0]['evidence'][0]
        self.assertEqual(source['passage_ids'], ['C1_P2'])
        self.assertEqual(source['quote'], '')
        self.assertTrue(value['events'][0]['state_delta']['objects'][0]['evidence'][0]['quote'])

    def test_unresolved_source_and_missing_delta_cannot_pass_review(self):
        value, chunks = dynamic_world(), chunk_text(TEXT)
        value.events[0].state_delta.objects[0].evidence = [Evidence(chunk=20, passage_ids=['C20_P1'])]
        self.assertTrue(any('第20块' in error for error in asset_errors(value, value, chunks)))
        value.events[0].state_delta = None
        self.assertTrue(any('EVT_C1_E1.state_delta' in error
                            for error in asset_errors(value, value, chunks, initial_state(), require_complete=True)))

    def test_relation_end_does_not_delete_another_relation(self):
        state = initial_state()
        state.relations.append(state.relations[0].model_copy(update={'relation': '朋友'}))
        result = apply_delta(state, dynamic_world().events[1].state_delta)
        statuses = {row.relation: row.status for row in result.relations}
        self.assertEqual(statuses, {'朋友': 'active', '雇员': 'inactive'})

    def test_state_only_correction_keeps_time_but_changes_fact_dependency(self):
        value = dynamic_world()
        patch = {'objects': [object_delta(condition='受损')]}
        review = Review(is_valid=False, summary='仅更正状态', issues=[], covered_chunks=[1],
                        correction=Correction(event_updates={value.events[0].id: {'state_delta': patch}}))
        candidate = apply_correction(value, review, chunk_text(TEXT))
        self.assertEqual(time_dependencies(candidate), time_dependencies(value))
        self.assertEqual(candidate.events[0].time_node_id, 'N1')
        self.assertEqual(candidate.events[0].state_delta.objects[0].set.condition, '受损')
        self.assertNotEqual(candidate.events[0].state_delta, value.events[0].state_delta)
        self.assertNotEqual(fact_dependencies(candidate, chunk_text(TEXT)),
                            fact_dependencies(value, chunk_text(TEXT)))

    def test_same_occurrence_merge_retains_delta_and_all_references(self):
        value = dynamic_world()
        duplicate = value.events[0].model_copy(deep=True, update={'id': 'EVT_COPY'})
        duplicate.state_delta = StateDelta(objects=[object_delta(condition='受损')])
        value.events.append(duplicate)
        review = Review(is_valid=False, summary='同一发生合并', issues=[], covered_chunks=[1],
                        correction=Correction(merges=[EventMerge(source='EVT_COPY', target=value.events[0].id, reason='同一次交接')],
                            event_updates={value.events[0].id: {'description': value.events[0].description}}))
        candidate = apply_correction(value, review, chunk_text(TEXT))
        fields = candidate.events[0].state_delta.objects[0].set
        self.assertEqual(fields.holder.id, 'ENT_2')
        self.assertEqual(fields.condition, '受损')
        self.assertNotIn('EVT_COPY', {row.id for row in candidate.events})
        renamed = remap_assets(candidate.events[0].state_delta, {'ENT_2': 'ENT_3', 'OBJ_1': 'OBJ_2'})
        self.assertEqual(renamed.objects[0].object_id, 'OBJ_2')
        self.assertEqual(renamed.objects[0].set.holder.id, 'ENT_3')
        self.assertEqual(value.events[0].state_delta.objects[0].set.holder.id, 'ENT_2')

    def test_catalog_merge_combines_compatible_deltas_and_evidence(self):
        delta = StateDelta(objects=[object_delta(holder=reference('ENT_2')),
                                   {**object_delta(condition='受损'), 'object_id': 'OBJ_2'}])
        result = remap_assets(delta, {'OBJ_2': 'OBJ_1'})
        self.assertEqual(len(result.objects), 1)
        self.assertEqual(set(result.objects[0].set.model_dump()), {'holder', 'condition'})
        state = initial_state()
        state.objects['OBJ_2'] = state.objects['OBJ_1'].model_copy(deep=True)
        state.objects['OBJ_2'].evidence = [Evidence.model_validate(row) for row in evidence(2)]
        result = remap_assets(state, {'OBJ_2': 'OBJ_1'})
        self.assertEqual(len(result.objects), 1)
        self.assertEqual(len(result.objects['OBJ_1'].evidence), 2)

    def test_full_event_merge_replay_does_not_lose_existing_delta(self):
        value = dynamic_world()
        duplicate = value.events[0].model_copy(deep=True, update={'id': 'EVT_COPY'})
        duplicate.state_delta = StateDelta(objects=[object_delta(condition='受损')])
        value.events.append(duplicate)
        target = value.events[0].model_dump(exclude={'state_delta'})
        review = Review(is_valid=False, summary='合并同一发生', issues=[], covered_chunks=[1],
            correction=Correction(events=[target], merges=[EventMerge(
                source='EVT_COPY', target=target['id'], reason='同一次交接')]))
        restored = Review.model_validate_json(review.model_dump_json())
        for submitted in (review, restored):
            candidate = apply_correction(value, submitted, chunk_text(TEXT))
            self.assertEqual(candidate.events[0].state_delta.objects[0].set.model_dump(),
                             {'holder': reference('ENT_2'), 'condition': '受损'})

    def test_conflicting_initial_state_rolls_back_catalog_merge_only(self):
        value = dynamic_world()
        value.objects['OBJ_2'] = value.objects['OBJ_1'].model_copy(deep=True)
        details = information().model_dump(mode='json')
        details['characters']['ENT_2'] = details['characters']['ENT_1']
        details['locations'] = {key: {'description': row.name} for key, row in value.locations.items()}
        state = initial_state()
        state.objects['OBJ_2'] = state.objects['OBJ_1'].model_copy(deep=True)
        state.objects['OBJ_2'].holder = ReferenceState(**reference('ENT_2'))
        normalize_evidence(state, chunk_text(TEXT))
        details['initial_state'] = state.model_dump(mode='json')
        storage = ReadOnlyStorage('fixture', details, {})
        storage.values['format'] = 38
        workflow = NovelExtractWorkflow()
        workflow.storage = storage
        workflow.failures = []
        review = Review(is_valid=False, summary='合并书的别称并补充离开地点', issues=[], covered_chunks=[1],
                        correction=Correction.model_validate({
                            'objects': {'OBJ_1': value.objects['OBJ_1'].model_dump()},
                            'object_merges': [{'source': 'OBJ_2', 'target': 'OBJ_1', 'reason': '同一本书'}],
                            'event_updates': {value.events[2].id: {'description': '乙带书离开屋子来到桥边'}},
                        }))
        submitted = review.model_copy(deep=True)
        candidate, saved = workflow.evaluate_review(review, value, chunk_text(TEXT))
        self.assertEqual(set(candidate.objects), {'OBJ_1', 'OBJ_2'})
        self.assertEqual(saved['initial_state'], details['initial_state'])
        self.assertEqual(candidate.events[2].description, '乙带书离开屋子来到桥边')
        self.assertTrue(any('物品合并后状态冲突' in error for error in review.issues), review.issues)
        resolved = initial_state()
        submitted.information = {'initial_state': resolved.model_dump(mode='json')}
        candidate, saved = workflow.evaluate_review(submitted, value, chunk_text(TEXT))
        self.assertEqual(set(candidate.objects), {'OBJ_1'})
        self.assertEqual(set(saved['initial_state']['objects']), {'OBJ_1'})
        self.assertFalse(any('未接收' in error for error in submitted.issues), submitted.issues)

    def test_export_does_not_publish_snapshots_depending_on_hidden_events(self):
        value = dynamic_world()
        details = information().model_dump(mode='json')  # ENT_2档案故意缺失，第三事件不能导出。
        details['locations'] = {key: {'description': row.name} for key, row in value.locations.items()}
        initial = initial_state()
        normalize_evidence(initial, chunk_text(TEXT))
        details['initial_state'] = initial.model_dump(mode='json')
        storage = ReadOnlyStorage('fixture', details, {k: digest(v) for k, v in time_dependencies(value).items()})
        storage.values['format'] = 38
        storage.snapshots['chunks'] = [chunk.payload() for chunk in chunk_text(TEXT)]
        files, excluded = NovelExtractExporter(storage).project(value)
        self.assertTrue(any('部分事件未进入repository' in row['reason'] for row in excluded))
        self.assertEqual({row['record']['id'] for row in excluded if row['kind'] == 'event'},
                         {event.id for event in value.events})
        self.assertFalse(any(path.endswith('state_before.json') for path in files))
        self.assertTrue(all(node['state_path'] is None for node in files['timeline/timeline.json']['nodes']))


class DynamicServer(FixtureServer):
    def reply(self, job, attempt, context):
        value = dynamic_world()
        if job == 'foundation.extract':
            details = information().model_dump(mode='json')
            details['characters']['ENT_2'] = {**details['characters']['ENT_1'], 'activity': 'npc', 'profile': '乙'}
            details['locations'] = {key: {'description': row.name} for key, row in value.locations.items()}
            details['relations'] = [{'owner': 'ENT_1', 'other': 'ENT_2', 'relation': ['雇员'], 'detail': '后来解除'}]
            details['initial_state'] = initial_state().model_dump(mode='json')
            return {'catalog': value.model_dump(include={'entities', 'locations', 'objects', 'world_traits'}),
                    'information': details}
        if job.endswith('.first_pass'):
            events = [row.model_copy(deep=True, update={'state_delta': None, 'time_node_id': 'TN_C1_1', 'event_order': 1})
                      for row in value.events]
            return EventPass(events=events, time_nodes=[TimeNode(id='TN_C1_1', name='交接与离开', order=1)],
                             coverage='三件关键发生').model_dump(mode='json')
        if job.endswith('.consequences'):
            return {'changes': [], 'coverage': '关系和持有状态均已提取',
                    'state_deltas': {row.id: row.state_delta.model_dump(mode='json') for row in value.events}}
        return super().reply(job, attempt, context)

    def timeline(self, ids):
        value = dynamic_world()
        return Timeline(nodes=value.time_nodes, timeline=[
            TimePlacement(id=row.id, node_id=row.time_node_id, order=row.event_order, basis='原文开始顺序')
            for row in value.events])


class DynamicWorkflowTests(unittest.TestCase):
    def make_harness(self):
        harness = harness_fixtures.HarnessTests(methodName='runTest')
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        return harness

    def test_complete_output_and_terminal_replay_without_new_model_stage(self):
        # 复用既有断网、临时SQLite、真实AutoGen和HTTP固定回复环境。
        harness = self.make_harness()
        server = DynamicServer()
        self.assertEqual(harness.run_pipeline(server, chunks=1, text=TEXT), 0, harness.snapshots().get('failures'))
        root = harness.output
        timeline = json.loads((root / 'timeline/timeline.json').read_text())
        self.assertNotIn('schema_version', timeline)
        self.assertEqual([node['order'] for node in timeline['nodes']], [1, 2])
        snapshots = [json.loads((root / 'timeline' / node['state_path']).read_text()) for node in timeline['nodes']]
        self.assertEqual(snapshots[1]['objects']['OBJ_1']['holder']['id'], 'ENT_2')
        self.assertEqual(snapshots[1]['relations'][0]['status'], 'inactive')
        tasks = json.loads((root / 'timeline' / timeline['nodes'][0]['path']).read_text())['todolist']
        self.assertEqual([row['order'] for row in tasks], [1, 1])
        self.assertEqual(set(tasks[0]['state_delta']['objects'][0]['set']), {'holder'})
        self.assertEqual(tasks[0]['object_ids'], ['OBJ_1'])
        self.assertNotIn('relation', json.loads((root / 'roles/ENT_1/role_info.json').read_text()))
        profile = json.loads((root / 'objects/OBJ_1/object_info.json').read_text())
        self.assertEqual(profile['object_name'], '书')
        def assert_no_evidence(value):
            if isinstance(value, dict):
                self.assertNotIn('evidence', value)
                for child in value.values():
                    assert_no_evidence(child)
            elif isinstance(value, list):
                for child in value:
                    assert_no_evidence(child)
        for file in root.rglob('*'):
            if file.suffix == '.json':
                assert_no_evidence(json.loads(file.read_text()))
            elif file.suffix == '.jsonl':
                for line in file.read_text().splitlines():
                    assert_no_evidence(json.loads(line))
        internal = harness.snapshots()
        self.assertEqual(internal['world']['objects']['OBJ_1']['evidence'][0]['passage_ids'], ['C1_P1'])
        self.assertTrue(internal['world']['events'][0]['state_delta']['objects'][0]['evidence'][0]['quote'])
        self.assertTrue(internal['information']['initial_state']['objects']['OBJ_1']['evidence'])
        requests = len(server.requests)
        self.assertEqual(requests, 8)  # 两次全文、四次分块、排时、独立复核。
        self.assertEqual(harness.run_pipeline(server, chunks=1), 0)
        self.assertEqual(len(server.requests), requests)

    def test_resume_preserves_sparse_delta_without_repeating_completed_request(self):
        harness, server = self.make_harness(), DynamicServer()
        original, interrupted = NovelExtractStorage.snapshot, []
        def stop(storage, stage, payload):
            original(storage, stage, payload)
            if stage == 'job:chunk.1.consequences' and payload.get('status') == 'complete' and not interrupted:
                interrupted.append(True)
                raise KeyboardInterrupt
        self.assertEqual(harness.run_pipeline(server, text=TEXT,
            hook=patch.object(NovelExtractStorage, 'snapshot', stop)), 130)
        self.assertEqual(harness.run_pipeline(server), 0)
        self.assertEqual(len(server.requests), 8)
        saved = harness.snapshots()['world']['events'][0]['state_delta']['objects'][0]['set']
        self.assertEqual(saved, {'holder': reference('ENT_2')})

    def test_conflict_feedback_repairs_delta_without_rebuilding_correct_timeline(self):
        class ConflictServer(DynamicServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job.endswith('.consequences'):
                    result['state_deltas']['EVT_C1_E2']['objects'] = [object_delta(holder=reference('ENT_1'))]
                if job == 'global.review.1':
                    result['correction'] = {'event_updates': {'EVT_C1_E2': {
                        'state_delta': dynamic_world().events[1].state_delta.model_dump(mode='json')}}}
                return result
        harness, server = self.make_harness(), ConflictServer()
        self.assertEqual(harness.run_pipeline(server, text=TEXT), 0, harness.snapshots().get('failures'))
        feedback = server.contexts['global.review.1']['last_delivery']['review']['program_feedback']
        self.assertTrue(any('同时更新冲突' in row for row in feedback), feedback)
        self.assertEqual([job for job, _, _ in server.requests if job.startswith('global.timeline')], ['global.timeline.0'])
        self.assertEqual(len(server.requests), 9)

    def test_global_check_cannot_accept_empty_initial_inventory_as_complete(self):
        class MissingInitialServer(DynamicServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job == 'foundation.extract':
                    result['information']['initial_state']['objects'] = {}
                if job == 'global.review.1':
                    result['information'] = {'initial_state': initial_state().model_dump(mode='json')}
                return result
        harness, server = self.make_harness(), MissingInitialServer()
        self.assertEqual(harness.run_pipeline(server, text=TEXT), 0, harness.snapshots().get('failures'))
        feedback = server.contexts['global.review.1']['last_delivery']['review']['program_feedback']
        self.assertTrue(any('initial_state.objects漏填' in row for row in feedback), feedback)
        self.assertEqual([job for job, _, _ in server.requests if job.startswith('global.timeline')], ['global.timeline.0'])
        self.assertEqual(len(server.requests), 9)


if __name__ == '__main__':
    unittest.main()
