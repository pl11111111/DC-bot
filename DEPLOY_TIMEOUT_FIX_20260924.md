# 付款超时结案修复 — 2026-09-24

本包包含之前的付款界面、手动退款、5.01 USDT 下限更新，以及本次超时修复。无需新增环境变量或修改数据库表结构。只含生产代码和说明，不含测试脚本。

## 修复内容

普通付款超时进入 `payment_timeout`，发送原有 5 分钟关闭提醒。无人申请保留、未识别到入款且查询成功时，以同一数据库事务完成：状态改为 `expired`、释放订单名额、返还尚未消耗的预留积分、记录审计，再删除频道。频道删除失败不会使订单重新占用名额，后台会重试清理。

点击付款求助／取消关闭会进入 `payment_review` 并保留频道，自动超时流程不会结束此类订单。已识别的迟到账款同样进入核实状态。旧 `payment_review` 不会因升级被自动批量取消，须执行下方明确的管理员批量操作。

入款查询失败、不完整、匹配流水尚未完成或缺少标识时，不按“未付款”处理。新增分页读取，避免只读取前 1000 条后误判。

新超时订单和本次批量取消的订单均保留原账单及流水；账单创建后的 89 天内继续自动检查精确金额的迟到账。发现迟到账会恢复 `payment_review`、保护记录并通知管理员，不自动放款；已删除的频道不会自动重建。超过自动核对窗口或金额不符的付款仍须人工核对。订单超时／取消不等于证明未曾收到任何款项。

超时返还的积分不会在后续退款时再返还一次。若管理员批准迟到账订单继续履约，则重新扣除原来已返还的抵扣积分；余额不足时拒绝继续履约，保留待核对状态。

## 部署和单次批量取消历史 payment_review

历史数据处理仅执行一次服务器内联命令，不添加 Discord 指令或 bot 自动批量取消任务。

先备份代码及新社群／共享支付数据库，停止 bot，再将更新包按目录覆盖到 `/opt/discordbot`。保留服务停止状态执行下方命令。不要只上传 `new_trading.py`，必须同时更新包内 `utils/` 文件。

```bash
sudo systemctl stop discordbot
```

更新代码后执行：

```bash
cd /opt/discordbot
./venv/bin/python - <<'PY'
import asyncio
from utils import new_store as db
from utils.trade_timeout import close_unpaid

async def main():
    try:
        orders = await db.query("SELECT id FROM orders WHERE status='payment_review' ORDER BY created_at,id")
        for order in orders:
            try:
                state = await close_unpaid(order['id'], 801583753737797652,
                    '管理员单次批量取消历史 payment_review 订单')
                print(order['id'], state, flush=True)
            except Exception as exc:
                print(order['id'], 'SKIPPED', str(exc), flush=True)
        rows = await db.query("SELECT id,status FROM orders WHERE status='payment_review' ORDER BY created_at")
        print('仍待核对：', db.encode(rows))
    finally:
        for pool in db._pools.values():
            pool.close()
        for pool in db._pools.values():
            await pool.wait_closed()

asyncio.run(main())
PY
sudo systemctl start discordbot
```

上面的用户 ID 用于管理员操作审计；若操作者不是该用户，请替换为实际管理员 ID。命令不调用提现接口，不执行转账。

这次批量范围是新社群数据库中所有当前 `payment_review` 订单，包括当前仍保留频道的订单。逐笔检查原账单、账户入款和提现记录，无匹配资金记录且检查成功的改为 `cancelled`、返还积分并解除该交易频道保留。有入款／提现记录、未完成入款、查询失败、预留积分异常、缺少账单或已被其他流程修改的订单显示 `SKIPPED`，不强制覆盖状态。

逐笔事务提交，单笔失败不会撤销已完成的其他订单。重复执行不会重复返还已结案订单的积分；所有处理保留审计记录。不删除订单行，不清除账单或流水，不修改原论坛保留状态。

账户里金额错误的付款可能无法通过精确金额匹配，不能把没有匹配记录当作未收钱的证明。若有已知的错付资金，不应把那笔订单作为无资金订单取消，应继续管理员退款处理。

`cancelled` 表示该笔处理成功；`SKIPPED` 表示保留并需要查看原因。重启后，已取消订单的现存交易频道约 5 分钟后进入清理；已删除频道无需处理。

## 验证

113 项自动测试通过；两套独立本地 MySQL 集成测试通过，包括并发超时只结案一次、积分释放、查询失败／保留标记阻止结案、批量取消、迟到账重新核实、退款积分不重复返还，以及迟到账继续履约时扣回积分和余额不足回滚。支付接口及 Discord 在测试中均为模拟，未连接生产数据库或发起真实转账。
