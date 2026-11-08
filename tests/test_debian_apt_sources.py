"""确认三个 Python 服务镜像不再关闭 Debian 签名校验，也不再强制改写到阿里云。"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILES = (
    ROOT / "backend-web" / "Dockerfile",
    ROOT / "websocket" / "Dockerfile",
    ROOT / "scheduler" / "Dockerfile",
)


def test_python_images_keep_debian_signature_verification():
    for dockerfile in DOCKERFILES:
        text = dockerfile.read_text(encoding="utf-8")
        assert "ARG APT_MIRROR=" in text
        assert "mirrors.aliyun.com" not in text
        assert "[trusted=yes]" not in text.lower()
        assert "--allow-unauthenticated" not in text.lower()
        assert "allowinsecure" not in text.lower()
        assert "APT_MIRROR must be a hostname" in text
        assert 'Acquire::EnableSrvRecords "false";' in text
        assert "AllowInsecureRepositories" not in text
        sed_command = text.split("sed -i", 1)[1].split("RUN pip", 1)[0]
        assert "Signed-By" not in sed_command
        assert "trusted" not in sed_command.lower()
