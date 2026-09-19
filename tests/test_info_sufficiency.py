"""信息核验前置护栏：模糊输入拦截 + 引导式追问。

踩过的坑：原 is_info_sufficient 只查「设备类型 + 故障现象」是否非空
就放行；「那台数控床一震一颤的」这种口语化、缺关键细节的输入会走完
9 步流程再降级。新人看到「9 步 → 知识库无依据」会觉得系统不行。

根因是判定时机太晚——信息核验节点本来就接在图里（need_more_info → END），
只是从来没被触发。本文件以对偶测试守住这条改动：
  - 模糊输入必须被拦下（应拦下）
  - 信息充分必须被放行（应放行）
  - 追问文案必须按不充分原因分型
"""

import pytest

from agents import (
    is_info_sufficient,
    generate_followup_question,
    _phenomenon_too_vague,
)


# ==================== 单条规则：_phenomenon_too_vague ====================

class TestPhenomenonTooVague:
    """单条现象是否过粗的判定规则。"""

    @pytest.mark.parametrize("phrase", [
        "一震一颤",      # 用户截图原话
        "一震一颤的",    # 同上（含"的"）
        "震",
        "震动",
        "抖动",
    ])
    def test_vague_single_phrase(self, phrase):
        """单个粗粒度形容词必须判为过粗。"""
        assert _phenomenon_too_vague([phrase]) is True

    def test_empty_list_is_vague(self):
        assert _phenomenon_too_vague([]) is True

    def test_none_is_vague(self):
        assert _phenomenon_too_vague(None) is True

    @pytest.mark.parametrize("phrase", [
        "主轴加工时机身震",      # 工况 + 部件
        "主轴在转时发出咣咣声",   # 部件 + 工况 + 声音
        "异响",                   # 异响是细化信号
        "加工时机身有异响",      # 加工 + 机身 + 异响
        "主轴轴承异响",          # 部件 + 部件 + 异响
        "E-203 报警",            # 报警代码
        "液压泵漏油",            # 部件 + 漏油
        "启动时主轴箱异响",      # 启动 + 主轴箱 + 异响
        "主轴温升",              # 部件 + 温度
        "严重震动",              # 程度 + 现象
    ])
    def test_specific_phrase_not_vague(self, phrase):
        """含细化信号（部件/工况/声音/报警/温度）的单条现象必须放行。"""
        assert _phenomenon_too_vague([phrase]) is False, f"应判定为非粗: {phrase!r}"

    def test_two_or_more_phrases_not_vague(self):
        """多条并存默认视为够丰富——用户分点说明常常已含不同维度信号。
        硬把『震 + 响』也判粗会逼正常用户重写，对老用户徒增打扰。"""
        assert _phenomenon_too_vague(["震", "响"]) is False
        assert _phenomenon_too_vague(["震", "响", "热"]) is False


# ==================== is_info_sufficient 对偶测试 ====================

class TestIsInfoSufficient:
    """三层对偶：必须拦下 / 必须放行 / 报警代码优先。"""

    # ----- 必须拦下 -----

    def test_user_screenshot_case_returns_false(self):
        """用户截图的原 case：'那台数控床一震一颤的' → 必须拦下。
        这是这次改动的根因 case，没拦住就视为回归。"""
        fault_info = {
            "设备类型": "数控机床",
            "故障现象": ["一震一颤"],
            "排除条件": [],
        }
        assert is_info_sufficient(fault_info) is False

    def test_no_device_returns_false(self):
        fault_info = {
            "设备类型": "",
            "故障现象": ["主轴震", "异响"],
            "排除条件": [],
        }
        assert is_info_sufficient(fault_info) is False

    def test_device_only_returns_false(self):
        """有设备但完全没现象——这种情况很罕见，主要是 extract_fault_info
        抽不出字段时；走追问而不是直接走流程。"""
        fault_info = {
            "设备类型": "数控机床",
            "故障现象": [],
            "排除条件": [],
        }
        assert is_info_sufficient(fault_info) is False

    def test_device_plus_vague_returns_false(self):
        """设备 + 单条粗粒度现象 = 不充分。"""
        fault_info = {
            "设备类型": "空压机",
            "故障现象": ["响"],
            "排除条件": [],
        }
        assert is_info_sufficient(fault_info) is False

    # ----- 必须放行 -----

    def test_device_plus_specific_phenomenon(self):
        fault_info = {
            "设备类型": "数控机床",
            "故障现象": ["主轴加工时机身震"],
            "排除条件": [],
        }
        assert is_info_sufficient(fault_info) is True

    def test_device_plus_multiple_phenomena_sufficient(self):
        """多条并存（即便每条单独看粗）——分点说明本身就是细化信号。"""
        fault_info = {
            "设备类型": "空压机",
            "故障现象": ["震", "响"],
            "排除条件": [],
        }
        assert is_info_sufficient(fault_info) is True

    # ----- 报警代码优先 -----

    def test_alarm_code_alone_sufficient(self):
        """报警代码本身就是强诊断信号——即使没设备、没现象也能进。
        否则前端会卡在追问页，用户明明给了 E-203 还被反复问型号。"""
        fault_info = {
            "设备类型": "",
            "故障现象": [],
            "报警代码": "E-203",
            "排除条件": [],
        }
        assert is_info_sufficient(fault_info) is True

    def test_alarm_with_vague_phenomenon_sufficient(self):
        """有报警代码时，现象粗不算粗——不另追细节，避免反复问。"""
        fault_info = {
            "设备类型": "数控机床",
            "故障现象": ["一震一颤"],
            "报警代码": "E-203",
            "排除条件": [],
        }
        assert is_info_sufficient(fault_info) is True


# ==================== 追问文案对偶测试 ====================

class TestFollowupQuestion:

    def test_empty_when_sufficient(self):
        """充分时不要让前端弹追问。"""
        fault_info = {
            "设备类型": "数控机床",
            "故障现象": ["主轴加工时机身震"],
            "排除条件": [],
        }
        assert generate_followup_question(fault_info) == ""

    def test_user_screenshot_case_guides_to_details(self):
        """用户截图的 case：必须给出引导式追问，覆盖至少部件/时机/
        异响/温度/报警代码/保养这些细化维度；否则又会被"请补充故障
        现象"这种空话噎住。"""
        fault_info = {
            "设备类型": "数控机床",
            "故障现象": ["一震一颤"],
            "排除条件": [],
        }
        q = generate_followup_question(fault_info)

        # 必须覆盖这几个引导维度（任一项缺失都会让用户不知如何补充）
        for signal in ["部件", "什么时候", "异响", "温度", "报警代码", "保养"]:
            assert signal in q, f"追问文案缺引导信号: {signal}\n\n{q}"

        # 不能只是干瘪的"请补充故障现象"——这就是被替换掉的老文案
        assert "请补充故障现象" not in q

    def test_no_device_prompts_for_model(self):
        """没设备类型时必须主动问用户要型号，否则诊断无从下手。"""
        fault_info = {
            "设备类型": "",
            "故障现象": [],
            "排除条件": [],
        }
        q = generate_followup_question(fault_info)
        assert "设备" in q
        assert any(kw in q for kw in ["型号", "名称", "主轴", "空压机", "液压泵"])

    def test_no_symptoms_prompts_for_description(self):
        """有设备没现象时，引导用户描述现象。"""
        fault_info = {
            "设备类型": "数控机床",
            "故障现象": [],
            "排除条件": [],
        }
        q = generate_followup_question(fault_info)
        assert any(kw in q for kw in ["现象", "什么时候", "声音", "震动"])
