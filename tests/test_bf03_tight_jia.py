"""effluent-bugfix-03 甲：加严须同事务重开未结挂起并改写挂起阈，否则整笔回退。

复现夹具（种子）：SM-OF-E1-A-002 持开放挂起 EX-SM-OF-E1-A-002-COD（旧阈 50），
排放口 OF-E1-A 现阈 50。加严至 40 后，未结挂起正式阈值必须刷新为 40；
新阈未严于旧阈属加严偏航，须记偏航并拒写。
"""
import pytest
from fastapi.testclient import TestClient

from app import app
from db import init_db

OUTFALL_CODE = "OF-E1-A"
HOLD_KEY = "EX-SM-OF-E1-A-002-COD"
OLD_LIMIT = 50.0
NEW_LIMIT = 40.0


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


def test_tighten_reopens_open_hold_same_tx():
    """甲：加严后未结挂起正式阈值刷新为新阈（同事务重开，否则整笔回退）。"""
    c = TestClient(app)
    h = _auth(c)
    of = _outfall(c, h)
    assert of["cod_limit"] == OLD_LIMIT
    hold = _open_hold(c, h)
    assert hold["limit_value"] == OLD_LIMIT

    r = c.post(
        "/api/exceeds/tighten",
        headers=h,
        json={"outfall_id": of["id"], "cod_limit": NEW_LIMIT},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["reopened_holds"] >= 1  # 埋雷版恒为 0 → 红

    hold2 = _open_hold(c, h)
    assert hold2["status"] == "开放"
    assert hold2["limit_value"] == NEW_LIMIT  # 埋雷版仍旧阈 50 → 红
    assert "加严重开" in (hold2["note"] or "")

    of2 = _outfall(c, h)
    assert of2["cod_limit"] == NEW_LIMIT


def test_tighten_deviation_refused():
    """偏航拒写：新阈未严于旧阈 → 记加严偏航并拒写，阈值与挂起零改。"""
    c = TestClient(app)
    h = _auth(c)
    of = _outfall(c, h)
    r = c.post(
        "/api/exceeds/tighten",
        headers=h,
        json={"outfall_id": of["id"], "cod_limit": 60.0},
    )
    assert r.status_code == 400  # 埋雷版照单全收 → 红
    assert "加严偏航" in r.json()["detail"]
    assert _outfall(c, h)["cod_limit"] == OLD_LIMIT
    assert _open_hold(c, h)["limit_value"] == OLD_LIMIT
    audit = c.get("/api/audit", headers=h).json()["items"]
    assert any(
        a["action"] == "tighten_deviation" and "加严偏航" in (a["payload"] or "")
        for a in audit
    )
