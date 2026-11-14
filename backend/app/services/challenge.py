"""验证码挑战分流业务规则。

渲染测试遇到需要识别验证码的页面时，由这里统一决定后续走向：

1. 继续自动识别（占用识别服务商名额）；
2. 进入人工打码等待队列（立即释放服务商名额）；
3. 降级渲染（把降级策略回写到渲染结果）。

状态机是单向的，见 ``STATUS_ORDER`` 与 ``TRANSITIONS``：任何回退、跨状态切换都会被
拒绝。归档与恢复同样是一条向前的迁移（已归档 → 已恢复），并写入挑战事件与操作审计，
保证变更可核对。队列等待不占用线程、不 sleep，因此不会阻塞其他站点采集。
"""
from __future__ import annotations

import threading
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

from app.store import store

CHALLENGE_MODULE = "challenge"
EVENT_MODULE = "challenge_event"
JSRE_MODULE = "jsrender"
CAPTCHA_MODULE = "captcha"
AUDIT_MODULE = "audittrail"

# 单向生命周期：索引只能递增，且只允许 TRANSITIONS 里列出的相邻迁移。
STATUS_ORDER = ["待分流", "自动识别中", "待人工打码", "人工认领中", "已完成", "已降级", "已归档", "已恢复"]
STATUS_INDEX = {status: index for index, status in enumerate(STATUS_ORDER)}
TRANSITIONS: dict[str, set[str]] = {
    "待分流": {"自动识别中", "待人工打码", "已降级"},
    "自动识别中": {"待人工打码", "已完成", "已降级"},
    "待人工打码": {"人工认领中", "已降级"},
    "人工认领中": {"已完成", "已降级"},
    "已完成": {"已归档"},
    "已降级": {"已归档"},
    "已归档": {"已恢复"},
    "已恢复": set(),
}
ACTIVE_STATUSES = {"待分流", "自动识别中", "待人工打码", "人工认领中"}
FINAL_STATUSES = {"已完成", "已降级"}

# 必须人工处理的验证码类型：机器识别成功率不可控，直接进打码队列。
MANUAL_ONLY_TYPES = {"点选验证码", "短信验证码", "行为验证码"}
# 允许自动识别的类型；不在白名单里的类型一律按“必须人工”处理。
AUTO_CAPABLE_TYPES = {"图像验证码", "滑块验证码", "算术验证码"}
# 识别服务商并发名额；未登记的服务商按 1 个名额计算，避免无限占用。
PROVIDER_QUOTAS = {"云打码": 2, "极验识别": 1, "自建OCR": 1}
DEFAULT_PROVIDER_QUOTA = 1
UNCONFIGURED_PROVIDERS = {"", "未配置", "不适用", "none", "None"}

RENDER_STATE = {
    "待分流": "待分流",
    "自动识别中": "自动识别中",
    "待人工打码": "等待人工打码",
    "人工认领中": "人工打码中",
    "已完成": "渲染成功",
    "已降级": "渲染降级",
    "已归档": "已归档",
    "已恢复": "已恢复",
}
DECISION_AUTO = "自动识别"
DECISION_MANUAL = "人工打码"
DECISION_DEGRADE = "降级渲染"

JSRE_ORDER = ["草稿", "渲染测试中", "已上线", "已降级"]

# 所有状态写入、名额计算、去重与认领都在这把锁内完成：同一挑战的重复提交、
# 人工处理与自动识别结果并发写入时，只有一个请求能改到状态。
_LOCK = threading.RLock()


class ChallengeService:
    # ------------------------------------------------------------------ 查询
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        rendering_id: int | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(CHALLENGE_MODULE)
        if keyword:
            rows = [
                row for row in rows
                if keyword in str(row.get("挑战编号", "")) or keyword in str(row.get("渲染配置编号", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if rendering_id is not None:
            rows = [row for row in rows if int(row.get("渲染配置ID", 0)) == rendering_id]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [deepcopy(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(CHALLENGE_MODULE, entry_id)
        return deepcopy(entry) if entry else None

    def find_by_number(self, number: str) -> dict[str, Any] | None:
        for row in store.rows(CHALLENGE_MODULE):
            if row.get("挑战编号") == number:
                return deepcopy(row)
        return None

    def list_events(self, entry_id: int) -> list[dict[str, Any]]:
        entry = store.find(CHALLENGE_MODULE, entry_id)
        if entry is None:
            return []
        events = [
            deepcopy(row) for row in store.rows(EVENT_MODULE)
            if row.get("挑战编号") == entry.get("挑战编号")
        ]
        return sorted(events, key=lambda row: int(row.get("事件序号", 0)))

    def queue_snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        """人工打码队列快照：只读，不阻塞，也不改变任何挑战状态。"""
        moment = now or _utcnow()
        waiting: list[dict[str, Any]] = []
        for row in store.rows(CHALLENGE_MODULE):
            if row.get("status") != "待人工打码":
                continue
            deadline = _parse_time(row.get("队列到期时间"))
            waiting.append({
                "挑战编号": row.get("挑战编号"),
                "渲染配置编号": row.get("渲染配置编号"),
                "验证码类型": row.get("验证码类型"),
                "识别服务商": row.get("识别服务商"),
                "打码等待队列": row.get("打码等待队列"),
                "页面等待条件": row.get("页面等待条件"),
                "点击选择器": row.get("点击选择器"),
                "队列进入时间": row.get("队列进入时间"),
                "队列到期时间": row.get("队列到期时间"),
                "剩余秒": max(int((deadline - moment).total_seconds()), 0) if deadline else None,
                "已超时": bool(deadline and deadline <= moment),
                "认领人": row.get("认领人") or "",
            })
        waiting.sort(key=lambda item: str(item.get("队列进入时间") or ""))
        return {
            "items": waiting,
            "total": len(waiting),
            "服务商名额占用": self._provider_usage(),
        }

    def summary(self) -> dict[str, Any]:
        rows = store.rows(CHALLENGE_MODULE)
        counts = {status: 0 for status in STATUS_ORDER}
        for row in rows:
            status = str(row.get("status") or "")
            if status in counts:
                counts[status] += 1
        return {
            "总数": len(rows),
            "状态分布": counts,
            "待人工打码": counts["待人工打码"],
            "已降级": counts["已降级"],
            "服务商名额": self._provider_usage(),
        }

    # -------------------------------------------------------------- 分流入口
    def submit_rendering(
        self,
        rendering_id: int,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str, bool]:
        """渲染测试遇到验证码页面时发起分流。

        返回 ``(挑战, 说明, 是否重复提交)``。重复提交返回已有挑战，不会重复认领、
        不会重复占用服务商名额。
        """
        payload = dict(values or {})
        operator = str(payload.get("操作人") or "采集调度器").strip()
        with _LOCK:
            rendering = store.find(JSRE_MODULE, rendering_id)
            if rendering is None:
                return None, f"渲染配置 {rendering_id} 不存在或已归档", False
            current_js_status = str(rendering.get("status") or "")
            if current_js_status not in ("草稿", "渲染测试中"):
                return None, (
                    f"渲染配置 {rendering.get('配置编号')} 当前为「{current_js_status}」，"
                    "只能沿生命周期前进，不能再发起渲染测试"
                ), False

            captcha, error = self._resolve_captcha(payload)
            if captcha is None:
                return None, error, False

            missing = [
                field for field in ("页面等待条件", "点击选择器")
                if not str(rendering.get(field) or "").strip()
            ]
            if missing:
                return None, (
                    f"渲染配置缺少{'、'.join(missing)}，挑战分流必须保留页面等待条件与点击选择器"
                ), False

            challenge_type = str(captcha.get("验证码类型") or "").strip()
            key = self._challenge_key(rendering, challenge_type)
            existing = self._active_by_key(key)
            if existing is not None:
                return deepcopy(existing), (
                    f"同一挑战已存在（{existing.get('挑战编号')}，状态「{existing.get('status')}」），"
                    "本次重复提交不再新建挑战，也不重复占用服务商名额"
                ), True

            observation = self._observation(payload, captcha)
            decision, reason, provider = self._decide(captcha, observation)
            entry = self._create_challenge(rendering, captcha, key)
            entry["分流结论"] = decision
            entry["已尝试次数"] = observation["已尝试次数"]
            entry["识别状态"] = observation["识别状态"]

            # 渲染配置进入渲染测试中（从草稿前进一格）。
            self._advance_jsrender(rendering, "渲染测试中")

            if decision == DECISION_AUTO:
                self._enter_auto(entry, provider, operator, reason)
                if observation["识别状态"] == "成功":
                    self._finish_auto(entry, operator, succeeded=True, elapsed=observation["识别用时秒"])
                    return deepcopy(entry), (
                        f"挑战 {entry['挑战编号']} 自动识别成功，渲染结果已回写"
                    ), False
                return deepcopy(entry), (
                    f"挑战 {entry['挑战编号']} 进入自动识别，已占用服务商「{provider}」名额 1 个"
                ), False

            if decision == DECISION_MANUAL:
                self._enter_manual_queue(entry, operator, reason)
                return deepcopy(entry), (
                    f"挑战 {entry['挑战编号']} 进入打码等待队列「{entry.get('打码等待队列')}」，"
                    f"已释放服务商名额；{reason}"
                ), False

            self._degrade(entry, operator, reason)
            return deepcopy(entry), (
                f"挑战 {entry['挑战编号']} 降级渲染：{reason}；降级策略已回写渲染结果"
            ), False

    # ------------------------------------------------------------ 自动识别结果
    def apply_auto_result(
        self,
        entry_id: int,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """写入一次自动识别结果；重试预算耗尽后转人工或降级。"""
        payload = dict(values or {})
        operator = str(payload.get("操作人") or "识别服务商回调").strip()
        result = str(payload.get("识别状态") or "失败").strip()
        elapsed = _as_int(payload.get("识别用时秒"), 0)
        with _LOCK:
            entry = store.find(CHALLENGE_MODULE, entry_id)
            if entry is None:
                return None, f"挑战 {entry_id} 不存在或已归档"

            status = str(entry.get("status") or "")
            if status == "人工认领中":
                return None, (
                    f"挑战 {entry.get('挑战编号')} 已由「{entry.get('认领人') or '打码员'}」认领，"
                    "人工处理优先，自动重试结果不能落库"
                )
            if status == "待人工打码":
                return None, (
                    f"挑战 {entry.get('挑战编号')} 已进入打码等待队列，"
                    "自动重试不得继续占用服务商名额"
                )
            if status in FINAL_STATUSES:
                return None, (
                    f"挑战 {entry.get('挑战编号')} 已有结果落库（当前「{status}」，"
                    f"来源「{entry.get('结果来源') or '未知'}」），拒绝重复写入自动识别结果"
                )
            if status != "自动识别中":
                return None, (
                    f"挑战 {entry.get('挑战编号')} 当前状态为「{status}」，"
                    "不能写入自动识别结果"
                )
            if not entry.get("服务商名额占用"):
                return None, f"挑战 {entry.get('挑战编号')} 未持有服务商名额，自动识别结果无效"

            entry["已尝试次数"] = int(entry.get("已尝试次数") or 0) + 1
            entry["识别用时秒"] = elapsed
            entry["识别状态"] = result
            timeout = int(entry.get("识别超时秒") or 0)
            limit = int(entry.get("重试次数上限") or 1)

            if result == "成功" and elapsed <= timeout:
                self._finish_auto(entry, operator, succeeded=True, elapsed=elapsed)
                return deepcopy(entry), (
                    f"挑战 {entry['挑战编号']} 自动识别成功，服务商名额已释放，渲染结果已回写"
                )

            timeout_reason = f"识别耗时 {elapsed}s 超过超时 {timeout}s" if elapsed > timeout else "识别失败"
            if int(entry["已尝试次数"]) >= limit:
                reason = f"自动识别{timeout_reason}，重试已达上限 {limit} 次"
                if _switch_on(entry.get("手动打码开关")) and entry.get("打码等待队列"):
                    self._release_provider(entry)
                    self._enter_manual_queue(entry, operator, reason)
                    return deepcopy(entry), (
                        f"挑战 {entry['挑战编号']} 重试耗尽，转入打码等待队列"
                        f"「{entry.get('打码等待队列')}」并释放服务商名额"
                    )
                self._degrade(entry, operator, reason + "；手动打码关闭")
                return deepcopy(entry), f"挑战 {entry['挑战编号']} 重试耗尽，已降级渲染：{reason}"

            self._record_event(
                entry, "自动识别重试", status, status,
                f"第 {entry['已尝试次数']}/{limit} 次{timeout_reason}，仍保留服务商名额", operator,
            )
            self._sync_render_result(entry)
            return deepcopy(entry), (
                f"挑战 {entry['挑战编号']} 记录第 {entry['已尝试次数']}/{limit} 次失败，"
                "未达重试上限，继续自动识别"
            )

    # -------------------------------------------------------------- 人工打码
    def claim_manual(self, entry_id: int, operator: str = "值班打码员") -> tuple[dict[str, Any] | None, str]:
        """认领打码任务；同一挑战只允许一个认领人。"""
        with _LOCK:
            entry = store.find(CHALLENGE_MODULE, entry_id)
            if entry is None:
                return None, f"挑战 {entry_id} 不存在或已归档"
            status = str(entry.get("status") or "")
            if status == "人工认领中":
                return None, (
                    f"挑战 {entry.get('挑战编号')} 已被「{entry.get('认领人') or '其他打码员'}」认领，不能重复认领"
                )
            if status != "待人工打码":
                return None, f"挑战 {entry.get('挑战编号')} 当前状态为「{status}」，不在可认领的打码等待队列中"
            if entry.get("认领人"):
                return None, f"挑战 {entry.get('挑战编号')} 已被认领，不能重复认领"
            if not str(entry.get("打码等待队列") or "").strip():
                return None, f"挑战 {entry.get('挑战编号')} 缺少打码队列标识，无法认领"

            entry["认领人"] = operator
            entry["认领时间"] = _iso(_utcnow())
            entry["识别状态"] = "人工打码中"
            self._move(entry, "人工认领中", "认领打码", f"打码队列「{entry.get('打码等待队列')}」", operator)
            return deepcopy(entry), (
                f"挑战 {entry['挑战编号']} 已由「{operator}」认领，队列标识与页面等待条件保持不变"
            )

    def submit_manual_result(
        self,
        entry_id: int,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """写入人工打码结果；与自动重试并发时只有先落库的一条生效。"""
        payload = dict(values or {})
        operator = str(payload.get("操作人") or "").strip() or "值班打码员"
        result = str(payload.get("识别状态") or "成功").strip()
        with _LOCK:
            entry = store.find(CHALLENGE_MODULE, entry_id)
            if entry is None:
                return None, f"挑战 {entry_id} 不存在或已归档"
            status = str(entry.get("status") or "")
            if status in FINAL_STATUSES:
                return None, (
                    f"挑战 {entry.get('挑战编号')} 已有结果落库（当前「{status}」，"
                    f"来源「{entry.get('结果来源') or '未知'}」），拒绝重复写入"
                )
            if status == "待人工打码":
                return None, f"挑战 {entry.get('挑战编号')} 尚未被认领，不能直接提交人工结果"
            if status != "人工认领中":
                return None, (
                    f"挑战 {entry.get('挑战编号')} 当前状态为「{status}」，"
                    "自动识别仍在进行，人工结果不能落库"
                )
            if entry.get("认领人") and str(entry.get("认领人")) != operator:
                return None, (
                    f"挑战 {entry.get('挑战编号')} 由「{entry.get('认领人')}」认领，"
                    f"「{operator}」无权提交结果"
                )

            entry["已尝试次数"] = int(entry.get("已尝试次数") or 0) + 1
            entry["识别状态"] = result
            entry["结果来源"] = "人工打码"
            if result == "成功":
                self._move(entry, "已完成", "提交人工打码结果", "人工打码成功", operator)
                entry["结束时间"] = _iso(_utcnow())
                self._sync_render_result(entry)
                return deepcopy(entry), f"挑战 {entry['挑战编号']} 人工打码成功，渲染结果已回写"
            self._degrade(entry, operator, "人工打码失败")
            return deepcopy(entry), f"挑战 {entry['挑战编号']} 人工打码失败，已按策略降级渲染"

    # -------------------------------------------------------------- 队列超时
    def expire_due_queue(self, values: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], list[str]]:
        """扫描到期的打码等待项并转降级。

        逐条处理、互不阻塞：单个挑战出错不影响同批其他站点，也不会占用请求线程等待。
        """
        payload = dict(values or {})
        operator = str(payload.get("操作人") or "队列巡检").strip()
        moment = _utcnow()
        if payload.get("当前时间"):
            parsed = _parse_time(payload.get("当前时间"))
            if parsed is None:
                return [], ["当前时间格式无法识别，请使用 ISO 8601（如 2026-11-14T10:00:00+00:00）"]
            moment = parsed
        expired: list[dict[str, Any]] = []
        errors: list[str] = []
        with _LOCK:
            due_ids = [
                int(row.get("id", 0))
                for row in store.rows(CHALLENGE_MODULE)
                if row.get("status") == "待人工打码"
                and (deadline := _parse_time(row.get("队列到期时间"))) is not None
                and deadline <= moment
            ]
            for entry_id in due_ids:
                entry = store.find(CHALLENGE_MODULE, entry_id)
                if entry is None or entry.get("status") != "待人工打码":
                    continue
                try:
                    timeout = int(entry.get("打码等待超时秒") or 0)
                    reason = f"打码等待队列「{entry.get('打码等待队列')}」超时（{timeout}s）无人认领"
                    self._degrade(entry, operator, reason)
                    expired.append(deepcopy(entry))
                except Exception as exc:  # 单条失败不阻断其他站点
                    errors.append(f"挑战 {entry.get('挑战编号')} 超时处理失败：{exc}")
        return expired, errors

    # ------------------------------------------------------------ 归档与恢复
    def archive(self, entry_id: int, operator: str = "值班管理员") -> tuple[dict[str, Any] | None, str]:
        """归档已有结论的挑战；写入事件与审计，变更可核对。"""
        with _LOCK:
            entry = store.find(CHALLENGE_MODULE, entry_id)
            if entry is None:
                return None, f"挑战 {entry_id} 不存在或已归档"
            if entry.get("status") not in FINAL_STATUSES:
                return None, (
                    f"挑战 {entry.get('挑战编号')} 当前状态为「{entry.get('status')}」，"
                    "只有已完成或已降级的挑战才允许归档"
                )
            self._move(entry, "已归档", "归档挑战", entry.get("降级原因") or "结论已确认", operator)
            entry["归档时间"] = _iso(_utcnow())
            self._sync_render_result(entry)
            return deepcopy(entry), f"挑战 {entry['挑战编号']} 已归档，归档记录可核对"

    def restore(self, entry_id: int, operator: str = "值班管理员") -> tuple[dict[str, Any] | None, str]:
        """恢复归档记录：迁移到独立的「已恢复」状态，不回退到旧状态。"""
        with _LOCK:
            entry = store.find(CHALLENGE_MODULE, entry_id)
            if entry is None:
                return None, f"挑战 {entry_id} 不存在"
            if entry.get("status") != "已归档":
                return None, (
                    f"挑战 {entry.get('挑战编号')} 当前状态为「{entry.get('status')}」，只有已归档记录允许恢复"
                )
            self._move(entry, "已恢复", "恢复挑战", "从归档只读副本恢复", operator)
            entry["恢复时间"] = _iso(_utcnow())
            self._sync_render_result(entry)
            return deepcopy(entry), (
                f"挑战 {entry['挑战编号']} 已恢复为「已恢复」，恢复事件与审计记录可核对"
            )

    # -------------------------------------------------------------- 内部分流
    def _resolve_captcha(self, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        number = str(payload.get("验证码策略编号") or "").strip()
        if not number:
            return None, "缺少验证码策略编号，无法按验证码类型与服务商分流"
        captcha = next(
            (row for row in store.rows(CAPTCHA_MODULE) if row.get("策略编号") == number),
            None,
        )
        if captcha is None:
            return None, f"验证码策略 {number} 不存在"
        return captcha, ""

    def _observation(self, payload: dict[str, Any], captcha: dict[str, Any]) -> dict[str, Any]:
        return {
            "识别状态": str(payload.get("识别状态") or "待识别").strip(),
            "识别用时秒": _as_int(payload.get("识别用时秒"), 0),
            "已尝试次数": _as_int(payload.get("已尝试次数"), 0),
            "超时秒": _as_int(captcha.get("识别超时秒"), 0),
            "重试上限": max(_as_int(captcha.get("重试次数上限"), 1), 1),
        }

    def _decide(self, captcha: dict[str, Any], observation: dict[str, Any]) -> tuple[str, str, str]:
        """按类型 → 策略状态 → 服务商 → 超时/重试上限的顺序给出分流结论。"""
        challenge_type = str(captcha.get("验证码类型") or "").strip() or "未知类型"
        provider = str(captcha.get("识别服务商") or "").strip()
        policy_status = str(captcha.get("status") or "")

        if policy_status in ("草稿", "已降级"):
            return DECISION_DEGRADE, f"验证码策略处于「{policy_status}」，没有可用识别通道", provider

        if challenge_type in MANUAL_ONLY_TYPES or challenge_type not in AUTO_CAPABLE_TYPES:
            return self._manual_or_degrade(
                captcha, f"验证码类型「{challenge_type}」必须人工处理", provider
            )

        if policy_status != "已启用":
            return self._manual_or_degrade(
                captcha,
                f"验证码策略当前为「{policy_status}」，自动识别结果不可信，转人工确认",
                provider,
            )

        if provider in UNCONFIGURED_PROVIDERS:
            return self._manual_or_degrade(captcha, "识别服务商未配置", provider)

        quota = PROVIDER_QUOTAS.get(provider, DEFAULT_PROVIDER_QUOTA)
        used = self._quota_used(provider)
        if used >= quota:
            return self._manual_or_degrade(
                captcha, f"识别服务商「{provider}」名额已满（{used}/{quota}）", provider
            )

        limit = max(int(observation["重试上限"]), 1)
        if int(observation["已尝试次数"]) >= limit:
            return self._manual_or_degrade(
                captcha, f"已尝试 {observation['已尝试次数']} 次，达到重试上限 {limit} 次", provider
            )

        if int(observation["识别用时秒"]) > int(observation["超时秒"]):
            return self._manual_or_degrade(
                captcha,
                f"识别耗时 {observation['识别用时秒']}s 超过超时 {observation['超时秒']}s",
                provider,
            )

        return DECISION_AUTO, "类型可自动识别、服务商有名额且未超预算", provider

    def _manual_or_degrade(self, captcha: dict[str, Any], reason: str, provider: str) -> tuple[str, str, str]:
        if _switch_on(captcha.get("手动打码开关")) and str(captcha.get("打码等待队列") or "").strip():
            return DECISION_MANUAL, reason, provider
        return DECISION_DEGRADE, f"{reason}；手动打码关闭或缺少队列标识", provider

    # ---------------------------------------------------------------- 状态写入
    def _create_challenge(
        self,
        rendering: dict[str, Any],
        captcha: dict[str, Any],
        key: str,
    ) -> dict[str, Any]:
        rows = store.rows(CHALLENGE_MODULE)
        next_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        now = _iso(_utcnow())
        entry: dict[str, Any] = {
            "id": next_id,
            "挑战编号": f"CHAL-{next_id:04d}",
            "挑战键": key,
            "渲染配置ID": int(rendering.get("id", 0)),
            "渲染配置编号": str(rendering.get("配置编号") or ""),
            "渲染任务标识": f"RENDER-CHAL-{next_id:04d}",
            "目标URL": str(rendering.get("目标URL") or ""),
            "验证码策略ID": int(captcha.get("id", 0)),
            "验证码策略编号": str(captcha.get("策略编号") or ""),
            "验证码类型": str(captcha.get("验证码类型") or ""),
            "识别服务商": str(captcha.get("识别服务商") or ""),
            "识别超时秒": _as_int(captcha.get("识别超时秒"), 0),
            "重试次数上限": max(_as_int(captcha.get("重试次数上限"), 1), 1),
            "已尝试次数": 0,
            "识别用时秒": 0,
            "识别状态": "待识别",
            "手动打码开关": captcha.get("手动打码开关"),
            "打码等待队列": str(captcha.get("打码等待队列") or ""),
            "打码等待超时秒": _as_int(captcha.get("打码等待超时秒"), 60),
            "降级策略": str(captcha.get("降级策略") or ""),
            "降级原因": "",
            # 分流时必须原样保留的渲染现场。
            "页面等待条件": str(rendering.get("页面等待条件") or ""),
            "点击选择器": str(rendering.get("点击选择器") or ""),
            "分流结论": "待分流",
            "status": "待分流",
            "pending": True,
            "abnormal": False,
            "服务商名额占用": False,
            "当前占用服务商": "",
            "服务商名额占用时间": "",
            "认领人": "",
            "认领时间": "",
            "队列进入时间": "",
            "队列到期时间": "",
            "结果来源": "",
            "结束时间": "",
            "归档时间": "",
            "恢复时间": "",
            "创建时间": now,
            "更新时间": now,
        }
        rows.append(entry)
        self._record_event(entry, "发起挑战分流", "-", "待分流", "渲染测试命中验证码页面", "采集调度器")
        return entry

    def _enter_auto(self, entry: dict[str, Any], provider: str, operator: str, reason: str) -> None:
        entry["服务商名额占用"] = True
        entry["当前占用服务商"] = provider
        entry["服务商名额占用时间"] = _iso(_utcnow())
        entry["识别状态"] = "自动识别中"
        self._move(entry, "自动识别中", "进入自动识别", reason, operator)

    def _finish_auto(self, entry: dict[str, Any], operator: str, *, succeeded: bool, elapsed: int) -> None:
        entry["识别用时秒"] = elapsed
        entry["识别状态"] = "成功" if succeeded else "失败"
        entry["结果来源"] = "自动识别"
        self._release_provider(entry)
        self._move(entry, "已完成", "自动识别完成", f"用时 {elapsed}s", operator)
        entry["结束时间"] = _iso(_utcnow())
        # 自动识别成功只说明渲染测试通过；是否上线仍由人工「标记上线」决定，
        # 这里不回退也不越级推进 JSRE 状态。
        self._sync_render_result(entry)

    def _enter_manual_queue(self, entry: dict[str, Any], operator: str, reason: str) -> None:
        # 进入人工队列前必须归还名额：后续自动重试不能继续占着服务商并发。
        self._release_provider(entry)
        entry["认领人"] = ""
        entry["认领时间"] = ""
        entry["识别状态"] = "待人工打码"
        now = _utcnow()
        wait_seconds = int(entry.get("打码等待超时秒") or 60)
        entry["队列进入时间"] = _iso(now)
        entry["队列到期时间"] = _iso(now + timedelta(seconds=wait_seconds))
        self._move(entry, "待人工打码", "进入打码等待队列", reason, operator)

    def _degrade(self, entry: dict[str, Any], operator: str, reason: str) -> None:
        self._release_provider(entry)
        entry["降级原因"] = reason
        entry["识别状态"] = "降级"
        entry["结果来源"] = entry.get("结果来源") or "分流判定"
        self._move(entry, "已降级", "降级渲染", reason, operator)
        entry["结束时间"] = _iso(_utcnow())
        self._sync_render_result(entry)
        self._advance_jsrender_by_id(entry, "已降级")

    def _move(
        self,
        entry: dict[str, Any],
        target: str,
        action: str,
        note: str,
        operator: str,
    ) -> None:
        current = str(entry.get("status") or "")
        allowed = TRANSITIONS.get(current, set())
        if target not in allowed:
            raise ValueError(f"挑战 {entry.get('挑战编号')} 不能从「{current}」变更为「{target}」")
        if STATUS_INDEX[target] <= STATUS_INDEX[current]:
            raise ValueError(f"挑战 {entry.get('挑战编号')} 状态只能前进，禁止从「{current}」回到「{target}」")
        entry["status"] = target
        entry["pending"] = target in {"待分流", "待人工打码"}
        entry["abnormal"] = target == "已降级"
        entry["更新时间"] = _iso(_utcnow())
        self._record_event(entry, action, current, target, note, operator)
        self._write_audit(entry, action, current, target, note, operator)
        self._sync_render_result(entry)

    def _record_event(
        self,
        entry: dict[str, Any],
        action: str,
        previous: str,
        target: str,
        note: str,
        operator: str,
    ) -> None:
        rows = store.rows(EVENT_MODULE)
        sequence = sum(
            1 for row in rows if row.get("挑战编号") == entry.get("挑战编号")
        ) + 1
        rows.append({
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "挑战编号": entry.get("挑战编号"),
            "事件序号": sequence,
            "动作": action,
            "原状态": previous,
            "新状态": target,
            "操作人": operator,
            "操作时间": _iso(_utcnow()),
            "说明": note,
            "页面等待条件": entry.get("页面等待条件"),
            "点击选择器": entry.get("点击选择器"),
            "打码等待队列": entry.get("打码等待队列"),
        })

    def _write_audit(
        self,
        entry: dict[str, Any],
        action: str,
        previous: str,
        target: str,
        note: str,
        operator: str,
        result: str = "成功",
    ) -> None:
        rows = store.rows(AUDIT_MODULE)
        next_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        rows.append({
            "id": next_id,
            "status": "已记录",
            "pending": False,
            "abnormal": result != "成功",
            "记录编号": f"AUDI-{next_id:04d}",
            "操作类型": action,
            "操作对象": f"挑战分流/{entry.get('挑战编号')}",
            "操作人": operator,
            "操作时间": _iso(_utcnow()),
            "操作IP": "127.0.0.1",
            "变更详情": f"{previous} → {target}：{note}",
            "操作结果": result,
        })

    # ---------------------------------------------------------------- 回写渲染
    def _sync_render_result(self, entry: dict[str, Any]) -> None:
        """把挑战结论回写到渲染结果。

        顶层字段反映最近一次更新的挑战；``挑战记录`` 按挑战编号保留每个挑战的结论，
        便于核对同一渲染任务下不同验证码类型的识别状态与降级策略。
        """
        rendering = store.find(JSRE_MODULE, int(entry.get("渲染配置ID", 0)))
        if rendering is None:
            return
        previous = rendering.get("渲染结果") if isinstance(rendering.get("渲染结果"), dict) else {}
        records = [
            deepcopy(item) for item in previous.get("挑战记录", [])
            if isinstance(item, dict) and item.get("挑战编号") != entry.get("挑战编号")
        ]
        record = {
            "挑战编号": entry.get("挑战编号"),
            "验证码类型": entry.get("验证码类型"),
            "验证码策略编号": entry.get("验证码策略编号"),
            "挑战状态": entry.get("status"),
            "分流结论": entry.get("分流结论"),
            "识别状态": entry.get("识别状态"),
            "结果来源": entry.get("结果来源"),
            "降级策略": entry.get("降级策略"),
            "降级原因": entry.get("降级原因"),
            "降级生效": entry.get("status") == "已降级",
            "打码等待队列": entry.get("打码等待队列"),
            "页面等待条件": entry.get("页面等待条件"),
            "点击选择器": entry.get("点击选择器"),
        }
        records.append(record)
        rendering["渲染结果"] = {
            "挑战编号": entry.get("挑战编号"),
            "渲染任务标识": entry.get("渲染任务标识"),
            "挑战状态": entry.get("status"),
            "渲染状态": RENDER_STATE.get(str(entry.get("status")), str(entry.get("status"))),
            "分流结论": entry.get("分流结论"),
            "验证码类型": entry.get("验证码类型"),
            "验证码策略编号": entry.get("验证码策略编号"),
            "识别服务商": entry.get("识别服务商"),
            "识别超时秒": entry.get("识别超时秒"),
            "重试次数上限": entry.get("重试次数上限"),
            "已尝试次数": entry.get("已尝试次数"),
            "识别状态": entry.get("识别状态"),
            "结果来源": entry.get("结果来源"),
            # 渲染现场不因分流或重试被覆盖。
            "页面等待条件": entry.get("页面等待条件"),
            "点击选择器": entry.get("点击选择器"),
            "打码等待队列": entry.get("打码等待队列"),
            "降级策略": entry.get("降级策略"),
            "降级原因": entry.get("降级原因"),
            "降级生效": entry.get("status") == "已降级",
            "挑战记录": records,
            "更新时间": _iso(_utcnow()),
        }

    def _advance_jsrender(self, rendering: dict[str, Any], target: str) -> None:
        current = str(rendering.get("status") or "")
        if current not in JSRE_ORDER or target not in JSRE_ORDER:
            return
        if JSRE_ORDER.index(target) <= JSRE_ORDER.index(current):
            return
        rendering["status"] = target
        rendering["pending"] = target == "渲染测试中"
        rendering["abnormal"] = target == "已降级"

    def _advance_jsrender_by_id(self, entry: dict[str, Any], target: str) -> None:
        rendering = store.find(JSRE_MODULE, int(entry.get("渲染配置ID", 0)))
        if rendering is not None:
            self._advance_jsrender(rendering, target)

    # ------------------------------------------------------------------ 名额
    def _release_provider(self, entry: dict[str, Any]) -> None:
        entry["服务商名额占用"] = False
        entry["当前占用服务商"] = ""
        entry["服务商名额占用时间"] = ""

    def _quota_used(self, provider: str, exclude_id: int | None = None) -> int:
        used = 0
        for row in store.rows(CHALLENGE_MODULE):
            if not row.get("服务商名额占用"):
                continue
            if row.get("当前占用服务商") != provider:
                continue
            if exclude_id is not None and int(row.get("id", 0)) == exclude_id:
                continue
            used += 1
        return used

    def _provider_usage(self) -> list[dict[str, Any]]:
        usage: list[dict[str, Any]] = []
        for provider in sorted({str(row.get("当前占用服务商") or "") for row in store.rows(CHALLENGE_MODULE) if row.get("服务商名额占用")}):
            if not provider:
                continue
            quota = PROVIDER_QUOTAS.get(provider, DEFAULT_PROVIDER_QUOTA)
            used = self._quota_used(provider)
            usage.append({"识别服务商": provider, "已占用": used, "名额上限": quota, "可用": quota - used})
        return usage

    def _active_by_key(self, key: str) -> dict[str, Any] | None:
        for row in store.rows(CHALLENGE_MODULE):
            if row.get("挑战键") == key and row.get("status") in ACTIVE_STATUSES:
                return row
        return None

    @staticmethod
    def _challenge_key(rendering: dict[str, Any], challenge_type: str) -> str:
        return "|".join([
            str(rendering.get("配置编号") or ""),
            str(rendering.get("目标URL") or ""),
            challenge_type,
        ])


# ---------------------------------------------------------------- 工具函数
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _switch_on(value: Any) -> bool:
    return str(value).strip() in {"开启", "是", "true", "True", "1", "on"}
