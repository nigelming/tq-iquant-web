from httpx import ASGITransport, AsyncClient
from pathlib import Path
import pytest

from core.main import app

# SPA 回退依赖构建产物 web/dist；未构建时跳过（避免测试环境无前端导致失败）
_DIST_EXISTS = (Path(__file__).resolve().parents[3] / "web" / "dist").exists()


@pytest.mark.anyio
async def test_health():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}


@pytest.mark.anyio
@pytest.mark.skipif(not _DIST_EXISTS, reason="web/dist 未构建，跳过 SPA 回退测试")
async def test_spa_fallback_serves_index_html_for_frontend_routes():
    """前端路由（如 /dashboard）刷新 → 回退 index.html，而非 {"detail":"Not Found"}。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/dashboard")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        assert '<div id="app"></div>' in resp.text


@pytest.mark.anyio
async def test_unknown_api_path_still_returns_json_404():
    """未知 /api 路径必须保持 JSON 404，不能被 SPA 回退劫持。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/definitely-not-a-route-xyz")
        assert resp.status_code == 404
        assert resp.json() == {"detail": "Not Found"}


@pytest.mark.anyio
async def test_auto_shutdown_endpoints_return_ok():
    """心跳/关闭/退出接口在开发模式（未 arm）下安全 no-op 并返回 ok。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for method, url in [("GET", "/api/system/heartbeat"),
                            ("POST", "/api/system/heartbeat"),  # sendBeacon 固定 POST
                            ("POST", "/api/system/closing"),
                            ("POST", "/api/system/shutdown")]:
            resp = await client.request(method, url)
            assert resp.status_code == 200, (method, url)
            assert resp.json()["code"] == 0, (method, url)
