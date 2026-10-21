"""只读查询公开 repository 数据库；按已保存的顺序回放状态，不推断小说事实。"""
from itertools import groupby
import json
from pathlib import Path
import sqlite3


class RepositoryReader:
    """用 with 管理连接；数据库必须已经存在，不创建文件或执行写入。"""

    def __init__(self, database):
        uri = Path(database).expanduser().resolve().as_uri() + "?mode=ro"
        self.connection = sqlite3.connect(uri, uri=True)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA query_only = ON")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self.connection.close()

    def _rows(self, sql, parameters=()):
        return [dict(row) for row in self.connection.execute(sql, parameters)]

    def _event(self, task_id):
        rows = self._rows("SELECT * FROM events WHERE task_id=?", (task_id,))
        if not rows:
            raise KeyError(f"事件不存在：{task_id}")
        return rows[0]

    def get_event_context(self, task_id):
        """返回原事件、所属节点、按原数组顺序排列的完整人物/物品/地点档案。"""
        event = self._event(task_id)
        node = self._rows("SELECT data_json FROM time_nodes WHERE node_id=?", (event["node_id"],))[0]
        people = self._rows("""
            SELECT p.position, p.original_goal, a.data_json
            FROM event_participants p JOIN assets a
              ON a.asset_type=p.role_type AND a.asset_id=p.role_id
            WHERE p.task_id=? ORDER BY p.position""", (task_id,))
        objects = self._rows("""
            SELECT a.data_json FROM event_objects o JOIN assets a
              ON a.asset_type=o.object_type AND a.asset_id=o.object_id
            WHERE o.task_id=? ORDER BY o.position""", (task_id,))
        locations = self._rows("""
            SELECT a.data_json FROM event_locations l JOIN assets a
              ON a.asset_type=l.location_type AND a.asset_id=l.location_id
            WHERE l.task_id=? ORDER BY l.position""", (task_id,))
        return {
            "event": json.loads(event["data_json"]),
            "node": json.loads(node["data_json"]),
            "participants": [{"position": row["position"], "original_goal": row["original_goal"],
                              "role": json.loads(row["data_json"])} for row in people],
            "objects": [json.loads(row["data_json"]) for row in objects],
            "locations": [json.loads(row["data_json"]) for row in locations],
        }

    def get_role_events(self, role_id):
        """只查实际参与的事件；同一事件重复引用人物时仍只返回一条事件。"""
        rows = self._rows("""
            SELECT n.node_order, e.data_json FROM events e
            JOIN time_nodes n ON n.node_id=e.node_id
            WHERE EXISTS (SELECT 1 FROM event_participants p
                          WHERE p.task_id=e.task_id AND p.role_id=?)
            ORDER BY n.node_order, e.event_order, e.task_id""", (role_id,))
        # task_id 仅稳定展示同组结果，不表示组内发生先后。
        return [{"node_order": row["node_order"], "event": json.loads(row["data_json"])} for row in rows]

    def get_object_history(self, object_id):
        """返回物品档案、各节点入口状态和逐字段变化，不混淆持有者与所有者。"""
        assets = self._rows("SELECT data_json FROM assets WHERE asset_type='object' AND asset_id=?", (object_id,))
        if not assets:
            raise KeyError(f"物品不存在：{object_id}")
        snapshots = self._rows("""
            SELECT n.node_order, s.node_id, s.owner_status, s.owner_id,
                   s.holder_status, s.holder_id, s.location_status, s.location_id, s.condition
            FROM object_snapshots s JOIN time_nodes n ON n.node_id=s.node_id
            WHERE s.object_id=? ORDER BY n.node_order""", (object_id,))
        changes = self._rows("""
            SELECT n.node_order, e.node_id, e.event_order, c.task_id, c.field,
                   c.ref_status, c.ref_id, c.text_value, c.basis
            FROM object_changes c JOIN events e ON e.task_id=c.task_id
            JOIN time_nodes n ON n.node_id=e.node_id
            WHERE c.object_id=? ORDER BY n.node_order, e.event_order, c.task_id, c.field""", (object_id,))
        return {"object": json.loads(assets[0]["data_json"]), "snapshots": snapshots, "changes": changes}

    def get_state_before(self, task_id):
        """节点入口状态加本节点更早 order 组的 delta；同组事件共享前态。"""
        event = self._event(task_id)
        return self._state_through(event["node_id"], event["event_order"] - 1)

    def get_state_after_group(self, node_id, event_order):
        """返回一个同时开始组之后的状态，不为单个同时事件制造先后关系。"""
        exists = self._rows("SELECT 1 FROM events WHERE node_id=? AND event_order=?", (node_id, event_order))
        if not exists:
            raise KeyError(f"事件组不存在：{node_id}/{event_order}")
        return self._state_through(node_id, event_order)

    def _snapshot(self, node_id):
        if not self._rows("SELECT 1 FROM state_snapshots WHERE node_id=?", (node_id,)):
            raise KeyError(f"节点快照不存在：{node_id}")
        relations = self._rows("""
            SELECT owner_id, other_id, relation, status, detail
            FROM relation_snapshots WHERE node_id=? ORDER BY owner_id, other_id, relation""", (node_id,))
        objects = {}
        for row in self._rows("SELECT * FROM object_snapshots WHERE node_id=? ORDER BY object_id", (node_id,)):
            objects[row["object_id"]] = {
                field: {"status": row[field + "_status"], "id": row[field + "_id"]}
                for field in ("owner", "holder", "location")}
            objects[row["object_id"]]["condition"] = row["condition"]
        return {tuple(row[key] for key in ("owner_id", "other_id", "relation")): row for row in relations}, objects

    def _state_through(self, node_id, last_order):
        relations, objects = self._snapshot(node_id)
        parameters = (node_id, last_order)
        relation_rows = self._rows("""
            SELECT e.event_order, c.* FROM relation_changes c JOIN events e ON e.task_id=c.task_id
            WHERE e.node_id=? AND e.event_order<=?""", parameters)
        object_rows = self._rows("""
            SELECT e.event_order, c.* FROM object_changes c JOIN events e ON e.task_id=c.task_id
            WHERE e.node_id=? AND e.event_order<=?""", parameters)
        updates = []
        for row in relation_rows:
            target = ("relation", row["owner_id"], row["other_id"], row["relation"], row["field"])
            updates.append((row["event_order"], target, row["value"], row["task_id"]))
        for row in object_rows:
            value = (row["text_value"] if row["field"] == "condition"
                     else {"status": row["ref_status"], "id": row["ref_id"]})
            updates.append((row["event_order"], ("object", row["object_id"], row["field"]), value, row["task_id"]))
        for order, group in groupby(sorted(updates, key=lambda row: row[0]), key=lambda row: row[0]):
            merged = {}
            for _, target, value, task_id in group:
                if target in merged and merged[target][0] != value:
                    raise ValueError(f"{node_id}/order={order}: 同时事件 {merged[target][1]}、{task_id} "
                                     f"对 {target} 提交冲突状态")
                merged[target] = (value, task_id)
            # 先检查整组冲突，再应用相容变化；绝不按事件编号覆盖另一事件。
            for target, (value, _) in merged.items():
                if target[0] == "relation":
                    _, owner, other, relation, field = target
                    row = relations.setdefault((owner, other, relation), {
                        "owner_id": owner, "other_id": other, "relation": relation,
                        "status": "unknown", "detail": None})
                    row[field] = value
                else:
                    _, object_id, field = target
                    objects[object_id][field] = value
        return {"relations": [relations[key] for key in sorted(relations)], "objects": objects}
