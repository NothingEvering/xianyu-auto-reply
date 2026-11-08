"""复现 apt 走 SRV 失败、关闭 SRV 后 apt-get update 成功。

urllib 直接请求 deb.debian.org 返回 200 不能代替 apt。本测试让 SRV 目标
debian.map.fastlydns.net 指向只返回 HTTP 500 的本地端口，确认：

- urllib 仍然从 deb.debian.org 取到 PGP InRelease
- 默认 apt-get update 失败，错误指向该 SRV 地址的 500
- Acquire::EnableSrvRecords=false 后 apt-get update 成功，且 InRelease 仍是 PGP 签名文件

不修改 AllowInsecureRepositories，不使用 trusted=yes。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import textwrap

import pytest

IMAGE = os.environ.get("APT_SRV_TEST_IMAGE", "python:3.11-slim")


def _docker_base() -> list[str] | None:
    candidates = [
        ["docker"],
        ["sudo", "-n", "docker"],
    ]
    for command in candidates:
        if command[0] == "sudo" and shutil.which("sudo") is None:
            continue
        if command[0] == "docker" and shutil.which("docker") is None:
            continue
        probe = subprocess.run(
            [*command, "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if probe.returncode == 0:
            return command
    return None


def test_apt_srv_record_failure_is_not_fixed_by_urllib_success():
    docker = _docker_base()
    if docker is None:
        pytest.skip("docker is not available to run the apt SRV reproduction")

    script = textwrap.dedent(
        """\
        set -eu
        python3 - <<'PY' &
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b"srv-relay-500")
            def log_message(self, fmt, *args):
                return
        ThreadingHTTPServer(("127.0.0.1", 80), Handler).serve_forever()
        PY
        sleep 0.4
        python3 - <<'PY'
        import urllib.request
        url = "http://deb.debian.org/debian/dists/trixie/InRelease"
        with urllib.request.urlopen(url, timeout=30) as response:
            body = response.read(40)
            if response.status != 200 or not body.startswith(b"-----BEGIN PGP SIGNED MESSAGE-----"):
                raise SystemExit(f"urllib control request failed: {response.status} {body!r}")
        print("URLLIB_DIRECT_OK")
        PY
        set +e
        apt-get update >/tmp/srv-on.log 2>&1
        srv_on=$?
        set -e
        if [ "$srv_on" -eq 0 ] || ! grep -F -q "500  Internal Server Error [IP: 127.0.0.1 80]" /tmp/srv-on.log; then
            echo "expected apt SRV path to fail against the local 500 relay" >&2
            cat /tmp/srv-on.log >&2
            exit 1
        fi
        apt-get update -o Acquire::EnableSrvRecords=false >/tmp/srv-off.log 2>&1
        python3 - <<'PY'
        from pathlib import Path
        path = Path("/var/lib/apt/lists/deb.debian.org_debian_dists_trixie_InRelease")
        body = path.read_bytes()
        if not body.startswith(b"-----BEGIN PGP SIGNED MESSAGE-----"):
            raise SystemExit("apt downloaded InRelease without a PGP header")
        if b"srv-relay-500" in body:
            raise SystemExit("apt stored the SRV relay error body")
        print("APT_DIRECT_SIGNED_OK", len(body))
        PY
        if grep -R -F -q 'AllowInsecureRepositories "true"' /etc/apt /tmp/srv-off.log; then
            echo "signature verification was disabled" >&2
            exit 1
        fi
        """
    )
    result = subprocess.run(
        [
            *docker,
            "run",
            "--rm",
            "--dns",
            "8.8.8.8",
            "--add-host",
            "debian.map.fastlydns.net:127.0.0.1",
            IMAGE,
            "bash",
            "-lc",
            script,
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stdout)
    assert "URLLIB_DIRECT_OK" in result.stdout
    assert "APT_DIRECT_SIGNED_OK" in result.stdout
