"""token 计数器工厂；编码表加载失败时明确失败，不偷偷换成字符估算。"""

from tc_rag.application.services.chunking import TokenCounter, character_estimate


def token_counter(name: str) -> TokenCounter:
    if name == "character_approx":
        return character_estimate
    if name != "tiktoken_approx":
        raise ValueError("未知 token 计数器")
    import tiktoken

    encoding = tiktoken.get_encoding("cl100k_base")
    return lambda text: len(encoding.encode(text, disallowed_special=()))
