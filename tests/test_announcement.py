import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal

from src.announcement import (
    AnnouncementCheckService,
    AnnouncementError,
    Attachment,
    Calendar,
    DeadlinePassed,
    OperationResult,
    Severity,
    VersionStatus,
    canonical_json,
    parse_amount_yi,
    parse_rate,
    parse_term_days,
    sha256_hex,
)

# 2026-09-19 是周六，模拟国庆调休补班；10-01 至 10-07 休市
HOLIDAYS = {date(2026, 10, d) for d in range(1, 8)}
MAKEUP = {date(2026, 9, 19)}

BASE_TIME = datetime(2026, 9, 24, 9, 30)


def make_clock(start=BASE_TIME):
    current = {"t": start}

    def clock():
        return current["t"]

    clock.advance = lambda **kw: current.__setitem__("t", current["t"] + timedelta(**kw))
    clock.set = lambda t: current.__setitem__("t", t)
    return clock


def op(
    operation_id="RR-20260924-01",
    trade_date=date(2026, 9, 24),
    op_type="逆回购",
    term_days=7,
    fixed_rate="1.50%",
    amount_yi="1820亿元",
    allocation="全额满足",
    idempotency_key="fetch-1",
    maturity_date=None,
):
    return OperationResult.from_dict(
        {
            "operation_id": operation_id,
            "trade_date": trade_date.isoformat(),
            "op_type": op_type,
            "term_days": term_days,
            "fixed_rate": fixed_rate,
            "amount_yi": amount_yi,
            "allocation": allocation,
            "idempotency_key": idempotency_key,
            **({"maturity_date": maturity_date.isoformat()} if maturity_date else {}),
        }
    )


def service(clock=None, ocr_adapter=None):
    return AnnouncementCheckService(
        calendar=Calendar(holidays=HOLIDAYS, makeup_workdays=MAKEUP),
        ocr_adapter=ocr_adapter,
        clock=clock or make_clock(),
    )


class ParsingTest(unittest.TestCase):
    def test_rate_amount_term_normalization(self):
        self.assertEqual(parse_rate("1.50％"), Decimal("1.50"))
        self.assertEqual(parse_amount_yi("0.18万亿元"), Decimal("1800"))
        self.assertEqual(parse_amount_yi("1,820亿"), Decimal("1820"))
        self.assertEqual(parse_term_days("7天期"), 7)

    def test_decimal_equivalence_in_comparison(self):
        # "1.50%" 与 1.5 数值等价，不得误报差异
        svc = service()
        svc.ingest_result(op(fixed_rate="1.50%"))
        svc.ingest_attachment(
            Attachment(
                attachment_id="att-1",
                kind="table",
                authorized=True,
                filename="t.csv",
                content=[{"操作编号": "RR-20260924-01", "固定利率": "1.5%", "操作量": "1820亿元"}],
            ),
            date(2026, 9, 24),
        )
        report = svc.check(date(2026, 9, 24))
        self.assertEqual(report.blockers, [])


class IdempotencyTest(unittest.TestCase):
    def test_duplicate_fetch_does_not_create_second_announcement(self):
        svc = service()
        self.assertEqual(svc.ingest_result(op()), "accepted")
        self.assertEqual(svc.ingest_result(op()), "duplicate")
        # 换抓取事件号但内容完全一致，仍然重复
        self.assertEqual(svc.ingest_result(op(idempotency_key="fetch-1-retry")), "duplicate")
        report = svc.check(date(2026, 9, 24))
        self.assertEqual(report.operation_count, 1)
        group = svc._get_group(date(2026, 9, 24))
        self.assertEqual(len(group.versions), 1)

    def test_same_idempotency_key_other_day_rejected(self):
        svc = service()
        svc.ingest_result(op())
        with self.assertRaises(AnnouncementError):
            svc.ingest_result(op(trade_date=date(2026, 9, 25), operation_id="RR-20260925-01"))

    def test_same_operation_changed_content_is_conflict(self):
        svc = service()
        svc.ingest_result(op())
        status = svc.ingest_result(op(idempotency_key="fetch-2", fixed_rate="1.60%"))
        self.assertEqual(status, "conflict")
        report = svc.check(date(2026, 9, 24))
        self.assertTrue(any(d.code == "HISTORY" for d in report.blockers))


class SameDayMultiOperationTest(unittest.TestCase):
    def test_two_operations_same_day_one_announcement(self):
        svc = service()
        svc.ingest_result(op())
        svc.ingest_result(
            op(operation_id="RR-20260924-02", idempotency_key="fetch-2", term_days=14, amount_yi="500亿元")
        )
        report = svc.check(date(2026, 9, 24))
        self.assertEqual(report.operation_count, 2)
        self.assertEqual(report.blockers, [])
        svc.freeze(date(2026, 9, 24), "审查人员A")
        artifacts = svc.publish(date(2026, 9, 24), "发布人员B")
        text = artifacts["file"].decode("utf-8")
        self.assertIn("1820", text)
        self.assertIn("500", text)


class ZeroPlacementTest(unittest.TestCase):
    def test_zero_amount_renders_net_line(self):
        clock = make_clock()
        svc = service(clock)
        # 7 天前（2026-09-17）投放 2000 亿，今日到期
        svc.ingest_result(
            op(
                operation_id="RR-20260917-01",
                trade_date=date(2026, 9, 17),
                amount_yi="2000亿元",
                idempotency_key="old-1",
            )
        )
        clock.advance(days=7)
        svc.ingest_result(op(amount_yi="0亿元", idempotency_key="today-1"))
        report = svc.check(date(2026, 9, 24), now=clock())
        self.assertEqual(report.due_yi, Decimal("2000"))
        self.assertEqual(report.net_yi, Decimal("-2000"))
        svc.freeze(date(2026, 9, 24), "审查人员A", now=clock())
        text = svc.publish(date(2026, 9, 24), "发布人员B")["file"].decode("utf-8")
        self.assertIn("实现净投放-2000亿元", text)


class CalendarTest(unittest.TestCase):
    def test_makeup_workday_saturday_is_tradable(self):
        svc = service()
        sat = date(2026, 9, 19)
        svc.ingest_result(
            op(
                operation_id="RR-20260919-01",
                trade_date=sat,
                idempotency_key="sat-1",
                term_days=7,
            )
        )
        report = svc.check(sat)
        self.assertEqual(report.blockers, [])
        # 7 天后为 9-26 周六，非补班日，到期顺延至 9-28 周一
        snap = svc._get_group(sat).current.operations
        self.assertEqual(snap["op:RR-20260919-01"]["maturity_date"], date(2026, 9, 28))

    def test_holiday_weekday_blocks_and_rolls_maturity(self):
        svc = service()
        national_day = date(2026, 10, 1)
        svc.ingest_result(
            op(
                operation_id="RR-20261001-01",
                trade_date=national_day,
                idempotency_key="hol-1",
                term_days=7,
            )
        )
        report = svc.check(national_day)
        self.assertTrue(any(d.code == "CALENDAR" and d.field == "trade_date" for d in report.blockers))
        with self.assertRaises(AnnouncementError):
            svc.freeze(national_day, "审查人员A")

    def test_explicit_wrong_maturity_flagged(self):
        svc = service()
        # 应顺延至 2026-10-08（国庆假期），却填了 10-01
        svc.ingest_result(op(trade_date=date(2026, 9, 24), maturity_date=date(2026, 10, 1)))
        report = svc.check(date(2026, 9, 24))
        self.assertTrue(any(d.code == "CALENDAR" and d.field == "maturity_date" for d in report.blockers))


class AttachmentTest(unittest.TestCase):
    CSV_HEADER = "操作编号,期限,固定利率,操作量,满足方式\r\n"

    def test_table_attachment_matches(self):
        svc = service()
        svc.ingest_result(op())
        csv_content = self.CSV_HEADER + "RR-20260924-01,7天期,1.50%,1820亿元,全额\r\n"
        svc.ingest_attachment(
            Attachment("att-1", "table", True, "omo.csv", csv_content), date(2026, 9, 24)
        )
        report = svc.check(date(2026, 9, 24))
        self.assertEqual(report.blockers, [])

    def test_rate_mismatch_blocks_freeze(self):
        svc = service()
        svc.ingest_result(op())
        svc.ingest_attachment(
            Attachment("att-1", "table", True, "omo.csv", self.CSV_HEADER + "RR-20260924-01,7天期,1.80%,1820亿元,全额满足\r\n"),
            date(2026, 9, 24),
        )
        report = svc.check(date(2026, 9, 24))
        mismatch = [d for d in report.blockers if d.code == "MISMATCH" and d.field == "fixed_rate"]
        self.assertEqual(len(mismatch), 1)
        self.assertIn("1.80", mismatch[0].message)
        with self.assertRaises(AnnouncementError):
            svc.freeze(date(2026, 9, 24), "审查人员A")

    def test_retransmitted_attachment_is_idempotent(self):
        svc = service()
        blob = self.CSV_HEADER + "RR-20260924-01,7天期,1.50%,1820亿元,全额满足\r\n"
        att = Attachment("att-1", "table", True, "omo.csv", blob)
        svc.ingest_result(op())
        self.assertEqual(svc.ingest_attachment(att, date(2026, 9, 24)), "accepted")
        self.assertEqual(svc.ingest_attachment(att, date(2026, 9, 24)), "retransmitted")
        version = svc._get_group(date(2026, 9, 24)).current
        self.assertEqual(len({k.split(":r")[1].split(":")[0] for k in version.attachments}), 1)

    def test_revised_attachment_keeps_both_revisions(self):
        svc = service()
        svc.ingest_result(op())
        svc.ingest_attachment(
            Attachment("att-1", "table", True, "omo.csv", self.CSV_HEADER + "RR-20260924-01,7天期,1.80%,1820亿元,全额满足\r\n"),
            date(2026, 9, 24),
        )
        # 重传修正版（字节不同）-> r2，与业务汇总一致
        status = svc.ingest_attachment(
            Attachment("att-1", "table", True, "omo.csv", self.CSV_HEADER + "RR-20260924-01,7天期,1.50%,1820亿元,全额满足\r\n"),
            date(2026, 9, 24),
        )
        self.assertEqual(status, "revised")
        report = svc.check(date(2026, 9, 24))
        # r1 的差异仍在（旧修订保留留痕），因此不能直接冻结——需要人工确认旧修订作废
        self.assertTrue(any("attachment:att-1:r1" in d.values_by_source for d in report.blockers))
        version = svc._get_group(date(2026, 9, 24)).current
        self.assertIn("attachment:att-1:r1", version.sources["op:RR-20260924-01"])
        self.assertIn("attachment:att-1:r2", version.sources["op:RR-20260924-01"])

    def test_unauthorized_attachment_blocks(self):
        svc = service()
        svc.ingest_result(op())
        status = svc.ingest_attachment(
            Attachment("att-x", "table", False, "leak.xls", [{"操作量": "9999亿元"}]), date(2026, 9, 24)
        )
        self.assertEqual(status, "unauthorized")
        report = svc.check(date(2026, 9, 24))
        self.assertTrue(any(d.code == "UNAUTHORIZED" for d in report.blockers))

    def test_image_attachment_through_ocr_adapter(self):
        svc = service(ocr_adapter=lambda content, name: [
            {"操作编号": "RR-20260924-01", "期限": "7天", "固定利率": "1.50%", "操作量": "1820亿", "满足方式": "全额"}
        ])
        svc.ingest_result(op())
        svc.ingest_attachment(
            Attachment("img-1", "image", True, "scan.png", b"\x89PNG-fake"), date(2026, 9, 24)
        )
        report = svc.check(date(2026, 9, 24))
        self.assertEqual(report.blockers, [])

    def test_attachment_only_operation_is_missing_authority(self):
        svc = service()
        svc.ingest_result(op())
        svc.ingest_attachment(
            Attachment(
                "att-1", "table", True, "omo.csv",
                [
                    {"操作编号": "RR-20260924-01", "固定利率": "1.50%", "操作量": "1820亿元"},
                    {"操作编号": "RR-20260924-02", "固定利率": "2.00%", "操作量": "100亿元", "期限": "14天"},
                ],
            ),
            date(2026, 9, 24),
        )
        report = svc.check(date(2026, 9, 24))
        self.assertTrue(any(d.operation_key == "op:RR-20260924-02" for d in report.blockers))


class ManualEditReviewTest(unittest.TestCase):
    def _svc_with_mismatch(self):
        svc = service()
        svc.ingest_result(op())
        svc.ingest_attachment(
            Attachment(
                "att-1", "table", True, "omo.csv",
                [{"操作编号": "RR-20260924-01", "固定利率": "1.80%", "操作量": "1820亿元"}],
            ),
            date(2026, 9, 24),
        )
        report = svc.check(date(2026, 9, 24))
        disc = next(d for d in report.blockers if d.field == "fixed_rate")
        return svc, disc

    def test_edit_requires_reason_and_second_pair_of_eyes(self):
        svc, disc = self._svc_with_mismatch()
        with self.assertRaises(AnnouncementError):
            svc.apply_manual_edit(
                date(2026, 9, 24), "op:RR-20260924-01", "fixed_rate", "1.50%", "", "一线执行人员"
            )
        edit_id = svc.apply_manual_edit(
            date(2026, 9, 24), "op:RR-20260924-01", "fixed_rate", "1.50%",
            "业务系统电话确认附件利率抄错", "一线执行人员", discrepancy_id=disc.id,
        )
        # 待复核期间不能冻结
        report = svc.check(date(2026, 9, 24))
        self.assertFalse(report.can_freeze())
        self.assertEqual(report.status, VersionStatus.PENDING_REVIEW)
        with self.assertRaises(AnnouncementError):
            svc.freeze(date(2026, 9, 24), "审查人员A")
        # 本人不能复核本人
        with self.assertRaises(AnnouncementError):
            svc.approve_edit(date(2026, 9, 24), edit_id, "一线执行人员")
        svc.approve_edit(date(2026, 9, 24), edit_id, "审查人员A", note="已与业务汇总核对")
        report = svc.check(date(2026, 9, 24))
        self.assertTrue(report.can_freeze())
        resolved = [d for d in report.resolved if d.field == "fixed_rate"]
        self.assertEqual(resolved[0].resolved_by_edit, edit_id)

    def test_approved_edit_value_flows_to_frozen_content(self):
        svc, disc = self._svc_with_mismatch()
        edit_id = svc.apply_manual_edit(
            date(2026, 9, 24), "op:RR-20260924-01", "fixed_rate", "1.50%",
            "业务系统确认", "一线执行人员",
        )
        svc.approve_edit(date(2026, 9, 24), edit_id, "审查人员A")
        manifest = svc.freeze(date(2026, 9, 24), "审查人员A")
        self.assertIn("1.50", manifest.rendered_text)
        self.assertNotIn("1.80", manifest.rendered_text)


class FreezePublishTest(unittest.TestCase):
    def test_three_artifacts_point_to_same_frozen_content(self):
        svc = service()
        svc.ingest_result(op())
        manifest = svc.freeze(date(2026, 9, 24), "审查人员A")
        with self.assertRaises(AnnouncementError):
            svc.get_publication(date(2026, 9, 24))
        first = svc.publish(date(2026, 9, 24), "发布人员B")
        # 已发布后同内容重复抓取仍是幂等 duplicate，不产生任何变更
        self.assertEqual(svc.ingest_result(op(idempotency_key="fetch-after-publish")), "duplicate")
        # 内容有变则必须走勘误，直接录入被拒绝
        with self.assertRaises(AnnouncementError):
            svc.ingest_result(op(idempotency_key="fetch-after-publish-2", fixed_rate="1.60%"))
        again = svc.get_publication(date(2026, 9, 24))
        for name in ("api", "web", "file"):
            self.assertEqual(first[name], again[name])
            self.assertIn(manifest.content_hash.encode(), first[name])
        consistency = svc.verify_artifact_consistency(date(2026, 9, 24))
        self.assertTrue(all(consistency.values()))
        # 冻结内容确定性：同输入同哈希
        import json as _json
        api_body = _json.loads(first["api"].decode("utf-8"))
        self.assertEqual(api_body["content_hash"], manifest.content_hash)
        web = first["web"].decode("utf-8")
        self.assertIn(f'content="{manifest.content_hash}"', web)

    def test_deadline_blocks_freeze_and_report_shows_remaining(self):
        clock = make_clock()
        svc = service(clock)
        deadline = clock() + timedelta(minutes=30)
        svc.ingest_result(op(), deadline=deadline)
        report = svc.check(date(2026, 9, 24))
        self.assertEqual(report.deadline_remaining(), timedelta(minutes=30))
        clock.advance(minutes=31)
        with self.assertRaises(DeadlinePassed):
            svc.freeze(date(2026, 9, 24), "审查人员A")

    def test_missing_field_shown_before_deadline(self):
        svc = service()
        svc.ingest_result(op(allocation="")) if False else None
        svc.ingest_result(
            OperationResult.from_dict(
                {
                    "operation_id": "RR-20260924-01",
                    "trade_date": "2026-09-24",
                    "op_type": "逆回购",
                    "term_days": 7,
                    "fixed_rate": "1.50%",
                    "amount_yi": "1820亿元",
                    "allocation": "全额满足",
                    "idempotency_key": "fetch-1",
                }
            )
        )
        # 业务结果本身会按日历补齐到期日，故必填完整；改为删除到期日验证缺失报告
        version = svc._get_group(date(2026, 9, 24)).current
        version.operations["op:RR-20260924-01"]["maturity_date"] = None
        report = svc.check(date(2026, 9, 24))
        self.assertIn("op:RR-20260924-01.maturity_date", report.missing_fields)


class CorrectionTest(unittest.TestCase):
    def _published(self):
        svc = service(make_clock())
        svc.ingest_result(op(fixed_rate="1.50%"))
        svc.freeze(date(2026, 9, 24), "审查人员A")
        svc.publish(date(2026, 9, 24), "发布人员B")
        return svc

    def test_correction_creates_version_chain_and_supersedes_old(self):
        svc = self._published()
        with self.assertRaises(AnnouncementError):
            svc.initiate_correction(date(2026, 9, 24), "", "一线执行人员")
        v2 = svc.initiate_correction(date(2026, 9, 24), "公告利率应为1.60%，原稿笔误", "业务负责人")
        self.assertEqual(v2, 2)
        group = svc._get_group(date(2026, 9, 24))
        self.assertEqual(group.versions[0].status, VersionStatus.PUBLISHED)  # 旧版在新版发布前仍有效
        # 勘误说明本身也要复核
        with self.assertRaises(AnnouncementError):
            svc.freeze(date(2026, 9, 24), "审查人员A")
        svc.approve_edit(date(2026, 9, 24), "E0001", "审查人员A", note="情况属实")
        edit_id = svc.apply_manual_edit(
            date(2026, 9, 24), "op:RR-20260924-01", "fixed_rate", "1.60%",
            "业务系统成交单为1.60%", "一线执行人员",
        )
        svc.approve_edit(date(2026, 9, 24), edit_id, "审查人员A")
        svc.freeze(date(2026, 9, 24), "审查人员A")
        svc.publish(date(2026, 9, 24), "发布人员B")
        self.assertEqual(group.versions[0].status, VersionStatus.SUPERSEDED)
        self.assertEqual(group.current.status, VersionStatus.PUBLISHED)
        text = svc.get_publication(date(2026, 9, 24))["file"].decode("utf-8")
        self.assertIn("1.60", text)
        self.assertIn("【勘误】公告利率应为1.60%，原稿笔误", text)
        # 公众接口只返回当前有效版本
        self.assertNotIn("1.50", text)

    def test_correction_requires_published_or_frozen(self):
        svc = service()
        svc.ingest_result(op())
        with self.assertRaises(AnnouncementError):
            svc.initiate_correction(date(2026, 9, 24), "想改", "业务负责人")

    def test_attachment_change_after_publish_requires_correction(self):
        svc = self._published()
        with self.assertRaises(AnnouncementError):
            svc.ingest_attachment(
                Attachment("att-9", "table", True, "new.csv", [{"操作量": "1亿元"}]),
                date(2026, 9, 24),
            )


class TraceabilityTest(unittest.TestCase):
    def test_trace_value_across_sources_edits_and_correction_chain(self):
        svc = service(make_clock())
        svc.ingest_result(op(fixed_rate="1.50%"))
        svc.ingest_attachment(
            Attachment(
                "att-1", "table", True, "omo.csv",
                [{"操作编号": "RR-20260924-01", "固定利率": "1.50%", "操作量": "1820亿元"}],
            ),
            date(2026, 9, 24),
        )
        svc.freeze(date(2026, 9, 24), "审查人员A")
        svc.publish(date(2026, 9, 24), "发布人员B")
        svc.initiate_correction(date(2026, 9, 24), "利率笔误", "业务负责人")
        svc.approve_edit(date(2026, 9, 24), "E0001", "审查人员A")
        edit_id = svc.apply_manual_edit(
            date(2026, 9, 24), "op:RR-20260924-01", "fixed_rate", "1.60%", "成交单确认", "一线执行人员"
        )
        svc.approve_edit(date(2026, 9, 24), edit_id, "审查人员A", note="已复核")
        svc.freeze(date(2026, 9, 24), "审查人员A")
        svc.publish(date(2026, 9, 24), "发布人员B")

        hits = svc.trace_value("1.60%", field_name="fixed_rate")
        self.assertEqual(len(hits), 1)
        hit = hits[0]
        self.assertEqual(hit["version_no"], 2)
        self.assertEqual(hit["frozen_by"], "审查人员A")
        self.assertEqual(hit["edits"][0]["editor"], "一线执行人员")
        self.assertEqual(hit["edits"][0]["reviewed_by"], "审查人员A")
        self.assertEqual(hit["predecessor_version"], 1)
        self.assertTrue(hit["published"])
        self.assertIn("business-summary", hit["sources"])
        self.assertTrue(hit["attachments"])
        self.assertEqual(hit["attachments"][0]["attachment_id"], "att-1")
        self.assertEqual(len(hit["attachments"][0]["digest"]), 64)

        # 媒体引用旧值 1.50 仍可定位到第 1 版及其被替代事实
        old = svc.trace_value("1.50", field_name="fixed_rate")
        self.assertEqual({h["version_no"] for h in old}, {1})
        self.assertEqual(old[0]["status"], VersionStatus.SUPERSEDED.value)

    def test_trace_amount_without_field_name(self):
        svc = service()
        svc.ingest_result(op(amount_yi="1820亿元"))
        svc.freeze(date(2026, 9, 24), "审查人员A")
        hits = svc.trace_value("1820")
        self.assertTrue(any(h["field"] == "amount_yi" for h in hits))


if __name__ == "__main__":
    unittest.main()
