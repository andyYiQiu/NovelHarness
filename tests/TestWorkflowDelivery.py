"""离线验证小说输入到 repository、SQLite 的顺序交接及恢复。"""
from contextlib import closing
from hashlib import sha256
import sqlite3
import unittest
from unittest.mock import patch

import test_harness as fixtures
from test_dynamic_assets import DynamicServer, TEXT
from storage.novel_extract.NovelExtractStorage import NovelExtractStorage
from tools import ImportRepository as importer
from tools.RepositoryReader import RepositoryReader


class WorkflowDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.harness = fixtures.HarnessTests(methodName='runTest')
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        self.database = self.harness.directory / 'public.sqlite3'

    def run_delivery(self, server, *extra_args):
        return self.harness.run_pipeline(server, chunks=1, text=TEXT, extra_args=[
            '--database', str(self.database), *extra_args])

    def public_hashes(self):
        return {str(path.relative_to(self.harness.output)): sha256(path.read_bytes()).hexdigest()
                for path in self.harness.output.rglob('*') if path.is_file()}

    def test_complete_pipeline_and_repeat_without_model_calls_or_database_changes(self):
        server = DynamicServer()
        self.assertEqual(self.run_delivery(server), 0)
        self.assertEqual(len(server.requests), 8)
        with RepositoryReader(self.database) as reader:
            self.assertTrue(reader.get_event_context('EVT_C1_E1')['participants'])
            self.assertTrue(reader.get_state_before('EVT_C1_E1')['objects'])
            tables = {row[0] for row in reader.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(len(tables), 13)
            self.assertNotIn('event_embeddings', tables)
        before, database_before = self.public_hashes(), self.database.read_bytes()
        self.assertEqual(self.run_delivery(server), 0)
        self.assertEqual(len(server.requests), 8)
        self.assertEqual(before, self.public_hashes())
        self.assertEqual(database_before, self.database.read_bytes())
        self.assertFalse(list(self.harness.directory.glob('*.faiss*')))

    def test_partial_never_imports(self):
        class OpenIssue(DynamicServer):
            def reply(self, job, attempt, context):
                result = super().reply(job, attempt, context)
                if job.startswith('global.review.'):
                    result.update(is_valid=False, issues=['背景习俗仍待核实'])
                return result

        with patch.object(importer, 'import_repository', side_effect=AssertionError('partial不能入库')):
            self.assertEqual(self.run_delivery(OpenIssue()), 2)
        self.assertTrue(self.harness.output.exists())
        self.assertFalse(self.database.exists())

    def test_import_failure_resumes_after_complete_without_reextraction(self):
        server = DynamicServer()
        with patch.object(importer, 'write_database', side_effect=OSError('模拟入库失败')):
            self.assertEqual(self.run_delivery(server), 1)
        self.assertEqual(NovelExtractStorage.read_status(self.harness.workspace)['status'], 'complete')
        self.assertFalse(self.database.exists())
        before = self.public_hashes()
        self.assertEqual(self.run_delivery(server), 0)
        self.assertEqual(len(server.requests), 8)
        self.assertEqual(before, self.public_hashes())

    def test_interruption_after_database_publication_reuses_existing_database(self):
        server = DynamicServer()
        publish = importer.os.link

        def interrupt(source, target):
            publish(source, target)
            raise OSError('模拟发布完成后中断')

        with patch.object(importer.os, 'link', side_effect=interrupt):
            self.assertEqual(self.run_delivery(server), 1)
        self.assertTrue(self.database.exists())
        before = self.database.read_bytes()
        self.assertEqual(self.run_delivery(server), 0)
        self.assertEqual(before, self.database.read_bytes())
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

    def test_preflight_checks_delivery_without_writing(self):
        server = DynamicServer()
        self.assertEqual(self.run_delivery(server, '--preflight'), 0)
        self.assertFalse(server.requests)
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
        for name in ('work.sqlite3', 'source.txt', 'config.json', 'repository/public.sqlite3', 'logs/public.sqlite3'):
            self.database = self.harness.directory / name
            with self.subTest(path=name):
                self.assertEqual(self.run_delivery(server), 1)
                self.assertFalse(server.requests)
        self.assertFalse(self.harness.workspace.exists())

    def test_existing_legacy_table_is_preserved_without_loading_vector_tools(self):
        server = DynamicServer()
        self.assertEqual(self.run_delivery(server), 0)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('CREATE TABLE event_embeddings(task_id TEXT, vector BLOB)')
            db.execute('INSERT INTO event_embeddings VALUES (?, ?)', ('EVT_C1_E1', b'legacy-vector'))
        before = self.database.read_bytes()
        self.assertEqual(self.run_delivery(server), 0)
        self.assertEqual(before, self.database.read_bytes())
        self.assertEqual(len(server.requests), 8)


if __name__ == '__main__':
    unittest.main()
