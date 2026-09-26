"""公告核对服务的场景测试。

日历设定: 2026-09-24 为周四(交易日); 2026-09-26 为周六但属补班;
2026-10-01 至 2026-10-07 为法定节假日, 2026-10-08 起恢复交易。
"""

import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal

from src.omo.calendar import TradingCalendar
from src.omo.extraction import CsvTableExtractor, ImageExtractor
from src.omo.models import (
    BusinessSummary,
    ConflictError,
    DomainError,
    EditStatus,
    NotFoundError,
    OperationResult,
    SourceKind,
    SummaryEntry,
    Template,
    VersionStatus,
)
from src.omo.service import AnnouncementCheckService

TRADE_DAY = date(2026, 9, 24)          # 周四
MAKEUP_SATURDAY = date(2026, 9, 26)    # 补班周六
HOLIDAYS = frozenset(date(2026, 10, 1) + timedelta(days=i) for i in range(7))
NOW = datetime(2026, 9, 24, 9, 30)

TEMPLATES = {
    "standard": Template(
        "standard",
        ("operation_type", "term_days", "rate_percent",
         "amount_yi", "allotment", "maturity_date"),
    ),
    "zero": Template("zero", ("operation_type", "amount_yi"), zero_injection=True),
}


def make_calendar():
    return TradingCalendar(holidays=HOLIDAYS, makeup_workdays=frozenset({MAKEUP_SATURDAY}))


def make_result(op_id="OP-001", amount="500", rate="1.80", term=7,
                payload=None, trade=TRADE_DAY):
    return OperationResult(
        operation_id=op_id,
        trade_date=trade,
        operation_type="逆回购",
        term_days=term,
        rate_percent=Decimal(rate),
        amount_yi=Decimal(amount),
        allotment="固定利率、数量招标",
        payload_id=payload or f"PAY-{op_id}",
    )


def make_summary(entries, trade=TRADE_DAY):
    return BusinessSummary(trade_date=trade, entries={e.operation_id: e for e in entries})


def make_entry(op_id="OP-001", amount="500", rate="1.80", term=7):
    return SummaryEntry(
        operation_id=op_id,
        operation_type="逆回购",
        term_days=term,
        rate_percent=Decimal(rate),
        amount_yi=Decimal(amount),
        allotment="固定利率、数量招标",
    )


def make_service(summary=None):
    service = AnnouncementCheckService(make_calendar(), TEMPLATES)
    service.load_summary(summary or make_summary([make_entry()]))
    return service


def errors(service, ann_id):
    return [f for f in service.validate(ann_id) if f.severity == "error"]


class IngestTest(unittest.TestCase):
    def test_ingest_creates_draft_with_calendar_maturity(self):
        service = make_service()
        outcome = service.ingest(make_result(), actor="业务员甲", now=NOW)
        self.assertTrue(outcome.created)
        ann = service.get(outcome.announcement_id)
        self.assertEqual(ann.latest.status, VersionStatus.DRAFT)
        # 7 天期限跨过国庆假期, 顺延到 10-08
        self.assertEqual(ann.latest.fields["maturity_date"], "2026-10-08")
        self.assertEqual(errors(service, ann.announcement_id), [])

    def test_duplicate_fetch_never_creates_second_announcement(self):
        service = make_service()
        first = service.ingest(make_result(), actor="业务员甲", now=NOW)
        # 同批次重抓
        second = service.ingest(make_result(), actor="业务员甲", now=NOW)
        # 不同批次但内容完全相同
        third = service.ingest(make_result(payload="PAY-RETRY"), actor="业务员甲", now=NOW)
        self.assertFalse(second.created)
        self.assertFalse(third.created)
        self.assertEqual(first.announcement_id, second.announcement_id)
        self.assertEqual(first.announcement_id, third.announcement_id)
        self.assertEqual(len(service.get(first.announcement_id).versions), 1)

    def test_same_payload_cannot_bind_two_operations(self):
        service = make_service()
        service.ingest(make_result(payload="PAY-X"), actor="业务员甲", now=NOW)
        with self.assertRaises(ConflictError):
            service.ingest(make_result(op_id="OP-002", payload="PAY-X"),
                           actor="业务员甲", now=NOW)

    def test_changed_result_before_freeze_evolves_same_announcement(self):
        service = make_service()
        outcome = service.ingest(make_result(), actor="业务员甲", now=NOW)
        changed = make_result(amount="600", payload="PAY-OP-001-V2")
        again = service.ingest(changed, actor="业务员甲", now=NOW)
        self.assertFalse(again.created)
        ann = service.get(outcome.announcement_id)
        self.assertEqual(len(ann.versions), 2)
        self.assertEqual(ann.latest.fields["amount_yi"], "600")
        # 与汇总不一致, 必须暴露为错误
        self.assertTrue(any(f.code == "field_mismatch" and f.field_name == "amount_yi"
                            for f in errors(service, ann.announcement_id)))

    def test_changed_result_after_freeze_requires_correction_note(self):
        service = make_service()
        outcome = service.ingest(make_result(), actor="业务员甲", now=NOW)
        service.review(outcome.announcement_id, reviewer="复核乙", now=NOW)
        service.freeze(outcome.announcement_id, now=NOW)
        with self.assertRaises(ConflictError):
            service.ingest(make_result(amount="600", payload="PAY-V2"),
                           actor="业务员甲", now=NOW)
        noted = service.ingest(make_result(amount="600", payload="PAY-V2"),
                               actor="业务员甲", now=NOW, note="业务汇总更正为重传结果")
        ann = service.get(noted.announcement_id)
        self.assertEqual(ann.latest.correction_reason, "业务汇总更正为重传结果")

    def test_multiple_operations_same_day_each_get_one_announcement(self):
        service = make_service(make_summary([make_entry("OP-001"), make_entry("OP-002")]))
        first = service.ingest(make_result("OP-001"), actor="业务员甲", now=NOW)
        second = service.ingest(make_result("OP-002"), actor="业务员甲", now=NOW)
        self.assertNotEqual(first.announcement_id, second.announcement_id)
        report = service.deadline_report(TRADE_DAY)
        self.assertEqual(len(report["announcements"]), 2)
        self.assertEqual(report["missing_operations"], [])


class CrossCheckTest(unittest.TestCase):
    def test_copied_wrong_rate_blocks_review_and_shows_in_report(self):
        service = make_service()  # 汇总利率 1.80
        outcome = service.ingest(make_result(rate="1.08"), actor="业务员甲", now=NOW)
        ann_id = outcome.announcement_id
        mismatch = [f for f in errors(service, ann_id) if f.code == "field_mismatch"]
        self.assertEqual([f.field_name for f in mismatch], ["rate_percent"])
        with self.assertRaises(ConflictError):
            service.review(ann_id, reviewer="复核乙", now=NOW)
        report = service.deadline_report(TRADE_DAY)
        row = report["announcements"][0]
        self.assertTrue(any("rate_percent" in msg for msg in row["differences"]))

    def test_missing_summary_is_an_error(self):
        service = AnnouncementCheckService(make_calendar(), TEMPLATES)
        outcome = service.ingest(make_result(), actor="业务员甲", now=NOW)
        self.assertTrue(any(f.code == "summary_missing"
                            for f in errors(service, outcome.announcement_id)))

    def test_zero_injection_uses_zero_template(self):
        service = make_service(make_summary([make_entry(amount="0", rate="0", term=0)]))
        outcome = service.ingest(make_result(amount="0", rate="0", term=0),
                                 actor="业务员甲", now=NOW)
        self.assertEqual(errors(service, outcome.announcement_id), [])
        service.review(outcome.announcement_id, reviewer="复核乙", now=NOW)
        service.freeze(outcome.announcement_id, now=NOW)
        service.publish(outcome.announcement_id, now=NOW)
        view = service.published_view(outcome.announcement_id, "api")
        self.assertEqual(view.body["fields"]["amount_yi"], "0")

    def test_maturity_roll_and_makeup_workday(self):
        calendar = make_calendar()
        self.assertTrue(calendar.is_trading_day(MAKEUP_SATURDAY))   # 补班
        self.assertFalse(calendar.is_trading_day(date(2026, 10, 3)))  # 假期
        self.assertEqual(calendar.maturity_date(TRADE_DAY, 7), date(2026, 10, 8))

    def test_manual_maturity_change_is_caught_by_calendar(self):
        service = make_service()
        outcome = service.ingest(make_result(), actor="业务员甲", now=NOW)
        ann_id = outcome.announcement_id
        edit = service.request_edit(ann_id, "maturity_date", "2026-10-01",
                                    reason="手工调整", editor="业务员甲", now=NOW)
        service.review_edit(edit.edit_id, reviewer="复核乙", approve=True, now=NOW)
        self.assertTrue(any(f.code == "maturity_mismatch"
                            for f in errors(service, ann_id)))


class ManualEditTest(unittest.TestCase):
    def test_edit_requires_reason(self):
        service = make_service()
        outcome = service.ingest(make_result(rate="1.08"), actor="业务员甲", now=NOW)
        with self.assertRaises(DomainError):
            service.request_edit(outcome.announcement_id, "rate_percent", "1.80",
                                 reason="  ", editor="业务员甲", now=NOW)

    def test_edit_requires_separate_reviewer(self):
        service = make_service()
        outcome = service.ingest(make_result(rate="1.08"), actor="业务员甲", now=NOW)
        edit = service.request_edit(outcome.announcement_id, "rate_percent", "1.80",
                                    reason="按业务汇总订正", editor="业务员甲", now=NOW)
        with self.assertRaises(DomainError):
            service.review_edit(edit.edit_id, reviewer="业务员甲", approve=True, now=NOW)

    def test_approved_edit_creates_version_with_provenance(self):
        service = make_service()
        outcome = service.ingest(make_result(rate="1.08"), actor="业务员甲", now=NOW)
        ann_id = outcome.announcement_id
        edit = service.request_edit(ann_id, "rate_percent", "1.80",
                                    reason="按业务汇总订正", editor="业务员甲", now=NOW)
        service.review_edit(edit.edit_id, reviewer="复核乙", approve=True, now=NOW)
        ann = service.get(ann_id)
        self.assertEqual(ann.latest.fields["rate_percent"], "1.8")
        provenance = ann.latest.provenance["rate_percent"]
        self.assertEqual(provenance.kind, SourceKind.MANUAL_EDIT)
        self.assertEqual(provenance.source_id, edit.edit_id)
        self.assertEqual(errors(service, ann_id), [])
        self.assertEqual(edit.status, EditStatus.APPROVED)
        self.assertEqual(edit.reviewer, "复核乙")

    def test_rejected_edit_leaves_fields_untouched(self):
        service = make_service()
        outcome = service.ingest(make_result(rate="1.08"), actor="业务员甲", now=NOW)
        ann_id = outcome.announcement_id
        edit = service.request_edit(ann_id, "rate_percent", "1.80",
                                    reason="按业务汇总订正", editor="业务员甲", now=NOW)
        service.review_edit(edit.edit_id, reviewer="复核乙", approve=False, now=NOW)
        self.assertEqual(service.get(ann_id).latest.fields["rate_percent"], "1.08")
        self.assertEqual(edit.status, EditStatus.REJECTED)


class AttachmentTest(unittest.TestCase):
    CSV = "operation_type,term_days,rate_percent,amount_yi,allotment\n" \
          "逆回购,7,1.80,500,固定利率、数量招标".encode("utf-8")

    def setUp(self):
        self.service = make_service()
        outcome = self.service.ingest(make_result(), actor="业务员甲", now=NOW)
        self.ann_id = outcome.announcement_id

    def test_unauthorized_attachment_rejected(self):
        with self.assertRaises(DomainError):
            self.service.add_attachment(self.ann_id, "table", self.CSV,
                                        uploaded_by="业务员甲", authorized_by="",
                                        now=NOW, extractor=CsvTableExtractor())

    def test_matching_attachment_passes_cross_check(self):
        self.service.add_attachment(self.ann_id, "table", self.CSV,
                                    uploaded_by="业务员甲", authorized_by="授权丙",
                                    now=NOW, extractor=CsvTableExtractor())
        self.assertFalse(any(f.code == "attachment_mismatch"
                             for f in self.service.validate(self.ann_id)))

    def test_conflicting_attachment_value_is_an_error(self):
        bad = self.CSV.replace(b"1.80", b"1.08")
        self.service.add_attachment(self.ann_id, "table", bad,
                                    uploaded_by="业务员甲", authorized_by="授权丙",
                                    now=NOW, extractor=CsvTableExtractor())
        mismatch = [f for f in errors(self.service, self.ann_id)
                    if f.code == "attachment_mismatch"]
        self.assertEqual([f.field_name for f in mismatch], ["rate_percent"])

    def test_identical_reupload_is_idempotent(self):
        first = self.service.add_attachment(self.ann_id, "table", self.CSV,
                                            uploaded_by="业务员甲", authorized_by="授权丙",
                                            now=NOW, extractor=CsvTableExtractor())
        again = self.service.add_attachment(self.ann_id, "table", self.CSV,
                                            uploaded_by="业务员甲", authorized_by="授权丙",
                                            now=NOW, extractor=CsvTableExtractor())
        self.assertEqual(first.attachment_id, again.attachment_id)
        self.assertEqual(len(self.service.get(self.ann_id).attachments), 1)

    def test_reupload_must_declare_superseded_attachment(self):
        first = self.service.add_attachment(self.ann_id, "table", self.CSV,
                                            uploaded_by="业务员甲", authorized_by="授权丙",
                                            now=NOW, extractor=CsvTableExtractor())
        new_content = self.CSV.replace(b"500", b"500 ")
        with self.assertRaises(ConflictError):
            self.service.add_attachment(self.ann_id, "table", new_content,
                                        uploaded_by="业务员甲", authorized_by="授权丙",
                                        now=NOW, extractor=CsvTableExtractor())
        second = self.service.add_attachment(
            self.ann_id, "table", new_content,
            uploaded_by="业务员甲", authorized_by="授权丙",
            now=NOW, extractor=CsvTableExtractor(),
            supersedes=first.attachment_id)
        active = self.service.active_attachment(self.ann_id)
        self.assertEqual(active.attachment_id, second.attachment_id)
        self.assertEqual(second.supersedes, first.attachment_id)

    def test_image_attachment_requires_ocr(self):
        with self.assertRaises(ValueError):
            ImageExtractor().extract(b"\x89PNG...")
        ocr = ImageExtractor(ocr=lambda content: {"rate_percent": "1.80"})
        record = self.service.add_attachment(self.ann_id, "image", b"\x89PNG...",
                                             uploaded_by="业务员甲", authorized_by="授权丙",
                                             now=NOW, extractor=ocr)
        self.assertEqual(record.extracted["rate_percent"], "1.80")


class PublishTest(unittest.TestCase):
    def _published(self, service, ann_id):
        service.review(ann_id, reviewer="复核乙", now=NOW)
        service.freeze(ann_id, now=NOW)
        return service.publish(ann_id, now=NOW)

    def test_three_channels_share_one_frozen_hash(self):
        service = make_service()
        outcome = service.ingest(make_result(), actor="业务员甲", now=NOW)
        published = self._published(service, outcome.announcement_id)
        hashes = {service.published_view(outcome.announcement_id, channel).content_hash
                  for channel in ("api", "web", "download")}
        self.assertEqual(hashes, {published.content_hash})
        for channel in ("api", "web", "download"):
            view = service.published_view(outcome.announcement_id, channel)
            self.assertEqual(view.body["fields"]["rate_percent"], "1.8")

    def test_freeze_requires_review_and_clean_findings(self):
        service = make_service()
        outcome = service.ingest(make_result(), actor="业务员甲", now=NOW)
        with self.assertRaises(ConflictError):
            service.freeze(outcome.announcement_id, now=NOW)  # 未复核
        with self.assertRaises(DomainError):
            service.review(outcome.announcement_id, reviewer="业务员甲", now=NOW)  # 本人

    def test_correction_chain_after_publish(self):
        service = make_service()
        outcome = service.ingest(make_result(), actor="业务员甲", now=NOW)
        ann_id = outcome.announcement_id
        first = self._published(service, ann_id)

        # 业务汇总本身勘误为 1.85, 公告走人工修改 + 复核 + 重发
        service.load_summary(make_summary([make_entry(rate="1.85")]))
        edit = service.request_edit(ann_id, "rate_percent", "1.85",
                                    reason="业务汇总勘误: 中标利率为1.85%",
                                    editor="业务员甲", now=NOW)
        service.review_edit(edit.edit_id, reviewer="复核乙", approve=True, now=NOW)
        service.review(ann_id, reviewer="审核丁", now=NOW)
        service.freeze(ann_id, now=NOW)
        second = service.publish(ann_id, now=NOW)

        ann = service.get(ann_id)
        self.assertNotEqual(first.content_hash, second.content_hash)
        self.assertEqual(ann.published_version, 2)
        self.assertEqual(ann.versions[0].status, VersionStatus.SUPERSEDED)
        self.assertEqual(ann.latest.correction_reason, "业务汇总勘误: 中标利率为1.85%")
        self.assertEqual(ann.latest.supersedes, 1)
        # 三个载体已切换到新冻结内容
        for channel in ("api", "web", "download"):
            self.assertEqual(service.published_view(ann_id, channel).content_hash,
                             second.content_hash)
        # 旧版本仍可追溯
        self.assertEqual(ann.versions[0].fields["rate_percent"], "1.8")


class TraceTest(unittest.TestCase):
    def test_trace_media_quoted_value_to_source_reviewer_and_chain(self):
        service = make_service()
        outcome = service.ingest(make_result(), actor="业务员甲", now=NOW)
        ann_id = outcome.announcement_id
        service.review(ann_id, reviewer="复核乙", now=NOW)
        service.freeze(ann_id, now=NOW)
        service.publish(ann_id, now=NOW)

        service.load_summary(make_summary([make_entry(rate="1.85")]))
        edit = service.request_edit(ann_id, "rate_percent", "1.85",
                                    reason="业务汇总勘误: 中标利率为1.85%",
                                    editor="业务员甲", now=NOW)
        service.review_edit(edit.edit_id, reviewer="复核乙", approve=True, now=NOW)
        service.review(ann_id, reviewer="审核丁", now=NOW)
        service.freeze(ann_id, now=NOW)
        service.publish(ann_id, now=NOW)

        # 媒体引用 1.85: 定位到更正版本、修改凭据与审核人
        hits = service.trace_value("rate_percent", "1.85")
        self.assertEqual(len(hits), 1)
        hit = hits[0]
        self.assertEqual(hit.version, 2)
        self.assertEqual(hit.provenance.kind, SourceKind.MANUAL_EDIT)
        self.assertEqual(hit.provenance.source_id, edit.edit_id)
        self.assertEqual(hit.reviewed_by, "审核丁")
        self.assertTrue(any("勘误" in reason for reason in hit.correction_chain))

        # 媒体引用旧值 1.80: 定位到原始结构化来源
        old = service.trace_value("rate_percent", "1.80")
        self.assertEqual(len(old), 1)
        self.assertEqual(old[0].version, 1)
        self.assertEqual(old[0].provenance.kind, SourceKind.STRUCTURED)
        self.assertEqual(old[0].provenance.source_id, "PAY-OP-001")


class DeadlineReportTest(unittest.TestCase):
    def test_report_shows_differences_missing_and_unpublished(self):
        service = make_service(make_summary([make_entry("OP-001"), make_entry("OP-002")]))
        outcome = service.ingest(make_result("OP-001", rate="1.08"),
                                 actor="业务员甲", now=NOW)
        report = service.deadline_report(TRADE_DAY)
        # OP-002 在汇总中但还没有公告
        self.assertEqual(report["missing_operations"], ["OP-002"])
        row = report["announcements"][0]
        self.assertEqual(row["announcement_id"], outcome.announcement_id)
        self.assertTrue(row["differences"])            # 利率抄错
        self.assertIsNone(row["published_version"])    # 截止前尚未发布
        self.assertTrue(any("附件" in w for w in row["warnings"]))


if __name__ == "__main__":
    unittest.main()
