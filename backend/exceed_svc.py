"""超标旁路挂起开闭。"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from locks import release, require_lock
from models import ExceedCloseIn, ExceedOpenIn

router = APIRouter(tags=["exceed"])


@router.get("/api/exceeds")
def list_exceeds(authorization: AuthHeader = None, status: str | None = None):
    require_user(authorization)
    with connect() as conn:
        q = """SELECT h.*, s.sample_code, o.outfall_code,
                      o.cod_limit AS outfall_cod_limit
               FROM exceed_holds h
               JOIN sample_batches s ON s.id=h.sample_id
               JOIN outfalls o ON o.id=s.outfall_id"""
        if status:
            rows = conn.execute(
                q + " WHERE h.status=? ORDER BY h.id DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute(q + " ORDER BY h.id DESC").fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/exceeds/open")
def open_exceed(body: ExceedOpenIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    lock_key = f"sample:{body.sample_id}"
    with connect() as conn:
        require_lock(conn, lock_key, user)
        try:
            sample = conn.execute(
                """SELECT s.*, o.cod_limit, o.nh3n_limit, o.tp_limit
                   FROM sample_batches s JOIN outfalls o ON o.id=s.outfall_id
                   WHERE s.id=?""",
                (body.sample_id,),
            ).fetchone()
            if not sample:
                raise HTTPException(404, "采样批次不存在")
            if sample["status"] == "作废":
                raise HTTPException(400, "已作废不可挂起")
            open_row = conn.execute(
                "SELECT id FROM exceed_holds WHERE sample_id=? AND status='开放'",
                (body.sample_id,),
            ).fetchone()
            if open_row:
                raise HTTPException(400, "已有开放挂起")
            import json

            metrics = json.loads(sample["metrics"] or "{}")
            from validators import adjudicate_exceed

            exceeded, metric, measured, limit = adjudicate_exceed(
                metrics,
                {
                    "cod": sample["cod_limit"],
                    "nh3n": sample["nh3n_limit"],
                    "tp": sample["tp_limit"],
                },
            )
            if not exceeded:
                raise HTTPException(400, "当前指标未超标，无需挂起")
            hold_key = f"EX-{sample['sample_code']}-{metric}-M"
            conn.execute(
                """INSERT INTO exceed_holds(sample_id,hold_key,exceed_metric,measured,
                   limit_value,status,opened_at,note) VALUES (?,?,?,?,?,?,?,?)""",
                (
                    body.sample_id,
                    hold_key,
                    metric,
                    measured,
                    limit,
                    "开放",
                    now_iso(),
                    body.note,
                ),
            )
            conn.execute(
                "UPDATE sample_batches SET status=? WHERE id=?",
                ("旁路挂起", body.sample_id),
            )
            write_audit(
                conn,
                user,
                "exceed_open",
                sample["sample_code"],
                {"hold_key": hold_key, "metric": metric},
            )
            conn.commit()
            return {"hold_key": hold_key, "metric": metric}
        finally:
            release(conn, lock_key, user)
            conn.commit()


@router.post("/api/exceeds/close")
def close_exceed(body: ExceedCloseIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        hold = conn.execute(
            "SELECT * FROM exceed_holds WHERE id=?", (body.hold_id,)
        ).fetchone()
        if not hold:
            raise HTTPException(404, "挂起不存在")
        if hold["status"] != "开放":
            raise HTTPException(400, "挂起已关闭")
        lock_key = f"sample:{hold['sample_id']}"
        require_lock(conn, lock_key, user)
        try:
            conn.execute(
                """UPDATE exceed_holds SET status='已关闭', closed_at=?, note=?
                   WHERE id=?""",
                (now_iso(), body.note or hold["note"], body.hold_id),
            )
            conn.execute(
                """UPDATE sample_batches SET status='已裁定', adjudicated_at=?
                   WHERE id=?""",
                (now_iso(), hold["sample_id"]),
            )
            sample = conn.execute(
                "SELECT sample_code FROM sample_batches WHERE id=?",
                (hold["sample_id"],),
            ).fetchone()
            write_audit(
                conn,
                user,
                "exceed_close",
                sample["sample_code"] if sample else str(hold["sample_id"]),
                {"hold_id": body.hold_id, "note": body.note},
            )
            conn.commit()
            return {"ok": True, "ticket_adjudicated": True, "sample_adjudicated": True}
        finally:
            release(conn, lock_key, user)
            conn.commit()


@router.post("/api/exceeds/tighten")
def tighten_threshold(body: dict, authorization: AuthHeader = None):
    """阈值加严（甲路径）：同事务重开未结挂起并改写挂起阈，否则整笔回退。

    口径：新阈须严于旧阈，偏离记「加严偏航」拒写；同口并发抢超标锁
    （exceed_tighten:outfall:{id}），败方只记争用痕、零改。
    """
    user = require_writer(authorization)
    outfall_id = int(body.get("outfall_id") or 0)
    cod = float(body.get("cod_limit") or 0)
    lock_key = f"exceed_tighten:outfall:{outfall_id}"
    with connect() as conn:
        if not conn.execute(
            "SELECT id FROM outfalls WHERE id=?", (outfall_id,)
        ).fetchone():
            raise HTTPException(404, "排放口不存在")
        if cod <= 0:
            raise HTTPException(400, "加严阈值非法")
        try:
            require_lock(conn, lock_key, user)
        except HTTPException:
            conn.commit()  # 败方只记争用痕：落痕后放行 409，业务零改
            raise
        except sqlite3.IntegrityError:
            # 锁行在 SELECT 与 INSERT 之间被并发方插入：视同争用败方
            conn.rollback()
            row = conn.execute(
                "SELECT holder FROM effluent_locks WHERE lock_key=?", (lock_key,)
            ).fetchone()
            conn.execute(
                """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
                   VALUES (?,?,?,?,?)""",
                (
                    lock_key,
                    user,
                    "争用失败",
                    f"持有人={row['holder'] if row else '?'}",
                    now_iso(),
                ),
            )
            conn.commit()
            raise HTTPException(409, f"锁被占用：{lock_key}")
        try:
            # 锁内重读现阈：并发请求被序列化后须按最新阈判定偏航
            outfall = conn.execute(
                "SELECT * FROM outfalls WHERE id=?", (outfall_id,)
            ).fetchone()
            old = float(outfall["cod_limit"])
            if cod >= old:
                write_audit(
                    conn,
                    user,
                    "tighten_deviation",
                    outfall["outfall_code"],
                    {
                        "old_limit": old,
                        "new_limit": cod,
                        "reason": "加严偏航：新阈未严于旧阈",
                    },
                )
                conn.commit()
                raise HTTPException(400, "加严偏航：新阈须严于旧阈，拒写")
            conn.execute(
                "UPDATE outfalls SET cod_limit=? WHERE id=?", (cod, outfall_id)
            )
            open_holds = conn.execute(
                """SELECT h.id FROM exceed_holds h
                   JOIN sample_batches s ON s.id=h.sample_id
                   WHERE s.outfall_id=? AND h.status='开放' AND h.exceed_metric='COD'""",
                (outfall_id,),
            ).fetchall()
            for hold in open_holds:
                conn.execute(
                    """UPDATE exceed_holds
                       SET limit_value=?, opened_at=?,
                           note=trim(COALESCE(note,'') || ' 加严重开')
                       WHERE id=?""",
                    (cod, now_iso(), hold["id"]),
                )
            write_audit(
                conn,
                user,
                "tighten",
                outfall["outfall_code"],
                {
                    "cod_limit": cod,
                    "old_limit": old,
                    "reopened_holds": len(open_holds),
                },
            )
            conn.commit()
        except Exception:
            conn.rollback()  # 整笔回退：阈值与挂起重开同生共死
            raise
        finally:
            release(conn, lock_key, user)
            conn.commit()
        return {
            "ok": True,
            "outfall_id": outfall_id,
            "cod_limit": cod,
            "old_limit": old,
            "reopened_holds": len(open_holds),
        }
