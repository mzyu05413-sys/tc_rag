"""定义 RAG 生成输入；路径读取和目录创建由入口或适配器负责。"""

from pathlib import Path
from typing import Literal

from pydantic import Field

from tc_rag.domain.models import DomainModel


class GenerationPolicy(DomainModel):
    """生成规范版本独立于用户指令，便于记录来源和计算重算指纹。"""

    prompt_version: str = Field(default="v1", min_length=1)
    skill_version: str = Field(default="v1", min_length=1)
    granularity: Literal["separate", "combine_field"] = "separate"


class PointsRequest(DomainModel):
    """需求文件经 RAG 到内部测试点事实。"""

    document_path: Path
    instruction: str = Field(min_length=1)
    output_dir: Path
    policy: GenerationPolicy = Field(default_factory=GenerationPolicy)


class CasesRequest(DomainModel):
    """从指定运行的 SQLite 测试点生成用例；XMind 只作为输出目录视图。"""

    run_id: str = Field(min_length=1)
    instruction: str = Field(min_length=1)
    output_dir: Path
    policy: GenerationPolicy = Field(default_factory=GenerationPolicy)
