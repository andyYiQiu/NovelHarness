"""正式79只读回放：锁定已恢复的结构/输出能力，并拒绝旧的空泛时间确认。"""
from contextlib import closing
import json
import sqlite3
import unittest

from common.novel_extract.NovelExtractReader import chunk_text
from common.novel_extract.NovelExtractRuntime import ROOT, digest
from common.novel_extract.NovelExtractSchema import Review, World, time_comparisons, time_dependencies, timeline_errors
from exporters.NovelExtractExporter import NovelExtractExporter
from workflows.NovelExtractWorkflow import NovelExtractWorkflow, ReviewState


class ReadOnlyStorage:
    def __init__(self, source, information, confirmations):
        self.values = {'source': source, 'review_state': {'confirmed_times': confirmations}}
        self.snapshots = {'information': information}

    def get(self, key, default=None):
        return self.values.get(key, default)

    def latest(self, key, default=None):
        return self.snapshots.get(key, default)


class Formal79NonRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database = ROOT / '.novel_agent_work/祝福_gpt4o_正式79.sqlite3'
        cls.repository = ROOT / 'repository/祝福_gpt4o_正式79'
        if not cls.database.exists() or not cls.repository.exists():
            raise unittest.SkipTest('正式79历史库或repository不在当前环境')
        with closing(sqlite3.connect(cls.database.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
            cls.meta = {key: json.loads(value) for key, value in db.execute('select key,value from meta')}
            cls.world = World.model_validate(json.loads(db.execute(
                "select payload from snapshots where stage='world' order by id desc limit 1").fetchone()[0]))
            cls.information = json.loads(db.execute(
                "select payload from snapshots where stage='information' order by id desc limit 1").fetchone()[0])
            deliveries = [json.loads(row[0]) for row in db.execute(
                "select payload from snapshots where stage='job:global.review.0' order by id")]
        cls.old_review = Review.model_validate(next(
            row['result'] for row in reversed(deliveries) if row.get('status') == 'complete'))

    def test_formal79_structural_capacity_is_preserved(self):
        self.assertEqual(len(self.world.events), 31)
        self.assertEqual(len(self.world.time_nodes), 9)
        self.assertFalse(timeline_errors(self.world))
        self.assertTrue(all(node.order is not None for node in self.world.time_nodes))
        self.assertTrue(all(event.event_order is not None for event in self.world.events))
        # 同序等价类、相邻序组和相邻节点构成覆盖全部事件的传递链，
        # 不再把同一全序展开为151个重复事件对。
        self.assertEqual(len(time_comparisons(self.world)), len(self.world.events) - 1)

    def test_current_exporter_keeps_formal79_repository_contract(self):
        confirmations = {key: digest(value) for key, value in time_dependencies(self.world).items()}
        projected, excluded = NovelExtractExporter(ReadOnlyStorage(
            self.meta['source'], self.information, confirmations)).project(self.world)
        self.assertFalse(excluded)
        self.assertEqual(len(projected), 39)
        self.assertEqual(set(projected), {
            str(path.relative_to(self.repository)) for path in self.repository.rglob('*') if path.is_file()})
        for relative, expected in projected.items():
            path = self.repository / relative
            if path.suffix == '.jsonl':
                actual = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
            else:
                actual = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(actual, expected, relative)

    def test_old_time_rows_do_not_auto_confirm_new_review(self):
        chunks = chunk_text(self.meta['input_text'])
        state = ReviewState()
        NovelExtractWorkflow().update_time_confirmations(
            state, self.old_review, self.world, self.world, chunks)
        self.assertFalse(state.confirmed_times)


if __name__ == '__main__':
    unittest.main()
