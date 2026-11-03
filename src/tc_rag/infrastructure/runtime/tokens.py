"""token 计数器工厂；编码表加载失败时明确失败，不偷偷换成字符估算。"""

import ast
import hashlib
import importlib
import inspect
import os
import tempfile
from pathlib import Path

from tc_rag.application.services.chunking import TokenCounter, character_estimate


def token_counter(name: str) -> TokenCounter:
    if name == "character_approx":
        return character_estimate
    if name != "tiktoken_approx":
        raise ValueError("未知 token 计数器")
    import tiktoken

    encoding = tiktoken.get_encoding("cl100k_base")
    return lambda text: len(encoding.encode(text, disallowed_special=()))


def offline_token_counter(name: str) -> TokenCounter:
    """诊断入口只使用已校验的本地编码缓存；缺失时不触发编码表下载。"""
    if name == "character_approx":
        return character_estimate
    if name != "tiktoken_approx":
        raise ValueError("未知 token 计数器")
    public = importlib.import_module("tiktoken_ext.openai_public")

    tree = ast.parse(inspect.getsource(public.cl100k_base))
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
            continue
        if call.func.id != "load_tiktoken_bpe":
            continue
        url = ast.literal_eval(call.args[0])
        expected = next(
            ast.literal_eval(item.value) for item in call.keywords if item.arg == "expected_hash"
        )
        directory = os.environ.get(
            "TIKTOKEN_CACHE_DIR",
            os.environ.get(
                "DATA_GYM_CACHE_DIR", str(Path(tempfile.gettempdir()) / "data-gym-cache")
            ),
        )
        cache = Path(directory) / hashlib.sha1(url.encode()).hexdigest()
        if not directory or not cache.is_file():
            raise ValueError("离线编码表缓存缺失")
        if hashlib.sha256(cache.read_bytes()).hexdigest() != expected:
            raise ValueError("离线编码表缓存校验失败")
        return token_counter(name)
    raise ValueError("当前编码表布局未识别，离线诊断不尝试下载")
