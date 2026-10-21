"""公开 repository 导入：保真、确定性拒收、事务与发布边界；不调用模型。"""
from contextlib import closing
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tools import ImportRepository as importer
from tools.database.Database import Base
from tools.database.table import Asset, Event, EventParticipant, RepositoryInfo, TimeNode


def reference(status="unknown", identity=None):
    return {"status": status, "id": identity}


def repository_fixture():
    source = "离线小说"
    documents = {}
    for identity, name in (("R1", "甲"), ("R2", "乙"), ("R3", "未参与者")):
        documents[f"roles/{identity}/role_info.json"] = {
            "role_code": identity, "role_name": name, "source": source, "activity": "npc",
            "nickname": None, "profile": None, "gender": None, "identity": [],
            "age": None, "personality": None, "biography": "有方向和阶段的人物经历"}
    documents["objects/O1/object_info.json"] = {
        "object_code": "O1", "object_name": "信", "source": source,
        "aliases": [], "description": None, "function": "传信", "significance": None}
    documents["locations/离线小说.json"] = {
        "L1": {"location_code": "L1", "location_name": "故乡", "source": source,
               "description": None, "detail": None}}
    documents["worlds/background/general.json"] = {
        "source": source, "world_name": None, "language": None, "description": None, "detail": None}
    documents["worlds/settings/离线小说.jsonl"] = [
        {"term": "习俗一", "nature": None, "detail": []},
        {"term": "习俗二", "nature": ["民俗"], "detail": ["含引号'的描述"]}]
    nodes = []
    for number, event_ids in ((1, ("E1", "E2")), (2, ("E3",))):
        key, folder = f"N{number}", f"N{number}_阶段"
        nodes.append({
            "node_id": key, "order": number, "node_name": "阶段",
            "time": {"relative": "此前" if number == 1 else "之后",
                     "exact_start": None, "exact_end": None, "duration": None},
            "interval_from_previous": None,
            "path": f"{folder}/todolist.json", "state_path": f"{folder}/state_before.json"})
        tasks = [{
            "task_id": identity, "order": 1, "todo": f"{identity}发生", "time": None,
            "epistemic_status": "事实（叙述者所见）" if identity == "E1" else "传闻（未独立证实）",
            "locations": ["故乡"], "participants": [{"role_code": "R1", "role_name": "甲", "original_goal": None}],
            "items": None, "object_ids": [], "required_results": None,
            "state_delta": {"relations": [], "objects": []}
        } for identity in event_ids]
        documents[f"timeline/{folder}/todolist.json"] = {"node_id": key, "todolist": tasks}
        documents[f"timeline/{folder}/state_before.json"] = {
            "node_id": key, "previous_node_id": None if number == 1 else "N1",
            "relations": [{"owner_id": "R2", "other_id": "R3", "relation": "朋友",
                           "status": "unknown" if number == 1 else "active", "detail": None}],
            "objects": {"O1": {"owner": reference("known", "R2"), "holder": reference("none"),
                               "location": reference(), "condition": None}}}
    documents["timeline/N1_阶段/todolist.json"]["todolist"][0]["state_delta"] = {
        "relations": [{"owner_id": "R2", "other_id": "R3", "relation": "朋友",
                       "set": {"status": "active"}, "basis": "事件确认了关系"}],
        "objects": [{"object_id": "O1", "set": {"condition": None}, "basis": "此时状况不明"}]}
    documents["timeline/timeline.json"] = {"source": source, "time_basis": "story", "nodes": nodes}
    return documents


def save_completion(workspace, repository, status="complete"):
    """仅为临时测试产物生成上游 meta 与导出收据。"""
    receipt = {"path": str(repository.resolve()), "files": {
        path.relative_to(repository).as_posix(): sha256(path.read_bytes()).hexdigest()
        for path in repository.rglob("*") if path.is_file()}}
    with closing(sqlite3.connect(workspace)) as connection, connection:
        connection.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", [
            (key, json.dumps(value)) for key, value in
            {"status": status, "output": str(repository.resolve()), "export": receipt}.items()])


class RepositoryImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.database = self.root / "database" / "novel.sqlite3"
        self.documents = repository_fixture()
        self.workspace = self.root / "work.sqlite3"
        self.save()

    def save(self):
        for relative, value in self.documents.items():
            path = self.repository / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.suffix == ".jsonl":
                content = "\n".join(json.dumps(row, ensure_ascii=False) for row in value) + "\n"
            else:
                content = json.dumps(value, ensure_ascii=False)
            path.write_text(content, encoding="utf-8")
        save_completion(self.workspace, self.repository)

    def test_hybrid_storage_preserves_every_original_record(self):
        before = {path: path.read_bytes() for path in self.repository.rglob("*") if path.is_file()}
        workspace_before = self.workspace.read_bytes()
        counts = importer.import_repository(self.repository, self.database, workspace=self.workspace)
        self.assertEqual(self.workspace.read_bytes(), workspace_before)
        self.assertEqual(counts, {"repository_info": 1, "assets": 5, "time_nodes": 2, "events": 3, "state_snapshots": 2,
                                  "event_participants": 3, "event_objects": 0, "event_locations": 3,
                                  "relation_snapshots": 2, "object_snapshots": 2,
                                  "relation_changes": 1, "object_changes": 1, "world_data": 2,
                                  "event_embeddings": 0})
        with closing(sqlite3.connect(self.database)) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(tables, {"repository_info", *counts})
            self.assertEqual(connection.execute("SELECT * FROM repository_info").fetchall(), [("离线小说", "story")])
            for path, raw in connection.execute("SELECT * FROM world_data"):
                self.assertEqual(json.loads(raw), self.documents[path])
            for kind, identity, name, raw in connection.execute("SELECT * FROM assets"):
                original = (self.documents["locations/离线小说.json"][identity] if kind == "location"
                            else self.documents[f"{'roles' if kind == 'role' else 'objects'}/{identity}/{kind}_info.json"])
                self.assertEqual(json.loads(raw), original)
                self.assertEqual(name, original[importer.ASSET_FIELDS[kind][1]])
            for identity, order, name, relative, raw in connection.execute("SELECT * FROM time_nodes ORDER BY node_order"):
                node = self.documents["timeline/timeline.json"]["nodes"][order - 1]
                self.assertEqual((identity, name, json.loads(raw)), (node["node_id"], node["node_name"], node))
                self.assertEqual(relative, node["time"]["relative"])
                original_tasks = self.documents["timeline/" + node["path"]]["todolist"]
                actual_tasks = connection.execute(
                    "SELECT task_id, event_order, todo, epistemic_status, data_json "
                    "FROM events WHERE node_id=? ORDER BY task_id", (identity,))
                for task, (task_id, event_order, todo, status, data) in zip(original_tasks, actual_tasks, strict=True):
                    self.assertEqual((task_id, event_order, todo, status, json.loads(data)),
                                     (task["task_id"], task["order"], task["todo"], task["epistemic_status"], task))
                snapshot = connection.execute("SELECT previous_node_id,data_json FROM state_snapshots WHERE node_id=?",
                                              (identity,)).fetchone()
                original = self.documents["timeline/" + node["state_path"]]
                self.assertEqual(snapshot[0], original["previous_node_id"])
                self.assertEqual(json.loads(snapshot[1]), original)
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

    def test_legacy_events_are_preserved_without_guessing_epistemic_status(self):
        for document in self.documents.values():
            if isinstance(document, dict):
                for event in document.get("todolist", []):
                    event["todo"] += "\n依据性质：" + event.pop("epistemic_status")
        self.save()
        importer.import_repository(self.repository, self.database, workspace=self.workspace)
        with closing(sqlite3.connect(self.database)) as connection:
            for todo, status, raw in connection.execute("SELECT todo, epistemic_status, data_json FROM events"):
                self.assertIsNone(status)
                self.assertNotIn("epistemic_status", json.loads(raw))
                self.assertEqual(todo, json.loads(raw)["todo"])
                self.assertIn("\n依据性质：", todo)

    def test_only_complete_workspace_can_authorize_import(self):
        for status in ("partial", "working", None):
            with self.subTest(status=status):
                save_completion(self.workspace, self.repository, status)
                with self.assertRaisesRegex(ValueError, "仅 complete 可入库"):
                    importer.import_repository(self.repository, self.database, workspace=self.workspace)
                self.assertFalse(self.database.parent.exists())
        missing = self.root / "missing.sqlite3"
        with self.assertRaisesRegex(ValueError, "缺少上游实验工作区"):
            importer.import_repository(self.repository, self.database, workspace=missing)
        self.assertFalse(missing.exists())

    def test_complete_status_needs_matching_export_receipt(self):
        cases = [
            ("缺收据", "export", None, "缺少 complete 导出收据"),
            ("错输出", "output", str(self.root / "other"), "output 与待入库"),
            ("错收据", "export", {"path": str(self.root / "other")}, "export.path 与待入库"),
            ("未完成收据", "export", {"path": str(self.repository), "incomplete_sha256": "partial"}, "仍标记为 partial"),
            ("空清单", "export", {"path": str(self.repository), "files": {}}, "缺少有效文件哈希"),
        ]
        for name, key, value, message in cases:
            with self.subTest(name=name):
                save_completion(self.workspace, self.repository)
                with closing(sqlite3.connect(self.workspace)) as connection, connection:
                    connection.execute("UPDATE meta SET value=? WHERE key=?", (json.dumps(value), key))
                with self.assertRaisesRegex(ValueError, message):
                    importer.import_repository(self.repository, self.database, workspace=self.workspace)
                self.assertFalse(self.database.parent.exists())

    def test_complete_receipt_rejects_changed_files_and_partial_sidecar(self):
        path = self.repository / "roles/R1/role_info.json"
        path.write_text(path.read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "文件与 complete 导出收据不一致"):
            importer.import_repository(self.repository, self.database, workspace=self.workspace)
        self.save()
        extra = self.repository / "unexpected.txt"
        extra.write_text("extra")
        with self.assertRaisesRegex(ValueError, "文件清单"):
            importer.import_repository(self.repository, self.database, workspace=self.workspace)
        extra.unlink()
        diagnostic = Path(str(self.repository) + ".incomplete.json")
        diagnostic.write_text("{}")
        with self.assertRaisesRegex(ValueError, "存在 partial 未完成清单"):
            importer.import_repository(self.repository, self.database, workspace=self.workspace)
        self.assertFalse(self.database.parent.exists())

    def test_rejects_incomplete_orders_and_references_without_creating_database(self):
        valid = importer.load_repository(self.repository)
        cases = [
            ("重复节点", lambda d: d.timeline["nodes"][1].update(order=1), "不能重复"),
            ("节点跳号", lambda d: d.timeline["nodes"][1].update(order=3), "连续编号"),
            ("事件空顺序", lambda d: d.events["N1"][0].update(order=None), "正整数"),
            ("依据性质类型", lambda d: d.events["N1"][0].update(epistemic_status=[]), "epistemic_status 须为文字"),
            ("布尔顺序", lambda d: d.events["N1"][0].update(order=True), "正整数"),
            ("事件跳号", lambda d: d.events["N1"][1].update(order=3), "连续编号"),
            ("重复事件", lambda d: d.events["N2"][0].update(task_id="E1"), "重复事件"),
            ("无事件", lambda d: d.events.update(N1=[]), "非空数组"),
            ("未知人物", lambda d: d.events["N1"][0]["participants"][0].update(role_code="BAD"), "参与者"),
            ("未知物品", lambda d: d.events["N1"][0].update(object_ids=["BAD"]), "物品引用"),
            ("未知地点", lambda d: d.events["N1"][0].update(locations=["不存在"]), "地点引用"),
            ("前节点错误", lambda d: d.snapshots["N2"].update(previous_node_id=None), "previous_node_id"),
            ("未交delta", lambda d: d.events["N1"][0].update(state_delta=None), "尚未交付"),
            ("快照漏物品", lambda d: d.snapshots["N1"].update(objects={}), "全部已提取物品"),
            ("未知所有者", lambda d: d.snapshots["N1"]["objects"]["O1"]["owner"].update(id="BAD"), "已有编号"),
            ("未知却带编号", lambda d: d.snapshots["N1"]["objects"]["O1"]["location"].update(id="L1"), "null"),
            ("空更新", lambda d: d.events["N1"][0]["state_delta"]["objects"][0].update(set={}), "状态字段"),
            ("缺更新依据", lambda d: d.events["N1"][0]["state_delta"]["relations"][0].pop("basis"), "basis"),
            ("错误状态", lambda d: d.snapshots["N1"]["relations"][0].update(status="BAD"), "status"),
        ]
        for name, mutate, message in cases:
            with self.subTest(name=name):
                data = deepcopy(valid)
                mutate(data)
                with patch.object(importer, "load_repository", return_value=data), self.assertRaisesRegex(ValueError, message):
                    importer.import_repository(self.repository, self.database, workspace=self.workspace)
                self.assertFalse(self.database.parent.exists())

    def test_rejects_unknown_missing_and_unsafe_documents(self):
        cases = [
            ("重复档案", lambda d: d.update({"locations/duplicate.json": d["locations/离线小说.json"]}), "重复编号"),
            ("档案编号错误", lambda d: d["roles/R1/role_info.json"].update(role_code="OTHER"), "不一致"),
            ("来源错误", lambda d: d["roles/R1/role_info.json"].update(source="另一部"), "source"),
            ("重复节点编号", lambda d: d["timeline/timeline.json"]["nodes"][1].update(node_id="N1"), "重复节点编号"),
            ("缺失文件", lambda d: d["timeline/timeline.json"]["nodes"][0].update(path="missing.json"), "missing.json"),
            ("越界路径", lambda d: d["timeline/timeline.json"]["nodes"][0].update(path="../outside.json"), "相对路径"),
            ("绝对路径", lambda d: d["timeline/timeline.json"]["nodes"][0].update(state_path="/outside.json"), "相对路径"),
            ("快照错绑", lambda d: d["timeline/N1_阶段/state_before.json"].update(node_id="N2"), "编号不匹配"),
            ("额外容器字段", lambda d: d["timeline/N1_阶段/todolist.json"].update(lost="data"), "容器字段"),
            ("未引用数据", lambda d: d.update({"relations.json": {"edge": "data"}}), "未识别"),
        ]
        for name, mutate, message in cases:
            with self.subTest(name=name), TemporaryDirectory() as directory:
                self.repository = Path(directory)
                self.documents = repository_fixture()
                mutate(self.documents)
                self.save()
                with self.assertRaisesRegex(ValueError, message):
                    importer.import_repository(self.repository, self.database, workspace=self.workspace)
                self.assertFalse(self.database.exists())

    def test_json_duplicate_keys_invalid_constants_and_jsonl_line_numbers(self):
        path = self.root / "data.json"
        for raw in ('{"a": 1, "a": 2}', '{"a": NaN}', '{"a": 1e999}', "[]"):
            with self.subTest(raw=raw):
                path.write_text(raw)
                with self.assertRaises(ValueError):
                    importer.read_document(path)
        path = path.with_suffix(".jsonl")
        path.write_text('{}\n{"broken":\n')
        with self.assertRaisesRegex(ValueError, "第2行"):
            importer.read_document(path)

    def test_symlink_rejected(self):
        (self.repository / "external.json").symlink_to(self.root / "outside.json")
        with self.assertRaisesRegex(ValueError, "符号链接"):
            importer.import_repository(self.repository, self.database, workspace=self.workspace)

    def test_existing_target_and_input_directory_are_never_overwritten(self):
        self.database.parent.mkdir()
        self.database.write_bytes(b"existing database")
        with self.assertRaisesRegex(ValueError, "目标已存在"):
            importer.import_repository(self.repository, self.database, workspace=self.workspace)
        self.assertEqual(self.database.read_bytes(), b"existing database")
        with self.assertRaisesRegex(ValueError, "输入 repository"):
            importer.import_repository(self.repository, self.repository / "new.sqlite3", workspace=self.workspace)
        self.assertFalse((self.repository / "new.sqlite3").exists())

    def test_sql_failure_rolls_back_schema_and_removes_temporary_output(self):
        data = importer.load_repository(self.repository)
        data.events["N1"][0]["task_id"] = data.events["N2"][0]["task_id"]
        engine = importer.create_import_engine(":memory:")
        self.addCleanup(engine.dispose)
        with self.assertRaises(IntegrityError):
            importer.write_database(engine, data)
        with engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT name FROM sqlite_master").fetchall(), [])
        real_write = importer.write_database
        with patch.object(importer, "write_database", side_effect=lambda engine, _: real_write(engine, data)):
            with self.assertRaises(IntegrityError):
                importer.import_repository(self.repository, self.database, workspace=self.workspace)
        self.assertEqual(list(self.database.parent.iterdir()), [])

    def test_sqlalchemy_connections_enforce_foreign_keys_and_rollback_late_failure(self):
        data = importer.load_repository(self.repository)
        # 绕过输入检查，在已经写入档案、事件和快照后触发数据库类型外键。
        data.events["N1"][0]["state_delta"]["objects"][0]["set"] = {"holder": reference("known", "L1")}
        path = self.root / "中文 ?# 数据.sqlite3"
        engine = importer.create_import_engine(path)
        try:
            with self.assertRaisesRegex(IntegrityError, "FOREIGN KEY constraint failed"):
                importer.write_database(engine, data)
            engine.dispose()
            with engine.connect() as connection:
                self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_keys").scalar(), 1)
                self.assertEqual(connection.exec_driver_sql("SELECT name FROM sqlite_master").fetchall(), [])
        finally:
            engine.dispose()
        self.assertTrue(path.exists())

    def test_table_classes_support_session_identity_and_association_queries(self):
        counts = importer.import_repository(self.repository, self.database, workspace=self.workspace)
        self.assertEqual({mapper.local_table.name for mapper in Base.registry.mappers}, set(counts))
        engine = importer.create_import_engine(self.database)
        try:
            with Session(engine) as session:
                event = session.get(Event, "E1")
                self.assertEqual((event.todo, event.event_order), ("E1发生", 1))
                self.assertEqual(event.epistemic_status, "事实（叙述者所见）")
                self.assertEqual(session.get(TimeNode, event.node_id).node_order, 1)
                self.assertEqual(session.get(RepositoryInfo, "离线小说").time_basis, "story")
                participants = session.execute(
                    select(Asset, EventParticipant.original_goal)
                    .join(EventParticipant,
                          (Asset.asset_type == EventParticipant.role_type)
                          & (Asset.asset_id == EventParticipant.role_id))
                    .where(EventParticipant.task_id == event.task_id)
                    .order_by(EventParticipant.position)
                ).all()
                self.assertEqual([(asset.name, goal) for asset, goal in participants], [("甲", None)])
        finally:
            engine.dispose()

    def test_concurrent_target_creation_does_not_overwrite_winner(self):
        real_link = importer.os.link

        def publish(source, target):
            Path(target).write_bytes(b"other importer")
            real_link(source, target)

        with patch.object(importer.os, "link", side_effect=publish), self.assertRaises(FileExistsError):
            importer.import_repository(self.repository, self.database, workspace=self.workspace)
        self.assertEqual(self.database.read_bytes(), b"other importer")
        self.assertEqual(list(self.database.parent.iterdir()), [self.database])

    def test_command_works_outside_project_without_model_configuration(self):
        command = [sys.executable, "-B", str(Path(importer.__file__).resolve()),
                   "--repository", str(self.repository), "--database", str(self.database),
                   "--workspace", str(self.workspace)]
        result = subprocess.run(command, cwd=self.root, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("导入完成", result.stdout)
        repeated = subprocess.run(command, cwd=self.root, capture_output=True, text=True, check=False)
        self.assertEqual(repeated.returncode, 1)
        self.assertIn("目标已存在", repeated.stdout)

    def test_invalid_or_missing_world_data_is_not_silently_dropped(self):
        path = self.repository / "worlds/background/general.json"
        for content in ("broken JSON", '{"source":"另一部小说"}', None):
            with self.subTest(content=content):
                if content is None:
                    path.unlink()
                else:
                    path.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    importer.import_repository(self.repository, self.database, workspace=self.workspace)
                self.assertFalse(self.database.exists())
        self.save()
        (self.repository / "worlds/settings/离线小说.jsonl").write_text('{}\n{"broken":\n')
        save_completion(self.workspace, self.repository)
        with self.assertRaisesRegex(ValueError, "第2行"):
            importer.import_repository(self.repository, self.database, workspace=self.workspace)
        self.assertFalse(self.database.exists())

    def test_array_positions_typed_links_and_sparse_changes(self):
        task = self.documents["timeline/N1_阶段/todolist.json"]["todolist"][0]
        task["participants"] *= 2
        task["participants"].insert(1, {"role_code": "R2", "role_name": "乙", "original_goal": "送信"})
        task["object_ids"] = ["O1", "O1"]
        task["locations"] *= 2
        task["state_delta"]["objects"][0]["set"]["holder"] = reference("known", "R1")
        task["state_delta"]["relations"][0]["set"]["detail"] = None
        self.save()
        importer.import_repository(self.repository, self.database, workspace=self.workspace)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute(
                "SELECT position, role_id, original_goal FROM event_participants WHERE task_id='E1' ORDER BY position"
            ).fetchall(), [(1, "R1", None), (2, "R2", "送信"), (3, "R1", None)])
            self.assertEqual(db.execute(
                "SELECT position, object_id FROM event_objects WHERE task_id='E1' ORDER BY position"
            ).fetchall(), [(1, "O1"), (2, "O1")])
            self.assertEqual(db.execute(
                "SELECT position, location_id FROM event_locations WHERE task_id='E1' ORDER BY position"
            ).fetchall(), [(1, "L1"), (2, "L1")])
            self.assertEqual(db.execute(
                "SELECT field,ref_type,ref_status,ref_id,text_value FROM object_changes ORDER BY field"
            ).fetchall(), [("condition", None, None, None, None), ("holder", "role", "known", "R1", None)])
            self.assertEqual(db.execute(
                "SELECT field,value FROM relation_changes ORDER BY field"
            ).fetchall(), [("detail", None), ("status", "active")])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM object_changes WHERE task_id='E2'").fetchone()[0], 0)

    def test_ambiguous_location_name_is_not_guessed(self):
        locations = self.documents["locations/离线小说.json"]
        locations["L2"] = {**locations["L1"], "location_code": "L2"}
        self.save()
        with self.assertRaisesRegex(ValueError, "地点名称不唯一"):
            importer.import_repository(self.repository, self.database, workspace=self.workspace)
        self.assertFalse(self.database.exists())

    def test_database_enforces_reference_types_and_state_values(self):
        importer.import_repository(self.repository, self.database, workspace=self.workspace)
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            statements = [
                "UPDATE event_participants SET role_id='O1'",
                "INSERT INTO event_objects(task_id,position,object_id) VALUES ('E1',1,'R1')",
                "UPDATE event_locations SET location_id='R1'",
                "UPDATE relation_snapshots SET owner_id='O1'",
                "UPDATE relation_snapshots SET status='invalid'",
                "UPDATE object_snapshots SET holder_status='known',holder_id='L1'",
                "UPDATE object_snapshots SET location_status='unknown',location_id='L1'",
                "UPDATE relation_changes SET value=NULL",
                "UPDATE object_changes SET field='location',ref_status='known',ref_id='R1'",
                "UPDATE object_changes SET field='owner',ref_status=NULL,ref_id=NULL",
                "DELETE FROM assets WHERE asset_type='role' AND asset_id='R1'",
            ]
            for statement in statements:
                with self.subTest(sql=statement), self.assertRaises(sqlite3.IntegrityError):
                    db.execute(statement)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
