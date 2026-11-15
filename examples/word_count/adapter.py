"""示例能力：只依赖标准库的空白分词统计器。"""


def run(payload: dict) -> dict:
    """接收已验证契约的输入，返回词数与 Unicode 字符数。"""
    text = payload["text"]
    if not text:
        raise ValueError("text 不能为空")
    return {"words": len(text.split()), "characters": len(text)}
