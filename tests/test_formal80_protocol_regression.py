"""正式80旧回复只读回放：旧证明表不能冒充新的全文时间审读。"""
from contextlib import closing
import json
import sqlite3
import unittest

from common.novel_extract.NovelExtractReader import chunk_text
from common.novel_extract.NovelExtractRuntime import ROOT
from common.novel_extract.NovelExtractSchema import Review, World
from workflows.NovelExtractWorkflow import NovelExtractWorkflow, ReviewState


class Formal80ProtocolRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database = ROOT / '.novel_agent_work/祝福_gpt4o_正式80.sqlite3'
        if not cls.database.exists():
            raise unittest.SkipTest('正式80历史库不在当前环境')
        with closing(sqlite3.connect(cls.database.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
            meta = {key: json.loads(value) for key, value in db.execute('select key,value from meta')}
            cls.chunks = chunk_text(meta['input_text'])
            cls.world = World.model_validate(json.loads(db.execute(
                "select payload from snapshots where id=151 and stage='world'").fetchone()[0]))
            # 第61次调用是22条笼统confirmed=false，正式80随后把它们写成time_doubts并重排。
            response = json.loads(db.execute('select response from calls where id=61').fetchone()[0])
            cls.review = Review.model_validate(json.loads(response['choices'][0]['message']['content']))

    def test_old_reply_does_not_create_new_time_confirmation(self):
        workflow = NovelExtractWorkflow()
        state = ReviewState(time_placements=[event.id for event in self.world.events])
        before = self.world.model_dump(mode='json')
        workflow.update_time_confirmations(
            state, self.review.model_copy(deep=True), self.world, self.world, self.chunks)
        self.assertFalse(state.confirmed_times)
        self.assertEqual(self.world.model_dump(mode='json'), before)

    def test_old_proof_rows_are_not_public_review_protocol(self):
        fields = Review.model_json_schema()['properties']
        self.assertNotIn('time_checks', fields)
        self.assertFalse(self.review.time_valid)


if __name__ == '__main__':
    unittest.main()
