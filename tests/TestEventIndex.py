"""固定向量只验证存储/检索通路；真实语义能力另用本地 Jina 样例验证。"""
from contextlib import closing
from importlib.util import find_spec
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

if any(find_spec(name) is None for name in ('faiss', 'numpy', 'torch')):
    raise unittest.SkipTest('事件索引测试需先安装根目录 requirements.txt')

import numpy as np
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from TestRepositoryImport import repository_fixture, save_completion
from tools import EventIndex as indexing
import faiss
from tools.database.Database import create_import_engine
from tools.database.table import EventEmbedding
from tools.ImportRepository import import_repository
from tools.JinaEmbedding import JinaEmbedding, model_revision


class FixedEmbedding:
    profile = {"embedding_model": "offline-fixture", "model_revision": "fixture-v1",
               "embedding_task": "text-matching", "dimension": 3}

    def __init__(self):
        self.calls = []

    def encode(self, texts, *, batch_size=16):
        self.calls.append(list(texts))
        # 故意让相似结果的事件编号排序不同于得分排序。
        vectors = {"E1发生": [1, 0, 0], "E2发生": [0, 1, 0], "E3发生": [.9, .1, 0],
                   "新描述": [1, 0, 0], "E1改写": [1, .1, 0]}
        return np.array([vectors[text] for text in texts], dtype="float32")


class EventIndexTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        repository, workspace = self.root / "repository", self.root / "work.sqlite3"
        for name, value in repository_fixture().items():
            path = repository / name
            path.parent.mkdir(parents=True, exist_ok=True)
            content = ("\n".join(json.dumps(row, ensure_ascii=False) for row in value)
                       if path.suffix == ".jsonl" else json.dumps(value, ensure_ascii=False))
            path.write_text(content)
        save_completion(workspace, repository)
        self.database, self.index = self.root / "novel.sqlite3", self.root / "events.faiss"
        import_repository(repository, self.database, workspace=workspace)
        self.embedder = FixedEmbedding()
        self.original = self.facts()

    def facts(self):
        with closing(sqlite3.connect(self.database)) as connection:
            tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
                      if row[0] != "event_embeddings"]
            return {table: connection.execute(f'SELECT * FROM "{table}"').fetchall() for table in tables}

    def build(self, **kwargs):
        return indexing.build_index(self.database, self.index, self.embedder, **kwargs)

    def test_vectors_persist_and_semantic_hits_keep_explicit_ids_and_rank(self):
        result = self.build(batch_size=2)
        self.assertEqual((result['events'], result['encoded']), (3, 3))
        self.assertEqual([len(call) for call in self.embedder.calls], [2, 1])
        engine = create_import_engine(self.database)
        try:
            with Session(engine) as session:
                rows = session.scalars(select(EventEmbedding).order_by(EventEmbedding.vector_id)).all()
                self.assertEqual([row.task_id for row in rows], ['E1', 'E2', 'E3'])
                self.assertTrue(all(len(row.vector) == 12 for row in rows))
                self.assertTrue(all(np.isclose(np.linalg.norm(np.frombuffer(row.vector, dtype='<f4')), 1) for row in rows))
        finally:
            engine.dispose()
        with indexing.EventIndex(self.database, self.index) as reader:
            hits = reader.search_event('E1', k=20)
            self.assertEqual([hit['task_id'] for hit in hits], ['E3', 'E2'])
            self.assertGreater(hits[0]['score'], hits[1]['score'])
            self.assertEqual(hits[0]['event']['todo'], 'E3发生')
            self.assertEqual(hits[0]['participants'][0]['role']['role_code'], 'R1')
            hits = reader.search_text('新描述', self.embedder, k=2)
            self.assertEqual([hit['task_id'] for hit in hits], ['E1', 'E3'])
        self.assertEqual(self.facts(), self.original)

    def test_noncontiguous_vector_ids_are_used_instead_of_row_positions(self):
        self.build()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('UPDATE event_embeddings SET vector_id=vector_id*10')
        self.index = self.root / 'remapped.faiss'
        self.build()
        with indexing.EventIndex(self.database, self.index) as reader:
            self.assertEqual([(r['vector_id'], r['task_id']) for r in reader.search_event('E1')],
                             [(30, 'E3'), (20, 'E2')])
        self.assertEqual(len(self.embedder.calls), 1)

    @unittest.skipUnless(os.environ.get('NOVEL_JINA_MODEL_PATH'), '真实集成测试需指定本地模型目录')
    def test_real_model_build_and_query_in_same_process(self):
        from TestEventEmbeddingSemantics import CASES

        texts = dict(zip(('E1', 'E3', 'E2'), CASES[1]))
        with closing(sqlite3.connect(self.database)) as db, db:
            for key, text in texts.items():
                db.execute("UPDATE events SET todo=?,data_json=json_set(data_json,'$.todo',?) WHERE task_id=?",
                           (text, text, key))
        encoder = JinaEmbedding(os.environ['NOVEL_JINA_MODEL_PATH'])
        self.addCleanup(encoder.close)
        self.assertEqual(indexing.build_index(self.database, self.index, encoder)['encoded'], 3)
        with indexing.EventIndex(self.database, self.index) as reader:
            hits = reader.search_event('E1', k=2)
            self.assertEqual([row['task_id'] for row in hits], ['E3', 'E2'])
            self.assertEqual(hits[0]['participants'][0]['role']['role_code'], 'R1')
            self.assertEqual(reader.search_text(texts['E1'], encoder, k=1)[0]['task_id'], 'E1')

    def test_repeated_build_and_lost_index_reuse_vectors_without_model_call(self):
        self.build()
        self.embedder.encode = Mock(side_effect=AssertionError('不能重复编码'))
        self.assertEqual(self.build()['encoded'], 0)
        self.index.unlink()
        self.assertEqual(self.build()['encoded'], 0)
        with indexing.EventIndex(self.database, self.index) as reader:
            self.assertEqual(reader.search_event('E1', 1)[0]['task_id'], 'E3')

    def test_interrupted_encoding_keeps_only_successful_batches_and_resumes(self):
        encode = self.embedder.encode
        def interrupt(texts, **kwargs):
            if texts == ['E2发生']:
                raise RuntimeError('模拟编码中断')
            return encode(texts, **kwargs)
        with patch.object(self.embedder, 'encode', side_effect=interrupt):
            with self.assertRaisesRegex(RuntimeError, '模拟编码中断'):
                self.build(batch_size=1)
        self.assertFalse(self.index.exists())
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute('SELECT task_id FROM event_embeddings').fetchall(), [('E1',)])
        self.assertEqual(self.build(batch_size=1)['encoded'], 2)
        self.assertEqual(self.embedder.calls, [['E1发生'], ['E2发生'], ['E3发生']])
        self.assertEqual(self.facts(), self.original)

    def test_missing_receipt_is_unusable_but_recoverable_without_recoding(self):
        publish = indexing.publish_file
        def interrupt(path, content):
            if path.suffix == '.json':
                raise OSError('模拟发布中断')
            publish(path, content)
        with patch.object(indexing, 'publish_file', side_effect=interrupt):
            with self.assertRaisesRegex(OSError, '模拟发布中断'):
                self.build()
        with self.assertRaises(FileNotFoundError):
            indexing.EventIndex(self.database, self.index)
        self.embedder.encode = Mock(side_effect=AssertionError('不能重复编码'))
        self.assertEqual(self.build()['encoded'], 0)

    def test_changed_text_invalidates_loaded_index_and_only_reencodes_changed_event(self):
        self.build()
        with indexing.EventIndex(self.database, self.index) as reader:
            with closing(sqlite3.connect(self.database)) as db, db:
                db.execute("UPDATE events SET todo='E1改写', data_json=json_set(data_json,'$.todo','E1改写') WHERE task_id='E1'")
            with self.assertRaisesRegex(ValueError, '已变化'):
                reader.search_event('E1')
        with self.assertRaisesRegex(ValueError, '已变化'):
            self.build()
        self.index = self.root / 'updated.faiss'
        self.assertEqual(self.build()['encoded'], 1)
        self.assertEqual(self.embedder.calls[-1], ['E1改写'])
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT vector_id FROM event_embeddings WHERE task_id='E1'").fetchone(), (1,))

    def test_wrong_encoding_space_and_corrupt_index_are_rejected(self):
        self.build()
        other = FixedEmbedding()
        other.profile = {**other.profile, 'embedding_task': 'retrieval.query'}
        with self.assertRaisesRegex(ValueError, '另一套'):
            indexing.build_index(self.database, self.root/'other.faiss', other)
        with indexing.EventIndex(self.database, self.index) as reader:
            with self.assertRaisesRegex(ValueError, '编码配置不同'):
                reader.search_text('新描述', other)
        self.index.write_bytes('损坏文件'.encode())
        with patch.object(faiss, 'deserialize_index', side_effect=AssertionError('不能加载损坏数据')):
            with self.assertRaisesRegex(ValueError, '哈希不一致'):
                indexing.EventIndex(self.database, self.index)

    def test_invalid_vectors_do_not_commit_a_batch(self):
        for vector in (np.zeros((3, 3)), np.ones((2, 3)), np.full((3, 3), np.nan)):
            with self.subTest(shape=vector.shape), patch.object(self.embedder, 'encode', return_value=vector):
                with self.assertRaises(ValueError):
                    self.build()
                with closing(sqlite3.connect(self.database)) as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM event_embeddings').fetchone()[0], 0)
        self.assertEqual(self.facts(), self.original)

    def test_changed_input_during_encoding_cannot_publish_stale_vectors(self):
        encode = self.embedder.encode
        def change(texts, **kwargs):
            with closing(sqlite3.connect(self.database)) as db, db:
                db.execute("UPDATE events SET todo='E1改写', data_json=json_set(data_json,'$.todo','E1改写') WHERE task_id='E1'")
            return encode(texts, **kwargs)
        with patch.object(self.embedder, 'encode', side_effect=change):
            with self.assertRaisesRegex(ValueError, '编码期间事件已变化'):
                self.build()
        self.assertFalse(self.index.exists())

    def test_inconsistent_event_json_and_bad_query_are_rejected_before_encoding(self):
        self.build()
        self.embedder.encode = Mock(side_effect=AssertionError('不能编码非法查询'))
        with indexing.EventIndex(self.database, self.index) as reader:
            for text, k in (('', 1), ('新描述', 0), ('新描述', True)):
                with self.subTest(text=text, k=k), self.assertRaises(ValueError):
                    reader.search_text(text, self.embedder, k)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE events SET todo='E1改写' WHERE task_id='E1'")
        with self.assertRaisesRegex(ValueError, '查询列与原 JSON 不一致'):
            indexing.build_index(self.database, self.root/'new.faiss', self.embedder)

    def test_foreign_key_and_vector_length_are_enforced(self):
        engine = create_import_engine(self.database)
        try:
            for task, vector in (('missing', b'\0' * 12), ('E1', b'\0')):
                with self.subTest(task=task), Session(engine) as session, self.assertRaises(IntegrityError):
                    session.add(EventEmbedding(task_id=task, **self.embedder.profile,
                                               text_hash='hash', vector=vector))
                    session.commit()
        finally:
            engine.dispose()

    def test_cli_event_query_needs_no_model_and_missing_database_is_not_created(self):
        self.build()
        tool = Path(indexing.__file__).resolve()
        result = subprocess.run([sys.executable, '-B', str(tool), 'search', '--database', str(self.database),
                                 '--index', str(self.index), '--event', 'E1', '--k', '1'],
                                cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)[0]['task_id'], 'E3')
        missing = self.root/'missing.sqlite3'
        with self.assertRaisesRegex(ValueError, '数据库不存在'):
            indexing.build_index(missing, self.index, self.embedder)
        self.assertFalse(missing.exists())

    def test_old_database_without_embedding_table_is_supported(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('DROP TABLE event_embeddings')
        self.assertEqual(self.build()['encoded'], 3)
        self.assertEqual(self.facts(), self.original)

    def test_jina_adapter_explicitly_uses_semantic_task_and_fixed_dimension(self):
        adapter = object.__new__(JinaEmbedding)
        adapter.model = Mock()
        adapter._encode_local(['他辞去了工作。', '她向雇主提出离职。'], batch_size=2)
        adapter.model.encode.assert_called_once_with(
            ['他辞去了工作。', '她向雇主提出离职。'], task='text-matching', truncate_dim=512,
            normalize_embeddings=True, batch_size=2, show_progress_bar=False)

    def test_model_revision_tracks_code_as_well_as_weights(self):
        model = self.root/'model'
        model.mkdir()
        (model/'model.safetensors').write_bytes(b'fixture')
        (model/'implementation.py').write_text('version = 1')
        before = model_revision(model)
        (model/'implementation.py').write_text('version = 2')
        self.assertNotEqual(before, model_revision(model))


if __name__ == '__main__':
    unittest.main()
