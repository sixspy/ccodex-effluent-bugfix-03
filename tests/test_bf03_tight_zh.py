"""effluent-bugfix-03 争用：两路会话同口加严须抢超标锁，败方只记争用痕、零改。

复现夹具（种子）：writer 持 exceed_tighten:outfall:{id} 超标锁（模拟在加严临界区），
admin 同口加严必须 409、落争用痕、阈值与挂起零改；释锁后持锁方加严始成。
"""
import pytest
from fastapi.testclient import TestClient

from app import app
from db import init_db

OUTFALL_CODE = "OF-E1-A"
HOLD_KEY = "EX-SM-OF-E1-A-002-COD"
OLD_LIMIT = 50.0
NEW_LIMIT = 40.0
LOCK_KEY_TMPL = "exceed_tighten:outfall:{}"


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user="admin", password="admin123") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": password}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _outfall(c: TestClient, h: dict) -> dict:
    items = c.get("/api/outfalls", headers=h).json()["items"]
    return next(x for x in items if x["outfall_code"] == OUTFALL_CODE)


def _open_hold(c: TestClient, h: dict) -> dict:
    items = c.get("/api/exceeds?status=开放", headers=h).json()["items"]
    return next(x for x in items if x["hold_key"] == HOLD_KEY)


def test_duel_tighten_loser_zero_change():
    """争用：同口加严败方 409 + 争用痕 + 零改；胜方落阈并同事务重开挂起。"""
    c = TestClient(app)
    admin = _auth(c, "admin", "admin123")
    writer = _auth(c, "writer", "writer123")
    of = _outfall(c, admin)
    lock_key = LOCK_KEY_TMPL.format(of["id"])

    # 会话 B（writer）持超标锁，视同已在加严临界区
    r = c.post(f"/api/locks/acquire?lock_key={lock_key}", headers=writer)
    assert r.status_code == 200
    try:
        # 会话 A（admin）同口加严 → 必须败
        r = c.post(
            "/api/exceeds/tighten",
            headers=admin,
            json={"outfall_id": of["id"], "cod_limit": NEW_LIMIT},
        )
        assert r.status_code == 409  # 埋雷版无锁 → 200 → 红

        # 败方零改：阈值与未结挂起都不动
        assert _outfall(c, admin)["cod_limit"] == OLD_LIMIT
        assert _open_hold(c, admin)["limit_value"] == OLD_LIMIT

        # 败方只记争用痕
        locks = c.get("/api/locks", headers=admin).json()
        traces = [
            x
            for x in locks["contention"]
            if x["lock_key"] == lock_key and x["actor"] == "admin"
        ]
        assert traces, "争用痕未落库"  # 埋雷版无锁亦无痕 → 红
    finally:
        c.post(f"/api/locks/release?lock_key={lock_key}", headers=writer)

    # 释锁后同口加严 → 成，未结挂起同事务刷新
    r = c.post(
        "/api/exceeds/tighten",
        headers=writer,
        json={"outfall_id": of["id"], "cod_limit": NEW_LIMIT},
    )
    assert r.status_code == 200
    assert r.json()["reopened_holds"] >= 1
    assert _open_hold(c, admin)["limit_value"] == NEW_LIMIT
