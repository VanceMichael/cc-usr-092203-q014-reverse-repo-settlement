"""公告核对服务: 接入、交叉校验、复核、冻结发布、勘误与溯源。

设计要点
--------
- 幂等: 以 operation_id 作为公告唯一键, 结构化结果按内容指纹去重;
  附件按内容哈希去重。重复抓取只回到原公告, 不会产生第二份有效公告。
- 版本链: 每次人工修改或已发布后的更正都追加新版本, 旧版本保留;
  冻结时计算 content_hash, 公众接口/网页/下载附件共用同一冻结内容。
- 拦截: 复核与冻结要求错误级核对项全部清零, 字段差异和缺失在
  deadline_report 中对发布人员可见。
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Mapping, Optional

from .calendar import TradingCalendar
from .extraction import Extractor
from .models import (
    Announcement,
    AnnouncementVersion,
    Attachment,
    BusinessSummary,
    ConflictError,
    DomainError,
    EditStatus,
    Finding,
    IngestOutcome,
    ManualEdit,
    NotFoundError,
    NUMERIC_FIELDS,
    OperationResult,
    Provenance,
    PublishedView,
    SourceKind,
    Template,
    TraceHit,
    VersionStatus,
    canon_number,
    canonical_hash,
    values_equal,
)

CHECK_FIELDS = ("operation_type", "term_days", "rate_percent", "amount_yi", "allotment")
CHANNELS = ("api", "web", "download")


class AnnouncementCheckService:
    def __init__(self, calendar: TradingCalendar, templates: Mapping[str, Template]):
        self._calendar = calendar
        self._templates = dict(templates)
        self._announcements: dict[str, Announcement] = {}
        self._op_index: dict[str, str] = {}          # operation_id -> announcement_id
        self._fingerprints: dict[str, str] = {}      # operation_id -> 上次指纹
        self._payloads: dict[str, str] = {}          # payload_id -> operation_id
        self._summaries: dict[str, BusinessSummary] = {}  # 按交易日
        self._edits: dict[str, ManualEdit] = {}
        self._channel_views: dict[str, dict[str, PublishedView]] = {
            c: {} for c in CHANNELS
        }

    # ------------------------------------------------------------------
    # 业务汇总(权威来源)
    # ------------------------------------------------------------------
    def load_summary(self, summary: BusinessSummary) -> None:
        self._summaries[summary.trade_date.isoformat()] = summary

    # ------------------------------------------------------------------
    # 结构化操作结果接入(幂等)
    # ------------------------------------------------------------------
    def ingest(self, result: OperationResult, actor: str, now: datetime,
               note: Optional[str] = None) -> IngestOutcome:
        existing_op = self._payloads.get(result.payload_id)
        if existing_op is not None and existing_op != result.operation_id:
            raise ConflictError("同一抓取批次被用于两笔操作, 拒绝重复接入")

        ann_id = self._op_index.get(result.operation_id)
        fingerprint = result.fingerprint()

        if ann_id is None:
            ann = self._create_announcement(result, actor, now)
            self._payloads.setdefault(result.payload_id, result.operation_id)
            self._fingerprints[result.operation_id] = fingerprint
            findings = self.validate(ann.announcement_id)
            return IngestOutcome(ann.announcement_id, True, tuple(findings))

        ann = self._announcements[ann_id]
        if self._fingerprints[result.operation_id] == fingerprint:
            # 完全相同的重复抓取: 不产生新版本
            return IngestOutcome(ann_id, False, tuple(self.validate(ann_id)))

        latest = ann.latest
        if latest.status in (VersionStatus.FROZEN, VersionStatus.PUBLISHED) and not note:
            raise ConflictError("操作结果在冻结/发布后发生变化, 必须附更正说明")
        self._append_structured_version(ann, result, actor, now, note)
        self._fingerprints[result.operation_id] = fingerprint
        self._payloads.setdefault(result.payload_id, result.operation_id)
        return IngestOutcome(ann_id, False, tuple(self.validate(ann_id)))

    def _create_announcement(self, result: OperationResult, actor: str,
                             now: datetime) -> Announcement:
        ann_id = f"ANN-{result.trade_date.isoformat()}-{result.operation_id}"
        fields = result.field_map()
        fields["maturity_date"] = self._calendar.maturity_date(
            result.trade_date, result.term_days
        ).isoformat()
        provenance = {
            name: Provenance(SourceKind.STRUCTURED, result.payload_id, actor, now)
            for name in fields
        }
        version = AnnouncementVersion(
            announcement_id=ann_id,
            version=1,
            fields=fields,
            provenance=provenance,
            status=VersionStatus.DRAFT,
            created_at=now,
            created_by=actor,
        )
        ann = Announcement(
            announcement_id=ann_id,
            operation_id=result.operation_id,
            trade_date=result.trade_date,
            versions=[version],
        )
        self._announcements[ann_id] = ann
        self._op_index[result.operation_id] = ann_id
        return ann

    def _append_structured_version(self, ann: Announcement, result: OperationResult,
                                   actor: str, now: datetime,
                                   note: Optional[str]) -> None:
        base = ann.latest
        fields = result.field_map()
        fields["maturity_date"] = self._calendar.maturity_date(
            result.trade_date, result.term_days
        ).isoformat()
        provenance = {
            name: Provenance(SourceKind.STRUCTURED, result.payload_id, actor, now)
            for name in fields
        }
        ann.versions.append(AnnouncementVersion(
            announcement_id=ann.announcement_id,
            version=base.version + 1,
            fields=fields,
            provenance=provenance,
            status=VersionStatus.DRAFT,
            created_at=now,
            created_by=actor,
            supersedes=ann.published_version,
            correction_reason=note,
        ))

    # ------------------------------------------------------------------
    # 附件接入(授权、去重、重传)
    # ------------------------------------------------------------------
    def add_attachment(self, announcement_id: str, kind: str, content: bytes,
                       uploaded_by: str, authorized_by: str, now: datetime,
                       extractor: Extractor,
                       supersedes: Optional[str] = None) -> Attachment:
        if not uploaded_by or not authorized_by:
            raise DomainError("附件必须记录上传人与授权人")
        ann = self._require_announcement(announcement_id)
        content_hash = hashlib.sha256(content).hexdigest()

        for prior in ann.attachments:
            if prior.content_hash == content_hash:
                return prior  # 同一字节内容重传, 幂等返回

        active = [a for a in ann.attachments
                  if not any(other.supersedes == a.attachment_id for other in ann.attachments)]
        if active and supersedes is None:
            raise ConflictError("已有生效附件, 重传必须声明被取代的附件")
        if supersedes is not None and supersedes not in {a.attachment_id for a in ann.attachments}:
            raise NotFoundError("被取代的附件不存在")

        attachment_id = f"ATT-{ann.announcement_id}-{len(ann.attachments) + 1:02d}"
        extracted = extractor.extract(content)
        record = Attachment(
            attachment_id=attachment_id,
            announcement_id=announcement_id,
            kind=kind,
            content_hash=content_hash,
            uploaded_by=uploaded_by,
            authorized_by=authorized_by,
            uploaded_at=now,
            extracted=dict(extracted),
            supersedes=supersedes,
        )
        ann.attachments.append(record)

        # 附件只补齐结构化结果缺失的字段, 不覆盖已有取值; 差异交给核对拦截。
        latest = ann.latest
        for name, value in extracted.items():
            if name not in latest.fields or not latest.fields[name]:
                latest.fields[name] = value
                latest.provenance[name] = Provenance(
                    SourceKind.ATTACHMENT, attachment_id, uploaded_by, now
                )
        return record

    def active_attachment(self, announcement_id: str) -> Optional[Attachment]:
        ann = self._require_announcement(announcement_id)
        superseded = {a.supersedes for a in ann.attachments if a.supersedes}
        active = [a for a in ann.attachments if a.attachment_id not in superseded]
        return active[-1] if active else None

    # ------------------------------------------------------------------
    # 交叉校验
    # ------------------------------------------------------------------
    def validate(self, announcement_id: str) -> list[Finding]:
        ann = self._require_announcement(announcement_id)
        version = ann.latest
        findings: list[Finding] = []
        fields = version.fields

        # 1) 与业务汇总逐项核对(固定利率、操作量、期限、满足方式)
        summary = self._summaries.get(ann.trade_date.isoformat())
        if summary is None:
            findings.append(Finding("error", "summary_missing", None,
                                    "缺少当日业务汇总, 无法完成权威核对"))
        else:
            entry = summary.entries.get(ann.operation_id)
            if entry is None:
                findings.append(Finding("error", "summary_entry_missing", None,
                                        "业务汇总中没有该笔操作"))
            else:
                for name in CHECK_FIELDS:
                    expected = entry.field_map()[name]
                    actual = fields.get(name, "")
                    if not actual:
                        findings.append(Finding("error", "field_missing", name,
                                                f"字段 {name} 缺失"))
                    elif not values_equal(name, actual, expected):
                        findings.append(Finding("error", "field_mismatch", name,
                        f"字段 {name} 与业务汇总不一致: 公告={actual} 汇总={expected}"))

        # 2) 历史日历: 交易日与到期日
        if not self._calendar.is_trading_day(ann.trade_date):
            findings.append(Finding("warning", "non_trading_day", None,
                                    "操作日不在日历的交易日内, 请确认是否漏维护补班安排"))
        term_raw = fields.get("term_days")
        if term_raw:
            try:
                maturity = self._calendar.maturity_date(
                    ann.trade_date, int(Decimal(term_raw))
                ).isoformat()
                if fields.get("maturity_date") and fields["maturity_date"] != maturity:
                    findings.append(Finding("error", "maturity_mismatch", "maturity_date",
                        f"到期日与日历推算不符: 公告={fields['maturity_date']} 日历={maturity}"))
            except (InvalidOperation, ValueError):
                findings.append(Finding("error", "bad_term", "term_days", "期限无法解析"))

        # 3) 发布模板: 必备字段与零投放模板
        amount_raw = fields.get("amount_yi", "")
        try:
            zero_injection = bool(amount_raw) and canon_number(amount_raw) == "0"
        except (InvalidOperation, ValueError):
            zero_injection = False
            findings.append(Finding("error", "bad_amount", "amount_yi", "操作量无法解析"))
        template = self._templates.get("zero" if zero_injection else "standard")
        if template is None:
            findings.append(Finding("error", "template_missing", None, "缺少发布模板"))
        else:
            for name in template.required_fields:
                if not fields.get(name):
                    findings.append(Finding("error", "template_field_missing", name,
                                            f"模板要求字段 {name} 缺失"))

        # 4) 附件提取值交叉核对
        attachment = self.active_attachment(announcement_id)
        if attachment is None:
            findings.append(Finding("warning", "attachment_missing", None,
                                    "尚无生效附件, 无法做附件交叉核对"))
        else:
            for name, value in attachment.extracted.items():
                actual = fields.get(name)
                if actual is not None and not values_equal(name, actual, value):
                    findings.append(Finding("error", "attachment_mismatch", name,
                        f"附件提取值与公告字段不一致: 公告={actual} 附件={value}"))

        # 5) 待决人工修改
        pending = [e for e in self._edits.values()
                   if e.announcement_id == announcement_id
                   and e.status == EditStatus.PENDING]
        for edit in pending:
            findings.append(Finding("warning", "pending_edit", edit.field_name,
                                    f"字段 {edit.field_name} 的人工修改尚待复核"))
        return findings

    # ------------------------------------------------------------------
    # 人工修改: 申请 + 他人复核 + 理由
    # ------------------------------------------------------------------
    def request_edit(self, announcement_id: str, field_name: str, new_value: str,
                     reason: str, editor: str, now: datetime) -> ManualEdit:
        if not reason.strip():
            raise DomainError("人工修改必须填写理由")
        ann = self._require_announcement(announcement_id)
        old_value = ann.latest.fields.get(field_name, "")
        if values_equal(field_name, old_value, new_value):
            raise ConflictError("新值与现值相同, 无需修改")
        edit = ManualEdit(
            edit_id=f"EDIT-{announcement_id}-{len(self._edits) + 1:03d}",
            announcement_id=announcement_id,
            field_name=field_name,
            old_value=old_value,
            new_value=str(new_value).strip(),
            reason=reason.strip(),
            editor=editor,
            created_at=now,
        )
        self._edits[edit.edit_id] = edit
        return edit

    def review_edit(self, edit_id: str, reviewer: str, approve: bool,
                    now: datetime) -> ManualEdit:
        edit = self._edits.get(edit_id)
        if edit is None:
            raise NotFoundError("修改申请不存在")
        if edit.status != EditStatus.PENDING:
            raise ConflictError("该修改已处理")
        if reviewer == edit.editor:
            raise DomainError("修改必须由申请人之外的人员复核")

        edit.status = EditStatus.APPROVED if approve else EditStatus.REJECTED
        edit.reviewer = reviewer
        edit.reviewed_at = now
        if approve:
            self._apply_edit(edit, reviewer, now)
        return edit

    def _apply_edit(self, edit: ManualEdit, reviewer: str, now: datetime) -> None:
        ann = self._announcements[edit.announcement_id]
        base = ann.latest
        fields = dict(base.fields)
        provenance = dict(base.provenance)
        new_value = edit.new_value
        if edit.field_name in NUMERIC_FIELDS:
            try:
                new_value = canon_number(new_value)
            except (InvalidOperation, ValueError):
                pass  # 保留原样, 由核对环节报字段无法解析
        fields[edit.field_name] = new_value
        provenance[edit.field_name] = Provenance(
            SourceKind.MANUAL_EDIT, edit.edit_id, edit.editor, now
        )
        is_correction = base.status in (VersionStatus.FROZEN, VersionStatus.PUBLISHED)
        ann.versions.append(AnnouncementVersion(
            announcement_id=ann.announcement_id,
            version=base.version + 1,
            fields=fields,
            provenance=provenance,
            status=VersionStatus.DRAFT,
            created_at=now,
            created_by=reviewer,
            supersedes=ann.published_version if is_correction else None,
            correction_reason=edit.reason if is_correction else None,
        ))

    # ------------------------------------------------------------------
    # 复核 -> 冻结 -> 三载体发布
    # ------------------------------------------------------------------
    def review(self, announcement_id: str, reviewer: str, now: datetime) -> AnnouncementVersion:
        ann = self._require_announcement(announcement_id)
        version = ann.latest
        if version.status != VersionStatus.DRAFT:
            raise ConflictError("只有草稿版本可以送复核")
        if reviewer == version.created_by:
            raise DomainError("复核人不能是版本制作人本人")
        errors = [f for f in self.validate(announcement_id) if f.severity == "error"]
        if errors:
            raise ConflictError("仍有错误级核对项未清零, 不能复核通过")
        version.status = VersionStatus.REVIEWED
        version.reviewed_by = reviewer
        return version

    def freeze(self, announcement_id: str, now: datetime) -> AnnouncementVersion:
        ann = self._require_announcement(announcement_id)
        version = ann.latest
        if version.status != VersionStatus.REVIEWED:
            raise ConflictError("只有复核通过的版本可以冻结")
        version.status = VersionStatus.FROZEN
        version.content_hash = canonical_hash(
            ann.announcement_id, version.version, version.fields
        )
        return version

    def publish(self, announcement_id: str, now: datetime) -> PublishedView:
        ann = self._require_announcement(announcement_id)
        version = ann.latest
        if version.status != VersionStatus.FROZEN:
            raise ConflictError("只有冻结版本可以发布")

        body = {
            "announcement_id": ann.announcement_id,
            "trade_date": ann.trade_date.isoformat(),
            "operation_id": ann.operation_id,
            "version": version.version,
            "fields": dict(version.fields),
        }
        # 先在暂存区为三个载体生成同哈希视图, 再一次性提交
        staged = {
            channel: PublishedView(
                announcement_id=ann.announcement_id,
                version=version.version,
                content_hash=version.content_hash,
                channel=channel,
                body=body,
            )
            for channel in CHANNELS
        }
        previous = ann.published_version
        self._channel_views["api"][ann.announcement_id] = staged["api"]
        self._channel_views["web"][ann.announcement_id] = staged["web"]
        self._channel_views["download"][ann.announcement_id] = staged["download"]
        ann.published_version = version.version
        version.status = VersionStatus.PUBLISHED
        if previous is not None:
            for old in ann.versions:
                if old.version == previous:
                    old.status = VersionStatus.SUPERSEDED
        return staged["api"]

    def published_view(self, announcement_id: str, channel: str) -> PublishedView:
        if channel not in CHANNELS:
            raise NotFoundError(f"未知发布载体: {channel}")
        view = self._channel_views[channel].get(announcement_id)
        if view is None:
            raise NotFoundError("该公告尚未在该载体发布")
        hashes = {self._channel_views[c][announcement_id].content_hash for c in CHANNELS}
        if len(hashes) != 1:
            raise ConflictError("三个发布载体内容哈希不一致, 冻结内容已被破坏")
        return view

    # ------------------------------------------------------------------
    # 截止前差异与缺失报告
    # ------------------------------------------------------------------
    def deadline_report(self, trade_date) -> dict:
        day = trade_date.isoformat() if hasattr(trade_date, "isoformat") else str(trade_date)
        rows = []
        announced_ops = set()
        for ann in self._announcements.values():
            if ann.trade_date.isoformat() != day:
                continue
            announced_ops.add(ann.operation_id)
            findings = self.validate(ann.announcement_id)
            rows.append({
                "announcement_id": ann.announcement_id,
                "operation_id": ann.operation_id,
                "version": ann.latest.version,
                "status": ann.latest.status.value,
                "missing": [f.field_name for f in findings
                            if f.code in ("field_missing", "template_field_missing")],
                "differences": [f.message for f in findings if f.severity == "error"],
                "warnings": [f.message for f in findings if f.severity == "warning"],
                "published_version": ann.published_version,
            })
        rows.sort(key=lambda row: row["announcement_id"])
        summary = self._summaries.get(day)
        missing_ops = ([] if summary is None
                       else sorted(set(summary.entries) - announced_ops))
        return {
            "trade_date": day,
            "summary_loaded": summary is not None,
            "announcements": rows,
            "missing_operations": missing_ops,
        }

    # ------------------------------------------------------------------
    # 从媒体引用的数值反查来源、审核人与更正链
    # ------------------------------------------------------------------
    def trace_value(self, field_name: str, value) -> list[TraceHit]:
        target = value
        hits: list[TraceHit] = []
        for ann in self._announcements.values():
            chain: list[str] = []
            for version in ann.versions:
                if version.correction_reason:
                    chain.append(f"v{version.version}: {version.correction_reason}")
                actual = version.fields.get(field_name)
                if actual is None:
                    continue
                if values_equal(field_name, actual, target):
                    hits.append(TraceHit(
                        announcement_id=ann.announcement_id,
                        version=version.version,
                        field_name=field_name,
                        value=actual,
                        provenance=version.provenance.get(field_name),
                        reviewed_by=version.reviewed_by,
                        correction_chain=tuple(chain),
                    ))
        return hits

    # ------------------------------------------------------------------
    def _require_announcement(self, announcement_id: str) -> Announcement:
        ann = self._announcements.get(announcement_id)
        if ann is None:
            raise NotFoundError(f"公告不存在: {announcement_id}")
        return ann

    def get(self, announcement_id: str) -> Announcement:
        return self._require_announcement(announcement_id)
