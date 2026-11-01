"""虚构跨页表格验证续页补表头，不使用真实需求资料。"""

import json

from tc_rag.application.services.chunking import structural_chunks
from tc_rag.domain import models as m


def table(block_id: str, page: int, order: int, rows: list[list[str]]) -> m.DocumentBlock:
    return m.DocumentBlock(
        block_id=block_id,
        document_id="d",
        kind="table",
        heading_path=["1.订单"],
        order=order,
        content=json.dumps(
            {"schema_version": "1.0", "candidate": True, "rows": rows},
            ensure_ascii=False,
        ),
        locator=m.Locator(page=page),
    )


def header_and_rows(table_block: m.DocumentBlock) -> list[list[str]]:
    return json.loads(table_block.content)["rows"]


def chunk_for(result, block_id: str):
    return next(chunk for chunk in result.chunks if chunk.sources[-1].block_id == block_id)


def test_cross_page_table_continuation_repeats_header():
    first = table(
        "t1",
        1,
        0,
        [
            ["字段名", "类型", "备注"],
            ["订单编号", "文本", "系统字段"],
            ["订单名称", "文本", "必填"],
        ],
    )
    second = table(
        "t2",
        2,
        1,
        [["单价", "数字", "自定义字段"], ["数量", "数字", "自定义字段"]],
    )
    result = structural_chunks([first, second], 120, 0)
    chunk = chunk_for(result, "t2")
    assert chunk.sources[0].block_id == "t1"
    assert chunk.text.startswith('["字段名", "类型", "备注"]')
    assert chunk.token_estimate == len(chunk.text)
    assert chunk.text.split("\n", 1)[1] == second.content


def test_cross_page_table_with_different_columns_not_repeated():
    first = table("t1", 1, 0, [["字段名", "类型"], ["订单编号", "文本"]])
    second = table("t2", 2, 1, [["字段名", "类型", "备注"], ["单价", "数字", "自定义"]])
    result = structural_chunks([first, second], 120, 0)
    assert all(source.block_id != "t1" for source in chunk_for(result, "t2").sources)


def test_same_page_table_not_treated_as_continuation():
    first = table("t1", 1, 0, [["字段名", "类型"], ["订单编号", "文本"]])
    second = table("t2", 1, 1, [["单价", "数字"], ["数量", "数字"]])
    result = structural_chunks([first, second], 120, 0)
    assert all(source.block_id != "t1" for source in chunk_for(result, "t2").sources)


def test_repeated_header_not_duplicated():
    first = table("t1", 1, 0, [["字段名", "类型"], ["订单编号", "文本"]])
    second = table("t2", 2, 1, [["字段名", "类型"], ["单价", "数字"]])
    result = structural_chunks([first, second], 120, 0)
    assert all(source.block_id != "t1" for source in chunk_for(result, "t2").sources)


def test_header_row_with_preamble_uses_most_non_empty_row():
    first = table(
        "t1",
        1,
        0,
        [[""], ["说明文字"], ["字段名", "类型", "备注"], ["订单编号", "文本", "系统字段"]],
    )
    second = table("t2", 2, 1, [["单价", "数字", "自定义"]])
    result = structural_chunks([first, second], 120, 0)
    assert chunk_for(result, "t2").text.startswith('["字段名", "类型", "备注"]')


def test_three_page_chain_uses_root_header():
    first = table("t1", 1, 0, [["字段名", "类型"], ["订单编号", "文本"]])
    second = table("t2", 2, 1, [["单价", "数字"], ["数量", "数字"]])
    third = table("t3", 3, 2, [["金额", "计算"], ["税率", "数字"]])
    result = structural_chunks([first, second, third], 120, 0)
    second_chunk = chunk_for(result, "t2")
    third_chunk = chunk_for(result, "t3")
    assert second_chunk.sources[0].block_id == "t1"
    assert third_chunk.sources[0].block_id == "t1"
    assert third_chunk.text.startswith('["字段名", "类型"]')
