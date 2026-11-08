"""
二维码扫码登录路由

提供二维码生成、状态查询和Cookie获取接口。
登录成功只按仅保存登录策略入库，不在该模式下启动或重启账号任务。
"""
from __future__ import annotations

import asyncio
from typing import Dict

from fastapi import APIRouter, Depends
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.services.account_service import AccountService
from app.services.login_task_dispatch import dispatch_login_task
from app.services.qr_login import qr_login_manager
from common.login_save_only import task_action_after_login
from app.api.deps import get_db_session as get_db
from common.models.user import User
from common.schemas.common import ApiResponse
from common.services.account_limit_service import AccountLimitExceededError

router = APIRouter(prefix="/qr-login", tags=["二维码登录"])

# 会话所有者映射
SESSION_OWNER: Dict[str, int] = {}

# 已处理的会话记录，防止重复处理
PROCESSED_SESSIONS: Dict[str, Dict] = {}

# 会话处理锁，防止并发处理同一个会话
SESSION_LOCKS: Dict[str, asyncio.Lock] = {}


def _get_session_lock(session_id: str) -> asyncio.Lock:
    """获取会话锁"""
    if session_id not in SESSION_LOCKS:
        SESSION_LOCKS[session_id] = asyncio.Lock()
    return SESSION_LOCKS[session_id]


def _cleanup_session(session_id: str):
    """清理会话相关数据"""
    SESSION_OWNER.pop(session_id, None)
    SESSION_LOCKS.pop(session_id, None)


def _build_processed_response(processed_info: dict) -> ApiResponse:
    processed_status = processed_info.get("status")
    processed_message = processed_info.get("message") or (
        "扫码登录失败" if processed_status == "failed" else "扫码登录已完成"
    )
    return ApiResponse(
        success=True,
        message=processed_message,
        data={
            "status": "failed" if processed_status == "failed" else "already_processed",
            "message": processed_message,
            "account_info": {
                "account_id": processed_info.get("account_id"),
                "is_new_account": bool(processed_info.get("is_new_account", False)),
                "status": processed_info.get("account_status"),
                "tasks_started": processed_info.get("tasks_started"),
            },
        },
    )


@router.post("/generate")
async def generate_qr_code(
    current_user: User = Depends(deps.get_current_active_user),
) -> ApiResponse:
    """
    生成二维码
    
    返回二维码图片的Base64编码和会话ID
    """
    try:
        result = await qr_login_manager.generate_qr_code()
        session_id = result.get("session_id")
        
        if result.get("success") and session_id:
            SESSION_OWNER[session_id] = current_user.id
            logger.info(f"二维码生成成功: session_id={session_id}, user_id={current_user.id}")
            return ApiResponse(
                success=True,
                message="二维码生成成功",
                data={
                    "session_id": session_id,
                    "qr_code_url": result.get("qr_code_url"),
                }
            )
        else:
            error_msg = result.get("message", "生成二维码失败")
            logger.error(f"二维码生成失败: {error_msg}")
            return ApiResponse(
                success=False,
                message=error_msg,
            )
    except Exception as e:
        logger.exception("生成二维码时发生异常")
        return ApiResponse(
            success=False,
            message=f"生成二维码失败: {str(e)}",
        )


@router.get("/status/{session_id}")
async def get_qr_status(
    session_id: str,
    current_user: User = Depends(deps.get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    """
    查询扫码状态
    
    状态说明：
    - waiting: 等待扫码
    - scanned: 已扫码，等待确认
    - success: 扫码成功
    - expired: 二维码已过期
    - cancelled: 用户取消登录
    - verification_required: 需要手机验证
    - not_found: 会话不存在
    - already_processed: 已处理过
    """
    try:
        # 检查是否已处理过
        if session_id in PROCESSED_SESSIONS:
            processed_info = PROCESSED_SESSIONS[session_id]
            return _build_processed_response(processed_info)
        
        status_info = qr_login_manager.get_session_status(session_id)
        status = status_info.get("status")
        
        # 如果扫码成功，自动创建或更新账号
        if status == "success":
            owner_id = SESSION_OWNER.get(session_id, current_user.id)
            
            # 使用锁防止并发处理
            lock = _get_session_lock(session_id)
            async with lock:
                # 双重检查
                if session_id in PROCESSED_SESSIONS:
                    processed_info = PROCESSED_SESSIONS[session_id]
                    return _build_processed_response(processed_info)
                
                cookies_info = qr_login_manager.get_session_cookies(session_id)
                if cookies_info:
                    cookies_str = cookies_info.get("cookies", "")
                    unb = cookies_info.get("unb")
                    
                    try:
                        account_service = AccountService(db)
                        account, is_new_account = await account_service.upsert_account_from_qr(
                            owner_id=owner_id,
                            cookies=cookies_str,
                            unb=unb,
                        )
                    except AccountLimitExceededError as exc:
                        message = str(exc)
                        PROCESSED_SESSIONS[session_id] = {
                            "status": "failed",
                            "message": message,
                            "account_id": unb or "",
                            "is_new_account": True,
                        }
                        _cleanup_session(session_id)
                        return ApiResponse(
                            success=True,
                            message=message,
                            data={
                                "status": "failed",
                                "message": message,
                                "account_info": {
                                    "account_id": unb or "",
                                    "is_new_account": True,
                                }
                            }
                        )

                    logger.info(
                        f"扫码登录：{'创建新账号' if is_new_account else '更新现有账号'} {account.account_id}"
                    )

                    action = task_action_after_login(status=account.status, created=is_new_account)
                    response = await dispatch_login_task(
                        account.account_id,
                        action,
                        cookie=cookies_str,
                        user_id=owner_id,
                    )
                    if action == "none":
                        logger.info(f"扫码登录：账号已保存且保持禁用 {account.account_id}")
                    elif response.get("success"):
                        logger.info(f"扫码登录：账号任务{action}完成 {account.account_id}")
                    else:
                        logger.warning(
                            f"扫码登录：账号任务{action}失败 {account.account_id}: {response.get('message')}"
                        )
                    
                    tasks_started = action in {"start", "restart"}
                    # 记录已处理
                    PROCESSED_SESSIONS[session_id] = {
                        "account_id": account.account_id,
                        "is_new_account": is_new_account,
                        "account_status": account.status,
                        "tasks_started": tasks_started,
                    }
                    
                    _cleanup_session(session_id)
                    
                    return ApiResponse(
                        success=True,
                        message="扫码登录已保存，账号保持禁用" if not tasks_started else "扫码登录成功",
                        data={
                            "status": "success",
                            "account_info": {
                                "account_id": account.account_id,
                                "is_new_account": is_new_account,
                                "status": account.status,
                                "tasks_started": tasks_started,
                            }
                        }
                    )
        
        # 清理过期或取消的会话
        if status in {"expired", "cancelled", "not_found"}:
            _cleanup_session(session_id)
            PROCESSED_SESSIONS.pop(session_id, None)
        
        return ApiResponse(
            success=True,
            message="查询成功",
            data=status_info
        )
        
    except Exception as e:
        logger.exception(f"查询扫码状态时发生异常: {session_id}")
        return ApiResponse(
            success=False,
            message=f"查询失败: {str(e)}",
        )


@router.get("/cookie/{session_id}")
async def get_qr_cookie(
    session_id: str,
    current_user: User = Depends(deps.get_current_active_user),
) -> ApiResponse:
    """
    获取登录Cookie
    
    仅在扫码成功后可用
    """
    try:
        cookies_info = qr_login_manager.get_session_cookies(session_id)
        
        if cookies_info:
            return ApiResponse(
                success=True,
                message="获取Cookie成功",
                data=cookies_info
            )
        else:
            return ApiResponse(
                success=False,
                message="Cookie不存在或会话未完成",
            )
    except Exception as e:
        logger.exception(f"获取Cookie时发生异常: {session_id}")
        return ApiResponse(
            success=False,
            message=f"获取Cookie失败: {str(e)}",
        )
