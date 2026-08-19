from dataclasses import asdict, dataclass
import re


@dataclass(frozen=True)
class EngineDecision:
    engine: str
    confidence: float
    reasons: tuple[str, ...]

    def to_dict(self):
        return asdict(self)


_BATCH = re.compile(r"(?:批量|每[一份个]|\d+\s*份|全部文件|固定字段|逐[个份])")
_PARALLEL = re.compile(r"(?:分别|同时|并行|多(?:个|家|来源)|对比|排查|财报|多个数据源)")
_LONG_FLOW = re.compile(r"(?:生成|编制|撰写|研报|标书|演示文稿|PPT|方案|搜集|调研|分析并|然后|最后)", re.IGNORECASE)
_DYNAMIC = re.compile(r"(?:探索|排查|诊断|视情况|根据结果|失败后|动态)")
_ARTIFACT_FOLLOWUP = re.compile(r"(?:还是|仍然|继续|再|重新|改|调整|修改|缩小|放大|太大|太小|大一点|小一点|换成)", re.IGNORECASE)


def select_engine(
    query: str,
    *,
    has_attachment: bool = False,
    estimated_tool_calls: int | None = None,
    has_artifact_context: bool = False,
) -> EngineDecision:
    """Apply stable, explainable routing rules before any optional model router."""
    text = query.strip()
    calls = estimated_tool_calls
    if calls is None:
        calls = 4 if _PARALLEL.search(text) else 3 if _LONG_FLOW.search(text) else 1

    if has_artifact_context and _ARTIFACT_FOLLOWUP.search(text):
        return EngineDecision("plan_execute", 0.94, ("正在延续修改历史产物", "需要实际工具执行并校验新产物"))
    if _BATCH.search(text) and not _DYNAMIC.search(text):
        return EngineDecision("static_plan", 0.88, ("批量或固定模式任务", "执行步骤可提前确定"))
    if calls >= 3 and _PARALLEL.search(text):
        return EngineDecision("dag", 0.86, ("包含多个相互独立的子任务", "可通过有限并发降低延迟"))
    if _LONG_FLOW.search(text) or has_attachment and len(text) > 30:
        return EngineDecision("plan_execute", 0.84, ("存在多步骤交付目标", "需要计划状态与失败后重规划"))
    return EngineDecision("react", 0.92, ("任务较短或目标开放", "预计工具调用不超过两次"))
