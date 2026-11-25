"""effluent-bugfix-03 乙：对账与挂起须同阈，禁止对账新阈/挂起旧阈分裂。

复现夹具（种子）：加严 OF-E1-A 至 40 后，对账页 /api/reconcile/DP-2026-E01 中
SM-OF-E1-A-002 的生效阈与开放挂起阈必须同源同值；超标页挂起阈与对账生效阈一致。
"""
import pytest
from fastapi.testclient import TestClient

from app import app
from db import init_db

OUTFALL_CODE = "OF-E1-A"
HOLD_KEY = "EX-SM-OF-E1-A-002-COD"
SAMPLE_CODE = "SM-OF-E1-A-002"
PERMIT_CODE = "DP-2026-E01"
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


def test_reconcile_and_hold_same_threshold():
    """乙：加严后对账生效阈与开放挂起阈同源，不得分裂。"""
    c = TestClient(app)
    h = _auth(c)
    of = next(
        x
        for x in c.get("/api/outfalls", headers=h).json()["items"]
        if x["outfall_code"] == OUTFALL_CODE
    )
    r = c.post(
        "/api/exceeds/tighten",
        headers=h,
        json={"outfall_id": of["id"], "cod_limit": NEW_LIMIT},
    )
    assert r.status_code == 200

    recon = c.get(f"/api/reconcile/{PERMIT_CODE}", headers=h).json()
    assert recon["threshold_aligned"] is True  # 埋雷版无此口径 → 红
    entry = next(s for s in recon["samples"] if s["sample_code"] == SAMPLE_CODE)
    assert entry["outfall_limits"]["cod"] == NEW_LIMIT  # 对账按新阈
    assert entry["open_hold_limit"] == NEW_LIMIT  # 挂起仍旧阈 50 → 分裂 → 红
    assert entry["threshold_aligned"] is True

    # 五页同源：超标页挂起阈 == 对账生效阈
    holds = c.get("/api/exceeds?status=开放", headers=h).json()["items"]
    hold = next(x for x in holds if x["hold_key"] == HOLD_KEY)
    assert hold["limit_value"] == entry["outfall_limits"]["cod"]
