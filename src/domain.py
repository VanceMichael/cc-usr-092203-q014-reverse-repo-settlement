"""读取并检查项目共享资料。"""

import json
from pathlib import Path

def load_domain(path: Path) -> dict:
    """读取字段完整且带版本的业务资料。"""
    value = json.loads(path.read_text(encoding="utf-8"))
    required = {"domain", "version", "sample_id", "actors", "facts", "constraints"}
    if not required.issubset(value):
        raise ValueError("共享资料缺少必要字段")
    if value["version"] < 1 or len(value["actors"]) < 2 or len(value["facts"]) < 2:
        raise ValueError("共享资料内容不完整")
    return value
