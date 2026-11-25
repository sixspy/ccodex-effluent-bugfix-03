# effluent-bugfix-03 加严修复说明（2026-11-25 封存）

## 缺陷锚

`backend/exceed_svc.py` 加严写口 `POST /api/exceeds/tighten` 只改阈值表
（`UPDATE outfalls.cod_limit`），不重开未结挂起、不接锁、不校验偏航：
未结挂起仍按旧阈值亮可闭环，超标页（`exceed_holds.limit_value`）仍旧阈，
而对账/排放口/采样已按新阈（`outfalls.cod_limit`），五页阈值分裂。

## 修复口径（甲路径）

加严阈值落库与未结挂起重开两边不得混用，取甲路径：**加严须同事务重开
未结挂起并改写挂起阈，否则整笔回退**。

- `exceed_svc.py::tighten_threshold`
  - 同口并发抢超标锁 `exceed_tighten:outfall:{id}`；败方 409，只记
    `contention_log` 争用痕（落痕先于异常抛出，不被回滚吞掉），业务零改。
  - 锁内重读现阈；新阈未严于旧阈即「加严偏航」，记
    `audit_trail(action=tighten_deviation)` 并拒写（400）。
  - 同事务：`UPDATE outfalls` 落新阈 + 逐条重开该口开放 COD 挂起
    （改写 `limit_value`、重戳 `opened_at`、注记「加严重开」）；
    任一步失败 `rollback` 整笔回退，阈值与挂起重开同生共死。
  - `GET /api/exceeds` 增 `outfall_cod_limit` 列，超标页挂起阈/当前口阈同源。
- `reconcile_svc.py::_reconcile_permit`
  - 每采样增 `outfall_limits` / `open_hold_metric` / `open_hold_limit` /
    `threshold_aligned`，许可级增 `threshold_aligned`，对账与挂起同阈可证。
- 页面：`exceed_view.js` 增加严面板与挂起阈/当前口阈同源列；
  `reconcile_view.js` 增阈值同源列；`api.js`/`app.js` 接 `tighten`。

## 禁止项遵守

未旁门只改阈值表；未只改超标页阈值标签；偏离（新阈不严于旧阈）记
加严偏航拒写；未见红前未动服务层（三红先落 `tests/` 见红后才改）。

## 复现与验证

- 复现夹具：种子 `SM-OF-E1-A-002` 持开放挂起 `EX-SM-OF-E1-A-002-COD`
  （旧阈 50），加严 OF-E1-A 至 40。
- 三红（分文件，已见红）：
  - `tests/test_bf03_tight_jia.py` 甲：未结挂起正式阈值未刷新
    （`reopened_holds` 0≥1 红；偏航 200≠400 红）。
  - `tests/test_bf03_tight_yi.py` 乙：对账新阈与挂起旧阈分裂
    （`threshold_aligned` KeyError 红）。
  - `tests/test_bf03_tight_zh.py` 争用：同口加严无锁（200≠409 红）。
- 三绿：`pytest` 全量 10 passed。
- 争用侧：`scripts/duel_tighten_th.py` 两路会话同口加严，败方 409 +
  争用痕 + 零改；并发双请求恰一成一败，终态同源。
- 五页同源依次浏览：超标页 → 对账页 → 采样页 → 准排页 → 排放口页，
  加严后同阈 40；准排页仍受开放挂起约束（开放挂起禁止准排）。

## 验收对照

- 加严后未结挂起阈值刷新 ✓（同事务重开，挂起阈=新阈）
- 对账与超标页同阈 ✓（`threshold_aligned`，挂起阈==对账生效阈）
- 准排页仍受开放挂起约束 ✓（`release_svc` 开放挂起禁止准排，回归测试守住）
- 双会话同口加严败方零改 ✓（409 + 争用痕，阈值/挂起不动）
