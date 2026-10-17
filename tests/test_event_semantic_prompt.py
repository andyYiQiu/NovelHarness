"""离线锁定事件语义提示契约；不调用模型，也不把关键词当语义评分。"""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ROOT / 'prompts' / 'novel_extract'


def read(name):
    return (PROMPTS / name).read_text(encoding='utf-8')


def same_occurrence(left, right):
    """人工验收反例使用的四项契约；生产语义仍由模型依据原文判断。"""
    keys = ('actor', 'occurrence', 'start_stage', 'occurrence_layer')
    return all(left[key] == right[key] for key in keys)


class EventSemanticPromptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = read('NovelContentPolicy.md')
        cls.extraction = read('NovelExtractionPrompt.md')
        cls.review = read('NovelCheckPrompt.md')

    def test_occurrence_identity_is_shared_by_extract_and_review(self):
        for dimension in ('实际主体', '发生或结果', '实际开始阶段', '发生层次'):
            self.assertIn(dimension, self.policy)
        self.assertIn('共享四项发生签名', self.extraction)
        # Review 不再让模型重抄签名，但仍须依同一组语义边界
        # 区分实际发生、讲述、获知和意图。
        for dimension in ('实际主体', '实际发生', '开始阶段', '叙事层'):
            self.assertIn(dimension, self.review)
        self.assertNotIn('candidate_checks', self.review)

    def test_actual_death_cannot_be_covered_by_learning_the_news(self):
        death = dict(actor='甲', occurrence='死亡', start_stage='死亡时', occurrence_layer='实际发生')
        learned = dict(actor='乙', occurrence='获知甲死亡', start_stage='后来', occurrence_layer='获知')
        self.assertFalse(same_occurrence(death, learned))
        self.assertIn('人物死亡不能由他人获知死讯代替', self.policy)

    def test_request_cannot_be_covered_by_later_completion(self):
        request = dict(actor='甲', occurrence='申请捐献', start_stage='次日', occurrence_layer='实际发生')
        completed = dict(actor='甲', occurrence='完成捐献', start_stage='近一年后', occurrence_layer='实际发生')
        self.assertFalse(same_occurrence(request, completed))
        self.assertIn('申请或打算不能代替后来完成', self.policy)

    def test_same_actual_occurrence_across_blocks_is_mergeable(self):
        first = dict(actor='甲', occurrence='遭抢劫', start_stage='早年',
                     occurrence_layer='实际发生', evidence_status='传闻')
        retold = dict(actor='甲', occurrence='遭抢劫', start_stage='早年',
                      occurrence_layer='实际发生', evidence_status='后来由原文确认')
        self.assertTrue(same_occurrence(first, retold))
        self.assertIn('只合并同一实际发生的重复记录', self.review)
        self.assertIn('最终以原文为准', self.review)

    def test_core_chain_and_two_pass_scope_are_preserved(self):
        self.assertIn('从结局和转折回查不可缺的前因、实施与后果', self.policy)
        self.assertIn('不设数量配额', self.policy)
        self.assertIn('可替代的准备动作和紧接反应', self.policy)
        self.assertIn('作为相关核心事件的证据或结果', self.policy)
        self.assertIn('实际逃离、实施或完成', self.policy)
        self.assertIn('逃离、被逐、脱离控制和身份转变', self.policy)
        self.assertIn('寒暄、追问、复述、赔罪', self.policy)
        self.assertIn('first_pass', self.extraction)
        self.assertIn('second_pass', self.extraction)
        self.assertIn('补齐关键言语、关系、信息传递', self.extraction)
        self.assertIn('consequences', self.extraction)

    def test_prompts_remain_compact(self):
        self.assertLessEqual(len(self.policy.splitlines()), 15)
        self.assertLessEqual(len(self.extraction.splitlines()), 20)
        self.assertLessEqual(len(self.review.splitlines()), 32)


if __name__ == '__main__':
    unittest.main()
