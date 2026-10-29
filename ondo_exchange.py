import hashlib
import hmac
import json
import os
import time
import uuid
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from urllib.parse import urlsplit


class OndoFatalError(RuntimeError):
    """订单状态无法安全恢复，需要停止策略。"""


class OndoAPIError(RuntimeError):
    def __init__(self, message, code="", uncertain=False):
        super().__init__(message)
        self.code = code
        self.uncertain = uncertain


class OndoExchange:
    id = "ondo"

    def __init__(
        self, api_key_id, api_secret,
        api_url="https://api.ondoperps.xyz", timeout=10,
    ):
        try:
            import requests
        except ImportError as exc:
            raise RuntimeError(
                "requests is not installed; use requirements-ondo.txt"
            ) from exc

        if not api_key_id or not api_secret:
            raise ValueError("Missing ONDO_API_KEY_ID or ONDO_API_SECRET")
        url = urlsplit(api_url)
        if (
            url.scheme != "https"
            or url.netloc not in (
                "api.ondoperps.xyz", "api.ondoperps-sandbox.xyz"
            )
            or url.path not in ("", "/")
            or url.query or url.fragment
        ):
            raise ValueError("ONDO_API_URL must be an official Ondo API URL")
        if not 0 < timeout < float("inf"):
            raise ValueError("ONDO_TIMEOUT must be positive and finite")

        self.api_key_id = api_key_id
        self._api_secret = api_secret
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self._requests = requests
        self._session = requests.Session()
        self._markets = {}
        self._markets_by_id = {}
        self._closed = False

    @classmethod
    def from_env(cls):
        api_key_id = os.getenv("ONDO_API_KEY_ID", "").strip().strip("'\"")
        api_secret = os.getenv("ONDO_API_SECRET", "").strip().strip("'\"")
        api_url = os.getenv(
            "ONDO_API_URL", "https://api.ondoperps.xyz"
        ).strip().strip("'\"")
        timeout = float(os.getenv("ONDO_TIMEOUT", "10").strip().strip("'\""))
        return cls(api_key_id, api_secret, api_url, timeout)

    def _request(self, method, path, params=None, body=None, private=True):
        if self._closed:
            raise RuntimeError("Ondo client is closed")

        method = method.upper()
        body_text = (
            json.dumps(body, separators=(",", ":")) if body is not None else ""
        )
        request = self._requests.Request(
            method, self.api_url + path, params=params,
            data=body_text.encode("utf-8") if body is not None else None,
            headers={"Content-Type": "application/json"},
        )
        prepared = self._session.prepare_request(request)
        if private:
            timestamp = str(int(time.time() * 1000))
            # 使用最终发送的 URI 和正文，避免编码或空格导致签名不一致。
            message = timestamp + method.upper() + prepared.path_url + body_text
            signature = hmac.new(
                self._api_secret.encode(), message.encode(), hashlib.sha256
            ).hexdigest()
            prepared.headers.update({
                "ONDO-KEY-ID": self.api_key_id,
                "ONDO-TIMESTAMP": timestamp,
                "ONDO-SIGN": signature,
            })

        settings = self._session.merge_environment_settings(
            prepared.url, {}, None, None, None
        )
        try:
            response = self._session.send(
                prepared, timeout=self.timeout, allow_redirects=False, **settings
            )
        except self._requests.RequestException as exc:
            raise OndoAPIError(
                f"Ondo {method} request failed: {type(exc).__name__}",
                uncertain=method == "POST",
            ) from None

        with response:
            status = response.status_code
            try:
                payload = response.json()
            except ValueError:
                raise OndoAPIError(
                    f"Ondo returned invalid JSON (HTTP {status})",
                    uncertain=method == "POST",
                ) from None
            if (
                not 200 <= status < 300
                or not isinstance(payload, dict)
                or payload.get("success") is not True
            ):
                code, message = "", "invalid response"
                if isinstance(payload, dict):
                    code = payload.get("error_code", "")
                    message = payload.get("error", "request failed")
                detail = f"Ondo HTTP {status}: {code} {message}"
                for secret in (self.api_key_id, self._api_secret):
                    detail = detail.replace(secret, "<redacted>")
                raise OndoAPIError(
                    detail, code=code,
                    uncertain=method == "POST" and (
                        status >= 500 or 300 <= status < 400
                        or not isinstance(payload, dict)
                        or payload.get("success") is not False
                    ),
                )
            return payload

    def load_markets(self):
        response = self._request("GET", "/v1/markets")
        markets = {}
        for detail in response["result"]["perps"]["tradingPairs"]:
            market_id = detail["market"]
            if (
                not market_id.endswith(".P") or detail.get("disabled")
                or detail.get("tradingState", "TRADING") != "TRADING"
            ):
                continue
            amount_step = Decimal(detail["baseIncrement"])
            price_step = Decimal(detail["quoteIncrement"])
            if any(not step.is_finite() or step <= 0
                   for step in (amount_step, price_step)):
                raise ValueError(f"Invalid Ondo increments for {market_id}")
            pair = detail.get("pair") or {}
            base = (pair.get("base") or market_id.rsplit("-", 1)[0]).upper()
            # USDT 格式仅作配置别名，请求仍使用原生市场 ID。
            symbol = f"{base}/USDT:USDT"
            if symbol in markets:
                raise ValueError(f"Ambiguous Ondo symbol {symbol!r}")
            markets[symbol] = {
                "id": market_id,
                "symbol": symbol,
                "base": base,
                "quote": pair.get("quote") or "USD",
                "swap": True,
                "active": True,
                "contractSize": 1,
                "precision": {
                    "amount": str(amount_step), "price": str(price_step),
                },
                "limits": {
                    "amount": {"min": float(amount_step), "max": None},
                    "cost": {"min": None, "max": None},
                },
                "info": detail,
            }
        if not markets:
            raise RuntimeError("Ondo returned no active perpetual markets")
        self._markets = markets
        self._markets_by_id = {m["id"].upper(): m for m in markets.values()}
        return markets

    def market(self, symbol):
        if not self._markets:
            self.load_markets()
        symbol = symbol.strip().upper()
        market = self._markets.get(symbol) or self._markets_by_id.get(symbol)
        if market is None:
            raise ValueError(
                f"Ondo perpetual market {symbol!r} was not found; "
                "use a symbol such as BTC/USDT:USDT (also accepts BTC-USD.P)"
            )
        return market

    def amount_to_precision(self, symbol, amount):
        step = Decimal(self.market(symbol)["precision"]["amount"])
        value = Decimal(str(amount))
        if not value.is_finite() or value < 0:
            raise ValueError("Ondo amount must be non-negative and finite")
        value = (value / step).to_integral_value(rounding=ROUND_DOWN) * step
        return format(value.quantize(step), "f")

    def price_to_precision(self, symbol, price):
        step = Decimal(self.market(symbol)["precision"]["price"])
        value = Decimal(str(price))
        if not value.is_finite() or value < 0:
            raise ValueError("Ondo price must be non-negative and finite")
        value = (value / step).to_integral_value(rounding=ROUND_HALF_UP) * step
        return format(value.quantize(step), "f")

    def fetch_order_book(self, symbol, limit=5):
        market = self.market(symbol)
        response = self._request(
            "GET", "/v1/perps/depth",
            params={"market": market["id"], "depth": max(1, min(int(limit), 100))},
            private=False,
        )
        book = response["result"]
        bids = [[float(p), float(q)] for p, q in book["bids"] if Decimal(q) > 0]
        asks = [[float(p), float(q)] for p, q in book["asks"] if Decimal(q) > 0]
        bids.sort(key=lambda level: level[0], reverse=True)
        asks.sort(key=lambda level: level[0])
        return {"bids": bids, "asks": asks}

    def fetch_ticker(self, symbol):
        market = self.market(symbol)
        response = self._request("GET", "/v1/perps/mark_prices", private=False)
        detail = response["result"][market["id"]]
        mark = float(detail.get("markPrice") or detail.get("price") or 0)
        return {"mark": mark, "last": None, "close": mark}

    def fetch_positions(self, symbols=None):
        if not self._markets:
            self.load_markets()
        response = self._request("GET", "/v1/perps/positions")
        requested = symbols if symbols is not None else list(self._markets)
        symbols_by_market = {}
        for symbol in requested:
            market = self.market(symbol)
            symbols_by_market[market["id"]] = market["symbol"]
        positions = []
        for position in response["result"]:
            symbol = symbols_by_market.get(position["market"])
            if symbol is None:
                continue
            contracts = abs(Decimal(position["netQuantity"]))
            side = position["direction"]
            if side not in {"long", "short", "neutral"}:
                raise ValueError("Unknown Ondo position direction")
            size = -contracts if side == "short" else contracts
            positions.append({
                "symbol": symbol,
                "contracts": float(contracts),
                "side": side,
                "unrealizedPnl": float(position["unrealizedPnl"]),
                "liquidationPrice": float(position.get("liquidationPrice") or 0),
                "info": {"positionAmt": str(size)},
            })
        return positions

    def _parse_order(self, order):
        order_id = str(order["orderId"] or "")
        amount = Decimal(order["size"])
        filled = Decimal(order["filledSize"])
        if (
            not order_id or not amount.is_finite() or not filled.is_finite()
            or amount <= 0 or not 0 <= filled <= amount
        ):
            raise ValueError("Invalid Ondo order ID or quantity")
        status = order["status"]
        if status == "canceled" and filled > 0:
            raise OndoFatalError(
                f"Ondo order {order_id} canceled after filling {filled}/{amount}; "
                "stop instead of replacing the full quantity"
            )
        if status == "fullyfilled":
            if filled != amount:
                raise ValueError("Inconsistent Ondo fullyfilled quantity")
            status = "filled"
        return {
            "id": order_id,
            "clientOrderId": order.get("clientOrderId"),
            "symbol": self.market(order["market"])["symbol"],
            "price": float(order["price"] or 0),
            "amount": float(amount),
            "filled": float(filled),
            "remaining": float(amount - filled),
            "side": order["side"],
            "status": status,
            "info": order,
        }

    def fetch_open_orders(self, symbol):
        market = self.market(symbol)
        params = {"market": market["id"], "status": "open", "limit": 1000}
        orders, cursors = {}, set()
        while True:
            page = self._request("GET", "/v1/perps/orders", params=params)
            if not isinstance(page["result"], list):
                raise ValueError("Invalid Ondo open orders response")
            for raw in page["result"]:
                if raw["market"] != market["id"]:
                    raise ValueError("Ondo returned orders for another market")
                if raw["status"] in {"open", "pending", "untriggered"}:
                    order = self._parse_order(raw)
                    orders[order["id"]] = order
            cursor = (page.get("pageInfo") or {}).get("nextCursor")
            if not cursor:
                return list(orders.values())
            if cursor in cursors:
                raise RuntimeError("Ondo returned a repeated pagination cursor")
            cursors.add(cursor)
            params["cursor"] = cursor

    def fetch_order_statuses(self, symbol, order_ids):
        market = self.market(symbol)
        statuses = {}
        for order_id in sorted(str(value) for value in order_ids):
            try:
                response = self._request("GET", f"/v1/perps/orders/{order_id}")
            except OndoAPIError as exc:
                if exc.code == "order_not_found":
                    continue
                raise
            order = self._parse_order(response["result"])
            if order["symbol"] != market["symbol"] or order["id"] != order_id:
                raise ValueError("Ondo returned a different order")
            statuses[order_id] = order["status"]
        return statuses

    def create_order(self, symbol, order_type, side, amount, price, params=None):
        if order_type != "limit" or side not in {"buy", "sell"}:
            raise ValueError("Ondo adapter requires a buy or sell limit order")
        params = params or {}
        if (
            str(params.get("timeInForce", "GTC")).upper() != "GTC"
            or params.get("postOnly") or params.get("reduceOnly")
        ):
            raise ValueError("Ondo adapter only supports ordinary GTC orders")
        market = self.market(symbol)
        amount = self.amount_to_precision(symbol, amount)
        price = self.price_to_precision(symbol, price)
        if Decimal(amount) <= 0 or Decimal(price) <= 0:
            raise ValueError("Ondo order amount and price must be positive")
        client_id = "grid_" + uuid.uuid4().hex
        body = {
            "market": market["id"], "type": "limit", "side": side,
            "size": amount, "price": price, "clientOrderId": client_id,
            "timeInForce": "GTC", "postOnly": False, "reduceOnly": False,
        }
        # POST 只发一次；结果不确定时按客户端 ID 确认，禁止盲目重发。
        for attempt in range(4):
            if attempt > 1:
                time.sleep(0.5)
            try:
                if attempt == 0:
                    response = self._request("POST", "/v1/perps/orders", body=body)
                else:
                    response = self._request(
                        "GET", f"/v1/perps/orders/client:{client_id}"
                    )
            except OndoAPIError as exc:
                if attempt == 0 and not exc.uncertain:
                    raise
                continue
            try:
                order = self._parse_order(response["result"])
            except (KeyError, TypeError, ValueError, InvalidOperation):
                continue
            if (
                order["symbol"] == market["symbol"]
                and order["clientOrderId"] == client_id
            ):
                return order
        raise OndoFatalError(
            f"Cannot confirm Ondo order client_id={client_id}; stopping to avoid duplicates"
        )

    def cancel_all_orders(self, symbol):
        market = self.market(symbol)
        return self._request(
            "DELETE", "/v1/perps/orders", params={"market": market["id"]}
        )

    def close(self):
        if not self._closed:
            self._session.close()
            self._closed = True
