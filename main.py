"""AutoGen 小说资料抽取入口。"""
import sys
from importlib.util import find_spec

if sys.version_info < (3, 10):
    raise SystemExit('需要 Python 3.10+；请使用项目 .venv/bin/python -B main.py。')
if '--status' not in sys.argv and any(find_spec(name) is None for name in ('autogen_agentchat', 'autogen_core', 'httpx', 'pydantic')):
    raise SystemExit('缺少依赖；请按 requirements.txt 安装到项目 .venv。')

from workflows.NovelExtractWorkflow import NovelExtractWorkflow

if __name__ == '__main__':
    raise SystemExit(NovelExtractWorkflow().run())
