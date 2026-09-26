"""公开市场操作公告核对服务。

接收结构化操作结果与经授权的表格/图片附件，提取字段后与业务汇总、
历史日历和发布模板交叉校验；人工修改必须留理由并经他人复核；公告经
冻结后多载体发布，勘误形成版本链，任一已发数值均可溯源。

本模块只依赖标准库。金额单位统一为亿元，利率为百分数（1.50 表示 1.50%）。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum


# ---------------------------------------------------------------------------
# 基础类型
# ---------------------------------------------------------------------------


class Severity(str, Enum):
    BLOCK = "block"  # 阻断冻结/发布
    WARN = "warn"  # 提示，不阻断


class VersionStatus(str, Enum):
    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    FROZEN = "frozen"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"


# 需要对外披露并逐字段核对的规范字段
CANONICAL_FIELDS = (
    "op_type",
    "term_days",
    "fixed_rate",
    "amount_yi",
    "allocation",
    "maturity_date",
)

FIELD_LABELS = {
    "op_type": "操作类型",
    "term_days": "期限",
    "fixed_rate": "固定利率",
    "amount_yi": "操作量",
    "allocation": "满足方式",
    "maturity_date": "到期日",
    "trade_date": "操作日期",
}

FIELD_ALIASES = {
    "操作类型": "op_type",
    "操作方式": "op_type",
    "类型": "op_type",
    "期限": "term_days",
    "期限品种": "term_days",
    "期限(天)": "term_days",
    "固定利率": "fixed_rate",
    "中标利率": "fixed_rate",
    "利率": "fixed_rate",
    "操作量": "amount_yi",
    "中标量": "amount_yi",
    "交易量": "amount_yi",
    "成交金额": "amount_yi",
    "金额": "amount_yi",
    "满足方式": "allocation",
    "中标方式": "allocation",
    "满足情况": "allocation",
    "到期日": "maturity_date",
    "到期日期": "maturity_date",
    "操作日期": "trade_date",
    "日期": "trade_date",
    "交易日": "trade_date",
    "操作编号": "operation_id",
    "编号": "operation_id",
}

_ALLOCATION_CANONICAL = {
    "全额": "全额满足",
    "全额满足": "全额满足",
    "全额成交": "全额满足",
    "比例": "比例满足",
    "比例满足": "比例满足",
    "比例成交": "比例满足",
}


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _to_date(value) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip())


def parse_rate(value) -> Decimal:
    """1.50% / '1.50' / Decimal 均解析为百分数 Decimal('1.50')。"""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    s = str(value).strip().replace("％", "%").replace(",", "")
    s = s.removesuffix("%")
    return Decimal(s)


def parse_amount_yi(value) -> Decimal:
    """把金额文本归一为亿元：'1800亿元'/'1800亿'/1800/'0.18万亿元'。"""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    s = str(value).strip().replace(",", "").replace(" ", "")
    multiplier = Decimal(1)
    for suffix, mult in (("万亿元", 10000), ("亿元", 1), ("万亿", 10000), ("亿", 1)):
        if s.endswith(suffix):
            multiplier = Decimal(mult)
            s = s[: -len(suffix)]
            break
    if not s:
        raise ValueError("金额为空")
    return Decimal(s) * multiplier


def parse_term_days(value) -> int:
    if isinstance(value, int):
        return value
    s = str(value).strip()
    for suffix in ("天期", "天", "日"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    return int(s)


def normalize_allocation(value: str) -> str:
    s = str(value).strip()
    return _ALLOCATION_CANONICAL.get(s, s)


def normalize_field(name: str, value):
    """按字段类型归一单个字段值；无法识别的值原样保留以便核对时报差异。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if name in ("trade_date", "maturity_date"):
        return _to_date(value)
    if name == "term_days":
        return parse_term_days(value)
    if name == "fixed_rate":
        return parse_rate(value)
    if name == "amount_yi":
        return parse_amount_yi(value)
    if name == "allocation":
        return normalize_allocation(value)
    return str(value).strip()


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


# ---------------------------------------------------------------------------
# 日历与模板
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Calendar:
    """节假日与补班日历。

    holidays: 休市的工作日（节假日调休）；
    makeup_workdays: 需要开市交易的周末（节假日补班）。
    """

    holidays: frozenset[date] = frozenset()
    makeup_workdays: frozenset[date] = frozenset()

    def __init__(self, holidays=(), makeup_workdays=()):
        object.__setattr__(self, "holidays", frozenset(holidays))
        object.__setattr__(self, "makeup_workdays", frozenset(makeup_workdays))

    def is_trading_day(self, day: date) -> bool:
        if day in self.makeup_workdays:
            return True
        if day in self.holidays:
            return False
        return day.weekday() < 5

    def maturity_date(self, trade_day: date, term_days: int) -> date:
        """期限按自然日计算，到期日遇休市顺延至下一交易日。"""
        day = trade_day + timedelta(days=term_days)
        while not self.is_trading_day(day):
            day += timedelta(days=1)
        return day


@dataclass(frozen=True)
class Template:
    template_id: str
    title: str
    required_fields: tuple[str, ...]
    allowed_allocation: frozenset[str] = frozenset(("全额满足", "比例满足"))
    operation_line: str = (
        "中国人民银行于{trade_date}以{op_type}方式开展了{amount_yi}亿元"
        "{term_days}天期{op_type}操作，中标利率{fixed_rate}%，{allocation}，"
        "到期日为{maturity_date}。"
    )
    net_line: str = "今日有{due_yi}亿元逆回购到期，实现净投放{net_yi}亿元。"

    def render_operation(self, values: dict) -> str:
        return self.operation_line.format(**{k: render_scalar(v) for k, v in values.items()})


def render_scalar(value) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


# ---------------------------------------------------------------------------
# 输入：操作结果与附件
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OperationResult:
    """结构化业务结果（业务汇总系统的权威输出）。"""

    operation_id: str
    trade_date: date
    op_type: str
    term_days: int
    fixed_rate: Decimal
    amount_yi: Decimal
    allocation: str
    idempotency_key: str  # 抓取事件去重键
    maturity_date: date | None = None
    source: str = "business-summary"

    @classmethod
    def from_dict(cls, data: dict) -> "OperationResult":
        return cls(
            operation_id=str(data["operation_id"]),
            trade_date=_to_date(data["trade_date"]),
            op_type=str(data["op_type"]).strip(),
            term_days=parse_term_days(data["term_days"]),
            fixed_rate=parse_rate(data["fixed_rate"]),
            amount_yi=parse_amount_yi(data["amount_yi"]),
            allocation=normalize_allocation(data["allocation"]),
            idempotency_key=str(data["idempotency_key"]),
            maturity_date=_to_date(data["maturity_date"]) if data.get("maturity_date") else None,
            source=str(data.get("source", "business-summary")),
        )

    def canonical_values(self) -> dict:
        return {
            "op_type": self.op_type,
            "term_days": self.term_days,
            "fixed_rate": self.fixed_rate,
            "amount_yi": self.amount_yi,
            "allocation": self.allocation,
            "maturity_date": self.maturity_date,
        }

    def payload_hash(self) -> str:
        body = canonical_json(
            {
                "operation_id": self.operation_id,
                "trade_date": self.trade_date.isoformat(),
                **{k: render_scalar(v) if isinstance(v, (Decimal, date)) else v for k, v in self.canonical_values().items()},
            }
        )
        return sha256_hex(body.encode("utf-8"))


@dataclass(frozen=True)
class Attachment:
    """经授权上传的表格或图片附件。

    表格 content 为 CSV 文本或 list[dict]；图片 content 为原始字节，
    由服务构造时注入的 OCR 适配器识别。
    """

    attachment_id: str
    kind: str  # "table" / "image"
    authorized: bool
    filename: str
    content: object
    sha256: str | None = None  # 不传则按 content 计算
    upload_id: str = ""

    def digest(self) -> str:
        if self.sha256:
            return self.sha256
        if isinstance(self.content, bytes):
            return sha256_hex(self.content)
        return sha256_hex(canonical_json(self.content).encode("utf-8"))


@dataclass
class ExtractedRow:
    fields: dict  # 规范字段名 -> 已归一值
    raw: dict
    attachment_id: str
    revision: int
    digest: str


# ---------------------------------------------------------------------------
# 核对记录
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Discrepancy:
    id: str
    code: str  # MISMATCH / MISSING / UNAUTHORIZED / CALENDAR / HISTORY / TEMPLATE
    severity: Severity
    operation_key: str
    field: str | None
    message: str
    values_by_source: dict = field(default_factory=dict)
    resolved_by_edit: str | None = None

    def resolved(self, edit_id: str) -> "Discrepancy":
        return Discrepancy(
            id=self.id,
            code=self.code,
            severity=self.severity,
            operation_key=self.operation_key,
            field=self.field,
            message=self.message,
            values_by_source=dict(self.values_by_source),
            resolved_by_edit=edit_id,
        )


@dataclass
class ManualEdit:
    edit_id: str
    operation_key: str  # 操作匹配键，或 "__header__"
    field_name: str
    old_value: object
    new_value: object
    reason: str
    editor: str
    created_at: datetime
    discrepancy_id: str | None = None
    reviewed_by: str | None = None
    review_note: str | None = None
    reviewed_at: datetime | None = None

    @property
    def approved(self) -> bool:
        return self.reviewed_by is not None


@dataclass
class FrozenManifest:
    content_hash: str
    rendered_text: str
    canonical: dict
    frozen_by: str
    frozen_at: datetime
    artifacts: dict[str, str] = field(default_factory=dict)  # 载体 -> 字节哈希
    published_at: datetime | None = None


@dataclass
class AnnouncementVersion:
    version_no: int
    status: VersionStatus
    created_at: datetime
    deadline: datetime | None
    operations: dict[str, dict] = field(default_factory=dict)  # key -> 数值快照
    sources: dict[str, dict] = field(default_factory=dict)  # key -> {来源: {字段: 值}}
    attachments: dict[str, ExtractedRow] = field(default_factory=dict)
    notices: list[Discrepancy] = field(default_factory=list)  # 接收期登记的差异（不参与重算）
    discrepancies: list[Discrepancy] = field(default_factory=list)
    edits: list[ManualEdit] = field(default_factory=list)
    manifest: FrozenManifest | None = None
    correction_reason: str | None = None
    superseded_at: datetime | None = None

    @property
    def pending_edits(self) -> list[ManualEdit]:
        return [e for e in self.edits if not e.approved]


@dataclass
class AnnouncementGroup:
    announcement_code: str
    trade_date: date
    template_id: str
    versions: list[AnnouncementVersion] = field(default_factory=list)

    @property
    def current(self) -> AnnouncementVersion:
        return self.versions[-1]


@dataclass
class CheckReport:
    trade_date: date
    version_no: int
    status: VersionStatus
    deadline: datetime | None
    now: datetime
    discrepancies: list[Discrepancy]
    missing_fields: list[str]
    pending_edits: list[ManualEdit]
    operation_count: int
    due_yi: Decimal
    net_yi: Decimal

    @property
    def blockers(self) -> list[Discrepancy]:
        return [d for d in self.discrepancies if d.severity == Severity.BLOCK and d.resolved_by_edit is None]

    @property
    def resolved(self) -> list[Discrepancy]:
        return [d for d in self.discrepancies if d.resolved_by_edit is not None]

    @property
    def warnings(self) -> list[Discrepancy]:
        return [d for d in self.discrepancies if d.severity == Severity.WARN]

    def can_freeze(self) -> bool:
        return not self.blockers and not self.pending_edits

    def deadline_remaining(self) -> timedelta | None:
        if self.deadline is None:
            return None
        return self.deadline - self.now


class AnnouncementError(RuntimeError):
    """业务规则被违反。"""


class DeadlinePassed(AnnouncementError):
    pass


# ---------------------------------------------------------------------------
# 提取器
# ---------------------------------------------------------------------------


def extract_table_rows(content, attachment_id: str, revision: int, digest: str, trade_hint: date | None) -> list[ExtractedRow]:
    if isinstance(content, str):
        rows = list(csv.DictReader(io.StringIO(content)))
    elif isinstance(content, list):
        rows = content
    else:
        raise AnnouncementError(f"附件 {attachment_id} 的表格内容格式不受支持")
    extracted = []
    for raw in rows:
        fields_ = {}
        for key, value in raw.items():
            canon = FIELD_ALIASES.get(str(key).strip())
            if canon is None:
                continue
            try:
                norm = normalize_field(canon, value)
            except Exception:
                norm = str(value).strip()
            if norm is not None:
                fields_[canon] = norm
        if "trade_date" not in fields_ and trade_hint is not None:
            fields_["trade_date"] = trade_hint
        extracted.append(ExtractedRow(fields=fields_, raw=dict(raw), attachment_id=attachment_id, revision=revision, digest=digest))
    return extracted


# ---------------------------------------------------------------------------
# 核对服务
# ---------------------------------------------------------------------------


class AnnouncementCheckService:
    def __init__(
        self,
        calendar: Calendar | None = None,
        template: Template | None = None,
        ocr_adapter=None,
        clock=datetime.now,
    ):
        self.calendar = calendar or Calendar()
        self.template = template or Template(
            template_id="omo-reverse-repo-v1",
            title="公开市场业务交易公告",
            required_fields=("op_type", "term_days", "fixed_rate", "amount_yi", "allocation", "maturity_date"),
        )
        self.ocr_adapter = ocr_adapter
        self.clock = clock
        self._groups: dict[tuple[date, str], AnnouncementGroup] = {}
        self._idempotency: dict[str, str] = {}  # 抓取键 -> 组键
        self._attachment_uploads: dict[str, str] = {}  # attachment_id -> 已接收最新摘要
        self._all_operations: dict[str, OperationResult] = {}  # operation_id -> 结果（含历史）
        self._code_seq = 0

    # -- 组与版本 ------------------------------------------------------------

    def _group_key(self, trade_date_: date) -> tuple[date, str]:
        return (trade_date_, self.template.template_id)

    def _get_group(self, trade_date_: date) -> AnnouncementGroup | None:
        return self._groups.get(self._group_key(trade_date_))

    def _require_group(self, trade_date_: date) -> AnnouncementGroup:
        group = self._get_group(trade_date_)
        if group is None or not group.versions:
            raise AnnouncementError(f"{trade_date_} 尚无公告草稿")
        return group

    def _ensure_draft(self, trade_date_: date, deadline: datetime | None = None) -> AnnouncementVersion:
        key = self._group_key(trade_date_)
        group = self._groups.get(key)
        if group is None:
            self._code_seq += 1
            code = f"OMO-{trade_date_.isoformat()}-{self._code_seq:03d}"
            group = AnnouncementGroup(announcement_code=code, trade_date=trade_date_, template_id=self.template.template_id)
            self._groups[key] = group
        if not group.versions:
            group.versions.append(
                AnnouncementVersion(version_no=1, status=VersionStatus.DRAFT, created_at=self.clock(), deadline=deadline)
            )
        version = group.current
        if version.status in (VersionStatus.FROZEN, VersionStatus.PUBLISHED):
            raise AnnouncementError(
                f"{trade_date_} 公告已{'发布' if version.status == VersionStatus.PUBLISHED else '冻结'}，修改须先发起勘误"
            )
        if deadline is not None and version.deadline is None:
            version.deadline = deadline
        return version

    @staticmethod
    def _operation_key(fields_: dict) -> str:
        if fields_.get("operation_id"):
            return f"op:{fields_['operation_id']}"
        return f"line:{fields_.get('trade_date')}|{fields_.get('op_type')}|{fields_.get('term_days')}"

    # -- 接收结构化操作结果 ----------------------------------------------------

    def ingest_result(self, result: OperationResult, deadline: datetime | None = None) -> str:
        """录入业务结果；重复抓取（去重键或内容哈希相同）幂等返回，不产生第二份公告。"""
        group_token = canonical_json(list(self._group_key(result.trade_date)))
        if result.idempotency_key in self._idempotency:
            if self._idempotency[result.idempotency_key] != group_token:
                raise AnnouncementError("去重键已用于其他公告日，疑似重复抓取串单")
            return "duplicate"

        previous = self._all_operations.get(result.operation_id)
        if previous is not None:
            if previous.payload_hash() == result.payload_hash() and previous.trade_date == result.trade_date:
                self._idempotency[result.idempotency_key] = group_token
                return "duplicate"
            # 同一业务编号出现不同内容：历史冲突，阻断，需人工核实或勘误
            version = self._ensure_draft(result.trade_date, deadline)
            self._record_notice(
                version,
                code="HISTORY",
                severity=Severity.BLOCK,
                operation_key=self._operation_key({"operation_id": result.operation_id}),
                field_name=None,
                message=f"操作 {result.operation_id} 与已收录的业务结果内容不一致",
                values={"previous": previous.payload_hash(), "current": result.payload_hash()},
            )
            return "conflict"

        version = self._ensure_draft(result.trade_date, deadline)
        self._all_operations[result.operation_id] = result
        self._idempotency[result.idempotency_key] = group_token

        okey = self._operation_key({"operation_id": result.operation_id})
        values = result.canonical_values()
        # 到期日以日历为准：结果未给则按“期限遇休市顺延”补齐
        values["maturity_date"] = values["maturity_date"] or self.calendar.maturity_date(result.trade_date, result.term_days)
        version.operations[okey] = {"operation_id": result.operation_id, "trade_date": result.trade_date, **values}
        version.sources.setdefault(okey, {})[result.source] = {
            k: v for k, v in result.canonical_values().items() if v is not None
        }
        return "accepted"

    # -- 接收附件 -------------------------------------------------------------

    def ingest_attachment(self, attachment: Attachment, trade_date_: date, deadline: datetime | None = None) -> str:
        digest = attachment.digest()
        prior_digest = self._attachment_uploads.get(attachment.attachment_id)
        if prior_digest == digest:
            # 同一附件重传：登记上传即可，不重新提取、不产生新内容
            return "retransmitted"
        group = self._get_group(trade_date_)
        if prior_digest is not None and group is not None and group.versions:
            if group.current.status in (VersionStatus.FROZEN, VersionStatus.PUBLISHED):
                raise AnnouncementError("公告已冻结/发布，更换附件须发起勘误")

        version = self._ensure_draft(trade_date_, deadline)
        revision = 1 + sum(1 for k in version.attachments if k.startswith(f"{attachment.attachment_id}:r"))
        if not attachment.authorized:
            self._record_notice(
                version,
                code="UNAUTHORIZED",
                severity=Severity.BLOCK,
                operation_key=f"attachment:{attachment.attachment_id}",
                field_name=None,
                message=f"附件 {attachment.filename} 未经授权，不得作为公告字段来源",
            )
            return "unauthorized"

        if attachment.kind == "table":
            rows = extract_table_rows(attachment.content, attachment.attachment_id, revision, digest, trade_date_)
        elif attachment.kind == "image":
            if self.ocr_adapter is None:
                raise AnnouncementError("图片附件需要配置 OCR 适配器")
            ocr_rows = self.ocr_adapter(attachment.content, attachment.filename)
            rows = extract_table_rows(ocr_rows, attachment.attachment_id, revision, digest, trade_date_)
        else:
            raise AnnouncementError(f"不支持的附件类型 {attachment.kind}")

        for row in rows:
            okey = self._operation_key(row.fields)
            version.attachments[f"{attachment.attachment_id}:r{revision}:{okey}"] = row
            source_name = f"attachment:{attachment.attachment_id}:r{revision}"
            version.sources.setdefault(okey, {})[source_name] = {
                k: v for k, v in row.fields.items() if k in CANONICAL_FIELDS or k == "trade_date"
            }
        self._attachment_uploads[attachment.attachment_id] = digest
        return "accepted" if prior_digest is None else "revised"

    # -- 差异登记 -------------------------------------------------------------

    @staticmethod
    def _record_notice(version: AnnouncementVersion, *, code, severity, operation_key, field_name, message, values=None):
        disc_id = f"N{len(version.notices) + 1:04d}"
        version.notices.append(
            Discrepancy(
                id=disc_id,
                code=code,
                severity=severity,
                operation_key=operation_key,
                field=field_name,
                message=message,
                values_by_source=values or {},
            )
        )
        return disc_id

    # -- 核对 -----------------------------------------------------------------

    def _recompute_check(self, version: AnnouncementVersion) -> list[Discrepancy]:
        discs: list[Discrepancy] = []
        seq = 0

        def add(*, code, severity, operation_key, field_name, message, values=None):
            nonlocal seq
            seq += 1
            discs.append(
                Discrepancy(
                    id=f"D{seq:04d}",
                    code=code,
                    severity=severity,
                    operation_key=operation_key,
                    field=field_name,
                    message=message,
                    values_by_source=values or {},
                )
            )

        operation_keys = set(version.operations)
        source_keys = {k for k in version.sources if not k.startswith("attachment:")}

        # 附件含某操作但业务汇总缺失：无法确认权威数值
        for okey in sorted(source_keys - operation_keys):
            add(
                code="MISSING",
                severity=Severity.BLOCK,
                operation_key=okey,
                field_name=None,
                message="附件含该操作但业务结果汇总中缺失，无法确认权威数值",
            )

        for okey in sorted(operation_keys | source_keys):
            snap = version.operations.get(okey)
            sources = version.sources.get(okey, {})
            trade_day = (snap or {}).get("trade_date")
            for svals in sources.values():
                if svals.get("trade_date"):
                    trade_day = svals["trade_date"]
                    break

            # 日历：操作日须为交易日（含节假日补班日）
            if trade_day is not None and not self.calendar.is_trading_day(trade_day):
                add(
                    code="CALENDAR",
                    severity=Severity.BLOCK,
                    operation_key=okey,
                    field_name="trade_date",
                    message=f"{trade_day} 非交易日且不在补班安排内，不得发布操作公告",
                )

            # 逐字段交叉比对（业务汇总 vs 各附件提取）
            present: dict[str, dict] = {}
            if snap:
                for fname in CANONICAL_FIELDS:
                    if snap.get(fname) is not None:
                        present.setdefault(fname, {})["business-summary"] = snap[fname]
            for sname, svals in sources.items():
                if sname == "business-summary":
                    continue
                for fname in CANONICAL_FIELDS:
                    if svals.get(fname) is not None:
                        present.setdefault(fname, {})[sname] = svals[fname]

            for fname in self.template.required_fields:
                by_source = present.get(fname, {})
                if not by_source:
                    add(
                        code="MISSING",
                        severity=Severity.BLOCK,
                        operation_key=okey,
                        field_name=fname,
                        message=f"缺少必填字段：{FIELD_LABELS.get(fname, fname)}",
                    )
                    continue
                distinct = {self._value_token(v) for v in by_source.values()}
                if len(distinct) > 1:
                    add(
                        code="MISMATCH",
                        severity=Severity.BLOCK,
                        operation_key=okey,
                        field_name=fname,
                        message=(
                            f"{FIELD_LABELS.get(fname, fname)}在不同来源间不一致："
                            + "、".join(f"{s}={render_scalar(v)}" for s, v in by_source.items())
                        ),
                        values={s: render_scalar(v) for s, v in by_source.items()},
                    )

            # 满足方式取值须落在模板允许范围
            alloc = (snap or {}).get("allocation")
            if alloc is not None and alloc not in self.template.allowed_allocation:
                add(
                    code="TEMPLATE",
                    severity=Severity.BLOCK,
                    operation_key=okey,
                    field_name="allocation",
                    message=f"满足方式 '{alloc}' 不在模板允许范围内",
                )

            # 到期日与日历核对（期限按自然日、遇休市顺延）
            if snap and snap.get("term_days") and snap.get("trade_date"):
                expected = self.calendar.maturity_date(snap["trade_date"], snap["term_days"])
                if snap.get("maturity_date") and snap["maturity_date"] != expected:
                    add(
                        code="CALENDAR",
                        severity=Severity.BLOCK,
                        operation_key=okey,
                        field_name="maturity_date",
                        message=f"到期日 {snap['maturity_date']} 与日历推算 {expected} 不符（期限遇休市应顺延）",
                    )

        # 已批准的人工修改消解同字段的比对类差异，并把批准值落进快照
        approved = {(e.operation_key, e.field_name): e for e in version.edits if e.approved}
        for i, disc in enumerate(discs):
            if disc.field is None:
                continue
            edit = approved.get((disc.operation_key, disc.field))
            if edit is not None and disc.code in ("MISMATCH", "MISSING", "TEMPLATE"):
                discs[i] = disc.resolved(edit.edit_id)
                if disc.operation_key in version.operations:
                    version.operations[disc.operation_key][disc.field] = edit.new_value

        return discs

    @staticmethod
    def _value_token(value) -> str:
        if isinstance(value, Decimal):
            return f"dec:{value.normalize()}"
        if isinstance(value, date):
            return f"date:{value.isoformat()}"
        return f"{type(value).__name__}:{value}"

    def check(self, trade_date_: date, now: datetime | None = None) -> CheckReport:
        now = now or self.clock()
        group = self._require_group(trade_date_)
        version = group.current
        version.discrepancies = self._recompute_check(version)
        if version.pending_edits:
            version.status = VersionStatus.PENDING_REVIEW
        elif version.status == VersionStatus.PENDING_REVIEW:
            version.status = VersionStatus.DRAFT

        missing, due, net = self._aggregate(group, version)
        return CheckReport(
            trade_date=trade_date_,
            version_no=version.version_no,
            status=version.status,
            deadline=version.deadline,
            now=now,
            discrepancies=list(version.notices) + list(version.discrepancies),
            missing_fields=missing,
            pending_edits=version.pending_edits,
            operation_count=len(version.operations),
            due_yi=due,
            net_yi=net,
        )

    def _aggregate(self, group: AnnouncementGroup, version: AnnouncementVersion):
        missing = []
        for okey, snap in version.operations.items():
            for fname in self.template.required_fields:
                if snap.get(fname) is None:
                    missing.append(f"{okey}.{fname}")
        # 今日到期回款：历史操作中到期日落于本日的部分（不含本日新操作自身）
        due = Decimal("0")
        current_ids = {snap.get("operation_id") for snap in version.operations.values()}
        for result in self._all_operations.values():
            if result.operation_id in current_ids:
                continue
            maturity = result.maturity_date or self.calendar.maturity_date(result.trade_date, result.term_days)
            if maturity == group.trade_date:
                due += result.amount_yi
        placed = sum((snap.get("amount_yi") or Decimal("0") for snap in version.operations.values()), Decimal("0"))
        net = placed - due
        return missing, due, net

    # -- 人工修改与复核 --------------------------------------------------------

    def apply_manual_edit(
        self,
        trade_date_: date,
        operation_key: str,
        field_name: str,
        new_value,
        reason: str,
        editor: str,
        discrepancy_id: str | None = None,
    ) -> str:
        if not reason or not reason.strip():
            raise AnnouncementError("人工修改必须填写理由")
        version = self._ensure_draft(trade_date_)
        if operation_key.startswith("__"):
            old_value = None
        else:
            version.operations.setdefault(operation_key, {"trade_date": trade_date_})
            old_value = version.operations[operation_key].get(field_name)
        norm_new = normalize_field(field_name, new_value) if field_name in FIELD_ALIASES.values() else new_value
        edit_id = f"E{len(version.edits) + 1:04d}"
        version.edits.append(
            ManualEdit(
                edit_id=edit_id,
                operation_key=operation_key,
                field_name=field_name,
                old_value=old_value,
                new_value=norm_new,
                reason=reason.strip(),
                editor=editor,
                created_at=self.clock(),
                discrepancy_id=discrepancy_id,
            )
        )
        version.status = VersionStatus.PENDING_REVIEW
        return edit_id

    def approve_edit(self, trade_date_: date, edit_id: str, reviewer: str, note: str = "") -> None:
        version = self._require_group(trade_date_).current
        edit = next((e for e in version.edits if e.edit_id == edit_id), None)
        if edit is None:
            raise AnnouncementError(f"修改记录 {edit_id} 不存在")
        if edit.approved:
            raise AnnouncementError("该修改已复核")
        if reviewer == edit.editor:
            raise AnnouncementError("复核人不得与修改人为同一人（四眼原则）")
        edit.reviewed_by = reviewer
        edit.review_note = note
        edit.reviewed_at = self.clock()
        # 批准值立即落到数值快照；对应差异在下次 check 时消解
        if not edit.operation_key.startswith("__"):
            version.operations.setdefault(edit.operation_key, {"trade_date": trade_date_})[edit.field_name] = edit.new_value
        if not version.pending_edits and version.status == VersionStatus.PENDING_REVIEW:
            version.status = VersionStatus.DRAFT

    # -- 冻结与多载体发布 ------------------------------------------------------

    def _ensure_before_deadline(self, version: AnnouncementVersion, now: datetime):
        if version.deadline is not None and now > version.deadline:
            raise DeadlinePassed(f"已超过截止时间 {version.deadline}")

    def freeze(self, trade_date_: date, frozen_by: str, now: datetime | None = None) -> FrozenManifest:
        now = now or self.clock()
        group = self._require_group(trade_date_)
        version = group.current
        self._ensure_before_deadline(version, now)
        report = self.check(trade_date_, now=now)
        if not report.can_freeze():
            parts = [d.message for d in report.blockers]
            if report.pending_edits:
                parts.append(f"待复核修改 {len(report.pending_edits)} 项")
            raise AnnouncementError("仍有阻断差异或待复核修改，不能冻结：" + "; ".join(parts))
        canonical = self._canonical_content(group, version, report)
        rendered = self._render_text(version, report)
        content_hash = sha256_hex(canonical_json(canonical).encode("utf-8"))
        version.manifest = FrozenManifest(
            content_hash=content_hash,
            rendered_text=rendered,
            canonical=canonical,
            frozen_by=frozen_by,
            frozen_at=now,
        )
        version.status = VersionStatus.FROZEN
        return version.manifest

    def _canonical_content(self, group: AnnouncementGroup, version: AnnouncementVersion, report: CheckReport) -> dict:
        lines = []
        for okey in sorted(version.operations):
            snap = version.operations[okey]
            lines.append({k: (render_scalar(v) if isinstance(v, (Decimal, date)) else v) for k, v in snap.items()})
        return {
            "announcement_code": group.announcement_code,
            "template_id": self.template.template_id,
            "trade_date": group.trade_date.isoformat(),
            "version_no": version.version_no,
            "correction_reason": version.correction_reason,
            "operations": lines,
            "due_yi": render_scalar(report.due_yi),
            "net_yi": render_scalar(report.net_yi),
        }

    def _render_text(self, version: AnnouncementVersion, report: CheckReport) -> str:
        parts = [self.template.title]
        for okey in sorted(version.operations):
            parts.append(self.template.render_operation(version.operations[okey]))
        if report.due_yi != 0 or any((snap.get("amount_yi") or 0) == 0 for snap in version.operations.values()):
            parts.append(self.template.net_line.format(due_yi=render_scalar(report.due_yi), net_yi=render_scalar(report.net_yi)))
        if version.correction_reason:
            parts.append(f"【勘误】{version.correction_reason}")
        return "\n".join(parts)

    def render_artifacts(self, trade_date_: date) -> dict[str, bytes]:
        """从冻结内容确定性渲染三类载体；任一载体都内嵌同一 content_hash。"""
        version = self._require_group(trade_date_).current
        if version.manifest is None:
            raise AnnouncementError("公告尚未冻结")
        m = version.manifest
        api = (canonical_json({"content_hash": m.content_hash, "content": m.canonical}) + "\n").encode("utf-8")
        web = (
            '<!doctype html><html><head><meta charset="utf-8">'
            f'<meta name="announcement-hash" content="{m.content_hash}">'
            f"<title>{self.template.title}</title></head><body>"
            + "".join(f"<p>{line}</p>" for line in m.rendered_text.splitlines())
            + "</body></html>"
        ).encode("utf-8")
        file_blob = (m.rendered_text + f"\n<!-- content_hash={m.content_hash} -->\n").encode("utf-8")
        return {"api": api, "web": web, "file": file_blob}

    def publish(self, trade_date_: date, published_by: str) -> dict[str, bytes]:
        group = self._require_group(trade_date_)
        version = group.current
        if version.status != VersionStatus.FROZEN:
            raise AnnouncementError("只有已冻结公告可以发布")
        self._ensure_before_deadline(version, self.clock())
        artifacts = self.render_artifacts(trade_date_)
        m = version.manifest
        m.artifacts = {name: sha256_hex(blob) for name, blob in artifacts.items()}
        # 同组旧版本失效，但保留在更正链上
        for old in group.versions[:-1]:
            if old.status == VersionStatus.PUBLISHED:
                old.status = VersionStatus.SUPERSEDED
                old.superseded_at = self.clock()
        m.published_at = self.clock()
        version.status = VersionStatus.PUBLISHED
        return artifacts

    def get_publication(self, trade_date_: date) -> dict[str, bytes]:
        """公众接口：始终返回已发布冻结内容的同一字节。"""
        version = self._require_group(trade_date_).current
        if version.status != VersionStatus.PUBLISHED or version.manifest is None:
            raise AnnouncementError("该日公告尚未发布")
        return self.render_artifacts(trade_date_)

    def verify_artifact_consistency(self, trade_date_: date) -> dict[str, bool]:
        """核对公众接口、网页正文与下载附件是否都指向同一冻结内容。"""
        version = self._require_group(trade_date_).current
        if version.manifest is None:
            raise AnnouncementError("公告尚未冻结")
        artifacts = self.render_artifacts(trade_date_)
        result = {}
        for name, blob in artifacts.items():
            recorded = version.manifest.artifacts.get(name)
            byte_ok = recorded is None or sha256_hex(blob) == recorded
            hash_ok = version.manifest.content_hash.encode("utf-8") in blob
            result[name] = byte_ok and hash_ok
        return result

    # -- 勘误 -----------------------------------------------------------------

    def initiate_correction(self, trade_date_: date, reason: str, editor: str) -> int:
        if not reason or not reason.strip():
            raise AnnouncementError("勘误必须说明理由")
        group = self._require_group(trade_date_)
        latest = group.current
        if latest.status not in (VersionStatus.FROZEN, VersionStatus.PUBLISHED):
            raise AnnouncementError("只能对已冻结或已发布的公告发起勘误")
        new_version = AnnouncementVersion(
            version_no=latest.version_no + 1,
            status=VersionStatus.DRAFT,
            created_at=self.clock(),
            deadline=latest.deadline,
            operations={k: dict(v) for k, v in latest.operations.items()},
            sources={k: dict(v) for k, v in latest.sources.items()},
            attachments=dict(latest.attachments),
            correction_reason=reason.strip(),
        )
        group.versions.append(new_version)
        # 勘误发起本身留痕，且需他人复核后才能再次冻结
        new_version.edits.append(
            ManualEdit(
                edit_id="E0001",
                operation_key="__header__",
                field_name="correction_reason",
                old_value=None,
                new_value=reason.strip(),
                reason=f"{editor} 发起勘误",
                editor=editor,
                created_at=self.clock(),
            )
        )
        return new_version.version_no

    # -- 溯源 -----------------------------------------------------------------

    def trace_value(self, value, field_name: str | None = None, trade_date_: date | None = None) -> list[dict]:
        """从公告中的任一数值反查来源、审核记录与更正链。"""
        token = None
        if field_name is not None:
            token = self._value_token(self._coerce(field_name, value))
        hits = []
        for group in self._groups.values():
            if trade_date_ is not None and group.trade_date != trade_date_:
                continue
            for version in group.versions:
                for okey, snap in version.operations.items():
                    for fname, fval in snap.items():
                        if field_name is not None and fname != field_name:
                            continue
                        if field_name is None:
                            try:
                                same = self._value_token(self._coerce(fname, value)) == self._value_token(fval)
                            except Exception:
                                same = False
                        else:
                            same = self._value_token(fval) == token
                        if not same:
                            continue
                        source_rows = version.sources.get(okey, {})
                        attachments_used = []
                        for sname in source_rows:
                            if sname.startswith("attachment:"):
                                att_id, _, rest = sname[len("attachment:") :].partition(":r")
                                rev = rest.split(":", 1)[0]
                                row = version.attachments.get(f"{att_id}:r{rev}:{okey}")
                                attachments_used.append(
                                    {
                                        "attachment_id": att_id,
                                        "revision": int(rev),
                                        "digest": row.digest if row else None,
                                    }
                                )
                        hits.append(
                            {
                                "announcement_code": group.announcement_code,
                                "trade_date": group.trade_date.isoformat(),
                                "version_no": version.version_no,
                                "status": version.status.value,
                                "operation_key": okey,
                                "field": fname,
                                "value": render_scalar(fval),
                                "sources": {s: {k: render_scalar(v) for k, v in sv.items()} for s, sv in source_rows.items()},
                                "attachments": attachments_used,
                                "edits": [
                                    {
                                        "edit_id": e.edit_id,
                                        "editor": e.editor,
                                        "reason": e.reason,
                                        "reviewed_by": e.reviewed_by,
                                        "review_note": e.review_note,
                                    }
                                    for e in version.edits
                                    if e.operation_key == okey and e.field_name == fname
                                ],
                                "frozen_by": version.manifest.frozen_by if version.manifest else None,
                                "content_hash": version.manifest.content_hash if version.manifest else None,
                                "correction_reason": version.correction_reason,
                                "predecessor_version": version.version_no - 1 if version.version_no > 1 else None,
                                "published": version.status == VersionStatus.PUBLISHED,
                            }
                        )
        return hits

    @staticmethod
    def _coerce(field_name: str, value):
        if field_name in FIELD_ALIASES.values():
            return normalize_field(field_name, value)
        return value
