# 项目协作说明

## 项目定位与文件

- 本项目是在 Ubuntu 上运行的 Python 永续合约中性网格脚本，每个进程通过 `EXCHANGE` 和 `SYMBOL` 选择一个平台、一个交易对。
- `ccxt_grid.py`：程序入口，`GridTrader` 负责配置、建网格、轮询补单、账户风控、日志和通知。
- `lighter_exchange.py`：Lighter 原生 SDK 的同步适配器，通过独立 asyncio 事件循环调用 SDK。
- `ondo_exchange.py`：Ondo Perps REST 适配器，负责请求签名、市场精度、订单及持仓转换和下单结果确认。
- Aster 使用 CCXT；Lighter 和 Ondo 使用各自适配器，不依赖 CCXT 初始化。
- `.env`：本地账户及策略配置，含敏感信息，已被 Git 忽略。
- `requirements.txt`、`requirements-aster.txt`、`requirements-ondo.txt`：分别用于 Lighter、Aster、Ondo 的独立虚拟环境。
- `README.md` 包含运行说明和历史需求；判断当前行为以实际代码为准。

## 修改约定

- 默认用中文沟通，代码和注释保持简洁易读，兼容 Ubuntu；Ondo 环境使用 Python 3.10+。
- 在现有结构上做最小必要修改，保留未涉及的用户改动，避免顺带重构或修改交易参数。
- 遵循 PEP 8，优先标准库、平坦流程和 early return；不要为仅使用一次的简短逻辑增加微型辅助函数或抽象层。
- 保留现有配置名及其大小写，如 `gridStep`、`gridLines`、`orderSize`、`signerAddress`；不要无故重命名现有状态变量。
- CCXT 平台优先使用其统一方法；平台特有的签名、字段和精度转换放在对应适配器中。
- 使用 `amount_to_precision()` 和 `price_to_precision()` 处理数量和价格，不引入手工指定小数位数的 `digital` 配置。
- 新增第三方依赖只加入对应平台的依赖文件，保持各平台依赖隔离。
- 新增或改变配置、平台接口及运行方式时，同步维护相关说明；配置示例仅使用占位值。

## 当前策略行为

以下是当前实现的基准。修改涉及这些行为时，应核对用户需求及跨平台影响。

- 只交易永续合约，启动时检查市场的 `swap` 标记；普通网格单使用限价单，不启用 post-only 或 reduce-only。
- `gridLines` 为正偶数；初始网格以 `myNewPrice` 为中心，上下各挂一半订单，数量为精度处理后的 `orderSize`，间距为 `gridStep`。
- 读取行情优先使用买一卖一中间价，盘口缺失时回退到 ticker。
- `All_orders` 保存本进程创建的订单：字符串订单 ID 映射到 `[边界标记, 价格, 方向]`。最外层买单和卖单标记为边界单；状态仅在内存中保存，不支持重启恢复归属。
- 普通买单被判定成交后，在原价格加一个 `gridStep` 处补卖单；普通卖单成交后，在原价格减一个 `gridStep` 处补买单。
- 同一轮多个待补订单按与该轮开始时的 `last_fill_price` 的绝对价差从近到远处理；尚无成交记录时使用 `myNewPrice`。铺单和补单之间均暂停 0.05 秒。
- 边界单被判定成交时，更新参考价格并设置 `switc=True`，下一轮撤单重建。若同轮有边界成交，优先进入重建流程，不逐笔补单。
- `last_fill_price` 记录的是脚本处理的成交订单的原限价，不是从交易明细取得的实际成交均价；同轮多单的处理顺序也不是交易所真实成交时间顺序。
- 目标仓位为 `(baseline - target_price) * orderSize / gridStep + left`，其中 `target_price = last_fill_price or myNewPrice`。该值及仓位差额仅用于日志，不自动补仓。
- `myNewPrice` 同时承载行情价和成交参考价；每 20 次循环的 `accountInfo()` 会用行情刷新它，可能影响重建网格的中心。

## 平台差异

### Aster / 其他 CCXT 平台

- Aster 使用 `PRIVATE_KEY`、`WALLET_ADDRESS`、`signerAddress`；Aster 和 Binance 下单指定 `positionSide=BOTH`。
- 交易对采用对应 CCXT 永续格式，例如 `BTC/USDT:USDT`。
- 本进程记录的订单从挂单列表消失即按成交处理，未逐笔核实最终状态；手动撤单也会进入该路径。

### Lighter

- 使用 `LIGHTER_NETWORK`、`LIGHTER_ACCOUNT_INDEX`、`LIGHTER_API_KEY_INDEX`、`LIGHTER_API_PRIVATE_KEY`；网络默认值为 `robinhood`。原生交易对示例为 `BTC`。
- 数量向下取整，价格四舍五入；下单前适配器还会检查最小数量、最小名义价值及最大名义价值。
- 对消失的订单查询非活跃订单：仅明确为 `filled` 才补反向单；以 `canceled` 开头的状态按原方向、原价格及配置数量重挂，成功后替换旧 ID，不将撤单本身计为严重错误。
- 状态未知或查询失败时等待后续轮询；当前非活跃订单查询最多取 100 条，没有翻页。
- 对已撤订单，即使此前发生部分成交，重挂数量仍为完整 `orderSize`，并非剩余数量。
- 策略的 GTC 参数在适配器中映射为 SDK 的限时有效订单，当前有效期为 28 天。

### Ondo Perps

- 使用 `ONDO_API_KEY_ID`、`ONDO_API_SECRET`、`ONDO_API_URL`、`ONDO_TIMEOUT`；交易对统一为 `BTC/USDT:USDT` 格式，并兼容 `BTC-USD.P` 等原生旧格式。
- `market["symbol"]`、持仓和订单返回统一名称；主脚本初始化时会将 Ondo 的 `self.symbol` 归一化。`market["id"]` 始终保留原生市场名，供所有 API 请求使用。统一名称中的 `USDT:USDT` 仅是配置别名，市场的真实 `quote` 及原始信息保留原值。
- 数量按 `baseIncrement` 向下取整，价格按 `quoteIncrement` 四舍五入；`gridStep` 必须是价格步长的正整数倍。
- 轮询与 Aster 一样采用“订单消失即成交”；适配器虽提供 `fetch_order_statuses()`，当前主循环没有调用它。
- 下单 POST 仅提交一次；结果不确定时用同一个客户端订单 ID 查询确认，仍无法确认则抛出 `OndoFatalError`，主循环立即走风控退出，不盲目重发。
- `_parse_order()` 若实际解析到部分成交后撤单会抛出 `OndoFatalError`，但当前轮询不会为消失订单查询该状态；不要将此理解为所有部分成交撤单均已被检测。
- 签名使用最终发送的 URI 和正文；修改请求构造时保证签名内容与发送内容一致，保留官方 API 地址检查及敏感信息脱敏。

## 配置、风控与验证

- `load_dotenv()` 默认不覆盖已有系统环境变量；同一 `.env` 中重复键以最后一项为准。不要仅凭前面一组配置判断实际使用的账户或交易对。
- 不输出、复制到文档或提交实际私钥、API Secret、Webhook 等敏感值；需要检查配置时只显示字段名、脱敏结果或必要的非敏感策略值。
- `accountInfo()` 每 20 次循环检查价格上下限、未实现盈亏 `MAX_LOSS`、绝对仓位 `maxposi` 和挂单数上限 `gridLines + 4`；`poller()` 还会检查挂单数是否小于 `gridLines // 3`。
- 重建网格、风控及 Ctrl+C 撤单作用于当前账户该 `SYMBOL` 的全部挂单，包括手动挂单。重建网格会先撤已有挂单；风控和 Ctrl+C 分别由 `CANCEL_ON_RISK`、`CANCEL_ON_EXIT` 控制。退出不自动平仓。
- 主循环按 `CRITICAL_ERROR_LIMIT` 处理连续关键错误；`poller()` 读取挂单失败会直接返回，不计入该计数，正常完成一轮后计数清零。
- 主策略 `validate_order()` 当前只启用数量和名义价值的上限检查，最小值检查被注释；平台适配器可能另有检查。
- 默认离线验证。只有任务明确授权相应实盘操作时，才运行主程序、下真实订单或撤销真实挂单；`python ccxt_grid.py` 会直接进入交易流程，不能用作普通冒烟检查。
- 导入 `ccxt_grid.py` 也会读取 `.env` 并创建日志。纯语法检查优先使用 `ast.parse` 等不执行模块代码的方法。
- 修改交易逻辑时，使用假交易所或模拟响应验证受影响路径，重点核对补单方向及价格、边界重建、状态未知、撤单重挂、异常与风控。纯文档修改只需检查内容和差异。
- 报告实际完成的检查，区分离线验证与 Ubuntu / 交易所实测；不要将本地静态检查描述为实盘验证。

<!-- codex-dedicated-temp -->
## Dedicated temporary directory

Use `C:\CodexTemp` for Codex task temporary files, downloads used only during a task, scratch scripts, intermediate outputs, and verification artifacts. Create a separate subdirectory per task to avoid collisions.
For commands and subprocesses, use `TEMP`, `TMP`, and `TMPDIR` set to `C:\CodexTemp`. Keep final deliverables in the requested project or output location.
<!-- /codex-dedicated-temp -->
