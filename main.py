"""小说提取、repository 入库与事件语义索引的统一入口。"""
import sys
from importlib.util import find_spec

if sys.version_info < (3, 11):
    raise SystemExit('需要 Python 3.11+；请使用项目 .venv/bin/python -B main.py。')
dependencies = ('autogen_agentchat', 'autogen_core', 'httpx', 'pydantic',
                'sqlalchemy', 'numpy', 'faiss', 'torch', 'transformers', 'einops')
if '--status' not in sys.argv and any(find_spec(name) is None for name in dependencies):
    raise SystemExit('缺少依赖；请按 requirements.txt 安装到项目 .venv。')

from workflows.NovelExtractWorkflow import NovelExtractWorkflow

if __name__ == '__main__':
    raise SystemExit(NovelExtractWorkflow().run())
