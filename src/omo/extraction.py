"""从经授权的附件中提取公告字段。

表格附件按 CSV 解析(首行字段名、次行取值); 图片附件本身不可直接解析,
需要调用方注入 OCR 能力, 未注入时拒绝提取, 避免静默生成无来源的字段。
"""

from __future__ import annotations

import csv
import io
from typing import Callable, Mapping, Optional, Protocol


class Extractor(Protocol):
    def extract(self, content: bytes) -> dict[str, str]: ...


class CsvTableExtractor:
    """解析两行式 CSV: 第一行字段名, 第二行取值。"""

    def extract(self, content: bytes) -> dict[str, str]:
        text = content.decode("utf-8-sig")
        rows = [row for row in csv.reader(io.StringIO(text)) if row]
        if len(rows) < 2:
            raise ValueError("表格附件缺少表头或数据行")
        header, values = rows[0], rows[1]
        if len(header) != len(values):
            raise ValueError("表格附件表头与数据列数不一致")
        return {name.strip(): value.strip() for name, value in zip(header, values)}


class ImageExtractor:
    """图片附件经注入的 OCR 函数提取字段。"""

    def __init__(self, ocr: Optional[Callable[[bytes], Mapping[str, str]]] = None):
        self._ocr = ocr

    def extract(self, content: bytes) -> dict[str, str]:
        if self._ocr is None:
            raise ValueError("图片附件缺少 OCR 提取能力, 无法核对")
        return {str(k).strip(): str(v).strip() for k, v in self._ocr(content).items()}
