"""本地 ONNX embedding（bge-small-zh-v1.5）。不可用时退化为 None。"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Sequence

import numpy as np

MODEL_NAME = "bge-small-zh-v1.5"
DEFAULT_DIM = 512


def _warn(msg: str) -> None:
    print(f"  ⚠️  memory embedding：{msg}", file=sys.stderr)


class Embedder:
    """懒加载 ONNX 编码器。ready=False 时 encode 返回 None。"""

    def __init__(self, model_dir: str | Path) -> None:
        self.model_dir = Path(model_dir)
        self.model_name = MODEL_NAME
        self.dim = DEFAULT_DIM
        self._session = None
        self._tokenizer = None
        self._lock = threading.Lock()
        self._failed = False
        self.ready = False

    def warm(self) -> bool:
        return self._ensure() is not None

    def _ensure(self):
        if self._failed:
            return None
        if self._session is not None:
            return self._session
        with self._lock:
            if self._session is not None:
                return self._session
            if self._failed:
                return None
            onnx_path = self._find_onnx()
            tok_path = self.model_dir / "tokenizer.json"
            if onnx_path is None or not tok_path.exists():
                self._failed = True
                _warn(f"模型未就绪（需要 {self.model_dir}/model.onnx + tokenizer.json），降级为关键词检索")
                return None
            try:
                import onnxruntime as ort  # type: ignore
                from tokenizers import Tokenizer  # type: ignore
            except ImportError as e:
                self._failed = True
                _warn(f"缺少依赖（onnxruntime/tokenizers）：{e}；`uv sync --extra memory`")
                return None
            try:
                self._tokenizer = Tokenizer.from_file(str(tok_path))
                # 读 config 拿 hidden size
                cfg = self.model_dir / "config.json"
                if cfg.exists():
                    data = json.loads(cfg.read_text(encoding="utf-8"))
                    self.dim = int(data.get("hidden_size") or data.get("dim") or DEFAULT_DIM)
                opts = ort.SessionOptions()
                opts.inter_op_num_threads = 1
                opts.intra_op_num_threads = 2
                self._session = ort.InferenceSession(
                    str(onnx_path),
                    sess_options=opts,
                    providers=["CPUExecutionProvider"],
                )
                self.ready = True
                return self._session
            except Exception as e:  # noqa: BLE001
                self._failed = True
                _warn(f"加载失败：{e}")
                return None

    def _find_onnx(self) -> Path | None:
        for name in ("model.onnx", "model_int8.onnx", "model_O4.onnx", "onnx/model.onnx"):
            p = self.model_dir / name
            if p.exists() and p.stat().st_size > 1000:
                return p
        # 任意 .onnx
        cands = sorted(self.model_dir.glob("**/*.onnx"))
        return cands[0] if cands else None

    def encode(self, texts: Sequence[str]) -> list[list[float]] | None:
        if not texts:
            return []
        sess = self._ensure()
        if sess is None or self._tokenizer is None:
            return None
        try:
            return [self._encode_one(t) for t in texts]
        except Exception as e:  # noqa: BLE001
            _warn(f"encode 失败：{e}")
            return None

    def encode_one(self, text: str) -> list[float] | None:
        out = self.encode([text])
        if out is None:
            return None
        return out[0] if out else None

    def _encode_one(self, text: str) -> list[float]:
        assert self._tokenizer is not None and self._session is not None
        # BGE 推荐加指令前缀（中文检索）
        q = f"为这个句子生成表示以用于检索：{text}" if text else text
        enc = self._tokenizer.encode(q)
        ids = enc.ids[:512]
        mask = enc.attention_mask[:512] if hasattr(enc, "attention_mask") else [1] * len(ids)
        # tokenizers EncodeOutput 用 attention_mask 属性；兜底全 1
        if not mask or len(mask) != len(ids):
            mask = [1] * len(ids)
        type_ids = enc.type_ids[:512] if hasattr(enc, "type_ids") and enc.type_ids else [0] * len(ids)
        if len(type_ids) != len(ids):
            type_ids = [0] * len(ids)

        input_ids = np.array([ids], dtype=np.int64)
        attention_mask = np.array([mask], dtype=np.int64)
        token_type_ids = np.array([type_ids], dtype=np.int64)

        feeds = {}
        for inp in self._session.get_inputs():
            name = inp.name
            if "input_ids" in name:
                feeds[name] = input_ids
            elif "attention_mask" in name:
                feeds[name] = attention_mask
            elif "token_type" in name:
                feeds[name] = token_type_ids
            else:
                # 未知输入：尽量按形状填
                feeds[name] = input_ids

        outs = self._session.run(None, feeds)
        arr = outs[0]
        # [1, seq, dim] mean-pool with mask，或 [1, dim]
        if arr.ndim == 3:
            mask_f = attention_mask.astype(np.float32)[:, :, None]
            summed = (arr * mask_f).sum(axis=1)
            denom = np.clip(mask_f.sum(axis=1), 1e-6, None)
            vec = (summed / denom)[0]
        else:
            vec = arr[0]
        # L2 normalize
        n = float(np.linalg.norm(vec))
        if n > 0:
            vec = vec / n
        self.dim = int(vec.shape[0])
        return vec.astype(np.float32).tolist()


class NullEmbedder:
    model_name = "null"
    dim = 0
    ready = False

    def warm(self) -> bool:
        return False

    def encode(self, texts: Sequence[str]) -> list[list[float]] | None:
        return None

    def encode_one(self, text: str) -> list[float] | None:
        return None


def build_embedder(model_dir: str | Path | None) -> Embedder | NullEmbedder:
    if not model_dir:
        return NullEmbedder()
    return Embedder(model_dir)
