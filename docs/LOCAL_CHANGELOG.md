# R20 本地改动记录

## 2026-09-25

### Jev 独立决策影子层

- 将 Jev 影子复核拆为两个隔离请求：独立市场/持仓判断与主脑提案审计。独立 state 使用白名单组装，不包含主脑动作、理由、置信度、入场、止盈或止损字段。
- 在 `ai_brain_trader.py` 内增加纯策略合并逻辑，在本地合并 `AGREE`、`WAIT_VS_ENTRY`、`OPPOSITE_DIRECTION`、`MAIN_WAIT_JEV_ENTRY`、`ABSTAIN`、`AUDIT_REJECT` 等关系；第一阶段有效执行模式固定为 `SHADOW`，不改变主脑下单、平仓或撤单。
- 记录 `schema_version=3`、独立/审计 state hash、双通道 request id、耗时、重试、审计旗标和通道状态；审计通道失败明确标记为 `audit_unavailable`，不伪装为批准。
- 增加 `R20_JEV_INDEPENDENT_ENABLED`、`R20_JEV_ENFORCEMENT`、独立置信度/动作边际和审计置信度配置。

## 2026-09-24

本日改动均属于 `r20-account-isolation` 工作区的本地修复与隔离适配。下一次同步原作者代码时，必须逐项对照上游实现：

- 上游已覆盖且行为一致的改动：删除本地重复实现；
- 上游未覆盖但仍是本项目部署或账户隔离所必需的改动：保留并重新验证；
- 仅为当前基线测试、部署路径或临时兼容而存在的改动：视情况移除，不直接长期固化。

### 账户隔离与运行环境

- 增加账户命名空间与台账筛选逻辑，避免未命名预留混入账户数据。
- 统一遵守 `R20_DATA_DIR` 沙箱/独立实例重定向契约，覆盖台账、自进化和运行时 scope 校验路径。
- 将环境校验从纯解析路径中剥离，避免显式传值时产生生产磁盘读取副作用。
- 本地 systemd、启动脚本和调度入口统一使用 `r20-account-isolation` 的虚拟环境与工作目录。

### 交易观测与快照

- 统一 brain 与 trader 的 `range_4h_*` 口径：使用已收盘的 4H K 线，避免同名指标数值不一致。
- 调整周期执行顺序，在 tracker 入场快照写入前完成 `observe_cycle`，补齐 `direction_observation`。
- 快照版本字段改为引用 `direction_observation` 的版本常量。
- 简化 `trend_4h` 分支，并对缺少账户环境标签的台账行计数告警而不是静默丢弃。

### 挂单生命周期与杠杆

- 增加有界的 AI KEEP 挂单租约：KEEP 只延长有限窗口，CANCEL 成功后清理租约，避免挂单无限期保留。
- 增加新孤儿挂单宽限、部分成交剩余单时限和最长挂单时限，撤单失败保持 fail-closed。
- 修正执行层杠杆对齐逻辑，使决策、下单和风险配置共同遵守当前 `R20_MAX_LEVERAGE`，目标配置为 10x 时不再被旧 3x/4x 档位覆盖。

### 可观测性与任务记录

- 将 `observe_cycle` 的输出压缩为必要摘要，减少 `job_runs.detail` 被重复观测行挤满的情况。
- 子进程诊断统一合并 stdout 与 stderr，避免非零退出或 SIGTERM 时只读取 stderr 导致诊断为空。
- 调度器 trader 槽位不再依赖过窄的 10 秒窗口，降低错过周期的概率。
- 接入 Jev TypeSafe 影子复核：主脑完成决策后只读复核，结果写入
  `data/jev_shadow_reviews.jsonl`，不修改 `ai_brain_decisions.json`，不参与放行、拦截或撤单。
- 增加 Typesafe 直连适配：配置 `R20_JEV_TYPESAFE_API_KEY` 或 `TYPESAFE_API_KEY` 后自动使用
  `https://api.typesafe.ai/v1/systemone` 与 `jev-latest`，将 `boolean/probability` 内部协议转换为
  Typesafe 的 `noul/noul` 协议；未配置直连 Key 时继续使用 Vercel Gateway，避免现有 Key 失效。
  API Key 仅从 `R20_JEV_API_KEY` / `AI_GATEWAY_API_KEY` 环境变量读取。
- 影子记录默认只保留最近 7 天，并以 1000 条为硬上限；清理与追加在同一文件锁内原子完成，
  可通过 `R20_JEV_SHADOW_RETENTION_DAYS` / `R20_JEV_SHADOW_MAX_RECORDS` 调整。
- 为每轮主脑决策增加 `cycle_id` / `decision_id`，并把标识传入方向观测、tracker 和入场信号日记，
  让影子复核可以按合约与决策轮次回溯。
- Jev 增加完整审查上下文：在保留独立盲审输入的同时，传入主脑提案、账户可用余额、挂单、杠杆、保证金、名义价值、强平价、开仓时间、资金费/已实现盈亏字段及来源标记；
  活动 OKX 持仓额外读取实时条件单，记录保护单明细、覆盖比例和 `fully/partially/unprotected/unknown` 状态；
  完整审查仍为影子结果，不参与执行，缺失字段保持显式缺失而不编造。
  保护单只读查询使用 2.5 秒专用超时，避免 Jev 数据补全拖慢主脑后的持仓管理。
- Jev 影子请求增加逐合约结构化复核：保留整轮健康度，同时记录每个候选的
  `data_valid`、`direction_consistent`、`execution_ready` 和建议动作。
- 对 Jev 影子调用增加 408/425/429/5xx 的有限退避重试，并记录每次尝试状态；新增
  `data/jev_shadow_evaluation.jsonl` 作为 60 天结构化评估台账，避免 7 天原始日志清理后无法完成 20-30 笔交易评估。
- Jev 动作复核采用三个布尔问题（多/空/等待）并由本地按概率归一化建议动作，兼容
  `evaluate` 接口不接受字符串问题类型的实际行为。
- 平仓台账写入时从当前 tracker 自动补入 `decision_id/cycle_id`，形成影子复核到实际盈亏的可关联链路。
- 影子复核补入活动持仓与 `position_management`：逐仓记录主脑的 `HOLD`、`CLOSE_MARKET`、
  `UPDATE_SL` 指令、Jev 的独立动作票和保护价一致性，不再只覆盖开仓候选。
- 新增 `data/jev_shadow_position_outcomes.jsonl`：记录 Jev 复核时的假设立即平仓净收益，
  并在后续周期补齐下一轮收益、4 小时收益、实际平仓收益、最大有利/不利波动；手续费、滑点、
  周期和保留天数均显式留痕并可通过 `R20_JEV_SHADOW_*` 环境变量调整。
- 新增 `data/jev_shadow_entry_outcomes.jsonl`：记录主脑与 Jev 的开仓分歧、WAIT 基线、
  API 不可用/数据无效/置信度不足样本，以及真实主脑成交与影子方向收益的关联结果。
- Jev 开仓评估改为盲测输入，隐藏主脑动作、置信度和目标价格；复核返回后刷新盘口，
  影子成交明确标记为纸面估算，不再冒充真实成交，并记录响应延迟、观察时间与窗口滞后。
- 主脑与 Jev 的持仓中途收益统一按同一套双边手续费和可执行盘口口径计算；数量按合约
  `minSz`、基准仓位和 `ctVal` 量化，动作票增加最低置信度、概率间隔和数据有效性门槛。
- Jev 方向动作增加 `execution_ready` 硬门槛，持仓复核缺少 `data_valid` 时 fail-closed；
  无效、未就绪和缺字段样本不再被误计为可执行方向样本。
- Jev 延迟入场报价强制使用 OKX 双域行情，OKX 不可用时记录 `NO_PRICE`，不再混入
  Binance/Gate 备源；台账同步记录 `jev_quote_source`。
- 主脑 WAIT、Jev 独立开仓时使用可追溯的统一影子预算：配置值优先，其次单标的
  `risk_per_trade_usd`，最后使用 15U 默认值；独立影子仓位不套用主执行层的基准仓位下限。
- 开仓样本增加 `initial`、`scale_in`、`position_management` 分类，已有仓位上的方向复核
  不再冒充首次开仓；旧 `FILLED_AT_REVIEW` 台账状态读取时迁移为当前纸面成交状态。
- 新建样本的主脑持仓收益从首轮起即使用可执行盘口与双边手续费净收益，避免先写原始 UPL、
  下一轮又切换口径导致时间序列跳变。

### 路由与目录文档门禁

- 路由拆包测试改为校验 35 条基线接口不丢失、不重排，同时允许后续版本追加接口。
- `scripts/README.md` 登记 `account_scope.py`、`direction_observation.py`，并同步根层模块数量。
- 补登记 `scripts/trader/order_lease.py`，避免目录说明门禁误报。

### 当前验证

- 路由拆包测试：5/5 通过。
- `scripts/` 根层模块登记测试：5/5 通过。
- 相关生产模块 `py_compile`：通过。
- `git diff --check`：通过。
- 完整目录文档测试仍有 1 个既有覆盖门禁失败：`order_lease.py` 尚未被测试文本引用；遵循“不要补充测试”的约束，本次未添加测试或伪造引用。

### 上游同步复核清单

下次更新原作者代码后，优先复核以下本地补丁是否仍需要：

1. `scripts/account_scope.py` 与 `R20_DATA_DIR` 相关改动；
2. `scripts/direction_observation.py`、4H 收盘 K 线和 `observe_cycle` 顺序；
3. `scripts/trader/order_lease.py` 与挂单超时/KEEP 规则；
4. 10x 杠杆配置及执行层对齐；
5. 调度器 stdout/stderr 合并和周期触发修复；
6. Jev 影子复核的启停、超时和结果质量；
7. 路由测试基线兼容规则及 `scripts/README.md` 登记内容。
