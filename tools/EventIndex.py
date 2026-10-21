"""事件语义向量的缓存、FAISS 索引与查询；不改变 repository 或事件事实。"""
import argparse
from contextlib import contextmanager
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
from tempfile import NamedTemporaryFile

import faiss
import numpy as np
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.orm import Session

if __package__:
    from .database.Database import create_import_engine
    from .database.table import Event, EventEmbedding
    from .JinaEmbedding import DEFAULT_MODEL_PATH, PROFILE_FIELDS, JinaEmbedding
    from .RepositoryReader import RepositoryReader
else:
    from database.Database import create_import_engine
    from database.table import Event, EventEmbedding
    from JinaEmbedding import DEFAULT_MODEL_PATH, PROFILE_FIELDS, JinaEmbedding
    from RepositoryReader import RepositoryReader


def text_hash(text):
    return sha256(text.encode("utf-8")).hexdigest()


def encode_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")


def event_texts(connection):
    events = {}
    for key, text, raw in connection.execute("SELECT task_id,todo,data_json FROM events ORDER BY task_id"):
        event = json.loads(raw)
        if event.get("task_id") != key or event.get("todo") != text:
            raise ValueError(f"事件 {key} 的查询列与原 JSON 不一致，不能建立语义索引")
        events[key] = text
    if not events or any(not isinstance(text, str) or not text.strip() for text in events.values()):
        raise ValueError("事件库为空或存在空 todo，不能建立语义索引")
    return events


def embedding_rows(connection):
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE name='event_embeddings' AND type='table'").fetchone()
    return [dict(row) for row in connection.execute("SELECT * FROM event_embeddings ORDER BY vector_id")] if exists else []


def normalize_vectors(vectors, count, dimension):
    vectors = np.asarray(vectors, dtype="float32")
    if vectors.shape != (count, dimension) or not np.isfinite(vectors).all():
        raise ValueError("embedding 返回的数量、维度不匹配或包含非有限数值")
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    if not np.isfinite(norms).all() or np.any(norms == 0):
        raise ValueError("embedding 返回零向量或无效范数")
    return np.ascontiguousarray(vectors / norms, dtype="<f4")


def cache_manifest(events, rows, profile):
    """发布和读取共用同一份完整性检查；编号、内容与编码空间必须一致。"""
    if {row["task_id"] for row in rows} != set(events) or len(rows) != len(events):
        raise ValueError("事件向量尚未完整生成，不能使用 FAISS 索引")
    digest = sha256()
    for row in rows:
        if any(row[field] != profile[field] for field in PROFILE_FIELDS):
            raise ValueError("向量的模型、版本、任务或维度不一致；不能混用编码空间")
        if row["text_hash"] != text_hash(events[row["task_id"]]):
            raise ValueError(f"事件 {row['task_id']} 已变化，向量与索引需要更新")
        vector = np.frombuffer(row["vector"], dtype="<f4")
        if (vector.shape != (profile["dimension"],) or not np.isfinite(vector).all()
                or not np.isclose(np.linalg.norm(vector), 1, atol=1e-5)):
            raise ValueError(f"事件 {row['task_id']} 的缓存向量损坏或未归一化")
        digest.update(encode_json({key: value for key, value in row.items() if key != "vector"}))
        digest.update(row["vector"])
    return {"format": 1, "profile": profile, "metric": "cosine", "count": len(rows),
            "events_hash": sha256(encode_json(events)).hexdigest(), "vectors_hash": digest.hexdigest()}


def publish_file(path, content):
    """只发布新文件；中断后允许复用完全相同的文件，绝不覆盖已有不同结果。"""
    with NamedTemporaryFile(dir=path.parent, prefix=".event-index-") as temporary:
        temporary.write(content)
        temporary.flush()
        os.fsync(temporary.fileno())
        try:
            os.link(temporary.name, path)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != content:
                raise FileExistsError(f"索引目标已存在且内容不同，请使用新路径：{path}") from None


@contextmanager
def build_lock(database):
    with Path(str(database) + ".embedding.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("同一数据库已有向量构建任务运行") from None
        yield


def build_index(database, index_path, embedder, *, batch_size=16):
    """按批保存成功向量；失败可续做，完整后发布索引及收据。不启动上游实验。"""
    database = Path(database).expanduser().resolve()
    index_path = Path(index_path).expanduser().absolute()
    receipt_path = Path(str(index_path) + ".json")
    if not database.is_file():
        raise ValueError(f"数据库不存在：{database}")
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
        raise ValueError("batch_size 须为正整数")
    if index_path.resolve() == database or receipt_path.resolve() == database:
        raise ValueError("索引路径不能覆盖数据库")
    with build_lock(database), RepositoryReader(database) as reader:
        events = event_texts(reader.connection)
        rows = embedding_rows(reader.connection)
        profile = embedder.profile
        if any(any(row[field] != profile[field] for field in PROFILE_FIELDS) for row in rows):
            raise ValueError("数据库已使用另一套 embedding 配置；请使用独立的新数据库")
        if receipt_path.exists() and index_path.exists():
            with EventIndex(database, index_path) as existing:
                if existing.manifest["profile"] != profile:
                    raise ValueError("已有索引使用另一套 embedding 配置")
            return {"events": len(events), "encoded": 0, "reused": len(events), "index": str(index_path)}
        cached = {row["task_id"]: row for row in rows}
        pending = [(key, text) for key, text in events.items()
                   if key not in cached or cached[key]["text_hash"] != text_hash(text)]
        engine = create_import_engine(database)
        try:
            with engine.begin() as connection:
                EventEmbedding.__table__.create(connection, checkfirst=True)
            for start in range(0, len(pending), batch_size):
                batch = pending[start:start + batch_size]
                vectors = normalize_vectors(embedder.encode([text for _, text in batch], batch_size=batch_size),
                                            len(batch), profile["dimension"])
                records = [{"task_id": key, **profile, "text_hash": text_hash(text), "vector": vector.tobytes()}
                           for (key, text), vector in zip(batch, vectors, strict=True)]
                with Session(engine) as session, session.begin():
                    current = dict(session.execute(select(Event.task_id, Event.todo).where(
                        Event.task_id.in_([key for key, _ in batch]))).all())
                    if current != dict(batch):
                        raise ValueError("编码期间事件已变化；本批未保存，请重新构建")
                    statement = insert(EventEmbedding).values(records)
                    session.execute(statement.on_conflict_do_update(
                        index_elements=["task_id"],
                        set_={key: statement.excluded[key] for key in records[0] if key != "task_id"}))
            if event_texts(reader.connection) != events:
                raise ValueError("构建期间事件集合已变化；索引未发布，请重新构建")
            rows = embedding_rows(reader.connection)
            manifest = cache_manifest(events, rows, profile)
            vectors = np.stack([np.frombuffer(row["vector"], dtype="<f4") for row in rows])
            index = faiss.IndexIDMap(faiss.IndexFlatIP(profile["dimension"]))
            index.add_with_ids(vectors, np.asarray([row["vector_id"] for row in rows], dtype="int64"))
            content = faiss.serialize_index(index).tobytes()
            manifest["index_sha256"] = sha256(content).hexdigest()
            index_path.parent.mkdir(parents=True, exist_ok=True)
            publish_file(index_path, content)
            # 收据最后发布；缺少收据的索引不提供查询，下次可直接复用已存向量。
            publish_file(receipt_path, encode_json(manifest))
        finally:
            engine.dispose()
    return {"events": len(events), "encoded": len(pending), "reused": len(events) - len(pending),
            "index": str(index_path)}


class EventIndex(RepositoryReader):
    """只读语义检索；按 FAISS 得分顺序关联完整事件资料，不按 SQL 返回顺序重排。"""

    def __init__(self, database, index_path):
        super().__init__(database)
        try:
            self.index_path = Path(index_path).expanduser().resolve()
            self.manifest = json.loads(Path(str(self.index_path) + ".json").read_text())
            self.rows = embedding_rows(self.connection)
            expected = cache_manifest(event_texts(self.connection), self.rows, self.manifest["profile"])
            if any(self.manifest.get(key) != value for key, value in expected.items()):
                raise ValueError("FAISS 收据与当前数据库不一致，请重新构建索引")
            content = self.index_path.read_bytes()
            if sha256(content).hexdigest() != self.manifest["index_sha256"]:
                raise ValueError("FAISS 文件与收据哈希不一致，拒绝加载")
            self.index = faiss.deserialize_index(np.frombuffer(content, dtype="uint8").copy())
            if (not isinstance(self.index, faiss.IndexIDMap) or self.index.metric_type != faiss.METRIC_INNER_PRODUCT
                    or self.index.d != self.manifest["profile"]["dimension"]
                    or list(faiss.vector_to_array(self.index.id_map)) != [row["vector_id"] for row in self.rows]):
                raise ValueError("FAISS 结构或向量编号映射与数据库不一致")
            self.by_id = {row["vector_id"]: row for row in self.rows}
            self.by_task = {row["task_id"]: row for row in self.rows}
        except BaseException:
            self.close()
            raise

    def search_event(self, task_id, k=5):
        """使用已保存向量，排除查询事件自身；不加载 embedding 模型。"""
        if task_id not in self.by_task:
            raise KeyError(f"事件不存在：{task_id}")
        vector = np.frombuffer(self.by_task[task_id]["vector"], dtype="<f4")
        return self._search(vector, k, exclude=task_id)

    def search_text(self, text, embedder, k=5):
        """新事件描述与入库事件使用完全相同的语义编码配置。"""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("查询文本不能为空")
        if isinstance(k, bool) or not isinstance(k, int) or k < 1:
            raise ValueError("k 须为正整数")
        if embedder.profile != self.manifest["profile"]:
            raise ValueError("查询模型与索引的编码配置不同")
        return self._search(embedder.encode([text]), k)

    def _search(self, vector, k, exclude=None):
        if isinstance(k, bool) or not isinstance(k, int) or k < 1:
            raise ValueError("k 须为正整数")
        # 长时间持有 Reader 时也不能悄悄使用已过期的向量或事件映射。
        expected = cache_manifest(event_texts(self.connection), embedding_rows(self.connection), self.manifest["profile"])
        if any(self.manifest[key] != value for key, value in expected.items()):
            raise ValueError("数据库已变化，请重新构建并加载索引")
        query = normalize_vectors(np.asarray(vector).reshape(1, -1), 1, self.index.d)
        scores, ids = self.index.search(query, min(k + bool(exclude), self.index.ntotal))
        results = []
        for score, vector_id in zip(scores[0], ids[0], strict=True):
            row = self.by_id[int(vector_id)]
            if row["task_id"] != exclude:
                results.append({"vector_id": int(vector_id), "task_id": row["task_id"], "score": float(score),
                                **self.get_event_context(row["task_id"])})
        return results[:k]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "search"))
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--model-path", type=Path, default=DEFAULT_MODEL_PATH)
    parser.add_argument("--device", default="cpu", choices=("cpu", "cuda", "mps"))
    parser.add_argument("--batch-size", type=int, default=16)
    query = parser.add_mutually_exclusive_group()
    query.add_argument("--event")
    query.add_argument("--text")
    parser.add_argument("--k", type=int, default=5)
    args = parser.parse_args(argv)
    if args.action == "search" and args.event is None and args.text is None:
        parser.error("search 需要 --event 或 --text")
    if args.action == "build" and (args.event is not None or args.text is not None):
        parser.error("build 不接受查询参数")
    try:
        if args.action == "build":
            with JinaEmbedding(args.model_path, args.device) as encoder:
                result = build_index(args.database, args.index, encoder, batch_size=args.batch_size)
        else:
            with EventIndex(args.database, args.index) as reader:
                if args.event is not None:
                    result = reader.search_event(args.event, args.k)
                else:
                    with JinaEmbedding(args.model_path, args.device) as encoder:
                        result = reader.search_text(args.text, encoder, args.k)
    except (OSError, ValueError, KeyError, RuntimeError, sqlite3.Error, SQLAlchemyError) as exc:
        print(f"事件索引操作失败：{exc.orig if isinstance(exc, DBAPIError) else exc}")
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
