"""运行清单逐字段白名单，凭据及完整端点不进入输出。"""

import json
from pathlib import Path

from pydantic import SecretStr

from tc_rag.core.config import AppConfig
from tc_rag.infrastructure.runtime.kb_manifest import runtime_manifest


def test_manifest_excludes_credential_fields():
    config = AppConfig()
    config.models.embedding.api_key = SecretStr("fictional-secret-marker")
    config.models.embedding.api_key_env = "FICTIONAL_PRIVATE_ENV"
    config.models.embedding.base_url = "https://fictional-private-host.example"
    manifest = runtime_manifest(config, Path("prompts"))
    text = json.dumps(manifest)
    assert "fictional-secret-marker" not in text
    assert "FICTIONAL_PRIVATE_ENV" not in text
    assert "fictional-private-host" not in text
    assert "api_key" not in text
    assert len(manifest["source_sha256"]) == 64
