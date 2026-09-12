# R20 交接文档

> 当前部署：**v7.8.3 · 源码 + systemd（不使用 Docker）** · 更新于 2026-09-12

---

## 2026-09-12 升级到 v7.8.3（源码 + systemd 部署）

### 版本与代码
- 代码库：`/root/development/r20-quantum-trader`
- 版本：`v7.4.2` → **`v7.8.3`**（tag `770a6ea`，快进升级）
- 分支：本地分支 **`release-v7.8.3`**，HEAD = `f39748c`（含下方的调度修复）
- 远程：`origin` = `https://github.com/555cute/r20-quantum-trader.git`

### 部署方式（重要：不再使用 Docker）
上游 v7.8.3 **已移除 Dockerfile / docker-compose.yml**，本机同步改为**源码直跑 + systemd**。此前的 Docker 部署（容器内装 OKX CLI、挂载 `/root/.okx`）已废弃。

服务拓扑为**两个 systemd 单元**（符合上游 `STANDALONE.md`）：

| 单元 | 作用 | 启动命令 |
|---|---|---|
| `r20-quantum.service` | 控制面 / 前端 API（端口 8080） | `.venv/bin/python -m uvicorn r20_backend.app:app --host 0.0.0.0 --port 8080` |
| `r20-gateway.service` | Gateway worker，**独占 scheduler** 与通知投递 | `.venv/bin/python -m r20_gateway.worker` |

- 单元文件位于 `/etc/systemd/system/r20-quantum.service`、`/etc/systemd/system/r20-gateway.service`（已按本机改造：`User=root`、`WorkingDirectory=/root/development/r20-quantum-trader`、`HOME=/root`、PATH 含 `.venv/bin` 与 `/usr/local/bin`）。
- ⚠️ **不要启用 `deploy/r20-scheduler.service`**：调度权归 gateway worker，双调度会重复执行任务。同理不要运行 `r20_backend.scheduler`。
- 环境变量来自 `EnvironmentFile=/root/development/r20-quantum-trader/.env`（含 OKX / LLM 凭据，权限 0600，已被 gitignore）。

### 依赖与构建
- Python：`.venv`（Python 3.11.2），`requirements.txt` 已含 v7.8.3 锁定版本（fastapi 0.141.1 / uvicorn 0.52.4）。
- 前端：`cd frontend && npm install && npm run build` 已重新构建（Node v22）。
- OKX CLI：`/usr/local/bin/okx`（1.4.5），授权在 `/root/.okx`（HOME=/root）。

### 本机补丁（已提交）
- 上游 v7.8.3 的 `r20_gateway/scheduler.py` 仍保留旧逻辑 `... % interval_seconds < 10`，**错过 15 分钟槽位不会补跑**。
- 已重新应用本机修复：`return slot > last_slot`（错过槽位自动补跑、按槽位去重），并同步修正 `tests/test_gateway_scheduler.py`。
- 提交：`f39748c fix(scheduler): reapply local missed-slot catch-up for trader job on v7.8.3`。
- 单测：`.venv/bin/python -m unittest tests.test_gateway_scheduler` → OK。

### 备份与回滚
- 升级前完整备份：**`/root/r20-upgrade-backup-20260912/`**
  - `env.bak`、`data/`（升级前台账与 DB 快照）、`HANDOVER.md`、`deploy/`
  - `local-hotfixes.diff`（升级前全部未提交改动）
  - `stashes/stash0-pre-v7.8.3.patch`、`stashes/stash1-2026-09-06.patch`（旧 stash 补丁存档）
- 旧 `git stash` 已清理（补丁已存档，可 `git apply` 恢复）。
- 回滚到旧版：`git switch main`（旧代码）并按需从备份恢复；Docker 时代产物不再维护。

### 验收（升级后实测）
- `systemctl is-enabled/is-active r20-quantum r20-gateway` → enabled / active。
- `GET /api/v1/health` → `{"version":"7.8.3","status":"ok"}`，`simulated_trading=true`。
- `GET /`、`GET /admin/login` → HTTP 200。
- `data/trading_ledger.json` 完好（103 条，100 已平仓）；`.env` 未改动。
- journal 无 error；gateway 日志显示启动后即补跑 `trader` / `news` / `factor_library`。

### 运行环境
- OKX 预检 `scripts/r20_okx_setup.py` → **READY**，环境 **DEMO**（`R20_OKX_ENV=demo`）。
- 切切实盘需在 `/admin` 配置 LIVE 凭据，勿直接改代码默认值。

### 后续升级流程（升级到更高 tag，如 v7.9.x）
```sh
cd /root/development/r20-quantum-trader
git fetch origin --tags
git switch -c release-vX.Y.Z vX.Y.Z     # 或 git checkout vX.Y.Z
.venv/bin/pip install -r requirements.txt
cd frontend && npm install && npm run build && cd ..
# 如上游仍未修复：重新应用 scheduler 补跑修复
systemctl restart r20-quantum r20-gateway
curl -s http://127.0.0.1:8080/api/v1/health
```

---

## 历史记录

### 2026-09-06 OKX 与交易调度故障
- 原因：从宿主机 Python 服务切换到 Docker 时，容器未安装 OKX CLI，也未挂载宿主机 `/root/.okx`，容器内报 `okx: not found`。
- 影响：交易任务被调度但在执行前中止；台账为空不能直接说明没有交易，应结合任务日志、持仓和订单状态判断。
- 调度修复：交易任务不再限定每 15 分钟槽位前 10 秒，错过槽位自动补跑，并按槽位去重。（此修复已在 v7.8.3 上重新应用，见上）
- 安全：当时 `OKX_DEMO=1`，验证使用只读健康/账户查询，不主动下单。

### 2026-09-06 前台台账为空
- 数据恢复：宿主机 OKX CLI 在授权环境执行 `python3 scripts/sync_full_ledger.py` 写入台账。
- 前端修复：`TradesLedger.vue` 在历史台账为空时，从当前持仓与待成交挂单生成可见记录。
- ⚠️ 已过时：文中「以后部署统一使用 Docker Compose」的结论自 v7.8.3 起废止——改为源码 + systemd，不再使用 Docker；`r20-local.service` 等宿主机残留单元已不存在，仅保留 `r20-quantum` / `r20-gateway` 两个单元。
