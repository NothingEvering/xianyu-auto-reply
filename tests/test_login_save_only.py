"""仅保存登录回归测试。

全部使用假账号、假 Cookie 和假 HTTP 客户端，不访问闲鱼，不发送外部消息。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.login_save_only import (  # noqa: E402
    LOGIN_SAVE_ONLY_DISABLE_REASON,
    apply_saved_login_state,
    assign_login_persistence,
    auto_enable_after_refresh_allowed,
    explicit_enable_task_action,
    is_account_enabled_for_tasks,
    login_task_endpoint,
    parse_login_save_only,
    read_login_save_only,
    resolve_imported_account_state,
    select_startup_cookie_ids,
    should_schedule_startup_task,
    task_action_after_login,
)

FAKE_COOKIE = "unb=fake-user; cookie2=fake-session"
FAKE_PASSWORD = "fake-password"
OUTBOUND_FRAGMENTS = ("send-message", "create-chat")


class _Scalars:
    def __init__(self, items):
        self._items = items

    def first(self):
        return self._items[0] if self._items else None

    def all(self):
        return list(self._items)


class _Result:
    def __init__(self, items=None, scalar=None):
        self._items = [] if items is None else items
        self._scalar = scalar

    def scalars(self):
        return _Scalars(self._items)

    def scalar(self):
        return self._scalar

    def scalar_one_or_none(self):
        return self._scalar


class FakeSession:
    def __init__(self, results):
        self.results = list(results)
        self.added = []
        self.commits = 0

    async def execute(self, _stmt):
        if not self.results:
            raise AssertionError("测试会话收到了意外查询")
        return self.results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def refresh(self, _obj):
        return None


class RecordingAccount:
    """记录每一次状态写入，用来确认旧的 active 没有被写回。"""

    def __init__(self, status, disable_reason=None, metadata=None):
        self.writes = []
        self._status = None
        self.status = status
        self.disable_reason = disable_reason
        self.metadata_json = metadata
        self.owner_id = 7
        self.account_id = "existing-account"
        self.cookie = ""
        self.username = None
        self.login_password = None
        self.show_browser = False
        self.login_method = "manual"
        self.unb = "fake-user"
        self.last_login_at = None
        self.updated_at = None

    @property
    def status(self):
        return self._status

    @status.setter
    def status(self, value):
        self._status = value
        self.writes.append(value)


def _load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _assert_no_secrets(metadata: dict) -> None:
    encoded = json.dumps(metadata, ensure_ascii=False)
    assert FAKE_COOKIE not in encoded
    assert FAKE_PASSWORD not in encoded
    assert "cookies_refresh_snapshot" not in metadata
    assert metadata["login_save_only"]["tasks_started"] is False


def test_snapshot_key_matches_cookie_refresh_module():
    from common.login_save_only import COOKIE_REFRESH_SNAPSHOT_KEY
    from common.utils.cookie_refresh import COOKIE_REFRESH_SNAPSHOT_KEY as REFRESH_KEY

    assert COOKIE_REFRESH_SNAPSHOT_KEY == REFRESH_KEY


def test_missing_setting_defaults_to_save_only():
    assert parse_login_save_only(None) is True
    assert parse_login_save_only("") is True
    assert parse_login_save_only("maybe") is True
    assert parse_login_save_only("false") is False
    assert parse_login_save_only("off") is False


def test_read_setting_uses_database_value_without_defaulting_over_false():
    async def scenario():
        enabled = await read_login_save_only(FakeSession([_Result(scalar=None)]))
        disabled = await read_login_save_only(FakeSession([_Result(scalar="false")]))
        assert enabled is True
        assert disabled is False

    asyncio.run(scenario())


def test_new_login_saves_disabled_metadata_without_secrets_or_outbound_action():
    status, reason, metadata = apply_saved_login_state(
        save_only=True,
        previous_status=None,
        previous_disable_reason=None,
        metadata={"ai_reply_settings": {"model_name": "deepseek-v4-flash"}},
    )

    assert status == "disabled"
    assert reason == LOGIN_SAVE_ONLY_DISABLE_REASON
    assert metadata["ai_reply_settings"]["model_name"] == "deepseek-v4-flash"
    assert metadata["login_save_only"]["enabled"] is True
    _assert_no_secrets(metadata)
    assert task_action_after_login(status=status, created=True) == "none"
    assert login_task_endpoint("none", "new-account") is None


def test_relogin_of_active_account_never_restores_active_status():
    account = RecordingAccount(
        "active",
        metadata={
            "cookies_refresh_snapshot": [{"name": "cookie2", "value": "old"}],
            "ai_reply_settings": {"base_url": "https://www.micuapi.ai/v1"},
        },
    )
    previous = account.status

    saved = assign_login_persistence(
        account,
        save_only=True,
        previous_status=previous,
        previous_disable_reason=None,
    )

    assert previous == "active"
    assert saved == "disabled"
    assert account.status == "disabled"
    assert account.writes[-1] == "disabled"
    assert "active" not in account.writes[1:]
    assert account.disable_reason == LOGIN_SAVE_ONLY_DISABLE_REASON
    assert account.metadata_json["ai_reply_settings"]["base_url"] == "https://www.micuapi.ai/v1"
    _assert_no_secrets(account.metadata_json)
    assert task_action_after_login(status=account.status, created=False) == "stop"


def test_relogin_keeps_existing_disabled_reason():
    status, reason, _metadata = apply_saved_login_state(
        save_only=True,
        previous_status="disabled",
        previous_disable_reason="手动禁用",
        metadata=None,
    )
    assert status == "disabled"
    assert reason == "手动禁用"
    assert task_action_after_login(status=status, created=False) == "stop"


def test_save_only_off_restores_previous_login_behavior():
    status, reason, metadata = apply_saved_login_state(
        save_only=False,
        previous_status="disabled",
        previous_disable_reason="手动禁用",
        metadata=None,
    )
    assert status == "active"
    assert reason is None
    assert metadata["login_save_only"]["tasks_started"] is True
    assert task_action_after_login(status=status, created=False) == "restart"
    assert task_action_after_login(status=status, created=True) == "start"


def test_import_ignores_sheet_active_and_enable_all_while_save_only():
    status, reason, metadata, clear_reason = resolve_imported_account_state(
        save_only=True,
        sheet_status="active",
        enable_all=True,
        previous_status="active",
        previous_disable_reason=None,
        metadata={"ai_reply_settings": {"enabled": True}},
    )
    assert status == "disabled"
    assert reason == LOGIN_SAVE_ONLY_DISABLE_REASON
    assert clear_reason is False
    assert metadata["ai_reply_settings"]["enabled"] is True

    unchanged, _reason, _metadata, clear_reason = resolve_imported_account_state(
        save_only=False,
        sheet_status="disabled",
        enable_all=False,
        previous_status="disabled",
        previous_disable_reason="手动禁用",
        metadata=None,
    )
    assert unchanged == "disabled"
    assert clear_reason is False


def test_explicit_enable_still_starts_and_disable_stops():
    assert explicit_enable_task_action(True) == "start"
    assert explicit_enable_task_action(False) == "stop"


def test_refresh_cannot_auto_enable_while_save_only():
    assert auto_enable_after_refresh_allowed(save_only=True) is False
    assert auto_enable_after_refresh_allowed(save_only=False) is True


def test_restart_only_schedules_explicitly_active_accounts():
    statuses = {
        "new-disabled": False,
        "old-relogin-disabled": False,
        "manually-enabled": True,
        "enabled-without-cookie": True,
    }
    cookies = {
        "new-disabled": FAKE_COOKIE,
        "old-relogin-disabled": FAKE_COOKIE,
        "manually-enabled": FAKE_COOKIE,
    }
    assert is_account_enabled_for_tasks("disabled") is False
    assert is_account_enabled_for_tasks("active") is True
    assert select_startup_cookie_ids(statuses) == ["manually-enabled", "enabled-without-cookie"]
    scheduled = [
        account_id
        for account_id in select_startup_cookie_ids(statuses)
        if should_schedule_startup_task(account_id, cookie_status=statuses, cookies=cookies)
    ]
    assert scheduled == ["manually-enabled"]


def test_login_endpoints_never_send_messages():
    for action in ("none", "stop", "start", "restart"):
        endpoint = login_task_endpoint(action, "account-1")
        if endpoint is not None:
            assert all(fragment not in endpoint for fragment in OUTBOUND_FRAGMENTS)
    with pytest.raises(ValueError):
        login_task_endpoint("send-message", "account-1")


def test_dispatch_save_only_does_not_call_start_restart_or_send():
    module = _load_module(
        "login_task_dispatch_under_test",
        "backend-web/app/services/login_task_dispatch.py",
    )

    class Client:
        def __init__(self):
            self.calls = []

        async def post(self, url, json=None):
            self.calls.append((url, json))
            if any(fragment in url for fragment in OUTBOUND_FRAGMENTS):
                raise AssertionError(f"登录流程发出了外部消息请求: {url}")
            return {"success": True}

    async def scenario(action):
        client = Client()
        result = await module.dispatch_login_task(
            "account-1",
            action,
            cookie=FAKE_COOKIE,
            user_id=7,
            client=client,
            base_url="http://websocket.internal",
        )
        return result, client.calls

    skipped, skipped_calls = asyncio.run(scenario("none"))
    stopped, stopped_calls = asyncio.run(scenario("stop"))
    assert skipped["skipped"] is True
    assert skipped_calls == []
    assert stopped_calls == [("http://websocket.internal/internal/accounts/account-1/stop", None)]
    assert FAKE_COOKIE not in json.dumps(stopped_calls)


def test_service_restart_lifecycle_skips_disabled_accounts():
    module = _load_module(
        "cookie_manager_lifecycle_under_test",
        "websocket/app/services/xianyu/cookie_manager_lifecycle.py",
    )

    class Probe:
        def __init__(self):
            self.cookie_status = {
                "new-disabled": False,
                "old-relogin-disabled": False,
                "manually-enabled": True,
            }
            self.cookies = {key: FAKE_COOKIE for key in self.cookie_status}
            self.user_ids = {key: 7 for key in self.cookie_status}
            self.started = []

        async def _add_cookie_async(self, cookie_id, _cookie, _user_id, restart_existing=False):
            assert restart_existing is False
            self.started.append(cookie_id)

    probe = Probe()
    asyncio.run(module.CookieManagerLifecycle.start_all_tasks(probe))
    assert probe.started == ["manually-enabled"]
    assert probe.startup_total == 1
    assert probe.startup_processed == 1

    class Row:
        def __init__(self, account_id, status):
            self.account_id = account_id
            self.cookie = FAKE_COOKIE
            self.status = status
            self.owner_id = 7
            self.auto_confirm = False

    class DbResult:
        def all(self):
            return [Row("new-disabled", "disabled"), Row("manually-enabled", "active")]

    class Db:
        async def execute(self, _stmt):
            return DbResult()

    loaded = Probe()
    loaded.cookies = {}
    loaded.cookie_status = {}
    loaded.user_ids = {}
    loaded.auto_confirm_settings = {}
    loaded.keywords = {}
    loaded._loading_account_changes = None
    asyncio.run(module.CookieManagerLifecycle.load_from_db(loaded, Db()))
    assert loaded.cookie_status == {"new-disabled": False, "manually-enabled": True}


def test_account_service_new_and_relogin_paths(monkeypatch):
    module = _load_module(
        "account_service_under_test",
        "backend-web/app/services/account_service.py",
    )

    async def allow_account(_self, _owner_id):
        return {"account_limit": None, "used_count": 0, "remaining_count": None}

    monkeypatch.setattr(module.AccountLimitService, "ensure_can_add_account", allow_account)

    async def scenario():
        new_session = FakeSession([_Result(scalar=0)])
        created = await module.AccountService(new_session).create_account(
            7,
            "new-account",
            FAKE_COOKIE,
            save_only=True,
        )
        assert created.status == "disabled"
        assert created.disable_reason == LOGIN_SAVE_ONLY_DISABLE_REASON
        assert FAKE_PASSWORD not in json.dumps(created.metadata_json, ensure_ascii=False)
        assert task_action_after_login(status=created.status, created=True) == "none"

        existing = RecordingAccount("active", metadata={"ai_reply_settings": {"enabled": False}})
        qr_session = FakeSession([_Result(items=[existing])])
        account, is_new = await module.AccountService(qr_session).upsert_account_from_qr(
            7,
            FAKE_COOKIE,
            "fake-user",
            save_only=True,
        )
        assert is_new is False
        assert account.status == "disabled"
        assert "active" not in account.writes[1:]
        assert account.cookie == FAKE_COOKIE
        assert task_action_after_login(status=account.status, created=False) == "stop"

        password_account = RecordingAccount("disabled", disable_reason="手动禁用")
        password_session = FakeSession([_Result(items=[password_account])])
        saved, created_password = await module.AccountService(password_session).upsert_account_from_password(
            7,
            "existing-account",
            "13800000000",
            FAKE_PASSWORD,
            FAKE_COOKIE,
            "fake-user",
            save_only=True,
        )
        assert created_password is False
        assert saved.status == "disabled"
        assert saved.disable_reason == "手动禁用"
        assert saved.login_password == FAKE_PASSWORD
        assert FAKE_PASSWORD not in json.dumps(saved.metadata_json, ensure_ascii=False)
        assert "active" not in saved.writes[1:]

        enabled_session = FakeSession([_Result(scalar=0)])
        enabled = await module.AccountService(enabled_session).create_account(
            7,
            "enabled-account",
            FAKE_COOKIE,
            save_only=False,
        )
        assert enabled.status == "active"
        assert enabled.disable_reason is None
        assert task_action_after_login(status=enabled.status, created=True) == "start"

    asyncio.run(scenario())


def test_entrypoints_use_save_only_guard_and_do_not_add_send_calls():
    expectations = {
        "backend-web/app/api/routes/qr_login.py": ("dispatch_login_task", "task_action_after_login"),
        "backend-web/app/api/routes/shared_scan.py": ("dispatch_login_task", "task_action_after_login"),
        "backend-web/app/services/password_login/flow.py": ("dispatch_login_task", "task_action_after_login"),
        "websocket/app/api/routes/password_login.py": ("task_action_after_login", "assign_login_persistence"),
        "backend-web/app/services/account_import_service.py": ("read_login_save_only", "resolve_imported_account_state"),
        "backend-web/app/api/routes/cookies.py": (
            "read_login_save_only",
            "task_action_after_login",
            "explicit_enable_task_action",
        ),
        "scheduler/app/services/scheduler/cookies_refresh_task.py": ("auto_enable_after_refresh_allowed",),
        "scheduler/app/services/scheduler/api_cookie_renew_task.py": ("auto_enable_after_refresh_allowed",),
        "websocket/app/services/xianyu/cookie_manager_lifecycle.py": (
            "is_account_enabled_for_tasks",
            "should_schedule_startup_task",
        ),
    }
    dispatch_source = (ROOT / "backend-web/app/services/login_task_dispatch.py").read_text(encoding="utf-8")
    assert all(fragment not in dispatch_source for fragment in OUTBOUND_FRAGMENTS)

    for relative_path, required in expectations.items():
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        missing = [name for name in required if name not in source]
        assert not missing, f"{relative_path} 缺少 {missing}"
