"""Agent repair-loop regressions; all model replies are fixed in memory."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pydantic import BaseModel

from agents.NovelExtractAgent import NovelExtractAgent
from common.novel_extract.NovelExtractSchema import (
    Correction, Entity, EventMerge, Foundation, FoundationFullReview, Review, merge_bundle_errors,
)
from common.novel_extract.NovelExtractRuntime import DeliveryStructureError, JobFailed


class Catalog(BaseModel):
    world_traits: dict[str, str]


class Delivery(BaseModel):
    catalog: Catalog


class MemoryStorage:
    def __init__(self):
        self.values = {}

    def latest(self, stage, default=None):
        return deepcopy(self.values.get(stage, default))

    def snapshot(self, stage, value):
        self.values[stage] = deepcopy(value)


class MemoryLog:
    def __init__(self):
        self.rows = []

    def write(self, kind, **data):
        self.rows.append((kind, data))


class FixedAssistant:
    responses = []
    tasks = []
    runs = 0

    def __init__(self, **_):
        pass

    async def run(self, task):
        type(self).tasks.append(task)
        index = type(self).runs
        type(self).runs += 1
        content = type(self).responses[min(index, len(type(self).responses) - 1)]
        return SimpleNamespace(messages=[SimpleNamespace(content=content)])

    async def save_state(self):
        return {'completed_calls': type(self).runs}

    async def load_state(self, _):
        return None


class AgentRepairResilienceTests(unittest.TestCase):
    def setUp(self):
        FixedAssistant.responses = []
        FixedAssistant.tasks = []
        FixedAssistant.runs = 0
        self.storage = MemoryStorage()
        self.log = MemoryLog()
        self.agent = NovelExtractAgent(self.storage, object(), self.log, repairs=5)

    def run_agent(self, schema=Delivery, validate=lambda _: [], *,
                  job='foundation.extract', role='extractor', context=None):
        context = {'source': 'fixed'} if context is None else context
        with patch('agents.NovelExtractAgent.AssistantAgent', FixedAssistant):
            return asyncio.run(self.agent.run(job, role, 'instructions', context, schema, validate))

    def test_validation_repair_is_compact_but_extraction_repair_keeps_source_context(self):
        invalid = json.dumps({'world_traits': {'WORLD_1': '习俗'}, 'catalog': {}},
                             ensure_ascii=False)
        valid = json.dumps({'catalog': {'world_traits': {'WORLD_1': '习俗'}}},
                           ensure_ascii=False)
        context = {
            'source_chunks': [{'chunk': 1, 'text': '不应在校验修复请求中重复的原文标记'}],
            'world': {'events': {'E1': {'description': '不应重复的世界状态标记'}}},
        }
        FixedAssistant.responses = [invalid, valid]

        self.run_agent(job='global.review.compact', role='check', context=context)

        self.assertEqual(FixedAssistant.runs, 2)
        self.assertEqual(json.loads(FixedAssistant.tasks[0]), context)
        validation_repair = json.loads(FixedAssistant.tasks[1])
        self.assertEqual(set(validation_repair), {'previous_candidate', 'repair', 'errors'})
        self.assertEqual(validation_repair['previous_candidate'], json.loads(invalid))
        self.assertNotIn('source_chunks', FixedAssistant.tasks[1])
        self.assertNotIn('不应重复的世界状态标记', FixedAssistant.tasks[1])

        FixedAssistant.responses = [invalid, valid]
        FixedAssistant.tasks = []
        FixedAssistant.runs = 0
        self.run_agent(job='chunk.1.extract', role='extractor', context=context)

        self.assertEqual(FixedAssistant.runs, 2)
        self.assertIn('source_chunks', FixedAssistant.tasks[1])
        self.assertIn('不应重复的世界状态标记', FixedAssistant.tasks[1])
        self.assertIn('previous_candidate', FixedAssistant.tasks[1])

    def test_full_foundation_repair_keeps_original_source(self):
        FixedAssistant.responses = [
            json.dumps({'summary': '缺少档案', 'issues': []}, ensure_ascii=False),
            json.dumps({'summary': '依据原文补齐', 'issues': [], 'information': {
                'story_overview': '甲出发',
                'story_spine': {'endings': ['甲出发'], 'turning_points': ['甲决定出发'],
                                'causal_chains': [[{'link': '甲出发', 'signature': {
                                    'actual_subjects': ['ENT_1'], 'occurrence': '出发',
                                    'start_anchor': '清晨', 'layer': '实际发生'},
                                    'core_reason': '删除此事会改变行程'}]]},
                'characters': {'ENT_1': {'activity': '主角', 'identity': ['出发者']}},
                'locations': {}, 'background': {'description': '清晨'},
            }}, ensure_ascii=False),
        ]
        result = self.run_agent(FoundationFullReview, job='foundation.check',
                                role='foundation_checker',
                                context={'novel_source': '甲在清晨出发。', 'current': {'information': {}}})

        self.assertEqual(FixedAssistant.runs, 2)
        repair = json.loads(FixedAssistant.tasks[1].split('\n', 1)[0])
        self.assertEqual(repair['novel_source'], '甲在清晨出发。')
        self.assertIn('previous_candidate', FixedAssistant.tasks[1])
        self.assertEqual(result.information.characters['ENT_1'].activity, '主角')

    def test_formal78_style_repair_moves_root_trait_and_completes(self):
        FixedAssistant.responses = [
            json.dumps({'catalog': {'entities': {}, 'locations': {}, 'objects': {}},
                        'information': {}}),
            json.dumps({'catalog': {'entities': {}, 'locations': {}, 'objects': {}},
                        'world_traits': {'WORLD_1': '鲁镇年终祝福'}, 'information': {}},
                       ensure_ascii=False),
        ]

        result = self.run_agent(Foundation)

        self.assertEqual(FixedAssistant.runs, 2)
        self.assertEqual(result.catalog.world_traits, {'WORLD_1': '鲁镇年终祝福'})
        self.assertEqual(self.storage.latest('job:foundation.extract')['status'], 'complete')
        self.assertIn('catalog.world_traits', FixedAssistant.tasks[1])

    def test_validation_path_and_identical_candidate_stop_loss_are_persisted(self):
        first = json.dumps({'world_traits': {'WORLD_1': '习俗'}, 'catalog': {}}, ensure_ascii=False)
        second = json.dumps({'catalog': {}, 'world_traits': {'WORLD_1': '习俗'}}, ensure_ascii=False,
                            sort_keys=True)
        FixedAssistant.responses = [first, second]

        with self.assertRaises(JobFailed):
            self.run_agent()

        saved = self.storage.latest('job:foundation.extract')
        self.assertEqual(FixedAssistant.runs, 2)
        self.assertEqual(saved['status'], 'failed')
        self.assertEqual(saved['repeat_failures'], 2)
        self.assertEqual(saved['candidate']['world_traits'], {'WORLD_1': '习俗'})
        self.assertEqual(saved['state'], {'completed_calls': 2})
        self.assertIn('字段路径 $.catalog.world_traits', saved['error'])
        self.assertIn('将字段 $.world_traits 移到 $.catalog.world_traits', saved['error'])
        self.assertIn('catalog.world_traits', saved['original_error'])
        self.assertIn('字段路径 $.catalog.world_traits', FixedAssistant.tasks[1])

    def test_changed_candidate_gets_another_repair_opportunity(self):
        FixedAssistant.responses = [
            json.dumps({'catalog': {}, 'world_traits': {'WORLD_1': '旧'}}),
            json.dumps({'catalog': {}, 'world_traits': {'WORLD_1': '新'}}),
            json.dumps({'catalog': {'world_traits': {'WORLD_1': '新'}}}),
        ]

        result = self.run_agent()

        self.assertEqual(FixedAssistant.runs, 3)
        self.assertEqual(result.catalog.world_traits, {'WORLD_1': '新'})
        self.assertEqual(self.storage.latest('job:foundation.extract')['status'], 'complete')

    def test_identical_workflow_validation_error_stops_after_two_replies(self):
        candidate = json.dumps({'catalog': {'world_traits': {'WORLD_1': '习俗'}}}, ensure_ascii=False)
        FixedAssistant.responses = [candidate]

        with self.assertRaises(JobFailed):
            self.run_agent(validate=lambda _: ['同一事实矛盾仍未修正'])

        saved = self.storage.latest('job:foundation.extract')
        self.assertEqual(FixedAssistant.runs, 2)
        self.assertEqual(saved['repeat_failures'], 2)
        self.assertEqual(saved['status'], 'failed')
        self.assertIn('同一事实矛盾仍未修正', saved['error'])

    def test_identical_incomplete_merge_bundle_stops_after_two_replies(self):
        candidate = {
            'is_valid': False, 'summary': '合并重复身份', 'issues': [], 'covered_chunks': [],
            'correction': {
                # 故意只交name；Pydantic规范化后会补aliases/description默认值，
                # 但失败候选恢复时仍必须记得它们未被模型显式交付。
                'entities': {'ENT_1': {'name': '甲'}},
                'entity_merges': [{'source': 'ENT_C1_1', 'target': 'ENT_1', 'reason': '同人'}],
            },
            'information': {'characters': {'ENT_1': {
                'activity': '主角', 'nickname': None, 'profile': '完整甲档案', 'gender': None,
                'identity': None, 'age': None, 'personality': None, 'biography': None,
            }}},
        }
        FixedAssistant.responses = [json.dumps(candidate, ensure_ascii=False)]

        def validate(value):
            errors = merge_bundle_errors(value.correction, value.information)
            if errors:
                raise DeliveryStructureError('\n'.join(errors))
            return []

        with self.assertRaises(JobFailed):
            self.run_agent(Review, validate)

        self.assertEqual(FixedAssistant.runs, 2)
        self.assertIn('$.correction.entities.ENT_1.aliases', FixedAssistant.tasks[1])
        saved = self.storage.latest('job:foundation.extract')
        self.assertEqual(saved['status'], 'failed')
        self.assertEqual(saved['stop_reason'], '连续收到语义相同的候选与相同错误，停止机械重发')
        self.assertNotIn('aliases', saved['submitted_candidate']['correction']['entities']['ENT_1'])
        recovered = Review.model_validate(saved['submitted_candidate'])
        self.assertIn('$.correction.entities.ENT_1.aliases',
                      '\n'.join(merge_bundle_errors(recovered.correction, recovered.information)))


if __name__ == '__main__':
    unittest.main()
