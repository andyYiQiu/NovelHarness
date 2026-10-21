"""本地 Jina v3 事件语义编码；不调用提取模型，不自动下载或访问 API。"""
from hashlib import file_digest, sha256
from importlib import import_module
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType
from weakref import finalize


MODEL_NAME = "jinaai/jina-embeddings-v3"
DEFAULT_MODEL_PATH = Path(__file__).resolve().parents[1] / ".embedding_models/JinaV3"
PROFILE_FIELDS = ("embedding_model", "model_revision", "embedding_task", "dimension")


def model_revision(directory):
    """同时绑定权重、分词器和编码实现；移动相同文件不改变版本。"""
    result = sha256()
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.suffix in (".safetensors", ".json", ".py", ".txt"):
            with path.open("rb") as stream:
                checksum = file_digest(stream, "sha256").hexdigest()
            result.update((path.relative_to(directory).as_posix() + "\0" + checksum + "\n").encode())
    return "sha256:" + result.hexdigest()


def stop_worker(process):
    """释放本实例专用的编码进程，不影响其他任务。"""
    try:
        process.stdin.close()
    except OSError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    process.stdout.close()


class JinaEmbedding:
    """复用一个本地编码进程，隔离 PyTorch/FAISS 的 OpenMP；支持 with 及时释放。"""

    def __init__(self, model_path=DEFAULT_MODEL_PATH, device="cpu"):
        self.path = Path(model_path).expanduser().resolve()
        for name in ("config.json", "model.safetensors", "tokenizer.json",
                     "configuration_xlm_roberta.py", "modeling_lora.py"):
            if not (self.path / name).is_file():
                raise ValueError(f"缺少本地 Jina 模型文件：{self.path / name}；请先准备完整模型目录")
        self.profile = dict(zip(PROFILE_FIELDS, (MODEL_NAME, model_revision(self.path), "text-matching", 512)))
        self.device = device
        self.model = None
        self.worker = None
        self.cleanup = None

    def encode(self, texts, *, batch_size=16):
        import numpy as np

        if not texts or any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError("embedding 输入须为非空事件文本列表")
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise ValueError("batch_size 须为正整数")
        if self.worker is None:
            env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
            env.setdefault("HF_HOME", str(DEFAULT_MODEL_PATH.parent / "Cache"))
            self.worker = subprocess.Popen(
                [sys.executable, "-B", str(Path(__file__).resolve()), str(self.path), self.device],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8", env=env,
            )
            self.cleanup = finalize(self, stop_worker, self.worker)
        try:
            self.worker.stdin.write(json.dumps({"texts": texts, "batch_size": batch_size}, ensure_ascii=False) + "\n")
            self.worker.stdin.flush()
            reply = self.worker.stdout.readline()
            if not reply:
                raise RuntimeError("本地编码进程退出，未返回向量；本批不会保存")
            result = json.loads(reply)
            if "error" in result:
                raise RuntimeError(result["error"])
            return np.asarray(result["vectors"], dtype="float32")
        except (OSError, ValueError, RuntimeError):
            self.close()
            raise

    def close(self):
        if self.cleanup is not None:
            self.cleanup()
        self.worker = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _encode_local(self, texts, *, batch_size):
        """仅在独立编码进程执行；同一进程的后续批次复用模型。"""
        if self.model is None:
            os.environ.setdefault("HF_HOME", str(DEFAULT_MODEL_PATH.parent / "Cache"))
            import torch

            # Jina 的官方实现包含多层相对导入；作为完整本地包加载，不下载远端代码。
            package_name = "_novel_jina_" + self.profile["model_revision"].split(":")[1]
            if package_name not in sys.modules:
                package = ModuleType(package_name)
                package.__path__ = [str(self.path)]
                sys.modules[package_name] = package
            config_class = import_module(package_name + ".configuration_xlm_roberta").XLMRobertaFlashConfig
            model_class = import_module(package_name + ".modeling_lora").XLMRobertaLoRA
            if self.device == "cpu":
                torch.set_num_threads(min(4, os.cpu_count() or 1))
            config = config_class.from_pretrained(self.path, local_files_only=True)
            config.use_flash_attn = False
            config._name_or_path = str(self.path)
            self.model = model_class.from_pretrained(
                self.path, config=config, local_files_only=True,
                use_safetensors=True, torch_dtype=torch.float32,
            ).to(self.device).eval()
        return self.model.encode(
            texts, task="text-matching", truncate_dim=512, normalize_embeddings=True,
            batch_size=batch_size, show_progress_bar=False,
        )


if __name__ == "__main__":
    encoder = JinaEmbedding(Path(sys.argv[1]), sys.argv[2])
    for line in sys.stdin:
        try:
            request = json.loads(line)
            with redirect_stdout(sys.stderr):
                vectors = encoder._encode_local(**request)
            reply = {"vectors": vectors.tolist()}
        except Exception as exc:
            reply = {"error": str(exc)}
        print(json.dumps(reply, allow_nan=False), flush=True)
