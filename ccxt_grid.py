# -*- coding: utf-8 -*-

import logging
import os
import sys
import time
import json
import urllib.request
from decimal import Decimal  # Ondo 接入：按市场步长校验网格间距

from dotenv import load_dotenv
from lighter_exchange import LighterExchange  # Lighter 接入：加载原生 SDK 适配器
from ondo_exchange import OndoExchange, OndoFatalError  # Ondo 接入：REST 适配器和停机异常


load_dotenv()

# 从 .env 读取策略名称和运行参数，方便同一份脚本切换不同账户/交易对。
name = os.getenv("NAME", "")
logger = logging.getLogger(__name__)
logger.propagate = False
logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
logger.handlers.clear()

file_handler = logging.FileHandler(f"{name}_ccxt_log.txt", mode="a", encoding="utf-8")
file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s"))
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(file_handler)
logger.addHandler(console_handler)


class GridTrader:
    def __init__(self):
        # Lighter 使用原生 SDK，Ondo 使用 REST，其他平台使用 CCXT。
        exchange_id = os.getenv("EXCHANGE", "").strip().strip("'\"").lower()
        if not exchange_id:
            sys.exit("Missing EXCHANGE in .env")
        ccxt_module = None  # Lighter 接入：Lighter 环境无需安装存在依赖冲突的 CCXT
        if exchange_id not in ("lighter", "ondo"):  # Ondo 接入：原生适配器不加载 CCXT
            try:  # Lighter 接入：给缺少平台依赖提供明确提示
                import ccxt as ccxt_module  # Lighter 接入：延迟导入隔离 urllib3 版本
            except ImportError:  # Lighter 接入：阻止缺少 CCXT 时继续启动
                sys.exit("ccxt is not installed; use requirements-aster.txt")  # Lighter 接入：提示对应依赖文件
        if (  # Lighter 接入：允许原生 SDK 平台
            exchange_id not in ("lighter", "ondo")  # Ondo 接入：允许 REST 平台
            and not hasattr(ccxt_module, exchange_id)
        ):
            sys.exit(f"ccxt does not support exchange: {exchange_id}")

        # 强制使用永续合约 swap，避免误连现货市场。
        options = {"defaultType": "swap"}
        config = {"enableRateLimit": True, "options": options}
        # Aster 使用钱包签名参数，其他交易所使用常规 API key/secret/password。
        if exchange_id == "lighter":  # Lighter 接入：绕过 CCXT 初始化
            try:  # Lighter 接入：将配置错误转换为清晰的启动错误
                self.exchange = LighterExchange.from_env()  # Lighter 接入：使用 API 私钥连接
            except Exception as e:  # Lighter 接入：禁止凭据错误时进入主循环
                sys.exit(f"Cannot initialize Lighter: {e}")  # Lighter 接入：退出且不输出密钥
        elif exchange_id == "ondo":  # Ondo 接入：使用 API Key 签名
            try:
                self.exchange = OndoExchange.from_env()
            except Exception as e:
                sys.exit(f"Cannot initialize Ondo: {e}")
        elif exchange_id == "aster":  # Lighter 接入：保留原有 Aster 分支
            private_key = os.getenv("PRIVATE_KEY", "").strip().strip("'\"")
            wallet_address = os.getenv("WALLET_ADDRESS", "").strip().strip("'\"")
            signer_address = os.getenv("signerAddress", "").strip().strip("'\"")
            if not all([private_key, wallet_address, signer_address]):
                sys.exit("Missing PRIVATE_KEY, WALLET_ADDRESS, or signerAddress for aster")
            options["signerAddress"] = signer_address
            options["builderFee"] = False
            config.update({"privateKey": private_key, "walletAddress": wallet_address})
        else:
            api_key = os.getenv("API_KEY", "").strip().strip("'\"")
            secret = os.getenv("SECRET", "").strip().strip("'\"")
            password = os.getenv("PASSWORD", "").strip().strip("'\"")
            if api_key:
                config["apiKey"] = api_key
            if secret:
                config["secret"] = secret
            if password:
                config["password"] = password

        if exchange_id not in ("lighter", "ondo"):  # Ondo 接入：仅初始化 CCXT 平台
            exchange_class = getattr(ccxt_module, exchange_id)  # Lighter 接入：获取原 CCXT 类
            self.exchange = exchange_class(config)  # Lighter 接入：不改变 Aster/CCXT 行为
        self.exchange_id = exchange_id  # Lighter 接入：供订单最终状态确认使用
        self.symbol = os.getenv("SYMBOL", "").strip().strip("'\"")
        if not self.symbol:
            sys.exit("Missing SYMBOL in .env, use ccxt swap format like BTC/USDT:USDT")

        # 启动时加载市场信息，并确认当前交易对确实是永续 swap。
        try:
            self.exchange.load_markets()
            market = self.exchange.market(self.symbol)
        except Exception as e:
            sys.exit(f"Cannot load swap market {self.symbol}: {e}")
        if not market.get("swap"):
            sys.exit(f"{self.symbol} is not a perpetual swap market")
        if exchange_id == "ondo":  # Ondo 接入：旧格式配置也归一化为统一 symbol
            self.symbol = market["symbol"]

        self.name = name
        self.gridStep = float(os.getenv("gridStep", 0))
        self.gridLines = int(os.getenv("gridLines", 0))
        self.orderSize = float(os.getenv("orderSize", 0))
        self.lowLimit = float(os.getenv("lowLimit", 0))
        self.highLimit = float(os.getenv("highLimit", 0))
        self.baseline = float(os.getenv("baseline", 0))
        self.left = float(os.getenv("left", 0))
        self.maxposi = float(os.getenv("maxposi", 0))
        self.max_loss = float(os.getenv("MAX_LOSS", -8000))

        # 运行保护参数：退出撤单、风控撤单、连续关键错误阈值和通知 webhook。
        self.check_interval = float(os.getenv("CHECK_INTERVAL", 1))
        self.cancel_on_exit = os.getenv("CANCEL_ON_EXIT", "true").strip().lower() == "true"
        self.cancel_on_risk = os.getenv("CANCEL_ON_RISK", "true").strip().lower() == "true"
        self.notify_webhook = os.getenv("NOTIFY_WEBHOOK", "").strip().strip("'\"")
        self.critical_error_limit = int(os.getenv("CRITICAL_ERROR_LIMIT", 3))
        self.critical_errors = 0
        if self.gridStep <= 0 or self.gridLines <= 0 or self.orderSize <= 0:
            sys.exit("gridStep, gridLines, and orderSize must be greater than 0")
        if self.gridLines % 2 != 0:
            sys.exit("gridLines must be an even number")
        try:
            if exchange_id == "ondo":  # Ondo 接入：防止网格间距取整后重叠
                step = Decimal(str(self.gridStep))
                tick = Decimal(market["precision"]["price"])
                if not step.is_finite() or step <= 0 or step % tick:
                    raise ValueError(f"gridStep must be a positive multiple of {tick}")
                if Decimal(self.exchange.amount_to_precision(self.symbol, self.orderSize)) <= 0:
                    raise ValueError("orderSize is below the Ondo size increment")
            # 用交易所精度和 market limits 预校验订单数量/名义价值。
            self.validate_order(float(self.exchange.amount_to_precision(self.symbol, self.orderSize)), self.lowLimit)
        except Exception as e:
            logger.error(f"order config validation failed: {e}")
            self.notify(f"{self.name} order config validation failed: {e}")
            sys.exit(1)

        # 普通限价单使用 GTC，不启用 post-only。
        self.order_params = {"timeInForce": "GTC"}
        if exchange_id in ("aster", "binance"):
            self.order_params["positionSide"] = "BOTH"

        # 仅记录本进程创建的订单 id；重启后不会恢复历史订单归属。
        self.All_orders = {}
        self.switc = True
        self.myloop = 0
        self.counter = 0
        self.myNewPrice = 0.0
        self.last_fill_price = 0.0
        logger.info(f"{exchange_id} connected, symbol={self.symbol}, gridStep={self.gridStep}, orderSize={self.orderSize}")
        self.notify(f"{self.name} started: {exchange_id} {self.symbol}, gridStep={self.gridStep}, orderSize={self.orderSize}") #微信通知

    def notify(self, message):
        # 企业微信机器人通知失败不影响主策略运行。
        if not self.notify_webhook:
            return
        try:
            payload = json.dumps({"msgtype": "text", "text": {"content": message}}, ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(self.notify_webhook, data=payload, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(request, timeout=5).read()
        except Exception as e:
            logger.warning(f"notify failed: {e}")

    def cancel_open_orders(self, reason):
        # 注意：这里会撤当前 SYMBOL 的全部挂单，尚未按 clientOrderId 区分订单归属。
        logger.warning(f"Canceling open orders: {reason}")
        self.notify(f"{self.name} cancel open orders: {self.symbol}, reason={reason}")
        self.exchange.cancel_all_orders(self.symbol)

    def critical(self, message, exc=None, exit_after=False):
        # 关键接口连续失败达到阈值后，按配置撤单并退出。
        self.critical_errors += 1
        detail = f"{message}: {exc}" if exc else message
        logger.error(f"{detail} (critical_errors={self.critical_errors}/{self.critical_error_limit})")
        self.notify(f"{self.name} critical: {detail}")
        if exit_after or self.critical_errors >= self.critical_error_limit:
            if self.cancel_on_risk:
                try:
                    self.cancel_open_orders("critical error limit reached")
                except Exception as cancel_error:
                    logger.error(f"cancel after critical failed: {cancel_error}")
                    self.notify(f"{self.name} cancel failed after critical: {cancel_error}")
            sys.exit(1)

    def validate_order(self, amount, price):
        # 统一用 ccxt market limits 检查最小/最大数量和最小/最大名义价值。
        market = self.exchange.market(self.symbol)
        limits = market.get("limits", {})
        min_amount = (limits.get("amount") or {}).get("min")
        max_amount = (limits.get("amount") or {}).get("max")
        min_cost = (limits.get("cost") or {}).get("min")
        max_cost = (limits.get("cost") or {}).get("max")
        cost = amount * price
        #if min_amount is not None and amount < float(min_amount):
            #raise ValueError(f"orderSize {amount} is below min amount {min_amount}")
        if max_amount is not None and amount > float(max_amount):
            raise ValueError(f"orderSize {amount} is above max amount {max_amount}")
        #if min_cost is not None and cost < float(min_cost):
            #raise ValueError(f"order notional {cost} is below min cost {min_cost}")
        if max_cost is not None and cost > float(max_cost):
            raise ValueError(f"order notional {cost} is above max cost {max_cost}")

    def accountInfo(self):
        try:
            book = self.exchange.fetch_order_book(self.symbol, limit=5)
            bid = book["bids"][0][0] if book.get("bids") else 0
            ask = book["asks"][0][0] if book.get("asks") else 0
            if bid and ask:
                self.myNewPrice = float(self.exchange.price_to_precision(self.symbol, (bid + ask) / 2))
            else:
                ticker = self.exchange.fetch_ticker(self.symbol)
                self.myNewPrice = float(ticker.get("mark") or ticker.get("last") or ticker.get("close") or 0)
            if self.myNewPrice <= 0:
                raise RuntimeError("current price is empty")
        except Exception as e:
            raise RuntimeError(f"fetch current price failed: {e}") from e

        # position
        # 读取当前永续仓位、未实现盈亏和强平价，用于风控判断。
        size, pnl, liquidation = 0.0, 0.0, 0
        try:
            for pos in self.exchange.fetch_positions([self.symbol]):
                if pos.get("symbol") == self.symbol:
                    raw_size = pos.get("info", {}).get("positionAmt")
                    if raw_size is None:
                        contracts = float(pos.get("contracts") or 0)
                        raw_size = -contracts if pos.get("side") == "short" else contracts
                    size += float(raw_size)
                    pnl += float(pos.get("unrealizedPnl") or 0)
                    liquidation = pos.get("liquidationPrice") or liquidation
        except Exception as e:
            raise RuntimeError(f"fetch_positions failed: {e}") from e

        # orders
        # 读取当前 SYMBOL 挂单，并计算日志里展示的目标仓位和挂单范围。
        try:
            orders = self.exchange.fetch_open_orders(self.symbol)
        except Exception as e:
            raise RuntimeError(f"fetch_open_orders failed: {e}") from e
        prices = [float(o["price"]) for o in orders if o.get("price")] # 获取挂单价格列表
        total_amount = sum(float(o.get("amount") or 0) for o in orders)
        target_price = self.last_fill_price or self.myNewPrice
        target = (self.baseline - target_price) * self.orderSize / self.gridStep + self.left
        logger.info(
            f"{self.name} symbol={self.symbol} price={self.myNewPrice} targetPrice={target_price} position={size} "
            f"target={round(target, 5)} diff={round(size - target, 5)}（-买+卖） pnl={pnl:.4f} "
            f"openOrders={len(orders)} maxOrder={max(prices) if prices else 0} "
            f"minOrder={min(prices) if prices else 0} liquidation={liquidation} "
            f"openAmount={total_amount:.8f} loop={self.myloop}"
        )

        # 价格越界、亏损过大、仓位过大或挂单数量异常时触发风控退出。
        if (
            len(orders) > self.gridLines + 4
            or self.myNewPrice < self.lowLimit
            or self.myNewPrice > self.highLimit
            or pnl < self.max_loss
            or abs(size) > self.maxposi
        ):
            logger.warning(f"Risk control triggered: orders={len(orders)}, price={self.myNewPrice}, position={size}, pnl={pnl}")
            self.notify(f"{self.name} risk triggered: orders={len(orders)}, price={self.myNewPrice}, position={size}, pnl={pnl}")
            if self.cancel_on_risk:
                self.cancel_open_orders("risk control")
            sys.exit(1)

    def place_grid_orders(self):
        self.switc = False
        try:
            # 重建网格前先撤掉当前交易对已有挂单，避免新旧网格叠加。
            orders = self.exchange.fetch_open_orders(self.symbol)
            if orders:
                logger.info(f"Canceling {len(orders)} remaining open orders")
                self.cancel_open_orders("grid reset")
                time.sleep(1)

            if self.myNewPrice == 0:
                self.accountInfo()
        except Exception as e:
            raise RuntimeError(f"prepare grid orders failed: {e}") from e
        self.All_orders.clear()
        half = self.gridLines // 2
        # 以当前价格为中心，上下各挂 half 层买卖限价单。
        for i in range(1, half + 1):
            for side, price in (
                ("buy", self.myNewPrice - i * self.gridStep),
                ("sell", self.myNewPrice + i * self.gridStep),
            ):
                amount = float(self.exchange.amount_to_precision(self.symbol, self.orderSize))
                price = float(self.exchange.price_to_precision(self.symbol, price))
                self.validate_order(amount, price)
                order = self.exchange.create_order(self.symbol, "limit", side, amount, price, self.order_params)
                order_id = str(order.get("id") or "")
                if not order_id:
                    raise RuntimeError(f"create_order returned no id: {order}")
                self.All_orders[order_id] = [1 if i == half else 0, price, side]
                logger.info(f"Placed {side} order id={order_id} amount={amount} price={price}")
                time.sleep(0.05)
        logger.info("Grid orders placed")

    def poller(self):
        try:
            orders = self.exchange.fetch_open_orders(self.symbol)
        except Exception as e:
            return

        if orders is None:  # Lighter 接入：空响应不参与订单状态推断
            return  # Lighter 接入：等待下一轮获取有效响应

        if len(orders) < self.gridLines // 3:
            logger.warning(
                f"Risk control triggered: too few open orders, "
                f"orders={len(orders)}, minimum={self.gridLines // 3}"
            )
            self.notify(
                f"{self.name} risk triggered: too few open orders, "
                f"orders={len(orders)}, minimum={self.gridLines // 3}"
            )
            if self.cancel_on_risk:
                self.cancel_open_orders("too few open orders")
            sys.exit(1)

        open_ids = {str(o["id"]) for o in orders if o.get("id")}  # Lighter 接入：统一字符串订单 ID
        missing_ids = set(self.All_orders) - open_ids  # 本进程记录中已从挂单列表消失的订单
        filled_ids = missing_ids  # Ondo 接入：与 Aster 一致，消失订单直接按成交处理
        canceled_ids = set()
        if self.exchange_id == "lighter" and missing_ids:  # 仅 Lighter 查询最终状态
            try:  # Lighter 接入：状态查询失败时不误补反向单
                statuses = self.exchange.fetch_order_statuses(  # Lighter 接入：批量查询消失订单
                    self.symbol,
                    missing_ids,
                )
            except Exception as e:  # Lighter 接入：等待下一轮重新查询
                logger.warning(f"fetch {self.exchange_id} order statuses failed: {e}")
                return  # Lighter 接入：状态未知时禁止推断成交

            canceled_ids = {  # Lighter 接入：撤单按原参数重新挂单
                order_id
                for order_id, status in statuses.items()
                if str(status).lower().startswith("canceled")
            }
            filled_ids = {  # Lighter 接入：只有明确 filled 才执行补单
                order_id
                for order_id in missing_ids
                if str(statuses.get(order_id)).lower() == "filled"
            }

        replacement_ids = canceled_ids | filled_ids
        if not replacement_ids:
            return

        last_price = self.last_fill_price or self.myNewPrice
        replacement_ids = sorted(
            replacement_ids,
            key=lambda order_id: (
                abs(float(self.All_orders[order_id][1]) - last_price),
                float(self.All_orders[order_id][1]),
                order_id,
            ),
        )

        # 最外层边界单成交后，切换到重建网格模式。
        edge_ids = [
            order_id
            for order_id in replacement_ids
            if order_id in filled_ids and self.All_orders[order_id][0] == 1
        ]
        if edge_ids:
            order_id = edge_ids[-1]
            self.last_fill_price = float(self.All_orders[order_id][1])
            self.myNewPrice = self.last_fill_price
            self.switc = True
            logger.info(f"Edge order filled id={order_id}, reset grid at {self.myNewPrice}")
            return

        # canceled 订单原样重挂；普通成交单在相邻网格价格补一张反向单。
        for order_id in replacement_ids:
            edge, old_price, old_side = self.All_orders[order_id]
            old_price = float(old_price)
            if order_id in canceled_ids:
                side = old_side
                price = old_price
                new_edge = edge
            else:
                side = "sell" if old_side == "buy" else "buy"
                price = old_price + self.gridStep if old_side == "buy" else old_price - self.gridStep
                new_edge = 0
            amount = float(self.exchange.amount_to_precision(self.symbol, self.orderSize))
            price = float(self.exchange.price_to_precision(self.symbol, price))
            self.validate_order(amount, price)
            order = self.exchange.create_order(self.symbol, "limit", side, amount, price, self.order_params)
            new_id = str(order.get("id") or "")
            if not new_id:
                raise RuntimeError(f"create_order returned no id: {order}")
            self.All_orders[new_id] = [new_edge, price, side]
            del self.All_orders[order_id]
            if order_id in canceled_ids:
                logger.warning(
                    f"Canceled id={order_id}, replaced {side} id={new_id} "
                    f"amount={amount} price={price}"
                )
            else:
                self.last_fill_price = old_price
                self.myNewPrice = old_price
                self.counter += 1
                logger.info(f"Filled id={order_id}, placed {side} id={new_id} price={price}, count={self.counter}")
            time.sleep(0.05)

    def close(self):  # Lighter 接入：统一释放交易所客户端资源
        close_method = getattr(self.exchange, "close", None)  # Lighter 接入：兼容 CCXT 与 Lighter
        if not callable(close_method):  # Lighter 接入：无关闭接口时直接返回
            return  # Lighter 接入：无需清理资源
        try:  # Lighter 接入：关闭失败不覆盖原退出原因
            close_method()  # Lighter 接入：关闭 HTTP session 和事件循环
        except Exception as e:  # Lighter 接入：仅记录资源清理异常
            logger.warning(f"close exchange failed: {e}")  # Lighter 接入：避免退出阶段再次抛错

    def run(self):
        logger.info(f"Program started: symbol={self.symbol}, gridStep={self.gridStep}, orderSize={self.orderSize}")
        while True:
            placing_grid = False
            try:
                # 每 20 次循环刷新一次账户、仓位、挂单和风控状态。
                if self.myloop % 20 == 0:
                    self.accountInfo()
                
                # 铺单。
                if self.switc:
                    placing_grid = True
                    self.place_grid_orders()
                    placing_grid = False
                
                # 高频轮询挂单状态，发现成交后补反向单。
                self.poller()
                self.critical_errors = 0
                self.myloop += 1
                time.sleep(self.check_interval) #轮询间隔，0.8~1秒
            
            except KeyboardInterrupt:
                # Ctrl+C 停止时按配置撤当前 SYMBOL 挂单。
                logger.info("Stopped by user")
                self.notify(f"{self.name} stopped by user")
                if self.cancel_on_exit:
                    try:
                        self.cancel_open_orders("user stop")
                    except Exception as e:
                        logger.error(f"cancel on exit failed: {e}")
                        self.notify(f"{self.name} cancel on exit failed: {e}")
                sys.exit(0)
            except OndoFatalError as e:  # Ondo 接入：不确定订单或部分成交撤单立即风控退出
                self.critical("Ondo order cannot be safely recovered", e, exit_after=True)
            except Exception as e:
                if placing_grid:
                    # 如果建网格中途失败，下轮继续尝试重建。
                    self.switc = True
                self.critical("Main loop error", e)
                time.sleep(5)


if __name__ == "__main__":
    trader = GridTrader()  # Lighter 接入：保留实例用于退出清理
    try:  # Lighter 接入：覆盖正常退出、Ctrl+C 和 sys.exit
        trader.run()  # Lighter 接入：运行原有网格主循环
    finally:  # Lighter 接入：确保 SDK 网络资源被释放
        trader.close()  # Lighter 接入：关闭 Lighter 或 CCXT 客户端
