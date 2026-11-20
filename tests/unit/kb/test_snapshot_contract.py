"""跨产品时期可以共库，重复逻辑版本必须显式选择。"""

import pytest
from pydantic import ValidationError

from tc_rag.domain.kb.models import DocumentVersion, KnowledgeSnapshot


def version(document, revision, product=None):
    return DocumentVersion(
        document_id=document, version_id=revision,
        content_sha256="a" * 64, product_version=product,
    )


def test_snapshot_preserves_mixed_and_unknown_product_versions():
    snapshot = KnowledgeSnapshot(
        kb_id="crm", snapshot_id="s1", embedding_space_id="space",
        documents=[version("approval", "a1", "v1"), version("order", "o1")],
    )
    assert snapshot.documents[0].product_version == "v1"
    assert snapshot.documents[1].product_version is None


@pytest.mark.parametrize("documents", [
    [version("approval", "a1"), version("approval", "a2")],
    [version("approval", "same"), version("order", "same")],
])
def test_snapshot_rejects_ambiguous_members(documents):
    with pytest.raises(ValidationError):
        KnowledgeSnapshot(
            kb_id="crm", snapshot_id="s1", embedding_space_id="space", documents=documents,
        )
