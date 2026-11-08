"""登录入库后的账号任务动作。

只允许启动、重启或停止账号连接，不调用发消息或创建会话接口。
仅保存登录时新账号不调度，已有账号只停止可能仍在运行的连接。
"""
from __future__ import annotations

from typing import Any

from loguru import logger

from common.login_save_only import login_task_endpoint


async def dispatch_login_task(
    account_id: str,
    action: str,
    *,
    cookie: str,
    user_id: int,
    client: Any | None = None,
    base_url: str | None = None,
) -> dict:
    """按登录结果调用 WebSocket 服务。连接失败不影响凭据已经保存。"""
    if action == "none":
        logger.info(f"仅保存登录：账号 {account_id} 已保存，不启动任务")
        return {"success": True, "skipped": True, "action": action}

    endpoint = login_task_endpoint(action, account_id)
    if endpoint is None:
        return {"success": True, "skipped": True, "action": action}

    if base_url is None:
        from app.core.config import get_settings

        base_url = get_settings().websocket_service_url
    if client is None:
        from app.core.http_client import get_http_client

        client = get_http_client()

    url = f"{base_url.rstrip('/')}{endpoint}"
    payload = None if action == "stop" else {"cookie_value": cookie, "user_id": user_id}
    try:
        response = await client.post(url, json=payload) if payload is not None else await client.post(url)
    except Exception as exc:
        logger.error(f"登录后账号任务{action}失败 {account_id}: {exc}")
        return {"success": False, "message": str(exc), "action": action}

    if isinstance(response, dict):
        response.setdefault("action", action)
        return response
    return {"success": True, "action": action}
