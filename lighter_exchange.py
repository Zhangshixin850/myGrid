import asyncio
import os
import time
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP


HTTP_OK = 200
AUTH_TOKEN_TTL_SECONDS = 3600
INACTIVE_ORDER_LIMIT = 100
UINT48_MAX = (1 << 48) - 1


def _get_env(name, default=""):
    value = os.getenv(name, default)
    return str(value).strip().strip("'\"")


class LighterExchange:
    id = "lighter"

    def __init__(self, network, account_index, api_key_index, api_private_key):
        try:
            import lighter
        except ImportError as exc:
            raise RuntimeError(
                "lighter-sdk is not installed; run pip install -r requirements.txt"
            ) from exc

        self._lighter = lighter
        self.network = network
        self.account_index = account_index
        self.api_key_index = api_key_index
        self._loop = asyncio.new_event_loop()
        self._client = None
        self._order_api = None
        self._account_api = None
        self._markets = {}
        self._market_details = {}
        self._last_client_order_index = 0
        self._closed = False

        try:
            self._run(self._initialize(api_private_key))
        except Exception:
            self.close()
            raise

    @classmethod
    def from_env(cls):
        network = _get_env("LIGHTER_NETWORK", "robinhood") or "robinhood"
        account_index = _get_env("LIGHTER_ACCOUNT_INDEX")
        api_key_index = _get_env("LIGHTER_API_KEY_INDEX")
        api_private_key = _get_env("LIGHTER_API_PRIVATE_KEY")

        missing = [
            name
            for name, value in (
                ("LIGHTER_ACCOUNT_INDEX", account_index),
                ("LIGHTER_API_KEY_INDEX", api_key_index),
                ("LIGHTER_API_PRIVATE_KEY", api_private_key),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"Missing Lighter settings: {', '.join(missing)}")

        try:
            account_index = int(account_index)
            api_key_index = int(api_key_index)
        except ValueError as exc:
            raise RuntimeError(
                "LIGHTER_ACCOUNT_INDEX and LIGHTER_API_KEY_INDEX must be integers"
            ) from exc

        if account_index < 0:
            raise RuntimeError("LIGHTER_ACCOUNT_INDEX must be non-negative")
        if not 2 <= api_key_index <= 254:
            raise RuntimeError("LIGHTER_API_KEY_INDEX must be between 2 and 254")

        return cls(network, account_index, api_key_index, api_private_key)

    async def _initialize(self, api_private_key):
        try:
            profile = self._lighter.get_endpoint_profile(self.network)
        except ValueError as exc:
            supported = "mainnet, testnet, robinhood, robinhood_testnet"
            raise RuntimeError(
                f"Unknown LIGHTER_NETWORK {self.network!r}; use one of: {supported}"
            ) from exc

        self._client = self._lighter.SignerClient(
            url=profile.api_url,
            account_index=self.account_index,
            api_private_keys={self.api_key_index: api_private_key},
            chain_id=profile.chain_id,
        )
        error = self._client.check_client()
        if error:
            raise RuntimeError(f"Lighter API key check failed: {error}")

        self._order_api = self._client.order_api
        self._account_api = self._lighter.AccountApi(self._client.api_client)

    def _run(self, coroutine):
        if self._closed:
            raise RuntimeError("Lighter client is closed")
        return self._loop.run_until_complete(coroutine)

    @staticmethod
    def _ensure_ok(response, action):
        if response is None:
            raise RuntimeError(f"{action} returned no response")

        code = getattr(response, "code", None)
        if code == HTTP_OK:
            return

        message = getattr(response, "message", None) or "unknown error"
        raise RuntimeError(f"{action} failed: code={code}, message={message}")

    @staticmethod
    def _native_symbol(symbol):
        return symbol.strip().upper().split("/", 1)[0].split(":", 1)[0]

    @staticmethod
    def _decimal(value):
        return Decimal(str(value))

    @staticmethod
    def _quantize(value, decimals, rounding):
        quantum = Decimal(1).scaleb(-decimals)
        return Decimal(str(value)).quantize(quantum, rounding=rounding)

    @staticmethod
    def _format_decimal(value, decimals):
        return f"{value:.{decimals}f}"

    def _auth_token(self):
        token, error = self._client.create_auth_token_with_expiry(
            deadline=AUTH_TOKEN_TTL_SECONDS,
            api_key_index=self.api_key_index,
        )
        if error:
            raise RuntimeError(f"create Lighter auth token failed: {error}")
        return token

    def load_markets(self):
        response = self._run(self._order_api.order_book_details())
        self._ensure_ok(response, "load Lighter markets")

        self._markets.clear()
        self._market_details.clear()
        for detail in response.order_book_details or []:
            if detail.market_type != "perp" or detail.status != "active":
                continue

            symbol = detail.symbol.upper()
            market = {
                "id": int(detail.market_id),
                "symbol": symbol,
                "base": symbol,
                "swap": True,
                "active": True,
                "precision": {
                    "amount": int(detail.supported_size_decimals),
                    "price": int(detail.supported_price_decimals),
                },
                "limits": {
                    "amount": {
                        "min": float(detail.min_base_amount),
                        "max": None,
                    },
                    "cost": {
                        "min": float(detail.min_quote_amount),
                        "max": float(detail.order_quote_limit),
                    },
                },
                "info": detail,
            }
            self._markets[symbol] = market
            self._market_details[symbol] = detail

        if not self._markets:
            raise RuntimeError("Lighter returned no active perpetual markets")
        return self._markets

    def market(self, symbol):
        if not self._markets:
            self.load_markets()

        native_symbol = self._native_symbol(symbol)
        market = self._markets.get(native_symbol)
        if market:
            return market

        available = ", ".join(sorted(self._markets)[:20])
        raise ValueError(
            f"Lighter perpetual market {native_symbol!r} was not found; "
            f"available examples: {available}"
        )

    def amount_to_precision(self, symbol, amount):
        decimals = self.market(symbol)["precision"]["amount"]
        value = self._quantize(amount, decimals, ROUND_DOWN)
        return self._format_decimal(value, decimals)

    def price_to_precision(self, symbol, price):
        decimals = self.market(symbol)["precision"]["price"]
        value = self._quantize(price, decimals, ROUND_HALF_UP)
        return self._format_decimal(value, decimals)

    def fetch_order_book(self, symbol, limit=5):
        market = self.market(symbol)
        response = self._run(
            self._order_api.order_book_orders(
                market_id=market["id"],
                limit=max(1, min(int(limit), 250)),
            )
        )
        self._ensure_ok(response, "fetch Lighter order book")

        bids = [
            [float(order.price), float(order.remaining_base_amount)]
            for order in response.bids or []
            if self._decimal(order.remaining_base_amount) > 0
        ]
        asks = [
            [float(order.price), float(order.remaining_base_amount)]
            for order in response.asks or []
            if self._decimal(order.remaining_base_amount) > 0
        ]
        bids.sort(key=lambda level: level[0], reverse=True)
        asks.sort(key=lambda level: level[0])
        return {"bids": bids, "asks": asks}

    def fetch_ticker(self, symbol):
        market = self.market(symbol)
        response = self._run(
            self._order_api.order_book_details(market_id=market["id"])
        )
        self._ensure_ok(response, "fetch Lighter ticker")

        details = response.order_book_details or []
        if not details:
            raise RuntimeError("Lighter ticker response contains no market")

        detail = details[0]
        mark = float(getattr(detail, "mark_price", 0) or 0)
        last = float(getattr(detail, "last_trade_price", 0) or 0)
        return {"mark": mark, "last": last, "close": last}

    def fetch_positions(self, symbols=None):
        response = self._run(
            self._account_api.account(
                by="index",
                value=str(self.account_index),
                active_only=True,
            )
        )
        self._ensure_ok(response, "fetch Lighter positions")

        requested = symbols or list(self._markets)
        symbols_by_market = {
            self.market(symbol)["id"]: symbol
            for symbol in requested
        }
        accounts = response.accounts or []
        if not accounts:
            return []

        account = next(
            (
                item
                for item in accounts
                if int(item.index) == self.account_index
            ),
            accounts[0],
        )
        positions = []
        for position in account.positions or []:
            symbol = symbols_by_market.get(int(position.market_id))
            if not symbol:
                continue

            contracts = abs(self._decimal(position.position))
            signed_size = contracts * int(position.sign)
            side = "short" if signed_size < 0 else "long"
            positions.append(
                {
                    "symbol": symbol,
                    "contracts": float(contracts),
                    "side": side,
                    "unrealizedPnl": float(position.unrealized_pnl or 0),
                    "liquidationPrice": float(position.liquidation_price or 0),
                    "info": {"positionAmt": str(signed_size)},
                }
            )
        return positions

    @staticmethod
    def _order_id(order):
        return str(order.client_order_index or order.order_index)

    def fetch_open_orders(self, symbol):
        market = self.market(symbol)
        response = self._run(
            self._order_api.account_active_orders(
                authorization=self._auth_token(),
                account_index=self.account_index,
                market_id=market["id"],
                market_type="perp",
            )
        )
        self._ensure_ok(response, "fetch Lighter open orders")

        return [
            {
                "id": self._order_id(order),
                "symbol": symbol,
                "price": float(order.price),
                "amount": float(order.initial_base_amount),
                "remaining": float(order.remaining_base_amount),
                "side": "sell" if order.is_ask else "buy",
                "status": order.status,
            }
            for order in response.orders or []
        ]

    def fetch_order_statuses(self, symbol, order_ids):
        wanted_ids = {str(order_id) for order_id in order_ids}
        if not wanted_ids:
            return {}

        market = self.market(symbol)
        response = self._run(
            self._order_api.account_inactive_orders(
                authorization=self._auth_token(),
                account_index=self.account_index,
                limit=INACTIVE_ORDER_LIMIT,
                market_id=market["id"],
                market_type="perp",
            )
        )
        self._ensure_ok(response, "fetch Lighter inactive orders")

        return {
            order_id: order.status
            for order in response.orders or []
            if (order_id := self._order_id(order)) in wanted_ids
        }

    def _validate_order(self, market, amount, price):
        min_amount = self._decimal(market["limits"]["amount"]["min"])
        min_cost = self._decimal(market["limits"]["cost"]["min"])
        max_cost = self._decimal(market["limits"]["cost"]["max"])
        cost = amount * price

        if amount <= 0 or price <= 0:
            raise ValueError("Lighter order amount and price must be positive")
        if amount < min_amount:
            raise ValueError(
                f"Lighter order amount {amount} is below minimum {min_amount}"
            )
        if cost < min_cost:
            raise ValueError(
                f"Lighter order notional {cost} is below minimum {min_cost}"
            )
        if cost > max_cost:
            raise ValueError(
                f"Lighter order notional {cost} is above maximum {max_cost}"
            )

    def _next_client_order_index(self):
        candidate = max(
            int(time.time() * 1000),
            self._last_client_order_index + 1,
        )
        if candidate > UINT48_MAX:
            raise RuntimeError("Lighter client_order_index exceeds uint48")
        self._last_client_order_index = candidate
        return candidate

    def create_order(self, symbol, order_type, side, amount, price, params=None):
        if order_type != "limit":
            raise ValueError("Lighter adapter only supports limit orders")
        if side not in {"buy", "sell"}:
            raise ValueError(f"Unsupported Lighter order side: {side}")

        time_in_force = str((params or {}).get("timeInForce", "GTC")).upper()
        if time_in_force not in {"GTC", "GTT"}:
            raise ValueError(
                f"Unsupported Lighter timeInForce: {time_in_force}"
            )

        market = self.market(symbol)
        precise_amount = self._decimal(self.amount_to_precision(symbol, amount))
        precise_price = self._decimal(self.price_to_precision(symbol, price))
        self._validate_order(market, precise_amount, precise_price)

        amount_scale = 10 ** market["precision"]["amount"]
        price_scale = 10 ** market["precision"]["price"]
        base_amount = int(precise_amount * amount_scale)
        scaled_price = int(precise_price * price_scale)
        client_order_index = self._next_client_order_index()

        _, response, error = self._run(
            self._client.create_order(
                market_index=market["id"],
                client_order_index=client_order_index,
                base_amount=base_amount,
                price=scaled_price,
                is_ask=side == "sell",
                order_type=self._client.ORDER_TYPE_LIMIT,
                time_in_force=self._client.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
                reduce_only=False,
                order_expiry=self._client.DEFAULT_28_DAY_ORDER_EXPIRY,
            )
        )
        if error:
            raise RuntimeError(f"create Lighter order failed: {error}")
        self._ensure_ok(response, "create Lighter order")

        return {
            "id": str(client_order_index),
            "info": {"txHash": response.tx_hash},
        }

    def cancel_all_orders(self, symbol):
        market = self.market(symbol)
        _, response, error = self._run(
            self._client.cancel_all_orders(
                time_in_force=self._client.CANCEL_ALL_TIF_IMMEDIATE,
                timestamp_ms=0,
                cancel_all_market_index=market["id"],
            )
        )
        if error:
            raise RuntimeError(f"cancel Lighter orders failed: {error}")
        self._ensure_ok(response, "cancel Lighter orders")
        return response

    def close(self):
        if self._closed:
            return

        try:
            if self._client and not self._loop.is_closed():
                self._loop.run_until_complete(self._client.close())
        finally:
            if not self._loop.is_closed():
                self._loop.close()
            self._closed = True
