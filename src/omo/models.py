"""公告核对服务的领域模型。

核心约定:
- 公告字段一律保存为规范字符串, 数值字段经 canon_number 归一,
  避免 1.80 与 1.8 被判成两个值;
- 每个字段都带 Provenance, 记录来自结构化结果、附件还是人工修改;
- 公告以版本链演进, 冻结后内容哈希固定, 勘误只能追加新版本。
"""

from __future__ import annotations

import enum
import hashlib
import json
from dataclasses import dataclass, field as dc_field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Mapping, Optional


class DomainError(Exception):
    """领域规则被拒绝。"""


class ConflictError(DomainError):
    """当前状态不允许该操作。"""


class NotFoundError(DomainError):
    """目标对象不存在。"""


class VersionStatus(str, enum.Enum):
    DRAFT = "draft"            # 草稿, 可随重抓或修改更新
    REVIEWED = "reviewed"      # 已经复核人签认
    FROZEN = "frozen"          # 已冻结, 内容哈希固定
    PUBLISHED = "published"    # 当前对外有效版本
    SUPERSEDED = "superseded"  # 已被勘误版本取代, 仅用于追溯


class SourceKind(str, enum.Enum):
    STRUCTURED = "structured"    # 结构化操作结果
    ATTACHMENT = "attachment"    # 经授权的表格或图片附件
    MANUAL_EDIT = "manual_edit"  # 人工修改(含勘误)


NUMERIC_FIELDS = frozenset({"rate_percent", "amount_yi", "term_days"})


def canon_number(value) -> str:
    """把数值规范成可比较的字面量。"""
    d = Decimal(str(value).strip())
    if d == d.to_integral_value():
        return str(d.quantize(Decimal("1")))
    return format(d.normalize(), "f")


def values_equal(field_name: str, left, right) -> bool:
    """按字段类型比较两个来源的取值。"""
    if field_name in NUMERIC_FIELDS:
        try:
            return canon_number(left) == canon_number(right)
        except (InvalidOperation, ValueError):
            return False
    return str(left).strip() == str(right).strip()


def canonical_hash(announcement_id: str, version: int, fields: Mapping[str, str]) -> str:
    """冻结内容的稳定哈希, 三个发布载体共用。"""
    payload = json.dumps(
        {"announcement_id": announcement_id, "version": version, "fields": dict(fields)},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Provenance:
    """单个字段的来源: 哪种渠道、哪份凭据、谁、何时。"""

    kind: SourceKind
    source_id: str
    actor: str
    at: datetime


@dataclass(frozen=True)
class OperationResult:
    """结构化操作结果(抓取或接口推送的业务结果)。"""

    operation_id: str
    trade_date: date
    operation_type: str
    term_days: int
    rate_percent: Decimal
    amount_yi: Decimal
    allotment: str
    payload_id: str  # 抓取批次标识, 用于幂等去重

    @property
    def is_zero_injection(self) -> bool:
        return self.amount_yi == 0

    def field_map(self) -> dict[str, str]:
        return {
            "operation_type": self.operation_type,
            "term_days": canon_number(self.term_days),
            "rate_percent": canon_number(self.rate_percent),
            "amount_yi": canon_number(self.amount_yi),
            "allotment": self.allotment,
        }

    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "operation_id": self.operation_id,
                "trade_date": self.trade_date.isoformat(),
                "fields": self.field_map(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SummaryEntry:
    """业务汇总中某笔操作的权威数值。"""

    operation_id: str
    operation_type: str
    term_days: int
    rate_percent: Decimal
    amount_yi: Decimal
    allotment: str

    def field_map(self) -> dict[str, str]:
        return {
            "operation_type": self.operation_type,
            "term_days": canon_number(self.term_days),
            "rate_percent": canon_number(self.rate_percent),
            "amount_yi": canon_number(self.amount_yi),
            "allotment": self.allotment,
        }


@dataclass(frozen=True)
class BusinessSummary:
    """某交易日的业务汇总, 是公告交叉校验的权威来源。"""

    trade_date: date
    entries: Mapping[str, SummaryEntry]


@dataclass(frozen=True)
class Template:
    """发布模板: 规定公告必须具备的字段。"""

    template_id: str
    required_fields: tuple[str, ...]
    zero_injection: bool = False


@dataclass(frozen=True)
class Attachment:
    """经授权的表格或图片附件及其提取结果。"""

    attachment_id: str
    announcement_id: str
    kind: str  # "table" | "image"
    content_hash: str
    uploaded_by: str
    authorized_by: str
    uploaded_at: datetime
    extracted: Mapping[str, str]
    supersedes: Optional[str]  # 被本附件取代的上一版附件 id


class EditStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass
class ManualEdit:
    """人工修改申请: 必须填写理由, 且由他人复核。"""

    edit_id: str
    announcement_id: str
    field_name: str
    old_value: str
    new_value: str
    reason: str
    editor: str
    created_at: datetime
    status: EditStatus = EditStatus.PENDING
    reviewer: Optional[str] = None
    reviewed_at: Optional[datetime] = None


@dataclass
class AnnouncementVersion:
    """公告的一个版本; 冻结后 content_hash 不再变化。"""

    announcement_id: str
    version: int
    fields: dict[str, str]
    provenance: dict[str, Provenance]
    status: VersionStatus
    created_at: datetime
    created_by: str
    reviewed_by: Optional[str] = None
    content_hash: Optional[str] = None
    supersedes: Optional[int] = None
    correction_reason: Optional[str] = None  # 勘误理由


@dataclass
class Announcement:
    """一笔操作对应一份公告; 重复抓取只会演进版本, 不会生成第二份。"""

    announcement_id: str
    operation_id: str
    trade_date: date
    versions: list[AnnouncementVersion] = dc_field(default_factory=list)
    attachments: list[Attachment] = dc_field(default_factory=list)
    published_version: Optional[int] = None

    @property
    def latest(self) -> AnnouncementVersion:
        return self.versions[-1]


@dataclass(frozen=True)
class Finding:
    """交叉校验发现的问题。"""

    severity: str  # "error" | "warning"
    code: str
    field_name: Optional[str]
    message: str


@dataclass(frozen=True)
class IngestOutcome:
    announcement_id: str
    created: bool  # False 表示命中已有公告(重复抓取或版本演进)
    findings: tuple[Finding, ...]


@dataclass(frozen=True)
class PublishedView:
    """某一发布载体读到的对外内容; 三个载体的 content_hash 必须一致。"""

    announcement_id: str
    version: int
    content_hash: str
    channel: str  # "api" | "web" | "download"
    body: Mapping


@dataclass(frozen=True)
class TraceHit:
    """从媒体引用的某个数值反查到的一次出现。"""

    announcement_id: str
    version: int
    field_name: str
    value: str
    provenance: Optional[Provenance]
    reviewed_by: Optional[str]
    correction_chain: tuple[str, ...]  # 截至该版本的勘误理由链
