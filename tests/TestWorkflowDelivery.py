"""离线验证小说输入到 repository、SQLite 与 FAISS 的顺序交接及恢复。"""
from contextlib import closing
from hashlib import sha256
import os
import sqlite3
import unittest
from unittest.mock import patch

import numpy as np

import test_harness as fixtures
from test_dynamic_assets import DynamicServer, TEXT
from storage.novel_extract.NovelExtractStorage import NovelExtractStorage
from tools import EventIndex as indexing


class FixtureEncoder:
    profile = {'embedding_model': 'offline-fixture', 'model_revision': 'fixture-v1',
               'embedding_task': 'text-matching', 'dimension': 3}

    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def encode(self, texts, *, batch_size=16):
        self.calls.append(list(texts))
        return np.array([[len(text), 1, 1] for text in texts], dtype='float32')


class WorkflowDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.harness = fixtures.HarnessTests(methodName='runTest')
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        self.database = self.harness.directory / 'public.sqlite3'
        self.index = self.database.with_suffix('.faiss')
        self.encoder = FixtureEncoder()

    def run_delivery(self, server, *extra_args):
        with patch('tools.JinaEmbedding.JinaEmbedding', return_value=self.encoder):
            return self.harness.run_pipeline(server, chunks=1, text=TEXT, extra_args=[
                '--database', str(self.database), '--embedding-model', str(self.harness.directory / 'model'),
                *extra_args])

    def public_hashes(self):
        return {str(path.relative_to(self.harness.output)): sha256(path.read_bytes()).hexdigest()
                for path in self.harness.output.rglob('*') if path.is_file()}

    def test_complete_pipeline_and_repeat_reuse_without_model_calls(self):
        server = DynamicServer()
        self.assertEqual(self.run_delivery(server), 0)
        self.assertEqual(len(server.requests), 8)
        self.assertEqual(len(self.encoder.calls), 1)
        with indexing.EventIndex(self.database, self.index) as reader:
            hits = reader.search_event('EVT_C1_E1')
            self.assertEqual(len(hits), 2)
            self.assertTrue(hits[0]['participants'])
            self.assertEqual(len(reader.rows), 3)
        before = self.public_hashes()
        self.assertEqual(self.run_delivery(server), 0)
        self.assertEqual(len(server.requests), 8)
        self.assertEqual(len(self.encoder.calls), 1)
        self.assertEqual(before, self.public_hashes())

    def test_partial_never_imports_or_encodes(self):
        class OpenIssue(DynamicServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job.startswith('global.review.'):
                    result.update(is_valid=False, issues=['背景习俗仍待核实'])
                return result

        self.assertEqual(self.run_delivery(OpenIssue()), 2)
        self.assertTrue(self.harness.output.exists())
        self.assertFalse(self.database.exists())
        self.assertFalse(self.index.exists())
        self.assertFalse(self.encoder.calls)

    def test_encoding_failure_resumes_after_complete_without_reextraction(self):
        server = DynamicServer()
        with patch.object(self.encoder, 'encode', side_effect=RuntimeError('模拟本地编码失败')):
            self.assertEqual(self.run_delivery(server), 1)
        self.assertEqual(NovelExtractStorage.read_status(self.harness.workspace)['status'], 'complete')
        self.assertTrue(self.database.exists())
        self.assertFalse(self.index.exists())
        self.assertEqual(self.run_delivery(server), 0)
        self.assertEqual(len(server.requests), 8)

    def test_interrupted_index_publication_reuses_saved_vectors(self):
        server = DynamicServer()
        publish = indexing.publish_file

        def interrupt(path, content):
            if path.suffix == '.json':
                raise OSError('模拟收据发布中断')
            publish(path, content)

        with patch.object(indexing, 'publish_file', side_effect=interrupt):
            self.assertEqual(self.run_delivery(server), 1)
        self.assertTrue(self.index.exists())
        self.assertEqual(self.run_delivery(server), 0)
        self.assertEqual(len(self.encoder.calls), 1)
        self.assertEqual(len(server.requests), 8)

    def test_changed_public_database_is_not_overwritten_on_resume(self):
        server = DynamicServer()
        self.assertEqual(self.run_delivery(server), 0)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE assets SET name='被外部修改'")
        before = sha256(self.database.read_bytes()).hexdigest()
        self.assertEqual(self.run_delivery(server), 1)
        self.assertEqual(before, sha256(self.database.read_bytes()).hexdigest())
        self.assertEqual(len(server.requests), 8)
        self.assertEqual(len(self.encoder.calls), 1)

    def test_preflight_checks_delivery_without_writing_or_encoding(self):
        server = DynamicServer()
        self.assertEqual(self.run_delivery(server, '--preflight'), 0)
        self.assertFalse(server.requests)
        self.assertFalse(self.encoder.calls)
        self.assertFalse(self.harness.workspace.exists())
        self.assertFalse(self.database.exists())

    def test_new_run_refuses_existing_delivery_target_before_model_requests(self):
        server = DynamicServer()
        self.database.write_bytes('保留已有文件'.encode())
        self.assertEqual(self.run_delivery(server), 1)
        self.assertFalse(server.requests)
        self.assertEqual(self.database.read_bytes(), '保留已有文件'.encode())
        self.assertFalse(self.harness.workspace.exists())

    def test_overlapping_workspace_or_source_paths_are_rejected(self):
        server = DynamicServer()
        for name in ('work.sqlite3', 'source.txt'):
            self.database = self.harness.directory / name
            with self.subTest(path=name):
                self.assertEqual(self.run_delivery(server), 1)
                self.assertFalse(server.requests)
        self.assertFalse(self.harness.workspace.exists())

    def test_missing_model_fails_before_extraction_starts(self):
        server = DynamicServer()
        self.assertEqual(self.harness.run_pipeline(server, chunks=1, text=TEXT, extra_args=[
            '--database', str(self.database), '--embedding-model', str(self.harness.directory / 'missing-model')]), 1)
        self.assertFalse(server.requests)
        self.assertFalse(self.harness.workspace.exists())

    @unittest.skipUnless(os.environ.get('NOVEL_JINA_MODEL_PATH'), '需显式指定本地模型；提取仍用固定回复')
    def test_offline_workflow_with_real_embedding_and_index(self):
        server = DynamicServer()
        arguments = ['--database', str(self.database), '--embedding-model', os.environ['NOVEL_JINA_MODEL_PATH']]
        self.assertEqual(self.harness.run_pipeline(server, chunks=1, text=TEXT, extra_args=arguments), 0)
        with indexing.EventIndex(self.database, self.index) as reader:
            self.assertEqual(len(reader.rows), 3)
            self.assertEqual(reader.index.d, 512)
            self.assertEqual(len(reader.search_event('EVT_C1_E1')), 2)
        with patch('tools.JinaEmbedding.JinaEmbedding.encode', side_effect=AssertionError('不能重复编码')):
            self.assertEqual(self.harness.run_pipeline(server, chunks=1, text=TEXT, extra_args=arguments), 0)
        self.assertEqual(len(server.requests), 8)


if __name__ == '__main__':
    unittest.main()
