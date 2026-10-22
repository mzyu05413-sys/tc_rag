"""Object identity and deterministic content fingerprints, without I/O."""

import hashlib
import json
import re
from typing import Any
from uuid import uuid4

from pydantic import BaseModel


def new_id(prefix: str) -> str:
    """Create an opaque identity, independent of object content."""
    if not re.fullmatch(r"[a-z][a-z0-9_]*", prefix):
        raise ValueError("ID prefix must use lowercase letters, digits or underscores")
    return f"{prefix}_{uuid4().hex}"


def fingerprint(value: Any) -> str:
    """Hash canonical JSON: keys sorted, list order and text preserved."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
