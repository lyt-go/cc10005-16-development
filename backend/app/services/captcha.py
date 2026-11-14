"""验证码处理业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "captcha"
REQUIRED_FIELDS = ["策略编号", "验证码类型", "识别服务商"]
STATUS_ORDER = ["草稿", "策略验证中", "已启用", "已降级"]
ACTION_RULES = {"提交策略验证": "策略验证中", "启用策略": "已启用", "降级策略": "已降级"}
NEGATIVE_ACTIONS = ["降级策略"]
OPTIONAL_FIELDS = ["识别超时秒", "重试次数上限", "降级策略", "手动打码开关", "打码等待队列", "打码等待超时秒"]
# 严格单向：不允许回退，也不允许跨状态跳转。
NEXT_STATUS = {
    "草稿": {"策略验证中"},
    "策略验证中": {"已启用", "已降级"},
    "已启用": {"已降级"},
    "已降级": set(),
}


class CaptchaService:
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
            rows = [row for row in rows if keyword in str(row.get("策略编号", ""))]
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
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"验证码策略 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于验证码处理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        current = str(entry.get("status") or "")
        if target not in NEXT_STATUS.get(current, set()):
            return None, (
                f"验证码策略当前为「{current}」，只能沿生命周期前进到"
                f"「{'/'.join(sorted(NEXT_STATUS.get(current, set()))) or '无'}」，不能变更为「{target}」"
            )
        entry["status"] = target
        entry["pending"] = target == "策略验证中"
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"验证码策略已{action}"
