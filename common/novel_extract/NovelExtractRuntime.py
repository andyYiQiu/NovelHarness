"""运行边界：稳定序列化、路径保护及版本指纹，不包含抽取规则。"""
from hashlib import sha256
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def digest(value):
    return sha256(encode(value).encode('utf-8')).hexdigest()


def safe_path(value):
    path = Path(value).expanduser().absolute()
    require(not any(p.is_symlink() for p in (path, *path.parents)), '路径不能包含符号链接：' + str(path))
    return path.resolve()


def fingerprint():
    """包含实际运行代码、原始提示词、依赖及投影契约；不读取模型密钥。"""
    paths = [ROOT / 'main.py', ROOT / 'requirements.txt']
    for folder in ('agents', 'common', 'exporters', 'model', 'storage', 'workflows', 'prompts'):
        paths += [p for p in (ROOT / folder).rglob('*')
                  if p.suffix in ('.py', '.md') and '__pycache__' not in p.parts]
    return digest({str(p.relative_to(ROOT)): sha256(p.read_bytes()).hexdigest() for p in sorted(set(paths))})


class RunStopped(Exception):
    """预算或接口政策停止信号，不能被普通格式纠错吞掉。"""


class JobFailed(Exception):
    """任务在有限修复内未交付；候选和反馈保留。"""


class DeliveryStructureError(ValueError):
    """一份交付缺少完成原子修订所需的结构字段。"""
