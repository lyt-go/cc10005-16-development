"""验证码挑战分流接口。

渲染测试命中验证码页面后：提交分流 → 自动识别结果 / 人工认领 / 人工结果 / 队列超时
→ 归档 / 恢复。所有状态判断都在 ``app.services.challenge`` 里，路由层只做参数与错误映射。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.challenge import STATUS_ORDER, ChallengeService

router = APIRouter(prefix="/api/challenge", tags=["挑战分流"])

service = ChallengeService()

LIST_FIELDS = [
    "挑战编号", "渲染配置编号", "验证码类型", "识别服务商", "识别超时秒", "重试次数上限",
    "已尝试次数", "识别状态", "分流结论", "打码等待队列", "页面等待条件", "点击选择器",
    "降级策略", "降级原因",
]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按挑战编号或渲染配置编号检索"),
    status: str | None = Query(default=None, description="、".join(STATUS_ORDER)),
    rendering_id: int | None = Query(default=None, description="按渲染配置ID检索"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按状态、渲染配置与关键字过滤挑战；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword, status=status, rendering_id=rendering_id, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/summary", response_model=dict)
def summary() -> dict:
    """挑战分流的看板指标：状态分布与识别服务商名额占用。"""
    return service.summary()


@router.get("/queue", response_model=dict)
def queue_snapshot() -> dict:
    """人工打码等待队列快照；只读接口，不会阻塞其他站点采集。"""
    return service.queue_snapshot()


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出挑战分流清单：返回当前全量数据，供人工核对。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "challenge", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单个挑战的明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"挑战 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/events", response_model=PageResult[dict])
def list_events(entry_id: int) -> PageResult[dict]:
    """读取挑战的状态变更事件流，用于核对归档与恢复产生的变更。"""
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"挑战 {entry_id} 不存在或已归档")
    events = service.list_events(entry_id)
    return PageResult(items=events, total=len(events), page=1, size=len(events) or 20)


@router.post("/submit", response_model=ActionResult)
def submit(payload: EntryPayload) -> ActionResult:
    """渲染测试命中验证码页面时提交分流。

    ``values`` 至少包含渲染配置ID 与验证码策略编号；同一挑战重复提交会得到已有记录。
    """
    rendering_id = payload.values.get("渲染配置ID")
    try:
        rendering_id = int(str(rendering_id))
    except (TypeError, ValueError):
        return ActionResult(ok=False, message="缺少渲染配置ID，无法发起挑战分流")
    entry, message, duplicated = service.submit_rendering(rendering_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    if duplicated:
        return ActionResult(ok=False, message=message, entry=entry)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/auto-result", response_model=ActionResult)
def apply_auto_result(entry_id: int, payload: EntryPayload) -> ActionResult:
    """写入自动识别结果；重试耗尽后按策略转人工或降级。"""
    entry, message = service.apply_auto_result(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/claim", response_model=ActionResult)
def claim_manual(entry_id: int, payload: EntryPayload) -> ActionResult:
    """认领打码任务；同一挑战只允许一个认领人。"""
    operator = str(payload.values.get("操作人") or "值班打码员").strip()
    entry, message = service.claim_manual(entry_id, operator)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/manual-result", response_model=ActionResult)
def submit_manual_result(entry_id: int, payload: EntryPayload) -> ActionResult:
    """提交人工打码结果；与自动重试并发时只有先落库的一条生效。"""
    entry, message = service.submit_manual_result(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/expire-queue", response_model=ActionResult)
def expire_queue(payload: EntryPayload) -> ActionResult:
    """扫描到期的打码等待项并逐条转降级；单条失败不阻断其他站点。"""
    expired, errors = service.expire_due_queue(payload.values)
    message = f"已处理 {len(expired)} 条超时队列"
    if errors:
        message += "；" + "；".join(errors)
    return ActionResult(ok=not errors, message=message, entry={"items": expired} if expired else None)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """归档与恢复挑战；两次迁移都会写入挑战事件与操作审计。"""
    action = str(payload.values.get("action") or "").strip()
    operator = str(payload.values.get("操作人") or "值班管理员").strip()
    if action == "归档挑战":
        entry, message = service.archive(entry_id, operator)
    elif action == "恢复挑战":
        entry, message = service.restore(entry_id, operator)
    else:
        return ActionResult(ok=False, message=f"动作「{action}」不属于挑战分流可执行范围")
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
