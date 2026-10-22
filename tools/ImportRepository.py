"""将公开 repository 无损导入 SQLite；只转换存储，不调用提取系统或推断事实。"""
import argparse
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from sqlalchemy import func, insert, select
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.orm import Session

if __package__:
    from .database import table as models
    from .database.Database import Base, create_import_engine
else:
    from database import table as models
    from database.Database import Base, create_import_engine

ASSET_FIELDS = {
    "role": ("role_code", "role_name"),
    "location": ("location_code", "location_name"),
    "object": ("object_code", "object_name"),
}


@dataclass
class RepositoryData:
    timeline: dict
    assets: dict[str, dict[str, dict]]
    world_data: dict[str, dict | list]
    events: dict[str, list[dict]]
    snapshots: dict[str, dict]


def require(condition, location, message):
    if not condition:
        raise ValueError(f"{location}: {message}")


def text(value):
    return isinstance(value, str) and bool(value.strip())


def unique_object(pairs):
    """JSON 重复键会覆盖内容，导入时必须明确拒绝。"""
    result = {}
    for key, value in pairs:
        require(key not in result, key, "JSON 字段重复")
        result[key] = value
    return result


def read_document(path, expected_sha256=None):
    """JSONL 按行存成数组，保留原顺序；所有原始字段留在普通 dict 中。"""
    try:
        content = path.read_bytes()
        if expected_sha256 is not None:
            require(sha256(content).hexdigest() == expected_sha256, path, "文件与 complete 导出收据不一致")
        raw = content.decode("utf-8")
        if path.suffix == ".jsonl":
            result = []
            for number, line in enumerate(raw.splitlines(), 1):
                if line.strip():
                    try:
                        row = json.loads(line, object_pairs_hook=unique_object)
                    except ValueError as exc:
                        raise ValueError(f"第{number}行: {exc}") from exc
                    require(isinstance(row, dict), f"第{number}行", "应为 JSON 对象")
                    result.append(row)
        else:
            result = json.loads(raw, object_pairs_hook=unique_object)
            require(isinstance(result, dict), "$", "应为 JSON 对象")
        # NaN、Infinity 和数值溢出不能转换成有效 JSON。
        json.dumps(result, allow_nan=False)
        return result
    except (ValueError, OSError) as exc:
        raise ValueError(f"{path}: {exc}") from exc


def load_repository(root, expected_files=None):
    """读取档案、世界资料和 timeline 声明的文件；未知数据拒收。"""
    root = Path(root).expanduser().resolve()
    require(root.is_dir(), root, "repository 目录不存在")
    entries = list(root.rglob("*"))
    require(not any(p.is_symlink() for p in entries), root, "repository 内不能有符号链接")
    if expected_files is not None:
        actual = {p.relative_to(root).as_posix() for p in entries if p.is_file()}
        require(actual == set(expected_files), root, "文件清单与 complete 导出收据不一致")
    for directory in ("roles", "objects", "locations", "worlds/background", "worlds/settings", "timeline"):
        require((root / directory).is_dir(), directory, "缺少目录")
    used = set()

    def read(relative):
        require(text(relative), "文件路径", "须为非空相对路径")
        path = Path(relative)
        require(not path.is_absolute() and ".." not in path.parts, relative, "不能越出 repository")
        relative = path.as_posix()
        used.add(relative)
        if expected_files is not None:
            require(relative in expected_files, relative, "文件未在 complete 导出收据中")
        return read_document(root / path, expected_files[relative] if expected_files is not None else None)

    timeline = read("timeline/timeline.json")
    require(set(timeline) == {"source", "time_basis", "nodes"}, "timeline.json", "顶层字段与当前契约不符")
    assets = {kind: {} for kind in ASSET_FIELDS}

    def add_asset(kind, row, location, expected_id):
        code, name = ASSET_FIELDS[kind]
        require(isinstance(row, dict), location, "档案须为对象")
        key = row.get(code)
        require(text(key) and key == expected_id, location, f"{code} 与目录或记录键不一致")
        require(key not in assets[kind], location, f"重复编号 {key}")
        require(text(row.get(name)), location, f"缺少 {name}")
        require(row.get("source") == timeline.get("source"), location, "source 与 timeline 不一致")
        assets[kind][key] = row

    for kind, directory, filename in (("role", "roles", "role_info.json"), ("object", "objects", "object_info.json")):
        for path in sorted((root / directory).glob(f"*/{filename}")):
            relative = path.relative_to(root).as_posix()
            add_asset(kind, read(relative), relative, path.parent.name)
    for path in sorted((root / "locations").glob("*.json")):
        relative = path.relative_to(root).as_posix()
        for key, row in read(relative).items():
            add_asset("location", row, f"{relative}/{key}", key)
    world_data = {}
    for pattern in ("worlds/background/*.json", "worlds/settings/*.jsonl"):
        for path in sorted(root.glob(pattern)):
            relative = path.relative_to(root).as_posix()
            world_data[relative] = read(relative)
    require("worlds/background/general.json" in world_data, "worlds/background", "缺少 general.json")
    require(world_data["worlds/background/general.json"].get("source") == timeline["source"],
            "worlds/background/general.json", "source 与 timeline 不一致")
    events, snapshots = {}, {}
    require(isinstance(timeline["nodes"], list) and timeline["nodes"], "timeline.nodes", "须为非空数组")
    for node in timeline["nodes"]:
        require(isinstance(node, dict) and text(node.get("node_id")), "timeline.nodes", "缺少节点编号")
        key = node["node_id"]
        require(key not in events, key, "重复节点编号")
        for field in ("path", "state_path"):
            require(text(node.get(field)), f"{key}.{field}", "缺少文件路径")
            path = Path(node[field])
            require(not path.is_absolute() and ".." not in path.parts,
                    f"{key}.{field}", "须为 timeline 内的相对路径")
        tasks = read("timeline/" + node["path"])
        snapshot = read("timeline/" + node["state_path"])
        require(set(tasks) == {"node_id", "todolist"} and tasks["node_id"] == key, node["path"], "节点或容器字段不匹配")
        require(snapshot.get("node_id") == key, node["state_path"], "快照节点编号不匹配")
        events[key], snapshots[key] = tasks["todolist"], snapshot
    unhandled = {p.relative_to(root).as_posix() for p in entries
                 if p.is_file() and p.suffix.lower() in (".json", ".jsonl")} - used
    require(not unhandled, root, f"存在未识别或未引用的数据文件：{sorted(unhandled)}")
    for relative in set(expected_files or {}) - used:
        require(sha256((root / relative).read_bytes()).hexdigest() == expected_files[relative],
                relative, "文件与 complete 导出收据不一致")
    return RepositoryData(timeline, assets, world_data, events, snapshots)


def validate_orders(rows, location, unique=False):
    require(isinstance(rows, list) and rows, location, "须为非空数组")
    require(all(isinstance(row, dict) and type(row.get("order")) is int and row["order"] > 0
                for row in rows), location, "order 须为正整数，不能为 null")
    orders = [row["order"] for row in rows]
    require(not unique or len(orders) == len(set(orders)), location, "节点 order 不能重复")
    require(sorted(set(orders)) == list(range(1, len(set(orders)) + 1)), location, "order 须从1连续编号")


def validate_state(state, assets, location, delta=False):
    """只查状态结构和引用；省略字段不补默认值，快照与 delta 均不重算。"""
    require(isinstance(state, dict), location, "状态尚未交付")
    relations, objects = state.get("relations"), state.get("objects")
    require(isinstance(relations, list), location, "relations 须为数组")
    require(isinstance(objects, list if delta else dict), location, "objects 结构不符")
    seen = set()
    for row in relations:
        require(isinstance(row, dict), location, "关系须为对象")
        owner, other, relation = row.get("owner_id"), row.get("other_id"), row.get("relation")
        require(text(owner) and text(other) and owner != other
                and owner in assets["role"] and other in assets["role"], location, "关系端点须为两个已有的人物编号")
        require(text(relation), location, "缺少关系名称")
        key = (owner, other, relation)
        require(key not in seen, location, f"重复人物关系 {key}")
        seen.add(key)
        fields = row.get("set") if delta else row
        require(isinstance(fields, dict) and fields, location, "关系更新不能为空")
        require(delta or "status" in fields, location, "缺少关系 status")
        if "status" in fields:
            require(fields["status"] in ("active", "inactive", "unknown"), location, "关系 status 非法")
        if "detail" in fields:
            require(fields["detail"] is None or isinstance(fields["detail"], str), location, "detail 须为文字或 null")
        if delta:
            require(set(fields) <= {"status", "detail"}, location, "关系 set 含未知字段")
            require(text(row.get("basis")), location, "关系更新缺少 basis")
    if delta:
        require(all(isinstance(row, dict) and text(row.get("object_id")) for row in objects), location, "缺少物品编号")
        require(all(text(row.get("basis")) for row in objects), location, "物品更新缺少 basis")
        rows = [(row["object_id"], row.get("set")) for row in objects]
        require(len({key for key, _ in rows}) == len(rows), location, "重复物品更新")
    else:
        require(set(objects) == set(assets["object"]), location, "快照须覆盖全部已提取物品")
        rows = objects.items()
    for key, fields in rows:
        require(key in assets["object"], location, f"未知物品编号 {key}")
        require(isinstance(fields, dict) and fields, location, f"{key} 缺少状态字段")
        if delta:
            require(set(fields) <= {"owner", "holder", "location", "condition"}, location, f"{key}.set 含未知字段")
        else:
            require("condition" in fields, location, f"{key} 缺少 condition")
        if "condition" in fields:
            require(fields["condition"] is None or isinstance(fields["condition"], str),
                    location, f"{key}.condition 须为文字或 null")
        for field, kind in (("owner", "role"), ("holder", "role"), ("location", "location")):
            if delta and field not in fields:
                continue
            ref = fields.get(field)
            label = f"{location}.{key}.{field}"
            require(isinstance(ref, dict) and "id" in ref, label, "须为带 status/id 的引用")
            status, identity = ref.get("status"), ref["id"]
            require(status in ("known", "none", "unknown"), label, "status 非法")
            require((text(identity) and identity in assets[kind]) if status == "known" else identity is None,
                    label, "known 须引用已有编号；none/unknown 的 id 须为 null")


def validate_repository(data):
    """检查可确定的存储契约，不对小说内容重新做语义校验。"""
    require(text(data.timeline["source"]) and data.timeline["time_basis"] == "story", "timeline.json", "来源或时间依据非法")
    validate_orders(data.timeline["nodes"], "timeline.nodes", unique=True)
    seen_events, previous = set(), None
    location_names = [row["location_name"] for row in data.assets["location"].values()]
    for node in sorted(data.timeline["nodes"], key=lambda row: row["order"]):
        key = node["node_id"]
        require(text(node.get("node_name")), key, "缺少节点名称")
        require(isinstance(node.get("time"), dict)
                and (node["time"].get("relative") is None or isinstance(node["time"]["relative"], str)),
                key, "time.relative 须为文字或 null")
        tasks, snapshot = data.events[key], data.snapshots[key]
        validate_orders(tasks, f"{node['path']}.todolist")
        require("previous_node_id" in snapshot and snapshot["previous_node_id"] == previous,
                node["state_path"], "previous_node_id 与实际节点顺序不符")
        validate_state(snapshot, data.assets, node["state_path"])
        previous = key
        for event in tasks:
            label = f"{node['path']}/{event.get('task_id')}"
            identity = event.get("task_id")
            require(text(identity) and identity not in seen_events, label, "缺少或重复事件编号")
            seen_events.add(identity)
            require(text(event.get("todo")), label, "事件内容为空")
            require("epistemic_status" not in event or isinstance(event["epistemic_status"], str),
                    label, "epistemic_status 须为文字")
            require(event.get("time") is None or isinstance(event["time"], str), label, "time 须为文字或 null")
            people, objects, locations = event.get("participants"), event.get("object_ids"), event.get("locations")
            require(isinstance(people, list) and all(isinstance(row, dict) and text(row.get("role_code"))
                    and row["role_code"] in data.assets["role"] for row in people), label, "参与者引用非法")
            require(isinstance(objects, list) and all(text(key) and key in data.assets["object"] for key in objects),
                    label, "物品引用非法")
            require(locations is None or isinstance(locations, list)
                    and all(text(name) and name in location_names for name in locations), label, "地点引用非法")
            require(all(row.get("original_goal") is None or isinstance(row["original_goal"], str)
                        for row in people), label, "original_goal 须为文字或 null")
            require(all(location_names.count(name) == 1 for name in locations or []),
                    label, "地点名称不唯一，不能确定地点引用")
            validate_state(event.get("state_delta"), data.assets, label + ".state_delta", delta=True)


def insert_rows(session, model, rows):
    """空集合不发 INSERT，非空集合交给驱动批量写入。"""
    if rows:
        session.execute(insert(model), rows, execution_options={"render_nulls": True})


def write_event_links(session, data):
    """稳定编号直接关联；地点名称在验证唯一后转换成已有地点编号。"""
    location_ids = {row["location_name"]: key for key, row in data.assets["location"].items()}
    events = [event for tasks in data.events.values() for event in tasks]
    insert_rows(session, models.EventParticipant, [
        {"task_id": event["task_id"], "position": position,
         "role_id": row["role_code"], "original_goal": row.get("original_goal")}
        for event in events for position, row in enumerate(event["participants"], 1)])
    insert_rows(session, models.EventObject, [
        {"task_id": event["task_id"], "position": position, "object_id": key}
        for event in events for position, key in enumerate(event["object_ids"], 1)])
    insert_rows(session, models.EventLocation, [
        {"task_id": event["task_id"], "position": position, "location_id": location_ids[name]}
        for event in events for position, name in enumerate(event.get("locations") or [], 1)])


def write_state_rows(session, data):
    """快照逐实体存储，delta 只为实际出现的字段写行；显式 null 原样保留。"""
    relation_rows, object_rows = [], []
    for node_id, state in data.snapshots.items():
        for row in state["relations"]:
            relation_rows.append({
                "node_id": node_id, "owner_id": row["owner_id"], "other_id": row["other_id"],
                "relation": row["relation"], "status": row["status"], "detail": row.get("detail")})
        for object_id, row in state["objects"].items():
            object_rows.append({
                "node_id": node_id, "object_id": object_id,
                "owner_status": row["owner"]["status"], "owner_id": row["owner"]["id"],
                "holder_status": row["holder"]["status"], "holder_id": row["holder"]["id"],
                "location_status": row["location"]["status"], "location_id": row["location"]["id"],
                "condition": row["condition"]})
    insert_rows(session, models.RelationSnapshot, relation_rows)
    insert_rows(session, models.ObjectSnapshot, object_rows)

    relation_rows, object_rows = [], []
    for tasks in data.events.values():
        for event in tasks:
            for row in event["state_delta"]["relations"]:
                for field, value in row["set"].items():
                    relation_rows.append({
                        "task_id": event["task_id"], "owner_id": row["owner_id"], "other_id": row["other_id"],
                        "relation": row["relation"], "field": field, "value": value, "basis": row["basis"]})
            for row in event["state_delta"]["objects"]:
                for field, value in row["set"].items():
                    ref = value if field != "condition" else {}
                    object_rows.append({
                        "task_id": event["task_id"], "object_id": row["object_id"], "field": field,
                        "ref_status": ref.get("status"), "ref_id": ref.get("id"),
                        "text_value": value if field == "condition" else None, "basis": row["basis"]})
    insert_rows(session, models.RelationChange, relation_rows)
    insert_rows(session, models.ObjectChange, object_rows)


def write_database(engine, data):
    """同一事务建表并批量写入；JSON 保存原记录，索引列只复制原值。"""
    def encode(value):
        return json.dumps(value, ensure_ascii=False, allow_nan=False)

    with Session(engine) as session, session.begin():
        Base.metadata.create_all(session.connection())
        insert_rows(session, models.RepositoryInfo, [
            {"source": data.timeline["source"], "time_basis": data.timeline["time_basis"]}])
        insert_rows(session, models.WorldData, [
            {"file_path": path, "content_json": encode(value)} for path, value in data.world_data.items()])
        insert_rows(session, models.Asset, [
            {"asset_type": kind, "asset_id": key, "name": row[ASSET_FIELDS[kind][1]], "data_json": encode(row)}
            for kind, rows in data.assets.items() for key, row in rows.items()])
        insert_rows(session, models.TimeNode, [
            {"node_id": node["node_id"], "node_order": node["order"], "node_name": node["node_name"],
             "time_relative": node["time"].get("relative"), "data_json": encode(node)}
            for node in data.timeline["nodes"]])
        insert_rows(session, models.Event, [
            {"task_id": event["task_id"], "node_id": key, "event_order": event["order"],
             "todo": event["todo"], "epistemic_status": event.get("epistemic_status"),
             "time_text": event.get("time"), "data_json": encode(event)}
            for key, events in data.events.items() for event in events])
        insert_rows(session, models.StateSnapshot, [
            {"node_id": key, "previous_node_id": row["previous_node_id"], "data_json": encode(row)}
            for key, row in data.snapshots.items()])
        write_event_links(session, data)
        write_state_rows(session, data)
        counts = {name: session.scalar(select(func.count()).select_from(table))
                  for name, table in sorted(Base.metadata.tables.items())}
    return counts


def completed_export(root, workspace):
    """只读上游完成状态和现有导出收据，不加载原文、候选或模型配置。"""
    workspace = Path(workspace).expanduser().resolve()
    require(workspace.is_file(), workspace, "缺少上游实验工作区，不能确认 complete")
    try:
        with closing(sqlite3.connect(workspace.as_uri() + "?mode=ro", uri=True)) as connection:
            metadata = {key: json.loads(value) for key, value in connection.execute(
                "SELECT key,value FROM meta WHERE key IN ('status','output','export')")}
    except (sqlite3.Error, ValueError) as exc:
        raise ValueError(f"{workspace}: 无法读取上游完成状态和导出收据：{exc}") from exc
    require(metadata.get("status") == "complete", workspace,
            f"上游状态为 {metadata.get('status')!r}，仅 complete 可入库")
    receipt = metadata.get("export")
    require(isinstance(receipt, dict), workspace, "缺少 complete 导出收据")
    for label, path in (("output", metadata.get("output")), ("export.path", receipt.get("path"))):
        require(text(path) and Path(path).resolve() == root, workspace, f"{label} 与待入库 repository 不一致")
    require(not receipt.get("incomplete_sha256"), workspace, "导出收据仍标记为 partial")
    diagnostic = Path(str(root) + ".incomplete.json")
    require(not diagnostic.exists() and not diagnostic.is_symlink(), diagnostic, "存在 partial 未完成清单，不能入库")
    files = receipt.get("files")
    require(isinstance(files, dict) and files
            and all(text(key) and text(value) for key, value in files.items()),
            workspace, "导出收据缺少有效文件哈希")
    return files


def matching_database(expected, target):
    """恢复交接时逐表比对十三张公开资料表，不以文件名猜测所属作品。"""
    with closing(sqlite3.connect(expected.as_uri() + '?mode=ro', uri=True)) as source, \
         closing(sqlite3.connect(target.resolve().as_uri() + '?mode=ro', uri=True)) as current:
        current.execute('BEGIN')
        counts = {}
        for name, table in Base.metadata.tables.items():
            columns = ','.join('"' + column.name + '"' for column in table.columns)
            query = f'SELECT {columns} FROM "{name}" ORDER BY {columns}'
            rows = current.execute(query).fetchall()
            require(source.execute(query).fetchall() == rows,
                    target, f'已有数据库的 {name} 与 complete repository 不一致，拒绝复用或覆盖')
            counts[name] = len(rows)
        return counts


def import_repository(repository, database, *, workspace, reuse_existing=False):
    """原子发布新数据库；工作流恢复仅可复用资料完全一致的库，永不覆盖。"""
    root = Path(repository).expanduser().resolve()
    target = Path(database).expanduser().absolute()
    require(not target.is_symlink() and (not target.exists() or (reuse_existing and target.is_file())),
            target, "目标已存在，拒绝覆盖")
    require(not target.resolve().is_relative_to(root), target, "数据库不能写入输入 repository")
    files = completed_export(root, workspace)
    data = load_repository(root, expected_files=files)
    validate_repository(data)
    target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".repository-import-", dir=target.parent) as temporary:
        staged = Path(temporary) / "repository.sqlite3"
        engine = create_import_engine(staged)
        try:
            counts = write_database(engine, data)
        finally:
            engine.dispose()
        if target.exists() and reuse_existing:
            try:
                return matching_database(staged, target)
            except sqlite3.Error as exc:
                raise ValueError(f'{target}: 已有数据库无法核对，拒绝复用：{exc}') from exc
        # 同目录硬链接发布完整文件；目标若被其他进程创建，原子失败而不覆盖。
        os.link(staged, target)
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path,
                        help="对应上游实验工作区；只读核对 complete 状态和导出收据")
    args = parser.parse_args(argv)
    try:
        counts = import_repository(args.repository, args.database, workspace=args.workspace)
    except (ValueError, OSError, SQLAlchemyError) as exc:
        # 数据库错误保留具体原因，不把整批 JSON 参数打印到终端。
        print(f"导入失败：{exc.orig if isinstance(exc, DBAPIError) else exc}")
        return 1
    print("导入完成：" + json.dumps(counts, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
