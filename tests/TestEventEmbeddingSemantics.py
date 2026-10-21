"""本地真实模型的少量中文语义反例；显式指定模型目录时运行，不联网。"""
import os
import unittest

from tools.JinaEmbedding import JinaEmbedding


# 先固定样例再运行；第三列与查询有词汇重叠，但实际发生不同。
CASES = [
    ('她违背父母安排，逃离家中，与心爱的人远走他乡。',
     '女子拒绝包办婚姻，和恋人私奔。',
     '她回到家中，接受父母安排，与指定的对象成婚。'),
    ('工人失去了工作，被老板赶出了工厂。',
     '雇主解雇了这名工人，他不得不离开厂里。',
     '工人离开工厂去吃午饭，随后回去继续工作。'),
    ('一场洪水冲毁了全村的房屋，村民从此无家可归。',
     '水灾摧毁村庄，人们失去住所，被迫流离失所。',
     '村民修好了房屋，还加固了河堤以防洪水。'),
    ('他把自己的积蓄全部捐给了学校，帮助贫困孩子继续读书。',
     '为了让穷学生不再失学，他将多年攒下的钱悉数用于助学。',
     '他去学校询问学费，随后把积蓄存入银行留给自己。'),
    ('她得知丈夫死于矿难，带着孩子返回娘家。',
     '丈夫在矿井事故中丧生后，妻子领着孩子投奔父母。',
     '她听说丈夫在矿井工作，带孩子去矿上探望他。'),
    ('小伙发现儿童落水，立刻跳入河中把他救上岸。',
     '少年奋力游向溺水的孩子，将其从水里救出。',
     '小伙在河岸给儿童讲解游泳动作。'),
]


@unittest.skipUnless(os.environ.get('NOVEL_JINA_MODEL_PATH'), '真实模型测试需显式设置本地 NOVEL_JINA_MODEL_PATH')
class EventEmbeddingSemanticsTests(unittest.TestCase):
    def test_paraphrased_occurrence_ranks_above_lexical_distractor(self):
        import numpy as np

        encoder = JinaEmbedding(os.environ['NOVEL_JINA_MODEL_PATH'])
        self.addCleanup(encoder.close)
        vectors = encoder.encode([text for case in CASES for text in case], batch_size=6)
        self.assertEqual(vectors.shape, (18, 512))
        self.assertTrue(np.isfinite(vectors).all())
        for i, (query, positive, negative) in enumerate(CASES):
            a, b, c = vectors[i * 3:i * 3 + 3]
            with self.subTest(query=query):
                self.assertGreater(float(a @ b), float(a @ c),
                                   f'语义同义项未超过文字干扰项：{positive} / {negative}')


if __name__ == '__main__':
    unittest.main()
