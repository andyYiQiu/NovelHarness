"""关联查询与状态回放测试；只使用临时数据库，不调用模型。"""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from TestRepositoryImport import repository_fixture, reference, save_completion
from tools.ImportRepository import import_repository
from tools.RepositoryReader import RepositoryReader


class RepositoryReaderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "novel.sqlite3"
        self.documents = repository_fixture()

    def reader(self):
        repository = self.root / "repository"
        for name, value in self.documents.items():
            path = repository / name
            path.parent.mkdir(parents=True, exist_ok=True)
            content = ("\n".join(json.dumps(row, ensure_ascii=False) for row in value)
                       if path.suffix == ".jsonl" else json.dumps(value, ensure_ascii=False))
            path.write_text(content, encoding="utf-8")
        workspace = self.root / "work.sqlite3"
        save_completion(workspace, repository)
        import_repository(repository, self.database, workspace=workspace)
        result = RepositoryReader(self.database)
        self.addCleanup(result.close)
        return result

    def test_context_and_role_history_use_typed_associations(self):
        event = self.documents["timeline/N1_阶段/todolist.json"]["todolist"][0]
        event["object_ids"] = ["O1"]
        event["participants"] *= 2
        reader = self.reader()
        context = reader.get_event_context("E1")
        self.assertEqual(context["event"], event)
        self.assertEqual(context["node"], self.documents["timeline/timeline.json"]["nodes"][0])
        self.assertEqual([row["position"] for row in context["participants"]], [1, 2])
        self.assertEqual(context["participants"][0]["role"], self.documents["roles/R1/role_info.json"])
        self.assertEqual(context["objects"], [self.documents["objects/O1/object_info.json"]])
        self.assertEqual(context["locations"], [self.documents["locations/离线小说.json"]["L1"]])
        self.assertEqual([row["event"]["task_id"] for row in reader.get_role_events("R1")], ["E1", "E2", "E3"])
        self.assertEqual(reader.get_role_events("R3"), [])

    def test_same_group_shares_prestate_and_nonparticipants_are_kept(self):
        reader = self.reader()
        before = reader.get_state_before("E1")
        self.assertEqual(before, reader.get_state_before("E2"))
        self.assertEqual(before["relations"][0]["owner_id"], "R2")
        self.assertEqual(before["relations"][0]["other_id"], "R3")
        self.assertEqual(before["relations"][0]["status"], "unknown")
        self.assertEqual(set(before["objects"]), {"O1"})
        after = reader.get_state_after_group("N1", 1)
        self.assertEqual(after["relations"][0]["status"], "active")
        self.assertEqual(after, reader.get_state_before("E3"))
        self.assertEqual(reader.get_state_before("E1"), before)

    def test_later_group_applies_only_earlier_changes_and_preserves_null(self):
        events = self.documents["timeline/N1_阶段/todolist.json"]["todolist"]
        later = deepcopy(events[1])
        later.update(task_id="E4", order=2)
        later["state_delta"]["objects"] = [
            {"object_id": "O1", "set": {"holder": reference("known", "R1")}, "basis": "交付"}]
        events.append(later)
        state = self.documents["timeline/N1_阶段/state_before.json"]
        state["objects"]["O1"]["condition"] = "完好"
        state["relations"][0]["detail"] = "旧说明"
        events[0]["state_delta"]["relations"][0]["set"]["detail"] = None
        reader = self.reader()
        before = reader.get_state_before("E4")
        self.assertIsNone(before["objects"]["O1"]["condition"])
        self.assertIsNone(before["relations"][0]["detail"])
        self.assertEqual(before["objects"]["O1"]["owner"], reference("known", "R2"))
        self.assertEqual(before["objects"]["O1"]["holder"], reference("none"))
        after = reader.get_state_after_group("N1", 2)
        self.assertEqual(after["objects"]["O1"]["holder"], reference("known", "R1"))
        self.assertIsNone(after["objects"]["O1"]["condition"])

    def test_compatible_simultaneous_changes_merge_and_new_relation_keeps_direction(self):
        events = self.documents["timeline/N1_阶段/todolist.json"]["todolist"]
        events[1]["state_delta"] = deepcopy(events[0]["state_delta"])
        events[1]["state_delta"]["objects"][0]["set"]["location"] = reference("known", "L1")
        events[1]["state_delta"]["relations"].append({
            "owner_id": "R3", "other_id": "R2", "relation": "朋友",
            "set": {"detail": "新认识"}, "basis": "当事人的说明"})
        reader = self.reader()
        after = reader.get_state_after_group("N1", 1)
        self.assertEqual(after["objects"]["O1"]["location"], reference("known", "L1"))
        self.assertEqual(after["relations"], [
            {"owner_id": "R2", "other_id": "R3", "relation": "朋友", "status": "active", "detail": None},
            {"owner_id": "R3", "other_id": "R2", "relation": "朋友", "status": "unknown", "detail": "新认识"}])

    def test_conflicting_simultaneous_changes_fail_without_writing(self):
        events = self.documents["timeline/N1_阶段/todolist.json"]["todolist"]
        events[1]["state_delta"] = deepcopy(events[0]["state_delta"])
        events[1]["state_delta"]["relations"][0]["set"]["status"] = "inactive"
        reader = self.reader()
        database_before = self.database.read_bytes()
        with self.assertRaisesRegex(ValueError, "同时事件.*提交冲突状态"):
            reader.get_state_after_group("N1", 1)
        self.assertEqual(self.database.read_bytes(), database_before)
        self.assertEqual(reader.get_state_before("E1")["relations"][0]["status"], "unknown")

    def test_object_history_retains_sparse_null_update_and_order(self):
        reader = self.reader()
        history = reader.get_object_history("O1")
        self.assertEqual(history["object"], self.documents["objects/O1/object_info.json"])
        self.assertEqual([row["node_order"] for row in history["snapshots"]], [1, 2])
        self.assertEqual(len(history["changes"]), 1)
        change = history["changes"][0]
        self.assertEqual((change["task_id"], change["field"], change["text_value"]), ("E1", "condition", None))
        self.assertEqual((change["node_order"], change["event_order"]), (1, 1))

    def test_read_only_and_explicit_not_found(self):
        with self.assertRaises(sqlite3.OperationalError):
            RepositoryReader(self.database)
        self.assertFalse(self.database.exists())
        reader = self.reader()
        with self.assertRaises(sqlite3.OperationalError):
            reader.connection.execute("DELETE FROM events")
        for call in (lambda: reader.get_event_context("missing"), lambda: reader.get_state_before("missing"),
                     lambda: reader.get_object_history("missing"), lambda: reader.get_state_after_group("N1", 5)):
            with self.assertRaises(KeyError):
                call()
        with RepositoryReader(self.database) as second:
            self.assertEqual(second.get_state_before("E1"), reader.get_state_before("E1"))
        with self.assertRaises(sqlite3.ProgrammingError):
            second.connection.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
