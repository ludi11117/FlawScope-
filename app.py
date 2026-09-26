import streamlit as st
import json
import io
import csv
import base64
import uuid
import time
from pathlib import Path
from config import settings
from database import (
    get_records, count_records, get_distinct_statuses, get_stats, save_diagnosis_record
)
from logging_config import configure_logging
from status_meta import status_meta
from workorder_export import workorder_to_markdown, workorder_filename

# Streamlit 进程此前从不调用 configure_logging()，走的是 structlog 默认配置：
# LOG_LEVEL / LOG_JSON 不生效，日志格式也和 API 侧对不上，排查线上问题时两边长得不一样。
configure_logging()

st.set_page_config(page_title="FlawScope 工业故障诊断系统", layout="wide")

st.markdown(
    """
    <style>
    /* 消除鼠标悬停引起的行高/位移抖动（历史列表 expander 逐行跳动） */
    div[data-testid="stExpander"],
    div[data-testid="stExpander"] details,
    div[data-testid="stExpander"] details > summary,
    div[data-testid="stExpander"] details > summary:hover {
        transition: none !important;
        transform: none !important;
    }
    /* 隐藏 <details> 默认三角标记，避免悬停时宽度/行高变化 */
    div[data-testid="stExpander"] details > summary::marker {
        content: "" !important;
    }
    div[data-testid="stExpander"] details > summary::-webkit-details-marker {
        display: none !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("🔧 FlawScope")
st.subheader("多智能体工业设备故障诊断与维修指导系统")

st.markdown("---")

PAGES = {
    "🔧 故障诊断": "diagnosis",
    "📋 诊断历史": "history",
    "📊 系统统计": "stats",
}

page = st.sidebar.radio("功能导航", list(PAGES.keys()))
page_key = PAGES[page]


def render_diagnosis():
    """故障诊断页：多轮追问 + 图片 + 完整诊断展示 + Token 流式输出"""
    # 初始化 session_state
    if "conversation" not in st.session_state:
        st.session_state.conversation = []  # 多轮对话记录
    if "accumulated_input" not in st.session_state:
        st.session_state.accumulated_input = []  # 累积的用户输入片段
    if "diagnosis_done" not in st.session_state:
        st.session_state.diagnosis_done = False
    if "last_result" not in st.session_state:
        st.session_state.last_result = None
    if "streaming_text" not in st.session_state:
        st.session_state.streaming_text = ""
    if "current_node" not in st.session_state:
        st.session_state.current_node = ""
    if st.sidebar.button("🔄 重置诊断会话"):
        for key in ["conversation", "accumulated_input", "diagnosis_done", "last_result",
                    "streaming_text", "current_node"]:
            st.session_state.pop(key, None)
        st.rerun()

    # 显示当前会话 ID
    if "session_id" not in st.session_state:
        st.session_state.session_id = str(uuid.uuid4())[:8]
    st.sidebar.caption(f"会话 ID: {st.session_state.session_id}")

    with st.form("fault_form"):
        fault_input = st.text_area(
            "请输入设备故障信息",
            placeholder="例如：设备类型：数控机床主轴电机\n报警代码：E-203\n现象：主轴转速不稳定，异响，温升异常",
            height=150
        )
        uploaded_image = st.file_uploader("上传设备图片（报警界面/故障部位，可选）", type=["jpg", "jpeg", "png"])
        if uploaded_image:
            st.image(uploaded_image, caption="预览", width=300)
        submitted = st.form_submit_button("发送", type="primary")

    if submitted:
        if not fault_input.strip():
            st.warning("请输入故障描述")
        else:
            image_base64 = ""
            if uploaded_image is not None:
                image_bytes = uploaded_image.getvalue()
                image_base64 = base64.b64encode(image_bytes).decode("utf-8")

            # 上一轮已经拿到完整结论 → 这一次是"新的一个故障"，先清掉累积上下文。
            # 否则新故障会被上一次的描述污染：抽取器会同时看到旧设备类型与新现象，
            # 抽出错误的结构化信息，进而检索到不相关的资料。
            if st.session_state.diagnosis_done:
                st.session_state.accumulated_input = []
                st.session_state.conversation = []

            # 累积对话输入（保留上下文，供多轮追问）
            st.session_state.accumulated_input.append(fault_input.strip())
            full_input = "\n".join(st.session_state.accumulated_input)
            st.session_state.conversation.append(("user", fault_input.strip()))
            st.session_state.diagnosis_done = False
            st.session_state.streaming_text = ""
            st.session_state.current_node = ""

            # 进度展示区域
            progress_placeholder = st.empty()
            token_placeholder = st.empty()
            node_placeholder = st.empty()

            progress_box = progress_placeholder.status("🚀 启动多Agent协作诊断...", expanded=True)
            result = None

            try:
                from orchestrator import run_diagnosis_stream
                for step_label, state_snapshot in run_diagnosis_stream(full_input, image_base64=image_base64):
                    progress_box.update(label=step_label, state="running")
                    result = state_snapshot

                    # 更新当前节点显示
                    st.session_state.current_node = step_label
                    node_placeholder.info(f"📍 当前节点: {step_label}")

                    # Token 级流式显示（模拟：显示当前节点的关键输出）
                    if result.get("diagnosis") and "根因判断" in result["diagnosis"]:
                        root = result["diagnosis"]["根因判断"]
                        if root != st.session_state.streaming_text:
                            st.session_state.streaming_text = root
                            token_placeholder.markdown(f"**🔍 实时诊断输出:**\n```\n{root}\n```")

            except Exception as e:
                progress_box.update(label=f"❌ 诊断失败: {str(e)}", state="complete")
                st.error(f"诊断出错: {e}")
                result = None
            else:
                if result:
                    progress_box.update(label="✅ 诊断流程执行完毕", state="complete")

            # 追问处理
            if result and result.get("status") == "need_more_info" and result.get("followup_question"):
                st.session_state.conversation.append(("assistant", result["followup_question"]))
                st.rerun()
            elif result:
                st.session_state.diagnosis_done = True
                st.session_state.last_result = result

                # 落库：前端产生的诊断同样要进历史与统计，否则"诊断历史"页永远只有 API 记录
                if result.get("status") != "need_more_info":
                    try:
                        save_diagnosis_record(
                            full_input,
                            result,
                            token_usage=result.get("token_usage", {})
                        )
                    except Exception as e:
                        st.warning(f"诊断完成，但历史记录保存失败：{e}")

                # 清理流式显示
                token_placeholder.empty()
                node_placeholder.empty()

    # 说明：这里刻意不提供"取消诊断"按钮。
    # Streamlit 的单次脚本执行是阻塞的——诊断进行中前端事件不会被处理，按钮点不动。
    # 此前那个按钮只在"没有诊断在跑"时显示（条件正好写反），等于完全无效，
    # 还会让用户以为诊断可以中断。想重新开始只能点"重置诊断会话"。

    # 显示对话历史
    st.markdown("## 💬 对话记录")
    for role, content in st.session_state.conversation:
        if role == "user":
            st.markdown(f"**🧑 用户：**\n\n{content}")
        else:
            st.markdown(f"**🤖 系统追问：**\n\n{content}")
        st.markdown("---")

    # 显示诊断结果
    if st.session_state.diagnosis_done and st.session_state.last_result:
        result = st.session_state.last_result
        st.markdown("## 诊断结果")

        status = result.get("status", "")
        # 状态文案与级别都取自 status_meta（唯一来源）。
        # 此前这里是 if/elif 硬编码四条：加一个状态就得记得回来补一条，
        # 漏了就"什么都不显示"——不报错，但用户不知道发生了什么。
        _banner = status_meta(status)
        if _banner.banner:
            {
                "error": st.error,
                "warning": st.warning,
                "success": st.success,
                "info": st.info,
            }.get(_banner.level, st.info)(_banner.banner)

        # Token 使用统计
        if result.get("token_usage"):
            with st.expander("📊 Token 使用统计", expanded=False):
                tu = result["token_usage"]
                col1, col2, col3 = st.columns(3)
                col1.metric("总 Tokens", tu.get("total_tokens", 0))
                col2.metric("Prompt Tokens", tu.get("prompt_tokens", 0))
                col3.metric("Completion Tokens", tu.get("completion_tokens", 0))
                if tu.get("by_node"):
                    st.markdown("**按节点统计:**")
                    st.json(tu["by_node"])

        col1, col2 = st.columns(2)

        with col1:
            st.markdown("### 📋 初始诊断")
            if result.get("diagnosis"):
                st.json(result["diagnosis"])
            else:
                st.info("无诊断结果")

            st.markdown("### 🔍 初始审核")
            if result.get("review"):
                st.json(result["review"])
            else:
                st.info("无审核结果")

        with col2:
            st.markdown("### ⚔️ 辩论过程")
            if result.get("rebuttal"):
                st.json(result["rebuttal"])
            else:
                st.info("未触发辩论机制")

            st.markdown("### 🛡️ 最终复审")
            if result.get("final_review"):
                st.json(result["final_review"])
            else:
                st.info("无复审结果")

        # 成本展示
        st.markdown("---")
        st.markdown("### 💰 成本估算")
        if result.get("cost"):
            cost = result["cost"]
            if cost.get("计费提示"):
                st.warning(f"⚠️ {cost['计费提示']}")
            st.json(cost)
        else:
            st.info("无成本数据")

        st.markdown("---")
        st.markdown("### 📄 最终维修工单")
        if result.get("workorder"):
            wo = result["workorder"]
            # 高亮风险等级
            risk = wo.get("风险等级", "")
            if risk:
                if "高风险" in risk:
                    st.error(f"🚨 {risk}: {wo.get('风险说明', '')}")
                elif "待人工" in risk:
                    st.warning(f"⚠️ {risk}: {wo.get('风险说明', '')}")
                else:
                    st.success(f"✅ {risk}")
            st.json(wo)
        else:
            st.info("未生成工单")

        # 导出当前诊断结果
        # 注意：download_button 必须直接渲染。此前把它嵌在 if st.button(...) 里，
        # 点击后页面重跑、按钮状态复位，下载按钮根本来不及出现。
        st.markdown("---")
        st.download_button(
            "📥 导出本次诊断 JSON",
            data=json.dumps(result, ensure_ascii=False, indent=2),
            file_name=f"diagnosis_{result.get('correlation_id', 'unknown')}.json",
            mime="application/json"
        )


def records_to_csv(records) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["记录ID", "创建时间", "故障描述", "状态", "工单号", "最终根因", "预计成本", "辩论轮数", "总Tokens"])
    for r in records:
        workorder = r.get("workorder") or {}
        diagnosis = r.get("diagnosis") or {}
        cost = r.get("cost") or {}
        token_usage = r.get("token_usage") or {}
        writer.writerow([
            r.get("id"),
            r.get("created_at", "0000-00-00T00:00:00")[:19],
            r.get("fault_description", "").replace("\n", " "),
            r.get("status", ""),
            workorder.get("工单编号", ""),
            diagnosis.get("根因判断", ""),
            cost.get("预计成本", ""),
            r.get("debate_round", 0),
            token_usage.get("total_tokens", 0) if isinstance(token_usage, dict) else 0
        ])
    return buf.getvalue()


PAGE_SIZE_OPTIONS = [20, 50, 100, 200]

# 历史页的翻页状态。存 session_state 而不是 URL，因为 Streamlit 的按钮回调
# 会整脚本重跑，局部变量留不住。
_HISTORY_PAGE_KEY = "history_page"
_HISTORY_SIG_KEY = "history_query_sig"


def _page_window(total: int, page: int, page_size: int) -> tuple:
    """把 (总数, 页码, 每页条数) 归一化成 (页码, 偏移, 总页数)。

    页码越界时必须夹回有效范围：换了筛选条件后结果变少（原来第 5 页，现在总共
    只剩 1 页），不夹的话用户会停在一个空列表上，看起来像"一条记录都没有"，
    而真实原因是"页码超了"。
    """
    page_size = max(1, int(page_size))
    page_count = max(1, -(-int(total) // page_size))  # 向上取整
    page = min(max(1, int(page)), page_count)
    return page, (page - 1) * page_size, page_count


def _history_summary(total: int, shown: int) -> str:
    """历史列表的计数说明。

    总数必须来自 count_records()，不能是 len(records)——后者被 LIMIT 截断，
    库里有 500 条时也会显示"共找到 20 条记录"，用户根本不知道还有更多。
    """
    if total > shown:
        return f"共找到 **{total}** 条记录，当前显示 **{shown}** 条"
    return f"共找到 **{total}** 条记录"


def render_history():
    """诊断历史页：搜索、筛选、翻页、展开详情、导出 CSV / 工单 Markdown"""
    st.markdown("## 📋 诊断历史记录")

    col1, col2, col3 = st.columns([3, 2, 1])
    keyword = col1.text_input("🔍 搜索故障描述关键词", placeholder="如：液压、E-203、主轴")
    statuses = get_distinct_statuses()
    status_options = ["全部"] + statuses
    status_filter = col2.selectbox("状态筛选", status_options)
    page_size = col3.selectbox("每页条数", PAGE_SIZE_OPTIONS)

    status_arg = "" if status_filter == "全部" else status_filter
    keyword_arg = keyword.strip()

    total = count_records(keyword=keyword_arg, status=status_arg)

    # 查询条件一变就回到第 1 页，否则换了关键词还停在第 3 页，大概率是空的
    sig = (keyword_arg, status_arg, page_size)
    if st.session_state.get(_HISTORY_SIG_KEY) != sig:
        st.session_state[_HISTORY_SIG_KEY] = sig
        st.session_state[_HISTORY_PAGE_KEY] = 1

    page, offset, page_count = _page_window(
        total, st.session_state.get(_HISTORY_PAGE_KEY, 1), page_size
    )
    st.session_state[_HISTORY_PAGE_KEY] = page

    records = get_records(keyword=keyword_arg, status=status_arg, limit=page_size, offset=offset)

    st.markdown(_history_summary(total, len(records)))

    # 翻页控件：只有超过一页时才显示，避免一页装得下的时候多两个没用的按钮
    if page_count > 1:
        nav_prev, nav_info, nav_next = st.columns([1, 2, 1])
        if nav_prev.button("⬅️ 上一页", disabled=(page <= 1)):
            st.session_state[_HISTORY_PAGE_KEY] = page - 1
            st.rerun()
        nav_info.markdown(
            f"<div style='text-align:center;padding-top:0.4rem'>第 <b>{page}</b> / {page_count} 页</div>",
            unsafe_allow_html=True,
        )
        if nav_next.button("下一页 ➡️", disabled=(page >= page_count)):
            st.session_state[_HISTORY_PAGE_KEY] = page + 1
            st.rerun()

    if records:
        st.download_button(
            "📥 导出当前页为 CSV",
            data=records_to_csv(records),
            file_name=f"agentdiag_records_{time.strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv"
        )

        for r in records:
            workorder = r.get("workorder") or {}
            diagnosis = r.get("diagnosis") or {}
            wo_id = workorder.get("工单编号", "-")
            root_cause = (diagnosis.get("根因判断") or workorder.get("根因") or "无")[:40]
            created = (r.get("created_at") or "")[:19]

            # 状态颜色（取自 status_meta 的唯一来源，不再各自维护一份字典）
            status = r.get("status", "")
            status_color = status_meta(status).icon

            title = f"{status_color} [ID {r.get('id')}] {created} ｜ {wo_id} ｜ {status}"
            with st.expander(title):
                st.markdown(f"**故障描述：**\n{r.get('fault_description')}")
                st.markdown(f"**最终根因：** {root_cause}")

                c1, c2 = st.columns(2)
                with c1:
                    st.markdown("**诊断**")
                    st.json(diagnosis)
                    st.markdown("**审核**")
                    st.json(r.get("review") or {})
                with c2:
                    st.markdown("**辩论**")
                    st.json(r.get("rebuttal") or {})
                    st.markdown("**最终复审**")
                    st.json(r.get("final_review") or {})

                c3, c4 = st.columns(2)
                with c3:
                    st.markdown("**成本**")
                    st.json(r.get("cost") or {})
                with c4:
                    st.markdown("**工单**")
                    st.json(workorder)

                # 单条记录导出：JSON 给程序用，Markdown 工单给人用
                d1, d2 = st.columns(2)
                with d1:
                    st.download_button(
                        "📥 导出此记录 JSON",
                        data=json.dumps(r, ensure_ascii=False, indent=2),
                        file_name=f"record_{r.get('id')}.json",
                        mime="application/json",
                        key=f"download_{r.get('id')}"
                    )
                with d2:
                    if workorder:
                        st.download_button(
                            "🖨️ 导出工单 Markdown",
                            data=workorder_to_markdown(
                                workorder,
                                cost=r.get("cost") or {},
                                correlation_id=r.get("correlation_id") or "",
                            ),
                            file_name=workorder_filename(workorder),
                            mime="text/markdown",
                            key=f"download_wo_{r.get('id')}"
                        )
    elif total > 0:
        # 有记录却这一页是空的：只可能是页码越界，说清楚而不是笼统说"没有记录"
        st.warning("当前页没有记录，请回到第 1 页查看。")
    else:
        st.info("没有符合条件的记录。先进行一次诊断，记录会自动保存。")


def render_stats():
    """系统统计页"""
    st.markdown("## 📊 系统统计")

    stats_data = get_stats()

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("总记录数", stats_data["total_records"])
    col2.metric("平均辩论轮数", stats_data["avg_debate_rounds"])
    col3.metric("总 Token 消耗", f"{stats_data['total_tokens']:,}")
    db_file = Path(settings.DB_PATH)
    col4.metric("数据库大小", f"{db_file.stat().st_size / 1024:.1f} KB" if db_file.exists() else "N/A")

    st.markdown("---")
    st.markdown("### 按状态分布")
    if stats_data["by_status"]:
        import pandas as pd
        df = pd.DataFrame(list(stats_data["by_status"].items()), columns=["状态", "数量"])
        st.bar_chart(df.set_index("状态"))
    else:
        st.info("暂无数据")


if page_key == "diagnosis":
    render_diagnosis()
elif page_key == "history":
    render_history()
elif page_key == "stats":
    render_stats()