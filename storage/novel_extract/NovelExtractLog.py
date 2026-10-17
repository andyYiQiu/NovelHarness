"""智能体执行日志及完整 HTTP 请求响应的本地记录。"""
import json
import logging
import threading
import time

from common.novel_extract.NovelExtractRuntime import safe_path


class NovelExtractLog(logging.Handler):
    def __init__(self, directory, terminal='summary'):
        super().__init__(logging.INFO)
        self.directory = safe_path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.file = open(self.directory / 'run.jsonl', 'a', encoding='utf-8')
        self.terminal = terminal
        self.write_lock = threading.RLock()
        self.secret = None

    def write(self, kind, **data):
        text = json.dumps({'at': time.time(), 'kind': kind, **data}, ensure_ascii=False, allow_nan=False)
        if self.secret:
            text = text.replace(self.secret, '[REDACTED]')
        with self.write_lock:
            self.file.write(text + '\n')
            self.file.flush()
            if self.terminal == 'full':
                print(text, flush=True)
            elif self.terminal == 'summary' and kind in ('stage', 'received', 'error', 'finished', 'replay'):
                safe_data = json.loads(text)
                print(json.dumps({'kind': kind, **{k: v for k, v in safe_data.items()
                      if k not in ('body', 'request', 'response')}}, ensure_ascii=False), flush=True)

    def emit(self, record):
        self.write('stage', level=record.levelname, message=record.getMessage())

    def close(self):
        if hasattr(self, 'file') and not self.file.closed:
            self.file.close()
        super().close()
