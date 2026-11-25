"""effluent-bugfix-03 争用侧：两路会话同口加严，败方只记争用痕、零改。

用法: python scripts/duel_tighten_th.py [base_url] [outfall_code]

阶段一（确定性争用）：writer 持 exceed_tighten:outfall:{id} 超标锁视同已在
加严临界区，admin 同口加严须 409 + 争用痕落库 + 阈值/挂起零改；释锁后
持锁方加严始成，未结挂起同事务刷新。
阶段二（并发双请求）：双线程同口同阈并发打加严写口，恰一成一败
（败方 409 争用或 400 加严偏航），终态同源一致。
"""
from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:11628"
OUTFALL_CODE = sys.argv[2] if len(sys.argv) > 2 else "OF-E1-A"
HOLD_KEY = "EX-SM-OF-E1-A-002-COD"

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


def req(method: str, path: str, token: str | None = None, body: dict | None = None):
    r = urllib.request.Request(BASE + path, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(r, data=data, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw}


def login(user: str, password: str) -> str:
    status, data = req("POST", "/api/login", body={"username": user, "password": password})
    assert status == 200, f"login {user} failed: {status} {data}"
    return data["token"]


def outfall(token: str) -> dict:
    _, data = req("GET", "/api/outfalls", token)
    return next(x for x in data["items"] if x["outfall_code"] == OUTFALL_CODE)


def open_hold_limit(token: str) -> float | None:
    q = urllib.parse.urlencode({"status": "开放"})
    _, data = req("GET", "/api/exceeds?" + q, token)
    row = next((x for x in data["items"] if x["hold_key"] == HOLD_KEY), None)
    return float(row["limit_value"]) if row else None


def main() -> int:
    admin = login("admin", "admin123")
    writer = login("writer", "writer123")

    of = outfall(admin)
    oid = of["id"]
    l0 = float(of["cod_limit"])
    l1 = l0 - 1.0
    l2 = l1 - 1.0
    if l2 <= 0:
        print(f"现阈 {l0} 过低，无法两段加严，换排放口")
        return 2
    lock_key = f"exceed_tighten:outfall:{oid}"
    print(f"靶口 {OUTFALL_CODE}(id={oid}) 现阈 {l0} → 阶段一 {l1} → 阶段二 {l2}")

    # ---- 阶段一：确定性争用（B 持锁在临界区，A 同口加严必败） ----
    s, _ = req("POST", f"/api/locks/acquire?lock_key={lock_key}", writer)
    check("阶段一 writer 持超标锁", s == 200, f"status={s}")
    s, body = req("POST", "/api/exceeds/tighten", admin,
                  {"outfall_id": oid, "cod_limit": l1})
    check("阶段一 admin 同口加严败 409", s == 409, f"status={s} body={body}")
    check("阶段一 败方阈值零改", float(outfall(admin)["cod_limit"]) == l0)
    hl = open_hold_limit(admin)
    check("阶段一 败方挂起零改", hl is None or hl != l1, f"挂起阈={hl}")
    _, locks = req("GET", "/api/locks", admin)
    traces = [x for x in locks["contention"]
              if x["lock_key"] == lock_key and x["actor"] == "admin"]
    check("阶段一 败方只记争用痕", len(traces) >= 1, f"痕数={len(traces)}")
    s, _ = req("POST", f"/api/locks/release?lock_key={lock_key}", writer)
    check("阶段一 writer 释锁", s == 200, f"status={s}")
    s, body = req("POST", "/api/exceeds/tighten", writer,
                  {"outfall_id": oid, "cod_limit": l1})
    check("阶段一 释锁后加严成", s == 200 and body.get("reopened_holds", 0) >= 1,
          f"status={s} body={body}")
    check("阶段一 挂起同事务刷新", open_hold_limit(admin) == l1,
          f"挂起阈={open_hold_limit(admin)}")

    # ---- 阶段二：并发双请求打同口加严写口 ----
    barrier = threading.Barrier(2)
    results: dict[str, tuple[int, dict]] = {}

    def fire(tag: str, token: str) -> None:
        barrier.wait()
        results[tag] = req("POST", "/api/exceeds/tighten", token,
                           {"outfall_id": oid, "cod_limit": l2})

    t1 = threading.Thread(target=fire, args=("admin", admin))
    t2 = threading.Thread(target=fire, args=("writer", writer))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    codes = sorted(v[0] for v in results.values())
    print(f"阶段二 并发回包: { {k: v[0] for k, v in results.items()} }")
    check("阶段二 恰一成", codes.count(200) == 1, f"codes={codes}")
    loser = [v[0] for v in results.values() if v[0] != 200]
    check("阶段二 败方 409/偏航400", loser and loser[0] in (400, 409),
          f"loser={loser}")
    check("阶段二 终态阈值同源", float(outfall(admin)["cod_limit"]) == l2,
          f"现阈={outfall(admin)['cod_limit']}")
    check("阶段二 终态挂起同阈", open_hold_limit(admin) == l2,
          f"挂起阈={open_hold_limit(admin)}")

    print("=" * 60)
    if FAILURES:
        print(f"DUEL FAIL: {len(FAILURES)} 项 — {FAILURES}")
        return 1
    print("DUEL PASS: 两路同口加严，败方只记争用痕零改，终态同源")
    return 0


if __name__ == "__main__":
    sys.exit(main())
