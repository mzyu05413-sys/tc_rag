"""可识别的运行失败，不让应用层依赖某个 SDK 或终端框架。"""


class StartupConfigurationError(ValueError):
    """启动配置错误；消息仅由程序生成，可展示字段名但不含配置值或凭据。"""


class WorkflowError(ValueError):
    """可展示的流程错误；只允许程序固定说明，不拼入模型或第三方异常正文。"""


class EmbeddingProviderError(RuntimeError):
    """向量服务失败；只携带适配器生成的状态码，不携带响应正文。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("向量模型调用失败：" + code)


class ContextBudgetError(ValueError):
    """只携带预算数字的可展示异常，不包含需求文本、模型响应或密钥。"""

    def __init__(self, estimated_tokens: int, limit: int) -> None:
        self.estimated_tokens = estimated_tokens
        self.limit = limit
        super().__init__(
            f"请求上下文估算为 {estimated_tokens} token，超过 runtime.max_context_tokens={limit}。"
            "估算包含原文、定位信息和输出结构；请按模型能力调整预算，或减小分块。"
        )


class OutputTruncatedError(RuntimeError):
    """输出被截断，上层需要缩小批次；残缺结果不得落库。"""


class VisionReadError(RuntimeError):
    """视觉适配器生成的固定错误码，可用于问题清单，不包含供应商响应正文。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("图片读取失败：" + code)
