"""JS渲染采集业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "jsrender"
REQUIRED_FIELDS = ["配置编号", "目标URL", "浏览器类型"]
STATUS_ORDER = ["草稿", "渲染测试中", "已上线", "已降级"]
ACTION_RULES = {"提交渲染测试": "渲染测试中", "标记上线": "已上线", "标记降级": "已降级"}
NEGATIVE_ACTIONS = ["标记降级"]
OPTIONAL_FIELDS = ["渲染等待秒", "滚动次数", "点击选择器", "页面等待条件", "截屏开关"]
# 严格单向：目标状态必须正好是当前状态的下一格，不允许跳跃、回退或原地切换。
NEXT_STATUS = {
    "草稿": {"渲染测试中"},
    "渲染测试中": {"已上线", "已降级"},
    "已上线": {"已降级"},
    "已降级": set(),
}


class JsrenderService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("配置编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in OPTIONAL_FIELDS:
            if values.get(field) is not None:
                entry[field] = values.get(field)
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["渲染结果"] = {}
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"渲染配置 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于JS渲染采集可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        current = str(entry.get("status") or "")
        if target not in NEXT_STATUS.get(current, set()):
            return None, (
                f"渲染配置当前为「{current}」，只能沿生命周期前进到"
                f"「{'/'.join(sorted(NEXT_STATUS.get(current, set()))) or '无'}」，不能变更为「{target}」"
            )
        entry["status"] = target
        entry["pending"] = target == "渲染测试中"
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"渲染配置已{action}"
