# R20 本地改动记录

## 2026-09-26（下半场之二）

### Jev 执行档位：从「读了不生效」改为真正可配置

问题：`enforcement_mode` 被硬编码成 `"shadow"`，`R20_JEV_ENFORCEMENT` 只被记进
`configured_enforcement` —— 环境变量**读了却不生效**，而切换档位恰恰必须改代码，
与方案 §6「`shadow`、`review`、`soft_veto` 必须可配置回滚，不得通过修改代码切换」
正好相反。**已确认为漏实现**（原注释写的是防环境变量笔误的安全意图，但代价是档位
不可配置，方向错了）。

修复：

- 新增 `_jev_resolve_enforcement`：解析 `R20_JEV_ENFORCEMENT`，接受
  `shadow` / `review` / `soft_veto` / `hard_veto`（大小写与空白容忍）。
  **非法值 fail-closed 回 `shadow`** —— 那是唯一在结构上不可能改变主脑执行的档位，
  因此「防笔误把观察者变成门禁」这个原始安全意图被完整保留，同时档位可配置。
- 新增 `_jev_enforcement_decision`：按方案 §9 阶段 C 逐候选判定「若该档位已启用，
  本候选会被怎么处理」，并把 `jev_enforcement_decision` / `jev_enforcement_reasons`
  落盘。判据：
  - 只针对主脑**新开仓**（`entry_mode == "initial"`），加仓与持仓管理不适用；
  - Jev 的 `data_status` 必须 `valid`，且**票决必须过门槛**；
  - **弱票不算否决依据**：`low_confidence` / `ambiguous` / `not_ready` 表示「拿不准」，
    不是「明确 WAIT」。把它当否决依据等于用弱信号推翻主脑，并把「模型犹豫」误读成
    「模型反对」；
  - 必须与主脑反向**或**明确 WAIT（`no_edge`），否则 `no_veto_grounds`；
  - `INSUFFICIENT_DATA` 不得当反向信号（§6 规则 5）。
- 代码硬门禁失败 ⇒ `HARD_VETO`，由代码产生、不需要 Jev 证明（§6 规则 1）。
- 新增 `R20_JEV_HARD_VETO_ONLY_CODE_GATES`（默认 1）：置 1 时 Jev 自身最高只能
  到软否决，硬否决只允许来自代码门禁。放开时需显式设为 0。
- **硬不变量**：持仓通道恒为 `SHADOW`、恒不影响执行，理由是
  `protection_always_code_controlled` —— 即使有人把档位设成 `soft_veto` 或
  `hard_veto`，也**改变不了保护单与止损**（§9 阶段 D：「平仓保护、止损和交易所
  安全门禁始终由代码控制」）。
- **`jev_enforcement_affects_execution` 恒为 `false`** 并显式落盘。阶段 C/D 的执行
  门控需要样本外证据（§9 阶段 D）；在证据到位前把判定接到执行上，等于用未标定的
  信号动真钱。写出来是为了让台账能自证这一点，而不是让读者去猜。
- 测试 `test_no_mode_ever_affects_execution_yet` 是**看门狗**：若有人提前接通执行，
  它会失败并提醒先拿出样本外证据。

验证（定向套件）：

- JEV 契约测试 **42/42** 通过，含新增三组：
  `EnforcementModeIsConfigurableTest`（5 条）、`EnforcementDecisionCriteriaTest`
  （10 条）、`ProtectionIsNeverVetoedTest`（2 条）。
- 相关套件（JEV / 方向观测 / 网关调度 / 路由拆包 / 账户范围）**67/67** 通过。
- 全量测试在本轮改动后跑过一次：失败集与改动前逐项相同（各 34 个既有失败），
  零新增。后续不再重复跑全量（约 10 分钟），改用定向套件。

## 2026-09-26（下半场）

### Jev 只读影子评估工具

- 新增 `scripts/jev_shadow_evaluator.py`：只读关联 review/outcome JSONL，默认按最新
  `question_semantics_version` 评估，支持终端摘要和 JSON stdout。
- 强制按语义版本和费用假设分 cohort；重复键、关联失败、损坏 JSON、relation 漂移、
  未成熟 horizon 与收益来源均单独计数，避免把数据缺口解释成策略结论。
- `no_entry_baseline` 不进入测量收益均值，只在 `WAIT_VS_ENTRY` /
  `MAIN_WAIT_JEV_ENTRY` 中作为显式 policy baseline 参与配对差值。
- 每类/每 horizon 输出净收益、胜率及 Wilson 区间、尾部、样本序列最大回撤和
  Jev-minus-main；样本门禁为 `<30` 证据不足、`30-99` 仅探索、`>=100` 可评估。
- 当前平均 R 与 Choice 校准不具备合法输入，明确输出 `unsupported`，不制造伪指标。
- P1 修正实验范围：`position_management` 统一归为 `OUT_OF_SCOPE`，不再把已有
  持仓上的方向回答伪造成独立新开仓样本。
- P1 增加纸面状态、成熟度和收益缺失原因诊断，区分“未成熟”“策略基线”与
  “成熟后仍无测量值”。
- P2 为主脑决策增加结构化 `decision_outcome_source` 和 `decision_rejection_code`，
  保持拦截器 `(action, reason, rr)` 接口不变，并贯穿 decision cache、Jev review 和
  entry outcome；评估器可区分模型主动 WAIT、风控拒绝和拦截器异常。
- P2 历史记录不会回填猜测值，统一显示 `missing`；从新周期开始累积可解释分布，
  在有证据前不调整主脑入场门槛。
- P2 将原 `quote_geometry_or_rr` 拆为 `quote_parse_error`、
  `quote_geometry_invalid`、`risk_unit_invalid`、`rr_calculation_error`、
  `rr_below_floor`、`rr_above_ceiling` 等源头结构化 code；既有三元组 API 保持兼容。
- 新增 `BOTH_WAIT_NO_EDGE` 描述类，保留主脑与 Jev 一致不入场的信息，但不把
  确定性零收益当成策略证据。
- 回撤统计增加 `drawdown_overlap_factor`、`drawdown_meaningful` 和解释标签；默认
  1cycle 为约 1x 近似代理，4h 为约 16x 高重叠、仅供描述。
- relation mismatch 不再只输出累计数：新增首末 mismatch、最新记录、末次 mismatch
  后记录数及北京时间小时分布；摘要可明确区分历史前缀漂移与当前持续异常。
- 摘要表新增 `baseline` 列，来自源台账中的 baseline 行数；`matured`、`baseline`、
  `measured` 三者分开展示，避免将无入场基线误读为收益测量失败。
- 修复评估器 `--since/--until` 的时区契约：无 offset 文本现在通过
  `r20_backend.time_utils.parse_beijing` 按北京时间解释，epoch 和显式 offset 保持原瞬间；
  JSON 报告的 `generated_at/since/until` 统一回显 `+08:00`，避免静默偏移 8 小时。
- 补齐评估器边界契约：epoch 毫秒会先归一化为秒；`no_entry_baseline` 仅接受显式 `0`，
  缺值或非零值标为无效且不能生成配对收益；同一时间戳的多标的收益先聚合再计算序列回撤，
  消除记录顺序对最大回撤的影响。
- 修正 relation mismatch 范围命名：只有 mismatch 位于全部干净记录之前才标
  `historical_prefix`；历史中段异常但后续恢复改标 `historical_segment_with_clean_tail`，
  避免把 45 条旧漂移误写成“历史前缀”。
- 修正 P2 原始诊断：不支持的模型动作保留原值并记为 `unsupported_action`，不再先归一化为
  `WAIT/model_wait`；`raw_confidence` 保存 clamp 前输入，`confidence` 继续保存执行使用的 0-100 值。
- 评估器将 `trading_ledger` 识别为真实终态收益来源；对有明确观测滞后的市价快照，超过一个轮询周期的
  `mark_to_market` / `live_position_mark_net` 不再计为测量，报告为 `observation_lag_exceeded`。
- 采集器固定 horizon 超窗时写 `missed_target_window` 且不重试；成交/平仓终态仍可结算。
- 补齐 `NO_EDGE`、Jev `WAIT` 和主脑 `WAIT` 的显式 `no_entry_baseline`，避免基线缺失造成假性未配对。
- relation 诊断新增 validated/missing/unvalidated 计数；缺关系记录不再被计入 mismatch 后的 clean tail。
- 修复评估器两处诊断边界：市价快照存在但 `lag_seconds` 非法时标记为 `observation_lag_invalid`；`main_raw_action_counts` 保留 `HOLD/NONE` 等原始动作，不再套用 WAIT 别名。

### Jev 关系分类与兼容票绝对尺度修复

- 修复主脑 `WAIT` 时审计 `NOT_APPLICABLE` 覆盖独立关系的问题：审计不适用只说明
  没有提案可审，不再吞掉 `MAIN_WAIT_JEV_ENTRY`、`AGREE` 或 `ABSTAIN`。
- 取消把三个独立 Noul 兼容票归一化为分类概率。`confidence` 改回最高原始分，
  `action_margin` 为最高分与次高分的启发式分离度；`0.20/0.10/0.00` 这类整体弱票
  不能再被放大成 `0.67` 的高置信方向。绝对 confidence 门槛同步恢复为 `0.70`。
- `not_ready` 与 `low_confidence` / `ambiguous` 统一归为 `ABSTAIN`，避免关系字段
  声称 `WAIT_VS_ENTRY`、执行档位却判定 `NONE` 的内部矛盾。
- 将方向门槛与明确 WAIT/no_edge 门槛拆开：方向维持 `0.70`，no_edge 使用独立的
  `R20_JEV_NO_EDGE_MIN_CONFIDENCE=0.54`，避免提高方向门槛后 no_edge 标签归零。
- 进一步拆开 no_edge **分类门槛**与 WAIT **否决门槛**：`0.54` 仅用于保留
  “市场平淡”标签；形成软否决候选必须另过
  `R20_JEV_VETO_WAIT_MIN_CONFIDENCE=0.70` 和 `R20_JEV_VETO_MIN_ACTION_MARGIN=0.15`。
  因此 `0.60` 的明确 WAIT 会记录为 `no_edge`，但不再获得否决资格。
- 修复 `R20_JEV_VETO_MIN_ACTION_MARGIN` 只约束 WAIT、未约束反向方向的问题。现在
  反向和明确 WAIT 两条否决路径都必须重新通过通用 veto margin；例如分类 margin
  `0.20` 虽已超过 `0.15`，当 veto margin 配为 `0.30` 时仍不得形成候选。
- 记录当前 WAIT 否决路径的休止状态：截至 2026-09-26，双通道 WAIT 观测最大值
  低于 `0.70`，因此零个 WAIT 否决候选是门槛不可达的预期后果，不代表逻辑已验证。
- 补齐方案中的 `MAIN_WAIT_JEV_ENTRY` relation 枚举，并新增低绝对票、高冲突票、
  not_ready 一致性和主脑 WAIT 反事实关系的回归测试。

### Jev 独立通道：拆分「完整性 / 一致性 / 方向性优势」

问题：代码侧 `data_quality` 恒为 `valid`（210/210），而模型被问的那道
`candidate_*_data_valid` 同时承担「数据够不够」与「有没有机会」两件事，于是它对
**优势**的判断被记成 `invalid_data` —— 线上 65/80 个候选如此，标签与事实 100%
矛盾。铁证：代码可测的数据属性**全部无方差**（`data_quality` 恒 valid、
`direction_layers` 三层 quality 恒 0.9/1.0/1.0、null 数恒定），而该字段 sd=0.161，
且与 `price_position_in_range` 的标的内中心化相关 r=−0.55 —— 数据没有方差，
字段有方差，它测的不是数据。

拆分后：

- **代码拥有**完整性与一致性，且**独占** `INSUFFICIENT_DATA`。新增
  `_jev_candidate_state_quality` / `_jev_position_state_quality`：必需字段缺失
  → `code_state_incomplete`；价格非正、盘口交叉、价格偏离报价、4H K 线时间戳
  错位、`position_in_range` 越界、方向层缺失 → `code_state_inconsistent`。
  **`direction_observation.status == CONFLICT` 不算不一致** —— 多周期证据互相
  矛盾是市场事实，不是载荷损坏；把它当数据问题会丢掉真实的分歧信息。
- **模型只答判断**：`candidate_*_data_valid` → `candidate_*_edge_present`
  （有没有方向性优势），`position_*_data_valid` → `position_*_management_warranted`
  （有没有需要动作的理由）。题面明确告知完整性与一致性「不是你的事」。
- **`edge_present` 只记录、不作否决门**。该题是全新的、无历史分布可标定；让未标定
  的问题否决方向，正是此前「90 个候选 0 个方向输出」的成因。等它有样本、可按已
  结算结果标定后，再考虑升级为门槛（方案 §5.1 本就把它定位为「解释维度和门槛」，
  先做前者）。测试 `test_low_edge_alone_does_not_veto_a_confident_direction`
  显式钉住这一点，防止有人无证据地把它改成门。
- **`no_edge` 成为独立状态**：票决明确选 WAIT ⇒ `no_edge`（市场没给方向），与
  `low_confidence`（拿不准）分开记录，不再冒充数据故障。
- 移除 `cycle_data_valid`：实测该答案**从不参与任何判定**，只被记进
  `aggregate_answers` —— 一道既名不副实又白付一次推理成本的问题。整轮质量改由
  代码汇总记录在 `code_cycle_quality`。
- 移除 `R20_JEV_DATA_VALID_MIN_PROBABILITY`：拆分后不再读取。留一个「读了却不
  生效」的环境变量比删掉它更危险（它会让人以为改得动）。
- 新增 `question_semantics_version=2` 标记语义版本：1 = 拆分前（模型答
  `*_data_valid`，低分记 `invalid_data`），2 = 拆分后。**新旧样本不可混统计。**
- 下游状态映射同步：`code_state_*` → `STATE_DEFECT`，`no_edge` → `NO_EDGE`；
  旧的 `invalid_data`/`missing_data_valid` 保留仅为兼容历史记录。

验证：

- JEV 契约测试 27/27 通过（含新增 `CompletenessIsCodeOwnedTest` 五条边界）。
- 全量 3235 个测试：失败集与拆分前**逐项相同**（各 34 个，均为既有失败），
  零新增。
- 线上实测（14:31、14:46 两轮，`question_semantics_version=2`）：
  `code_cycle_quality` 全部 `ok`（0 不完整 / 0 不一致），**`invalid_data` 与
  `INSUFFICIENT_DATA` 归零**（此前 65/80 被误标），产出方向信号
  （UNI BUY_LONG ×2、BTC SELL_SHORT ×1）。`edge_present` 取值 0.20~0.68，
  说明新题面在真实 state 上有判别力。

## 2026-09-26（上半场）

### Jev 独立通道：测量有效性修复（Tier 1 + Tier 2）

背景：独立通道在无锚状态下整条答案分布被压缩到 [0.2, 0.68]，`data_valid`
中位数 0.36，而门槛按「有主脑提案做锚」的旧分布标定（旧 `would_wait` 中位
0.79），导致 90 个候选里 **0 个**达到 0.70 的 confidence 门槛、**0 个**方向性输出。
下面的改动分两类：纯 bug 修复（零证据代价）与测量有效性修复（会重置样本）。

**Tier 1 · 纯 bug 修复**

- A `max(0.0, …)` 压掉 `-1.0`「未作答」哨兵（3 处写入点）：该写法把「没问过」
  压成 0.0，与「模型明确答 0.0」无法区分，任何求均值都会把未测量的行当成强烈
  否定。`audit_probabilities` 一直保留哨兵，本次补齐同源字段
  `data_valid_probability` / `execution_ready_probability`，并加测试钉住。
- A 台账迁移：64 条独立通道 422 失败记录的 793 行由 `0.0` 改写为 `-1.0`，
  并置 `lane_unavailable` / `independent_lane_unavailable`，使其无法再被平均进
  任何统计（旧口径全量中位 0.07 即由此污染；只取真正测量过的 90 行则中位 0.36）。
- B 删死键：`trend_4h_bullish` / `trend_4h_bearish` 在 710/710 行恒为 null
  （brain package 从不产生这两个键，白名单在宣称一个永远填不上的字段）；
  硬编码的 `liquidity_state:"UNKNOWN"`（71/71 轮）删除，改为真实可算的
  `spread_bps`（取自本轮已持有的盘口），深度显式记为
  `depth_state:"unavailable"` 而不编造桶值。
- C 收敛双重 `price_position_in_range`：`enrich_brain_package`（最新 8 根、含未
  收盘）与 `four_hour_range`（已收盘 12 根）曾共用同名键，710 行中 707 行互相
  矛盾，模型拿到两个互斥的箱体位置。前者改名
  `price_position_in_range_recent8`，权威口径统一取已收盘 12 根，并新增
  `price_position_basis` 记录实际沿用哪个窗口。2026-09-24 那次「统一
  brain/trader 的 `range_4h_*` 口径」只统了 range 字段，漏掉了这个位置字段。
- D 持仓路径改真白名单：原实现是「完整 62 键上下文减去 4 个具名 `main_*` 键」的
  黑名单，而注释却宣称两条通道都走白名单 —— 将来任何换个名字编码主脑结论的新
  字段都会自动泄漏。改为显式 `_JEV_NEUTRAL_POSITION_KEYS`（56 个事实键，
  fail-closed），并补 `IndependentStateLeakGuardTest`（此前无任何测试断言
  `independent_state` 不含主脑结论）。

**Tier 2 · 测量有效性（作废此前样本）**

- E 加校准指引：JEV 的 payload 是 `{model, state, questions}`，**没有 system
  prompt**，校准只能进题面。为 `data_valid` / `execution_ready` / `would_*`
  写明量表语义，并明确「数据完整但行情平淡」不等于数据不足 —— 后者应体现在
  方向票里而不是数据题里。同真实 state 的 A/B：中位 0.52 → 0.65。
- F（已被同日后续修正替代）曾把三个独立布尔兼容票归一化成互斥分布。后续验证
  发现这会把整体弱票放大成高置信方向，现已恢复按原始绝对分和启发式分离度判定；
  原始最大值与票和继续保留在 `jev_raw_max_vote` / `jev_vote_sum`。
- G 门槛可配置并重标：此前 4 处 `0.5` 是硬编码（`data_valid`、
  `execution_ready`、`audit_data_valid`、`protection`），是全项目唯一不可配置的
  JEV 阈值。现全部走环境变量。`execution_ready=0.44` 沿用实测中位数；独立动作
  confidence 在恢复原始绝对分后同步恢复为方案建议的 `0.70`，避免把题面定义为
  “摇摆”的约 `0.5` 分数当作高置信方向；`audit_data_valid` / `protection` / 审计旗标
  维持 0.50（审计分布未变）。

**验证**

- 全量测试 3235 个：失败集与改动前**逐项相同**（各 34 个，均为既有失败），
  本次改动引入 0 个新失败；JEV 相关套件 26/26 通过。
- 线上实测（调度器每轮新起进程，改动即时生效）：
  10:31（旧）0 方向 → 11:01（+E）`data_valid` 0.16–0.82、**3 个 BUY_LONG accepted**
  → 11:16 **1 个 BUY_LONG accepted**。
- 台账迁移经两次线上写入后仍完整（793 行哨兵，0 行回退）。

**仍未解决 / 已知局限**

- 本次标定仅基于 9 轮 / 90 个候选，属**临时标定**，必须按已结算结果复标；
  `R20_JEV_*` 环境变量可回滚，不得靠改代码切换。
- 证据显示 `data_valid` 实际在测量**方向倾向**而非数据健康（与
  `price_position_in_range` 的标的内中心化相关 r=−0.55，与 `entry_15m.direction`
  r=−0.55），而代码侧 `data_quality` 恒为 `valid`。「数据是否有效」究竟归代码
  还是归模型、该不该拆成 `data_complete` / `data_consistent` / `directional_edge`
  三个概念，尚未决策。
- 尚未验证：补足中性 state（方案 §4.1 规定的 `trend_4h`、`momentum_15m`、
  `volatility`、`funding_state`、`bullish/bearish_evidence` 等均未实现）能否
  进一步抬高答案。需要一次 A/B，不应默认「补数据=更准」。
- `build_jev_independent_state` / `build_jev_audit_state` /
  `run_jev_shadow_requests` / `scripts/trader/jev_policy.py` 四个方案点名的
  模块仍不存在，state 依然内联在 `_run_jev_shadow_review` 中。
- 2026-09-25 13:00 存在一次未解释的体制跃迁（中位数 0.17→0.67，同模型同路由同
  题集）。重构前代码从未提交（父提交 `07a0c82` 中 JEV 引用为 0），该段历史不可
  复原；应视为「该台账存在未解释的体制漂移」这一已知局限。

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
