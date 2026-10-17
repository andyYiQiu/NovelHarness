"""正式84暴露的交接回归：有效档案字段和时间草稿不能整批丢失。"""
import unittest
from contextlib import closing
import json
import sqlite3

from common.novel_extract.NovelExtractSchema import (
    Event, EventPass, Evidence, Foundation, Register, RepositoryInformation, TimelinePlan, World,
)
from common.novel_extract.NovelExtractRuntime import ROOT
from exporters.NovelExtractExporter import NovelExtractExporter
from workflows.NovelExtractWorkflow import NovelExtractWorkflow


def link(name, stage):
    return {'link': name, 'signature': {
        'actual_subjects': ['ENT_1'], 'occurrence': name,
        'start_anchor': stage, 'layer': '实际发生'},
        'core_reason': '删除此事会改变后来出发的结果'}


class Formal84RecoveryTests(unittest.TestCase):
    def test_second_pass_saved_replies_are_review_proposals(self):
        path = ROOT / '.novel_agent_work/祝福_gpt4o_正式84.sqlite3'
        if not path.exists():
            self.skipTest('正式84历史工作区不在当前环境')
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
            def result(stage):
                row = db.execute('select payload from snapshots where stage=? order by id desc limit 1',
                                 (stage,)).fetchone()
                return EventPass.model_validate(json.loads(row[0])['result'])
            first = result('job:chunk.5.first_pass')
            second = result('job:chunk.5.second_pass')

        additions, proposals = NovelExtractWorkflow.separate_second_pass(first.events, second.events)
        self.assertEqual(additions, [])
        self.assertEqual({(p['original']['id'], p['proposed']['id']) for p in proposals},
                         {('EVT_C5_E1', 'EVT_C5_S1'), ('EVT_C5_E2', 'EVT_C5_S2'),
                          ('EVT_C5_E3', 'EVT_C5_S3')})

    def test_bad_profile_field_does_not_erase_valid_foundation_repair(self):
        initial = Foundation(catalog=Register(
            entities={'ENT_1': {'name': '甲'}}, locations={}, objects={}, world_traits={}), information={
            'story_overview': '甲决定出发并实际动身',
            'story_spine': {'endings': ['甲已经出发'], 'turning_points': ['甲决定出发'],
                            'causal_chains': [[link('甲决定出发', '早晨')]]},
            'characters': {'ENT_1': {'activity': 'npc', 'profile': '甲是故事中的出发者',
                                     'identity': ['出发者']}},
            'locations': {}, 'background': {'description': '原文中的出发环境'},
        })
        submitted = {'summary': '补齐主干', 'audit': {'checked_sections': [
            'characters', 'relations', 'locations', 'objects', 'background',
            'settings', 'story_overview', 'story_spine']},
            'information': {'characters': {'ENT_1': {
                'activity': '甲经历多次转折', 'biography': '甲后来实际出发'}},
                'story_spine': {'endings': ['甲已经出发'], 'turning_points': ['甲决定出发'],
                                'causal_chains': [[link('甲决定出发', '早晨'),
                                                   link('甲实际出发', '中午')]]}}}

        _, repaired, rejected = NovelExtractWorkflow.recover_foundation_fields(initial, submitted)
        information = RepositoryInformation.model_validate(repaired.information)

        self.assertEqual(rejected, ['characters.ENT_1.activity'])
        self.assertEqual(information.characters['ENT_1'].activity, 'npc')
        self.assertEqual(information.characters['ENT_1'].biography, '甲后来实际出发')
        self.assertEqual(len(information.story_spine.causal_chains[0]), 2)
        self.assertEqual(len(initial.information['story_spine']['causal_chains'][0]), 1)

    def test_missing_events_still_prevent_complete_time_but_valid_groups_can_survive(self):
        events = [Event(id=f'E{i}', description=f'甲完成第{i}件事', actor_ids=['ENT_1'],
                        temporal_basis=f'阶段{i}', core_reason='改变故事后续结果',
                        evidence=[Evidence(chunk=1, quote=f'第{i}件事')],
                        epistemic_status='事实', occurrence_layer='实际发生',
                        occurrence_signature={'actual_subjects': ['ENT_1'],
                                              'occurrence': f'第{i}件事',
                                              'start_anchor': f'阶段{i}', 'layer': '实际发生'})
                  for i in range(1, 4)]
        world = World(entities={'ENT_1': {'name': '甲'}}, locations={}, objects={}, world_traits={},
                      events=events)
        plan = TimelinePlan.model_validate({'summary': '按开始时间排列', 'time': {'nodes': [
            {'id': 'N1', 'name': '第一阶段', 'relative': '早于第二阶段',
             'events': [{'id': 'E1', 'order': 1, 'basis': '原文先发生'}]},
            {'id': 'N2', 'name': '第二阶段', 'relative': '晚于第一阶段',
             'events': [{'id': 'E2', 'order': 1, 'basis': '原文随后发生'}]},
        ]}})

        with self.assertRaisesRegex(ValueError, '完整分组缺少事件'):
            NovelExtractWorkflow.apply_time_plan(plan, world, [])
        partial = plan.model_copy(update={'time': plan.time.model_copy(update={'complete': False})})
        saved = NovelExtractWorkflow.apply_time_plan(partial, world, [])

        self.assertEqual([(n.id, n.order) for n in saved.time_nodes], [('N1', 1), ('N2', 2)])
        self.assertEqual([(e.id, e.time_node_id) for e in saved.events],
                         [('E1', 'N1'), ('E2', 'N2'), ('E3', None)])

    def test_formal84_replies_keep_saved_facts_visible_in_partial_projection(self):
        path = ROOT / '.novel_agent_work/祝福_gpt4o_正式84.sqlite3'
        if not path.exists():
            self.skipTest('正式84历史工作区不在当前环境')
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
            def saved(stage):
                row = db.execute('select payload from snapshots where stage=? order by id desc limit 1',
                                 (stage,)).fetchone()
                return json.loads(row[0])
            initial = Foundation.model_validate(saved('foundation.initial'))
            submitted = saved('job:foundation.check')['submitted_candidate']
            world = World.model_validate(saved('world'))
            time_plan = TimelinePlan.model_validate(saved('job:global.timeline.2')['submitted_candidate'])
        _, repaired, rejected = NovelExtractWorkflow.recover_foundation_fields(initial, submitted)
        partial = time_plan.model_copy(update={'time': time_plan.time.model_copy(update={'complete': False})})
        placed = NovelExtractWorkflow.apply_time_plan(partial, world, [])

        class ReadOnlyStorage:
            def get(self, key, default=None):
                return {'source': '祝福_鲁迅', 'review_state': {}}.get(key, default)

            def latest(self, key, default=None):
                return repaired.information if key == 'information' else default

        files, excluded = NovelExtractExporter(ReadOnlyStorage()).project(placed)
        self.assertEqual(rejected, ['characters.ENT_4.activity'])
        self.assertEqual(sum(name.startswith('roles/') for name in files), 8)
        self.assertEqual(len(placed.events), 22)
        self.assertEqual(len(time_plan.time.timeline), 19)
        self.assertEqual(sum(row['kind'] == 'event' for row in excluded), 0)


if __name__ == '__main__':
    unittest.main()
