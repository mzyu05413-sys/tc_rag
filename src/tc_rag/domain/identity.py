"""生成对象标识与稳定内容指纹，不执行输入输出操作。"""

import hashlib
import json
import re
from typing import Any
from uuid import uuid4

from pydantic import BaseModel


def new_id(prefix: str) -> str:
    """生成独立于对象内容的唯一标识。"""
    # ID 用于对象间引用；内容相同的两个对象也可以拥有不同身份。
    if not re.fullmatch(r"[a-z][a-z0-9_]*", prefix):
        raise ValueError("ID prefix must use lowercase letters, digits or underscores")
    return f"{prefix}_{uuid4().hex}"


def fingerprint(value: Any) -> str:
    """对规范 JSON 计算哈希：排序字典键，保留列表顺序与原始文本。"""
    # Pydantic 对象先转为 JSON 兼容值，避免直接编码对象实例。
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    # 只规范字典键和编码格式，不修改业务文本，也不自动移除随机 ID。
    # 调用方应挑选参与比较的字段；否则整个对象的 ID 变化也会改变指纹。
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
