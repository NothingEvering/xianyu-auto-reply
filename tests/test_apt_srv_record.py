"""用 legacy builder 和显式网络复现 apt 的 SRV 失败。

默认 ``APT_SRV_DOCKER_NETWORK=host``，对应获准的
``DOCKER_BUILDKIT=0 docker build --network=host``。不使用 Docker 默认网桥，
也不传入 ``--dns``。SRV 目标只在这个构建进程里被转到本地高端口的 500 服务，
不修改 daemon、主机 DNS 或安全规则。

urllib 对 deb.debian.org 返回 200 只作为对照。通过条件是默认 apt-get update
失败并指向该本地端口，且 ``Acquire::EnableSrvRecords=false`` 之后 apt-get update
成功，InRelease 仍以 PGP 签名开头。
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PRELOAD = ROOT / "tests" / "apt_srv_repro" / "preload.c"
IMAGE = os.environ.get("APT_SRV_TEST_IMAGE", "python:3.11-slim")


def _docker() -> list[str]:
    candidates = [["docker"], ["sudo", "-n", "docker"]]
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
    raise AssertionError("docker is required for the apt SRV build test and was not usable")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_apt_srv_record_failure_is_not_fixed_by_urllib_success(tmp_path: Path):
    docker = _docker()
    network = os.environ.get("APT_SRV_DOCKER_NETWORK", "host").strip() or "host"
    port = _free_port()
    shutil.copy(PRELOAD, tmp_path / "preload.c")
    (tmp_path / "repro.sh").write_text(
        textwrap.dedent(
            f"""\
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
            ThreadingHTTPServer(("127.0.0.1", {port}), Handler).serve_forever()
            PY
            sleep 0.4
            env -u LD_PRELOAD python3 - <<'PY'
            import socket
            import urllib.request
            infos = socket.getaddrinfo("deb.debian.org", 80, socket.AF_INET, socket.SOCK_STREAM)
            ip = infos[0][4][0]
            if ip.startswith("198.18."):
                raise SystemExit(f"direct A record is the relay: {{ip}}")
            request = urllib.request.Request(
                f"http://{{ip}}/debian/dists/trixie/InRelease",
                headers={{"Host": "deb.debian.org"}},
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read(40)
                if response.status != 200 or not body.startswith(b"-----BEGIN PGP SIGNED MESSAGE-----"):
                    raise SystemExit(f"urllib control request failed: {{response.status}} {{body!r}}")
            print("URLLIB_DIRECT_OK", ip)
            PY
            set +e
            LD_PRELOAD=/opt/apt-srv-preload.so APT_SRV_RELAY_PORT={port} apt-get update >/tmp/srv-on.log 2>&1
            srv_on=$?
            set -e
            if [ "$srv_on" -eq 0 ] || ! grep -F -q "500  Internal Server Error [IP: 127.0.0.1 {port}]" /tmp/srv-on.log; then
                echo "expected apt SRV path to fail against the local 500 relay" >&2
                cat /tmp/srv-on.log >&2
                exit 1
            fi
            env -u LD_PRELOAD apt-get update -o Acquire::EnableSrvRecords=false >/tmp/srv-off.log 2>&1
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
        ),
        encoding="utf-8",
    )
    (tmp_path / "Dockerfile").write_text(
        textwrap.dedent(
            f"""\
            FROM {IMAGE}
            COPY preload.c /tmp/preload.c
            COPY repro.sh /tmp/repro.sh
            RUN printf '%s\\n' 'Acquire::EnableSrvRecords "false";' > /etc/apt/apt.conf.d/99disable-srv-records \\
             && apt-get update \\
             && apt-get install -y --no-install-recommends gcc libc6-dev \\
             && gcc -shared -fPIC -ldl -o /opt/apt-srv-preload.so /tmp/preload.c \\
             && rm -f /etc/apt/apt.conf.d/99disable-srv-records \\
             && rm -rf /var/lib/apt/lists/* \\
             && bash /tmp/repro.sh
            """
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            *docker,
            "build",
            "--network",
            network,
            "--no-cache",
            "-f",
            str(tmp_path / "Dockerfile"),
            "-t",
            "apt-srv-repro:temporary",
            str(tmp_path),
        ],
        env={**os.environ, "DOCKER_BUILDKIT": "0"},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.returncode != 0 or "URLLIB_DIRECT_OK" not in result.stdout or "APT_DIRECT_SIGNED_OK" not in result.stdout:
        raise AssertionError(result.stdout[-8000:])
    subprocess.run(
        [*docker, "rmi", "-f", "apt-srv-repro:temporary"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
