from tests.test_api_support import HDR, make_app, make_client


async def test_negative_limits_bounded(tmp_path):
    app = make_app(tmp_path)
    async with make_client(app) as c:
        for url in ("/api/events", "/api/runs", "/api/proposals", "/api/trades", "/api/autopsies"):
            r = await c.get(url, params={"limit": -1}, headers=HDR)
            assert r.status_code == 200, url
            assert len(r.json()) <= 1, url
    app.state.db.close()
