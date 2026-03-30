"""
审批路由 — HITL 审批 API
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from aiops.core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/approval", tags=["approval"])


class ApproveRequest(BaseModel):
    """审批请求."""
    approver: str
    comment: str = ""


class RejectRequest(BaseModel):
    """拒绝请求."""
    rejector: str
    reason: str


@router.get("/pending")
async def list_pending(session_id: str | None = None) -> dict[str, Any]:
    """列出待审批请求."""
    from aiops.hitl.gate import HITLGate

    gate = HITLGate(auto_approve_dev=False)
    pending = gate.list_pending(session_id)
    return {
        "total": len(pending),
        "requests": [
            {
                "id": r.id,
                "operation": r.operation,
                "risk_level": r.risk_level,
                "status": r.status.value,
                "created_at": r.created_at,
                "timeout_seconds": r.timeout_seconds,
            }
            for r in pending
        ],
    }


@router.post("/{request_id}/approve")
async def approve_request(
    request_id: str,
    body: ApproveRequest,
) -> dict[str, Any]:
    """批准审批请求."""
    from aiops.hitl.gate import HITLGate

    gate = HITLGate(auto_approve_dev=False)
    try:
        request = gate.approve(request_id, body.approver, body.comment)
        return {
            "status": request.status.value,
            "approved_by": request.approved_by,
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{request_id}/reject")
async def reject_request(
    request_id: str,
    body: RejectRequest,
) -> dict[str, Any]:
    """拒绝审批请求."""
    from aiops.hitl.gate import HITLGate

    gate = HITLGate(auto_approve_dev=False)
    try:
        request = gate.reject(request_id, body.rejector, body.reason)
        return {
            "status": request.status.value,
            "rejected_by": request.rejected_by,
            "reason": request.reject_reason,
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{request_id}")
async def get_request(request_id: str) -> dict[str, Any]:
    """获取审批请求详情."""
    from aiops.hitl.gate import HITLGate

    gate = HITLGate()
    request = gate.get_request(request_id)
    if not request:
        raise HTTPException(status_code=404, detail="Request not found")

    return {
        "id": request.id,
        "operation": request.operation,
        "risk_level": request.risk_level,
        "status": request.status.value,
        "approval_type": request.approval_type.value,
        "approved_by": request.approved_by,
        "rejected_by": request.rejected_by,
        "reject_reason": request.reject_reason,
        "created_at": request.created_at,
        "resolved_at": request.resolved_at,
    }
