"""仅保存登录。

登录、导入和续期成功后只保存凭据与不含密钥的 metadata。
系统设置缺失时默认为开启，避免旧的启用状态在重新登录或服务重启时被写回。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.system_setting import SystemSetting

LOGIN_SAVE_ONLY_SETTING_KEY = "account.login_save_only"
LOGIN_SAVE_ONLY_DISABLE_REASON = "仅保存登录，未启用自动任务"
LOGIN_SAVE_ONLY_META_KEY = "login_save_only"
COOKIE_REFRESH_SNAPSHOT_KEY = "cookies_refresh_snapshot"

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}
_DISABLED_STATUSES = {"inactive", "disabled", "suspended", "deleted"}
_TASK_ACTIONS = {"none", "stop", "start", "restart"}
_OUTBOUND_ACTIONS = frozenset({"send-message", "create-chat"})


def parse_login_save_only(value: str | None) -> bool:
    """解析系统设置。缺失、空白或无法识别的值按开启处理。"""
    if value is None:
        return True
    text = str(value).strip().lower()
    if not text or text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    return True


def is_disabled_status(status: str | None) -> bool:
    """判断账号是否处于不可自动执行任务的状态。"""
    return (status or "").strip().lower() in _DISABLED_STATUSES


def is_account_enabled_for_tasks(status: str | None) -> bool:
    """仅明确为 active 的账号可在服务重启后被调度。"""
    return (status or "").strip().lower() == "active"


def select_startup_cookie_ids(cookie_status: dict[str, bool]) -> list[str]:
    """返回 WebSocket 启动调度候选，保持原有字典顺序。"""
    return [key for key, enabled in cookie_status.items() if enabled]


def should_schedule_startup_task(
    cookie_id: str,
    *,
    cookie_status: dict[str, bool],
    cookies: dict[str, Any],
) -> bool:
    """启动循环中的单个账号是否应建立连接。"""
    return bool(cookie_status.get(cookie_id, False)) and cookie_id in cookies


def _without_cookie_refresh_snapshot(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    """去掉续期快照。键名与 common.utils.cookie_refresh.COOKIE_REFRESH_SNAPSHOT_KEY 保持一致。"""
    if not isinstance(metadata, dict) or COOKIE_REFRESH_SNAPSHOT_KEY not in metadata:
        return metadata
    stored = dict(metadata)
    stored.pop(COOKIE_REFRESH_SNAPSHOT_KEY, None)
    return stored or None


def apply_saved_login_state(
    *,
    save_only: bool,
    previous_status: str | None,
    previous_disable_reason: str | None,
    metadata: dict[str, Any] | None,
) -> tuple[str, str | None, dict[str, Any]]:
    """计算登录入库后的状态、禁用原因和 metadata。

    开启仅保存登录时结果始终是 disabled，不会把 previous_status 写回去。
    已经禁用且有禁用原因的账号保留原原因。metadata 只记录模式本身，
    并清掉 Cookie 续期快照；其余已有配置（如 AI 回复设置）原样保留。
    """
    cleared = _without_cookie_refresh_snapshot(metadata if isinstance(metadata, dict) else None)
    stored = dict(cleared or {})
    if not save_only:
        stored[LOGIN_SAVE_ONLY_META_KEY] = {"enabled": False, "tasks_started": True}
        return "active", None, stored

    if is_disabled_status(previous_status) and previous_disable_reason:
        reason = previous_disable_reason
    else:
        reason = LOGIN_SAVE_ONLY_DISABLE_REASON
    stored[LOGIN_SAVE_ONLY_META_KEY] = {"enabled": True, "tasks_started": False}
    return "disabled", reason, stored


def assign_login_persistence(
    account: Any,
    *,
    save_only: bool,
    previous_status: str | None,
    previous_disable_reason: str | None,
) -> str:
    """把登录入库结果写到账号对象上，返回最终状态。"""
    status, reason, metadata = apply_saved_login_state(
        save_only=save_only,
        previous_status=previous_status,
        previous_disable_reason=previous_disable_reason,
        metadata=getattr(account, "metadata_json", None),
    )
    account.status = status
    account.disable_reason = reason
    account.metadata_json = metadata
    return status


def task_action_after_login(*, status: str, created: bool) -> str:
    """登录入库后允许的任务动作。不会返回发消息或创建会话。"""
    if is_account_enabled_for_tasks(status):
        return "start" if created else "restart"
    return "none" if created else "stop"


def explicit_enable_task_action(enabled: bool) -> str:
    """账号管理里的手动启用/禁用，不受仅保存登录开关影响。"""
    return "start" if enabled else "stop"


def auto_enable_after_refresh_allowed(*, save_only: bool) -> bool:
    """续期成功后是否允许把禁用账号改回启用并启动任务。"""
    return not save_only


def resolve_imported_account_state(
    *,
    save_only: bool,
    sheet_status: str | None,
    enable_all: bool,
    previous_status: str | None,
    previous_disable_reason: str | None,
    metadata: dict[str, Any] | None,
) -> tuple[str, str | None, dict[str, Any] | None, bool]:
    """解析导入后的状态。

    返回 (status, disable_reason, metadata, clear_disable_reason)。
    仅保存登录时忽略表格中的启用状态和“全部启用”。
    """
    if save_only:
        status, reason, stored = apply_saved_login_state(
            save_only=True,
            previous_status=previous_status,
            previous_disable_reason=previous_disable_reason,
            metadata=metadata,
        )
        return status, reason, stored, False

    status = (sheet_status or "").strip() or "active"
    if enable_all:
        status = "active"
    return status, previous_disable_reason, metadata, enable_all


def login_task_endpoint(action: str, account_id: str) -> str | None:
    """把任务动作映射到内部账号接口。没有发消息接口。"""
    if action not in _TASK_ACTIONS:
        raise ValueError(f"未知的登录任务动作: {action}")
    if action == "none":
        return None
    if action in _OUTBOUND_ACTIONS:
        raise ValueError(f"登录流程不能发送外部消息: {action}")
    return f"/internal/accounts/{account_id}/{action}"


async def read_login_save_only(session: AsyncSession) -> bool:
    """读取系统设置。没有记录时视为开启。"""
    result = await session.execute(
        select(SystemSetting.value).where(SystemSetting.key == LOGIN_SAVE_ONLY_SETTING_KEY)
    )
    return parse_login_save_only(result.scalar_one_or_none())
