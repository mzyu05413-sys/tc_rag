"""只对实际发送证据核验引用，空范围拒答不调用生成模型。"""

import pytest
from test_kb_ingestion import setup

from tc_rag.application.kb.answer import AnswerService
from tc_rag.application.kb.search import SearchService
from tc_rag.application.ports.contracts import Capabilities, ModelResult
from tc_rag.application.ports.errors import ContextBudgetError


class AnswerModel:
    capabilities = Capabilities(structured_output=True)

    def __init__(self, invalid=False):
        self.calls = 0
        self.invalid = invalid

    async def generate(self, request, schema):
        self.calls += 1
        evidence = request["evidence"][0]
        return ModelResult(output=schema.model_validate({
            "status": "answered", "claims": [{
                "text": "会签需要全员同意。", "citations": [{
                    "chunk_id": evidence["chunk_id"],
                    "quote": "不存在的引文" if self.invalid else evidence["text"],
                }],
            }],
        }))


@pytest.mark.asyncio
async def test_answer_and_empty_scope(tmp_path):
    components, _, _, paths = setup(tmp_path)
    await components.ingest("crm", paths)
    model = AnswerModel()
    service = AnswerService(SearchService(components), model, "fixture")
    result = await service.ask("crm", "会签条件")
    assert result["answer"]["status"] == "answered"
    empty = await service.ask("crm", "会签条件", document_ids=set())
    assert empty["answer"]["status"] == "abstained"
    assert model.calls == 1


@pytest.mark.asyncio
async def test_invalid_citation_is_error(tmp_path):
    components, _, _, paths = setup(tmp_path)
    await components.ingest("crm", paths)
    with pytest.raises(ValueError, match="引文"):
        await AnswerService(SearchService(components), AnswerModel(True), "fixture").ask(
            "crm", "会签条件",
        )


@pytest.mark.asyncio
async def test_oversized_question_fails_before_generation(tmp_path):
    components, _, _, paths = setup(tmp_path)
    await components.ingest("crm", paths)
    model = AnswerModel()
    service = AnswerService(SearchService(components), model, "fixture", max_input_tokens=10)
    with pytest.raises(ContextBudgetError):
        await service.ask("crm", "规则" * 100)
    assert model.calls == 0
