"""回归测试：本次验收修复的安全漏洞。

1. /api/v1/scripts、/terms、/social-metadata 必须要求认证
   （修复前这三个端点无任何认证依赖，未登录即可调用并真实生成脚本/术语/元数据）。
2. /api/v1/auth/register 响应体不得泄露明文密码
   （修复前 register 返回体含 "password": body.password 字段）。
"""
from fastapi.testclient import TestClient

from app.asgi import app


def test_scripts_requires_auth():
    client = TestClient(app)
    resp = client.post(
        "/api/v1/scripts",
        json={
            "video_subject": "regression test",
            "video_language": "en",
            "paragraph_number": 1,
        },
    )
    # 修复前返回 200 并真实生成脚本；修复后必须 401
    assert resp.status_code == 401, f"expected 401, got {resp.status_code}"


def test_terms_requires_auth():
    client = TestClient(app)
    resp = client.post(
        "/api/v1/terms",
        json={"video_subject": "regression test", "video_script": "s", "amount": 1},
    )
    assert resp.status_code == 401, f"expected 401, got {resp.status_code}"


def test_social_metadata_requires_auth():
    client = TestClient(app)
    resp = client.post(
        "/api/v1/social-metadata",
        json={
            "video_subject": "regression test",
            "video_script": "s",
            "language": "en",
            "platform": "youtube",
        },
    )
    assert resp.status_code == 401, f"expected 401, got {resp.status_code}"


def test_register_response_does_not_leak_password():
    """register 源码层回归：响应体不得再返回 password 字段。

    创建用户时的 `password=body.password` 属正常入参（存入 DB 前的赋值），
    只有响应字典里出现 `"password": body.password,` 才是泄露。
    """
    from pathlib import Path

    src = Path("app/auth/router.py").read_text(encoding="utf-8")
    assert '"password": body.password,' not in src
