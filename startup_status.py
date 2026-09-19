"""启动进度状态（`startup_status.py`）。

## 为什么需要这个模块

`/health/live` 只要进程活着就返回 200，所以它在**启动过程中也会立刻返回 200**——
那时向量库、BM25 索引都还没加载好，第一次诊断必然失败。前端 5 秒轮询一次，
一旦看到 200 就显示"服务正常"，用户照此点"开始诊断"，大概率撞上一个
「模型服务调用失败」或超时。

这正是本项目最忌讳的一类缺陷：**探针在真实可用之前就宣告可用**。
`/health/live` 的语义被刻意定义为"进程存活"（K8s liveness 不能因为依赖没就绪
就重启容器），所以不能直接改它的返回值去表达"就绪"——那会破坏它的既有契约。
正确的做法是**新增一个可表达就绪的端点**，由前端的状态灯去用。

## 设计取舍

- **状态用锁保护**：lifespan 在启动线程写，请求线程读。CPython 下单次赋值是原子的，
  但这里要一起读 "phase + ready" 两个字段，不加锁会读到自相矛盾的组合
  （phase 已是 ready、ready 还是 False）。数据量极小，锁开销可忽略。
- **不引入 threading.Event**：需要的是"当前阶段 + 能否服务"两个信息，
  Event 只能表达后者，还要额外维护一个字段，反而更绕。
- **失败要能被看见**：启动步骤抛异常时记为 failed 并带上原因，
  端点和前端横幅都能据此说明"为什么起不来"，而不是永远卡在"启动中…"。
"""

import threading
import time
from typing import Any, Dict

# 进程启动时刻。用于计算 elapsed_ms，以及和 app 的创建时间对照。
_PROCESS_START = time.monotonic()

_lock = threading.Lock()

_state: Dict[str, Any] = {
    # 当前步骤的中文描述，直接可显示给用户
    "step": "进程已启动，等待初始化",
    # 步骤序号 / 总步数，用于"3/5"这种进度呈现
    "step_index": 0,
    "step_total": 0,
    # 是否已经可以对外提供完整服务（三件事都做完了）
    "ready": False,
    # 启动失败时的原因；None 表示尚未失败
    "error": None,
    # 启动耗时（毫秒），就绪后填充
    "elapsed_ms": None,
}


def set_step(step: str, index: int, total: int) -> None:
    """推进到某个启动步骤（初始化开始时调用）。"""
    with _lock:
        _state["step"] = step
        _state["step_index"] = index
        _state["step_total"] = total
        _state["error"] = None


def mark_ready() -> None:
    """标记启动完成。就绪后不会再回退——运行期的依赖抖动由 /health 负责表达。"""
    with _lock:
        _state["step"] = "启动完成"
        _state["ready"] = True
        _state["error"] = None
        _state["elapsed_ms"] = int((time.monotonic() - _PROCESS_START) * 1000)


def mark_failed(reason: str) -> None:
    """标记启动失败。`ready` 保持 False——半初始化的服务不能对外宣告可用。"""
    with _lock:
        _state["step"] = "启动失败"
        _state["ready"] = False
        _state["error"] = reason


def snapshot() -> Dict[str, Any]:
    """读取当前状态快照（拷贝，调用方拿到的是不可变视图）。"""
    with _lock:
        return dict(_state)


def reset_for_tests() -> None:
    """仅供测试使用：把状态复位到"刚启动"的样子。

    必须有这个口子：模块级状态在同一个 pytest 进程里只初始化一次，
    测试之间会互相污染（前一个测试 mark_ready 了，后一个测试就再也测不到
    "未就绪"分支）。这不是"为了测试改生产代码"——状态复位本身就是
    一个合理的公开操作，只是生产路径不需要调它。
    """
    with _lock:
        _state.update(
            {
                "step": "进程已启动，等待初始化",
                "step_index": 0,
                "step_total": 0,
                "ready": False,
                "error": None,
                "elapsed_ms": None,
            }
        )
