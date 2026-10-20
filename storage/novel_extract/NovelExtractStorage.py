"""单个实验的持久化：请求、智能体阶段快照和导出收据。"""
from contextlib import closing, contextmanager
import fcntl
import json
import sqlite3
import threading
import time

from common.novel_extract.NovelExtractRuntime import encode, require, safe_path, RunStopped


class NovelExtractStorage:
    FORMAT_VERSION = 39

    def __init__(self, path):
        self.path = safe_path(path)
        if self.path.exists():
            require(self.read_status(self.path).get('format') == self.FORMAT_VERSION,
                    '历史工作区仅支持 --status；新路线须使用新的工作区')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.owner = open(str(self.path) + '.lock', 'a+')
        try:
            fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.db = sqlite3.connect(self.path, check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS calls(
                    id INTEGER PRIMARY KEY, request_key TEXT NOT NULL, occurrence INTEGER NOT NULL,
                    request TEXT NOT NULL, started REAL NOT NULL, ended REAL,
                    status INTEGER, response TEXT, error TEXT, usage TEXT);
                CREATE INDEX IF NOT EXISTS replay ON calls(request_key, occurrence);
                CREATE TABLE IF NOT EXISTS snapshots(
                    id INTEGER PRIMARY KEY, stage TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL);
            ''')
        except BaseException:
            if hasattr(self, 'db'):
                self.db.close()
            self.owner.close()
            raise

    @staticmethod
    def read_status(path):
        """只读兼容历史库；不创建文件、不加载智能体或密钥。"""
        path = safe_path(path)
        require(path.is_file(), '工作区不存在：' + str(path))
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            meta = {k: json.loads(v) for k, v in db.execute('SELECT key,value FROM meta')}
            keys = ('format', 'status', 'source', 'stage', 'started_at', 'finished_at', 'termination',
                    'source_sha256', 'fingerprint', 'policy', 'output', 'log_dir', 'phase', 'position', 'review_position', 'review_state', 'export')
            result = {k: meta[k] for k in keys if k in meta}
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'calls' in tables and meta.get('format') in range(26, NovelExtractStorage.FORMAT_VERSION + 1):
                result['requests'] = db.execute('SELECT count(*) FROM calls').fetchone()[0]
                result['responses'] = db.execute('SELECT count(*) FROM calls WHERE response IS NOT NULL').fetchone()[0]
                result['unknown_requests'] = db.execute('SELECT count(*) FROM calls WHERE ended IS NULL').fetchone()[0]
            if 'records' in tables:
                result['legacy_records'] = db.execute('SELECT count(*) FROM records').fetchone()[0]
            if 'pages' in tables:
                result['legacy_pages'] = db.execute('SELECT count(*) FROM pages').fetchone()[0]
            return result

    @contextmanager
    def transaction(self):
        with self.lock:
            outer = not self.db.in_transaction
            if outer:
                self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
                if outer:
                    self.db.commit()
            except BaseException:
                if outer:
                    self.db.rollback()
                raise

    def get(self, key, default=None):
        with self.lock:
            row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.transaction():
            self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, encode(value)))

    def snapshot(self, stage, payload):
        """追加完整阶段输出；后续修复不会覆盖旧值。"""
        with self.transaction():
            self.db.execute('INSERT INTO snapshots(stage,payload,created) VALUES(?,?,?)',
                            (stage, encode(payload), time.time()))

    def latest(self, stage, default=None):
        with self.lock:
            row = self.db.execute('SELECT payload FROM snapshots WHERE stage=? ORDER BY id DESC LIMIT 1',
                                  (stage,)).fetchone()
            return json.loads(row[0]) if row else default

    def reserve(self, request_key, occurrence, request, limit, can_send=True):
        """同一实验累计计数，含重试与中断后的未知请求；先落盘再发送。"""
        with self.transaction():
            cached = self.db.execute('''SELECT * FROM calls WHERE request_key=? AND occurrence=?
                                       AND ended IS NOT NULL ORDER BY id DESC LIMIT 1''',
                                     (request_key, occurrence)).fetchone()
            if cached:
                return dict(cached), True
            if not can_send:
                raise RunStopped('未设置配置指定的密钥环境变量')
            count = self.db.execute('SELECT count(*) FROM calls').fetchone()[0]
            if count >= limit:
                raise RunStopped(f'累计请求已达上限 {limit}，包含重试与未知请求')
            cursor = self.db.execute('''INSERT INTO calls(request_key,occurrence,request,started)
                                       VALUES(?,?,?,?)''', (request_key, occurrence, encode(request), time.time()))
            return {'id': cursor.lastrowid}, False

    def received(self, call_id, status, response, usage):
        with self.transaction():
            self.db.execute('UPDATE calls SET ended=?,status=?,response=?,usage=? WHERE id=?',
                            (time.time(), status, response, encode(usage), call_id))

    def failed(self, call_id, error):
        with self.transaction():
            self.db.execute('UPDATE calls SET ended=?,error=? WHERE id=?', (time.time(), error, call_id))

    def close(self):
        self.db.close()
        self.owner.close()
