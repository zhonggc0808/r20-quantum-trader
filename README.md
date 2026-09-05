# R20 Quantum Trader (R20 智能对冲对冲基金投委会量化系统)

<div align="center">

[![Version](https://img.shields.io/badge/version-v7.4.0-blue.svg?style=flat-square)](https://github.com/555cute/r20-quantum-trader/releases/tag/v7.4.0)
[![License](https://img.shields.io/badge/license-MIT-green.svg?style=flat-square)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg?style=flat-square)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100%2B-009688.svg?style=flat-square)](https://fastapi.tiangolo.com/)
[![Vue 3](https://img.shields.io/badge/Vue-3.x-4FC08D.svg?style=flat-square)](https://vuejs.org/)
[![Tailwind CSS](https://img.shields.io/badge/Tailwind-3.x-38B2AC.svg?style=flat-square)](https://tailwindcss.com/)
[![Tests](https://img.shields.io/badge/tests-116%20passed-brightgreen.svg?style=flat-square)](tests/)

**全新演进的 LLM 原生数字资产对冲量化交易系统**  
*策略大一统版本快照 · 具名归档与一键原子回滚 · 同等身份交易员双轮质询 · 核心风控不可绕过底座 · 交易所原生云端 OCO 风控*

[在线官网与实盘大屏](https://www.r20.cn) · [快速上手](#-快速启动指南) · [策略版本控制台](#-四大单元策略版本快照工作台) · [投委会架构](#-投委会决策架构) · [核心特性](#-系统核心架构与特性) · [版本日志](CHANGELOG.md)

</div>

---

## 🏛️ v7.4.0 重磅升级总览 (Release Highlights)

在 **v7.4.0** 中，系统迎来了四大策略核心单元的架构级大重构与策略版本控制中心的全面上线：

1. **策略大一统版本快照控制台 (Policy Snapshot Workbench)**：
   - **指纹聚合 (Policy Fingerprinting)**：实时聚合提示词工作室、自进化心法库、物理拦截插件与模型委员会四大核心单元的不可变指纹，生成确定性策略版本号（如 `v7.4.0@4aa048db`）；
   - **具名策略归档入库 (Archive Vault)**：支持一键将当前生效的完整策略打包固化为具名里程碑（如《顺势打折高胜率版》）；
   - **一键秒级原子回滚 (One-Click Rollback)**：任何时候调乱参数，0.5 秒内将四大单元真实配置全盘恢复回历史瞬间；支持归档删除与版本管理；
   - **实盘交易台账 100% 绑定追溯**：在监控大屏的「交易台账」中实时展示订单对应的策略版本哈希徽标，彻底告别“事后复盘说不清楚当时用了什么策略”。

2. **四大策略单元深度加固与去伪存真**：
   - **自进化心法防线 (Evolution Shield)**：彻底根治白盒审核全部拒绝仍被发布的漏洞，实现跨进程原子写锁与 CAS 乐观并发锁（428/409 拦截过期覆盖）；
   - **提示词策略工作室 (Prompt Studio)**：模板占位符单次延迟显式渲染，彻底杜绝配置阶段提前吃掉真实数据或伪装成虚假空仓；
   - **物理拦截器核心底座 (Core Risk Guardrails)**：确立了有限数值、正确几何、最低 75%/80% 置信度与 2.0R 盈亏比**不可被任何可选插件绕过**的物理安全底座，并在最终下单前对真实生效报价进行严格二次复验；
   - **模型委员会 (Trading Desk Council)**：剔除无代码分支的虚假共识选项，收敛为 `standard`（标准提案裁决）与 `cross_examination`（双轮真实交叉质询找茬）两种硬核模式，落地 `adopted_role` 采纳追踪与毫秒级截止时间安全降级。

3. **OKX 手动平仓 502 报错彻底根治**：
   - 快速平仓模块全面对齐现代 Chrome 浏览器 User-Agent 与 Accept 标头，杜绝被 OKX Cloudflare WAF 误杀拦截；
   - 平仓前同步扫描并预先清理同标的在途云端 OCO 策略委托，消除交易所撮合冲突。

---

## 📸 实景截图矩阵

### 1. 策略大一统版本快照控制台 (Policy Snapshot Workbench)
![策略版本快照控制台](docs/images/admin_policy_snapshot.png)

### 2. 对冲基金投委会决策中枢 (Trading Desk Council)
![对冲基金投委会中枢](docs/images/admin_council.png)

### 3. 真实量化实盘监控终端全景
![实盘监控终端全景](docs/images/dashboard_trading.png)

---

## ⚡ 系统核心架构与特性

- **大模型核心决策 (LLM-Native 70% 权重)**：告别僵化死板的传统指标策略，由 DeepSeek / Claude / GPT / Gemini 等旗舰大模型担任全权量化决策大脑。
- **微积分行情动力学**：实时解构 15M/1H 价格时间序列的一阶导数（速度 $v$）、二阶导数（加速度 $a$）及定积分动能（做功 $E$），量化趋势爆发力。
- **Top 100 聪明钱雷达**：全天候扫描全网持仓前 100 名主力账户的真实多空持仓、平均建仓成本与资金净流向。
- **确定性硬门禁 (Deterministic Interceptors)**：置信度 $\ge 75\%$、盈亏比 $R:R \ge 2.0$、2.0x ATR 防插针宽止损、保本移损与反向持仓防对冲。
- **交易所原生 OCO 委托**：所有策略发单强制绑定 OKX 云端条件单，即使后端离线断网，交易所撮合引擎仍严格执行防穿仓兜底。
- **双模响应式界面**：Vue 3 + Tailwind CSS 极简响应式架构，完美适配手机移动端与宽屏桌面，支持深浅双模极致对比度。

---

## 🚀 快速启动指南

### 1. 环境克隆与依赖安装
```bash
git clone https://github.com/555cute/r20-quantum-trader.git
cd r20-quantum-trader

# 创建并激活虚拟环境
python3 -m venv venv
source venv/bin/activate  # Linux/macOS
# .\venv\Scripts\activate  # Windows

# 安装 Python 后端核心依赖
pip install -r requirements.txt
```

### 2. 配置环境变量
```bash
cp env.example .env
# 编辑 .env 配置你的 OKX API 凭证与默认大模型 API Key
```

### 3. 构建前端静态资源
```bash
cd frontend
npm install
npm run build
cd ..
```

### 4. 启动量化服务与控制面
```bash
# 启动常驻量化核心与控制后台 (监听 0.0.0.0:8080)
python3 -m uvicorn r20_backend.app:app --host 0.0.0.0 --port 8080 --reload
```
打开浏览器访问：
- **实盘监控终端**：`http://localhost:8080/`
- **管理控制面**：`http://localhost:8080/admin/`
- **API 交互文档**：`http://localhost:8080/api/docs`

---

## 🧪 自动化测试验证

系统包含覆盖策略引擎、投委会机制、拦截器插件与安全鉴权的自动化单元测试：
```bash
python3 -m unittest discover -s tests/
# 输出: Ran 116 tests ... OK (100% 通过)
```

---

## 📄 开源协议与免责声明

- 本项目基于 **[MIT License](LICENSE)** 开源。
- **免责声明**：本项目仅供量化交易研究与学术交流使用。加密货币属于高风险高波动资产，策略历史表现不代表未来收益，请务必根据自身风险承受能力理性参与实盘。
