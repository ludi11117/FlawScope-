"""本轮新增能力的回归测试：分页翻页、工单 Markdown 导出、诊断过载保护。

全部离线运行：不调用 LLM、不联网、不触碰真实数据库与向量库。
"""

import threading
import time

import pytest
from fastapi import HTTPException

import database
import workorder_export


@pytest.fixture(scope="module")
def app_module():
    """导入 Streamlit 前端模块，用于测试其中的纯逻辑。

    app.py 顶层会整脚本执行一遍（Streamlit 裸模式下控件返回默认值），这是它的
    固有形态；这里只取 _page_window / _history_summary 这两个纯函数来验证。
    顺手把 streamlit 的日志压掉，否则每次跑测试都会刷一屏 ScriptRunContext 警告。
    """
    import logging
    logging.getLogger("streamlit").setLevel(logging.ERROR)
    import app
    return app


# ========== 分页：total 修对之后还必须真的能翻到第二页 ==========

def test_get_records_offset_pages_do_not_overlap_or_skip(isolated_db):
    """只有 limit 没有 offset 时，total 再准也只能取到第一页。

    这里插入 5 条，按每页 2 条翻三页，要求：不重、不漏、顺序稳定。
    """
    for i in range(5):
        isolated_db.save_diagnosis_record(f"故障{i}", {"status": "done"}, {})

    page1 = isolated_db.get_records(limit=2, offset=0)
    page2 = isolated_db.get_records(limit=2, offset=2)
    page3 = isolated_db.get_records(limit=2, offset=4)

    ids = [r["id"] for r in page1 + page2 + page3]
    assert len(ids) == 5
    assert len(set(ids)) == 5, "翻页出现了重复记录"
    assert ids == sorted(ids, reverse=True), "翻页顺序必须与列表一致（id 倒序）"
    assert len(page3) == 1


def test_get_records_clamps_negative_offset(isolated_db):
    """负数 offset 在不同 SQLite 版本上行为不一致，统一夹到 0。"""
    isolated_db.save_diagnosis_record("故障", {"status": "done"}, {})
    assert len(isolated_db.get_records(limit=10, offset=-5)) == 1


def test_count_records_matches_sum_of_pages(isolated_db):
    """总数必须等于逐页取回的行数之和——分页与计数用的是同一套过滤条件。"""
    for i in range(5):
        isolated_db.save_diagnosis_record(f"故障{i}", {"status": "done"}, {})

    paged = sum(
        len(isolated_db.get_records(limit=2, offset=offset))
        for offset in range(0, 10, 2)
    )
    assert paged == isolated_db.count_records() == 5


def test_records_endpoint_serves_second_page(isolated_db):
    """端到端：limit=2 时第二页必须拿到的是第 3、4 条，而不是又一份前两条。"""
    for i in range(5):
        isolated_db.save_diagnosis_record(f"故障{i}", {"status": "done"}, {})

    from api import records as records_endpoint

    first = records_endpoint(keyword="", status="", limit=2, offset=0, _=None)
    second = records_endpoint(keyword="", status="", limit=2, offset=2, _=None)

    assert first["total"] == second["total"] == 5
    assert first["offset"] == 0 and second["offset"] == 2
    assert first["limit"] == second["limit"] == 2

    first_ids = {r["id"] for r in first["records"]}
    second_ids = {r["id"] for r in second["records"]}
    assert first_ids.isdisjoint(second_ids), "两页返回了同一条记录"


def test_records_endpoint_declares_offset_as_optional():
    """不带 offset 的老调用方必须不受影响：offset 在 HTTP 契约里是可选项、默认 0。

    这里查 OpenAPI schema，而不是直接调用端点函数——直接调用会绕过 FastAPI 的
    参数解析，拿到的是 Query 对象本身，测不出真实契约。
    """
    from api import app as fastapi_app

    params = fastapi_app.openapi()["paths"]["/records"]["get"]["parameters"]
    by_name = {p["name"]: p for p in params}

    assert "offset" in by_name, "/records 缺少 offset 参数，无法翻页"
    assert by_name["offset"]["required"] is False
    assert by_name["offset"]["schema"]["default"] == 0
    assert by_name["offset"]["schema"]["minimum"] == 0


def test_workorder_markdown_endpoint_is_registered():
    """导出接口必须在 OpenAPI 里可见，否则前端拼出来的下载链接是 404。"""
    from api import app as fastapi_app

    assert "/records/{record_id}/workorder.md" in fastapi_app.openapi()["paths"]


# ========== 前端分页窗口与计数口径 ==========

def test_page_window_clamps_page_into_range(app_module):
    """换了筛选条件后结果变少，停在第 5 页会看到空列表，用户会以为"一条都没有"。
    必须夹回最后一页。"""
    assert app_module._page_window(total=100, page=5, page_size=20) == (5, 80, 5)
    assert app_module._page_window(total=10, page=5, page_size=20) == (1, 0, 1)
    assert app_module._page_window(total=10, page=0, page_size=20) == (1, 0, 1)


def test_page_window_handles_empty_result(app_module):
    """没有任何记录时，也必须是合法的第 1 页而不是第 0 页。"""
    assert app_module._page_window(total=0, page=1, page_size=20) == (1, 0, 1)


def test_page_window_rounds_page_count_up(app_module):
    """21 条按每页 20 条，必须是 2 页——少一页会让最后一条永远看不到。"""
    assert app_module._page_window(total=21, page=1, page_size=20)[2] == 2
    assert app_module._page_window(total=20, page=1, page_size=20)[2] == 1


def test_history_summary_uses_true_total_not_page_size(app_module):
    """前端此前用 len(get_records(...)) 当总数，被 LIMIT 截断：
    库里有 500 条也只显示"共找到 20 条记录"，用户根本不知道还有更多。"""
    text = app_module._history_summary(total=500, shown=20)
    assert "500" in text and "20" in text

    # 一页装得下时不必画蛇添足地写"当前显示"
    text = app_module._history_summary(total=8, shown=8)
    assert "8" in text
    assert "当前显示" not in text


# ========== 工单 Markdown 导出 ==========

FULL_WORKORDER = {
    "工单编号": "WO-20260917-001",
    "故障现象": "主轴转速不稳、异响、温升异常",
    "根因": "主轴轴承损坏或润滑不良",
    "维修方案": "更换主轴轴承并补充润滑脂",
    "备件清单": ["主轴轴承", "润滑脂"],
    "预计成本": "1230元",
    "安全注意事项": "断电挂牌后作业",
    "风险等级": "高风险待复核",
    "风险说明": "诊断结论经多轮辩论仍未通过审核，建议人工复核后执行",
}


def test_workorder_markdown_renders_all_sections():
    md = workorder_export.workorder_to_markdown(FULL_WORKORDER)

    assert md.startswith("# 维修工单 WO-20260917-001")
    assert "高风险待复核" in md
    for heading in ("故障现象", "根因", "维修方案", "备件清单", "安全注意事项"):
        assert f"## {heading}" in md
    assert "| 1 | 主轴轴承 |" in md
    assert "断电挂牌后作业" in md


def test_workorder_markdown_flags_unpriced_parts():
    """价格表没覆盖的备件是不计费的，导出单上必须写出来，
    否则现场会以为报价是完整的。"""
    cost = {
        "预计成本": "1230元",
        "预计工时": "2.0小时",
        "成本明细": {"备件费用": 850, "工时费用": 300, "总费用": 1150, "未知备件": ["外星轴承"]},
        "计费提示": "以下备件不在价格表中，未计入报价，需人工核价：外星轴承",
    }
    md = workorder_export.workorder_to_markdown(FULL_WORKORDER, cost=cost)

    assert "## 费用" in md
    assert "外星轴承" in md
    assert "未计入报价" in md


def test_workorder_markdown_does_not_invent_sections_for_degraded_order():
    """降级工单只有 工单编号 / 风险等级 / 风险说明 三个字段。

    渲染器不能替它补出空的「维修方案」章节——那看起来像"方案就是空的"，
    而不是"这一轮根本没产出方案"。
    """
    degraded = {
        "工单编号": "WO-abc12345",
        "风险等级": "待人工确认（模型服务异常）",
        "风险说明": "工单生成失败，请人工根据诊断与成本信息补全后执行",
    }
    md = workorder_export.workorder_to_markdown(degraded)

    assert "WO-abc12345" in md
    assert "待人工确认（模型服务异常）" in md
    for heading in ("维修方案", "备件清单", "费用", "安全注意事项"):
        assert f"## {heading}" not in md


def test_degraded_note_keeps_every_line_inside_blockquote():
    """多行风险说明的**每一行**都得留在引用块里。

    降级工单会附"参考方向"的逐条排查动作（降级报告里是换行拼的）。只给首行
    加 `> ` 前缀的话，后续行会跳出引用块，整段 markdown 结构就散了。

    判别式：除标题外每一行都必须以 `>` 开头——退化回"只加一次前缀"时这里会红。
    """
    degraded = {
        "工单编号": "WO-abc12345",
        "风险等级": "待人工确认（知识库无依据）",
        "风险说明": (
            "知识库未收录「注塑机」。可尝试：补充案例到 data/raw/ 后重建。\n"
            "以下排查动作来自「冷水机组」——非本设备根因，执行前请现场确认\n"
            "- 停机后手动盘车\n"
            "- 检查润滑脂状态"
        ),
    }
    md = workorder_export.workorder_to_markdown(degraded)

    assert "- 停机后手动盘车" in md
    assert "- 检查润滑脂状态" in md
    for line in md.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        assert line.startswith(">"), f"这行跳出了引用块: {line!r}"


def test_workorder_markdown_survives_empty_and_malformed_input():
    """导出接口不该因为"这轮没出工单"而 500。"""
    assert "未生成工单" in workorder_export.workorder_to_markdown({})
    assert "未生成工单" in workorder_export.workorder_to_markdown(None)

    # 模型偶尔把字段写成列表，导出器不能假设 preprocess 一定修过
    md = workorder_export.workorder_to_markdown({"工单编号": "WO-1", "维修方案": ["换轴承", "补润滑"]})
    assert "换轴承；补润滑" in md


def test_workorder_filename_sanitizes_unsafe_characters():
    """工单编号来自模型输出，带 `/` 或 `:` 时会写到别的目录去。"""
    assert workorder_export.workorder_filename({"工单编号": "WO-1"}) == "WO-1.md"
    assert "/" not in workorder_export.workorder_filename({"工单编号": "a/b:c"})
    assert workorder_export.workorder_filename({}) == "workorder.md"
    assert workorder_export.workorder_filename({"工单编号": "   "}) == "workorder.md"


def test_workorder_markdown_endpoint_renders_saved_record(isolated_db):
    isolated_db.save_diagnosis_record(
        "主轴异响",
        {
            "status": "done",
            "correlation_id": "cid-export",
            "workorder": FULL_WORKORDER,
            "cost": {
                "预计成本": "1230元",
                "成本明细": {"备件费用": 850, "工时费用": 300, "未知备件": ["外星轴承"]},
                "计费提示": "以下备件不在价格表中，未计入报价，需人工核价：外星轴承",
            },
        },
        {},
    )
    record_id = isolated_db.get_records()[0]["id"]

    from api import workorder_markdown_endpoint

    response = workorder_markdown_endpoint(record_id, _=None)
    body = response.body.decode("utf-8")

    assert "# 维修工单 WO-20260917-001" in body
    assert "## 维修方案" in body
    assert "外星轴承" in body
    assert "cid-export" in body          # 追踪 ID 写进页脚，方便对回日志
    assert "attachment" in response.headers["content-disposition"]


def test_workorder_markdown_endpoint_404s(isolated_db):
    from api import workorder_markdown_endpoint

    with pytest.raises(HTTPException) as missing:
        workorder_markdown_endpoint(99999, _=None)
    assert missing.value.status_code == 404

    # 只走到追问环节的记录没有工单，应当是 404 而不是 500
    isolated_db.save_diagnosis_record("坏了", {"status": "need_more_info"}, {})
    record_id = isolated_db.get_records()[0]["id"]

    with pytest.raises(HTTPException) as no_workorder:
        workorder_markdown_endpoint(record_id, _=None)
    assert no_workorder.value.status_code == 404
    assert "工单" in no_workorder.value.detail


# ========== /diagnose 过载保护 ==========

def _request():
    from api import DiagnosisRequest
    return DiagnosisRequest(fault_description="数控机床主轴异响")


def test_diagnose_rejects_with_503_when_slots_are_exhausted(monkeypatch):
    """一次诊断要串行发 6~9 次 LLM 调用、耗时数十秒。不限并发的话突发流量会占满
    FastAPI 线程池（连 /health 都排队），并把模型配额成倍烧掉。

    超限必须是明确的 503 + Retry-After，而不是无声排队——
    排队只会让调用方一直挂到超时，还占着线程不放。
    """
    import api

    monkeypatch.setattr(api, "_diagnosis_slots", threading.BoundedSemaphore(1))
    monkeypatch.setattr(api, "save_diagnosis_record", lambda *a, **kw: None)

    release = threading.Event()

    def _blocking_run(*args, **kwargs):
        release.wait(timeout=5)
        return {"status": "done"}

    monkeypatch.setattr(api, "run_diagnosis", _blocking_run)

    holder = threading.Thread(target=lambda: api.diagnose(_request(), _=None), daemon=True)
    holder.start()
    time.sleep(0.3)                      # 等它占住唯一一格

    try:
        with pytest.raises(HTTPException) as exc:
            api.diagnose(_request(), _=None)
        assert exc.value.status_code == 503
        assert exc.value.headers["Retry-After"] == "10"
    finally:
        release.set()
        holder.join(timeout=5)


def test_diagnose_releases_slot_when_pipeline_raises(monkeypatch):
    """诊断抛异常时必须放回闸门（finally）。

    漏掉的话，连续几次失败后闸门会被永久占满，整个 /diagnose 再也不接请求，
    而且从外部看只是"一直 503"，很难联想到是异常路径漏了 release。
    """
    import api

    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(api, "_diagnosis_slots", slots)

    def _boom(*args, **kwargs):
        raise RuntimeError("模型网关炸了")

    monkeypatch.setattr(api, "run_diagnosis", _boom)

    with pytest.raises(HTTPException) as exc:
        api.diagnose(_request(), _=None)
    assert exc.value.status_code == 500

    assert slots.acquire(blocking=False), "异常路径没有放回闸门"
    slots.release()


def test_diagnose_releases_slot_on_success(monkeypatch):
    """成功路径同样要放回，否则第二次请求就会被自己拒掉。"""
    import api

    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(api, "_diagnosis_slots", slots)
    monkeypatch.setattr(api, "save_diagnosis_record", lambda *a, **kw: None)
    monkeypatch.setattr(
        api, "run_diagnosis",
        lambda *a, **kw: {"status": "done", "correlation_id": "cid-ok"},
    )

    for _ in range(3):
        payload = api.diagnose(_request(), _=None)
        assert payload["status"] == "done"


def test_slot_count_comes_from_settings():
    """闸门大小必须可配置，且默认值不能让单机直接过载。"""
    from config import settings

    assert settings.MAX_CONCURRENT_DIAGNOSES >= 1
    assert settings.MAX_CONCURRENT_DIAGNOSES <= 64


def test_database_json_columns_are_parsed_for_list_and_detail(isolated_db):
    """get_records 与 get_record_by_id 共用同一套 JSON 列反解析。

    两份实现各写一遍时，加一个 JSON 列就会漏掉一处，出现
    "列表里有、详情里没有"。这里对同一列在两条路径上都断言。
    """
    isolated_db.save_diagnosis_record(
        "主轴异响",
        {"status": "done", "diagnosis": {"根因判断": "轴承损坏"}, "cost": {"预计成本": "100元"}},
        {},
    )
    record_id = isolated_db.get_records()[0]["id"]

    listed = isolated_db.get_records()[0]
    detail = isolated_db.get_record_by_id(record_id)

    for row in (listed, detail):
        assert row["diagnosis"]["根因判断"] == "轴承损坏"
        assert row["cost"]["预计成本"] == "100元"
        # 没写过的 JSON 列必须回退成 {}，而不是 None 或空串
        assert row["rebuttal"] == {}
        assert row["final_review"] == {}


def test_json_parsing_falls_back_on_corrupt_data(isolated_db):
    """历史脏数据（非法 JSON）不能把整个查询打挂。"""
    isolated_db.save_diagnosis_record("主轴异响", {"status": "done"}, {})
    with database.get_db_connection() as conn:
        conn.execute("UPDATE diagnosis_records SET diagnosis = ? WHERE id = 1", ("{不是合法JSON",))

    row = isolated_db.get_records()[0]
    assert row["diagnosis"] == {}
    assert row["status"] == "done"       # 其余列不受影响
