
"""Loads config/settings.yaml and .env; validates signal_weights sum to 100."""
from __future__ import annotations
import os
import yaml
from dotenv import load_dotenv

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config", "settings.yaml")


def load_config(path: str = CONFIG_PATH) -> dict:
    load_dotenv()
    with open(path, "r") as f:
        cfg = yaml.safe_load(f)

    total_weight = sum(cfg["signal_weights"].values())
    if abs(total_weight - 100) > 0.01:
        raise ValueError(f"signal_weights in {path} must sum to 100, got {total_weight}")

    return cfg
.env
.venv/
__pycache__/
*.pyc
.pytest_cache/
*.log
data_cache/
.streamlit/
*.egg-info/
dist/
build/
"""
CLI entrypoint.

  python main.py scan                                   # full scan: core symbols + universe + gainers/losers
  python main.py scanner --top 10                        # gainers/losers only
  python main.py backtest --symbol BTCUSDT --timeframe 1h --start 2023-01-01 --end 2024-01-01
"""
from __future__ import annotations
import argparse
import logging
import sys

import pandas as pd

from config_loader import load_config
from data.market_data import BinanceSpotClient
from data.futures_data import BinanceFuturesClient
from data.news_data import NewsClient, TokenomicsClient
from analysis.pipeline import analyze_symbol
from scanner.universe import build_universe
from scanner.gainers import top_gainers
from scanner.losers import top_losers
from backtest.walk_forward import walk_forward_split
from alerts.telegram import send_telegram_alert
from alerts.webhook import send_webhook_alert

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")


def print_row(result: dict) -> None:
    print(f"{result['symbol']:<12} price={result.get('price', 'N/A'):<14} "
          f"decision={result.get('decision', 'N/A'):<12} "
          f"confidence={result.get('signal_confidence', 'N/A'):<6} "
          f"reason={result.get('reason', '')}")


def maybe_alert(result: dict, cfg: dict) -> None:
    if result.get("decision") in ("BUY SETUP", "SELL SETUP"):
        msg = f"*{result['decision']}* {result['symbol']} @ {result.get('price')} " \
              f"(confidence {result.get('signal_confidence')}) — {result.get('reason')}"
        send_telegram_alert(msg, cfg)
        send_webhook_alert(msg, cfg)


def cmd_scan(cfg: dict) -> None:
    spot, fut = BinanceSpotClient(), BinanceFuturesClient()
    news_client, token_client = NewsClient(), TokenomicsClient()

    btc_df = spot.klines("BTCUSDT", cfg["timeframes"]["primary"][-1], 300)

    print("\n=== CORE SYMBOLS ===")
    for symbol in cfg["core_symbols"]:
        result = analyze_symbol(symbol, cfg, spot, fut, news_client, token_client, btc_df=btc_df)
        print_row(result)
        maybe_alert(result, cfg)

    universe = build_universe(spot, cfg)
    print(f"\n=== UNIVERSE ({len(universe)} symbols after filters) ===")

    n = cfg["gainers_losers"]["top_n"]
    gainers = top_gainers(universe, n)
    losers = top_losers(universe, n)

    print(f"\n=== TOP {n} GAINERS ===")
    for _, row in gainers.iterrows():
        result = analyze_symbol(row["symbol"], cfg, spot, fut, news_client, token_client, btc_df=btc_df)
        print_row(result)
        maybe_alert(result, cfg)

    print(f"\n=== TOP {n} LOSERS ===")
    for _, row in losers.iterrows():
        result = analyze_symbol(row["symbol"], cfg, spot, fut, news_client, token_client, btc_df=btc_df)
        print_row(result)
        maybe_alert(result, cfg)


def cmd_scanner(cfg: dict, top_n: int) -> None:
    spot = BinanceSpotClient()
    universe = build_universe(spot, cfg)
    print(top_gainers(universe, top_n)[["symbol", "lastPrice", "priceChangePercent", "quoteVolume"]])
    print(top_losers(universe, top_n)[["symbol", "lastPrice", "priceChangePercent", "quoteVolume"]])


def cmd_backtest(cfg: dict, symbol: str, timeframe: str, start: str | None, end: str | None) -> None:
    spot = BinanceSpotClient()
    start_ms = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000) if start else None
    end_ms = int(pd.Timestamp(end, tz="UTC").timestamp() * 1000) if end else None

    frames = []
    cursor = start_ms
    while True:
        df = spot.klines(symbol, timeframe, 1000, start_time_ms=cursor, end_time_ms=end_ms)
        if df.empty:
            break
        frames.append(df)
        last_close_ms = int(df["close_time"].iloc[-1].timestamp() * 1000)
        if end_ms and last_close_ms >= end_ms:
            break
        if len(df) < 1000:
            break
        cursor = last_close_ms + 1

    full_df = pd.concat(frames).drop_duplicates(subset="open_time").reset_index(drop=True)
    print(f"Fetched {len(full_df)} candles for {symbol} {timeframe}")

    result = walk_forward_split(full_df, cfg)
    import json
    print(json.dumps(result, indent=2, default=str))


def main() -> None:
    parser = argparse.ArgumentParser(description="Crypto Multi-Factor AI Market Scanner")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("scan")

    p_scanner = sub.add_parser("scanner")
    p_scanner.add_argument("--top", type=int, default=10)

    p_bt = sub.add_parser("backtest")
    p_bt.add_argument("--symbol", required=True)
    p_bt.add_argument("--timeframe", default="1h")
    p_bt.add_argument("--start", default=None)
    p_bt.add_argument("--end", default=None)

    args = parser.parse_args()
    cfg = load_config()

    if args.command == "scan":
        cmd_scan(cfg)
    elif args.command == "scanner":
        cmd_scanner(cfg, args.top)
    elif args.command == "backtest":
        cmd_backtest(cfg, args.symbol, args.timeframe, args.start, args.end)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
      # All scanner behaviour is controlled from this file. No code edits needed.

core_symbols: ["BTCUSDT", "ETHUSDT"]

universe:
  quote_asset: "USDT"
  min_quote_volume_24h: 5000000      # USDT
  min_price: 0.0001
  max_spread_pct: 0.5                # % of mid price
  min_trading_history_days: 30
  exclude_stablecoins: true
  exclude_leveraged_tokens: true      # filters symbols containing UP/DOWN/BULL/BEAR
  max_symbols_scanned: 60             # safety cap to respect rate limits

timeframes:
  primary: ["15m", "1h"]
  higher: ["4h", "1d"]
  lower: ["1m", "5m"]
  default_backtest: "1h"

gainers_losers:
  top_n: 10

signal_weights:            # must sum to 100; enforced at load time
  market_regime: 15
  structure: 20
  liquidity: 10
  spot_flow: 10
  futures_flow: 10
  open_interest: 10
  funding: 5
  liquidations: 5
  volume: 5
  news: 5
  tokenomics: 5

thresholds:
  buy_setup_min_score: 65
  sell_setup_min_score: 65
  min_data_quality: "PARTIAL"   # GOOD | PARTIAL -> below this, force NO TRADE
  funding_extreme_pct: 0.05     # +/-0.05% per 8h considered "extreme"
  oi_abnormal_change_pct: 15.0  # % change over lookback considered abnormal
  liquidity_sweep_lookback: 50  # candles

indicators:
  rsi_period: 14
  macd_fast: 12
  macd_slow: 26
  macd_signal: 9
  ema_periods: [20, 50, 200]
  atr_period: 14
  bb_period: 20
  bb_stddev: 2
  volume_ma_period: 20

backtest:
  fee_pct: 0.04          # taker fee per side
  slippage_pct: 0.02
  max_holding_period_candles: 96
  train_test_split: 0.7

alerts:
  telegram:
    enabled: false
  webhook:
    enabled: false
  triggers:
    - breakout
    - breakdown
    - liquidity_sweep
    - choch
    - bos
    - abnormal_oi
    - abnormal_volume
    - funding_extreme
    - major_news
    - signal_change

scan_interval_seconds: 300
request_timeout_seconds: 10
max_retries: 3
"""Tiny in-memory TTL cache so repeated scans don't hammer Binance's rate limits."""
from __future__ import annotations
import time
from typing import Any, Callable, Optional


class TTLCache:
    def __init__(self) -> None:
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Optional[Any]:
        item = self._store.get(key)
        if item is None:
            return None
        expires_at, value = item
        if time.time() > expires_at:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        self._store[key] = (time.time() + ttl_seconds, value)

    def get_or_set(self, key: str, ttl_seconds: float, producer: Callable[[], Any]) -> Any:
        cached = self.get(key)
        if cached is not None:
            return cached
        value = producer()
        self.set(key, value, ttl_seconds)
        return value


CACHE = TTLCache()
"""
Binance SPOT public REST client.

Official docs: https://developers.binance.com/docs/binance-spot-api-docs/rest-api
Only public, unauthenticated market-data endpoints are used — no API key needed.

Every function returns real data straight from Binance, or raises
`DataUnavailableError` (never fabricates a value) if the request ultimately
fails after retries.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

import pandas as pd
import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from data.cache import CACHE

BASE_URL = "https://api.binance.com"
REQUEST_TIMEOUT = 10


class DataUnavailableError(Exception):
    """Raised instead of ever silently substituting fake/zero data."""


KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_asset_volume", "num_trades", "taker_buy_base_volume",
    "taker_buy_quote_volume", "ignore",
]


class BinanceSpotClient:
    def __init__(self, base_url: str = BASE_URL, timeout: int = REQUEST_TIMEOUT):
        self.base_url = base_url
        self.timeout = timeout
        self.session = requests.Session()

    @retry(
        reraise=True,
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((requests.RequestException, DataUnavailableError)),
    )
    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as e:
            raise DataUnavailableError(f"network error calling {path}: {e}") from e

        if resp.status_code == 429 or resp.status_code == 418:
            # rate-limited / banned -> back off and let tenacity retry
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                time.sleep(min(float(retry_after), 10))
            raise DataUnavailableError(f"rate limited on {path} (status {resp.status_code})")

        if resp.status_code != 200:
            raise DataUnavailableError(f"{path} returned status {resp.status_code}: {resp.text[:200]}")

        try:
            return resp.json()
        except ValueError as e:
            raise DataUnavailableError(f"invalid JSON from {path}: {e}") from e

    # ---------------------------------------------------------------- symbols

    def exchange_info(self) -> dict:
        return CACHE.get_or_set("spot_exchange_info", 3600, lambda: self._get("/api/v3/exchangeInfo"))

    def all_tradable_usdt_symbols(self, exclude_leveraged: bool = True) -> list[str]:
        info = self.exchange_info()
        out = []
        for s in info.get("symbols", []):
            if s.get("quoteAsset") != "USDT":
                continue
            if s.get("status") != "TRADING":
                continue
            if not s.get("isSpotTradingAllowed", True):
                continue
            base = s.get("baseAsset", "")
            if exclude_leveraged and any(tag in base for tag in ("UP", "DOWN", "BULL", "BEAR")):
                continue
            out.append(s["symbol"])
        return out

    # ---------------------------------------------------------------- klines

    def klines(self, symbol: str, interval: str, limit: int = 500,
               start_time_ms: Optional[int] = None, end_time_ms: Optional[int] = None) -> pd.DataFrame:
        params = {"symbol": symbol, "interval": interval, "limit": min(limit, 1000)}
        if start_time_ms:
            params["startTime"] = start_time_ms
        if end_time_ms:
            params["endTime"] = end_time_ms
        raw = self._get("/api/v3/klines", params)
        if not raw:
            raise DataUnavailableError(f"no klines returned for {symbol} {interval}")
        df = pd.DataFrame(raw, columns=KLINE_COLUMNS)
        numeric_cols = ["open", "high", "low", "close", "volume", "quote_asset_volume",
                         "taker_buy_base_volume", "taker_buy_quote_volume"]
        df[numeric_cols] = df[numeric_cols].astype(float)
        df["num_trades"] = df["num_trades"].astype(int)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
        df["symbol"] = symbol
        df["interval"] = interval
        return df.drop(columns=["ignore"])

    # ---------------------------------------------------------------- ticker

    def ticker_24hr(self, symbol: Optional[str] = None) -> Any:
        params = {"symbol": symbol} if symbol else None
        return self._get("/api/v3/ticker/24hr", params)

    def ticker_24hr_all(self) -> pd.DataFrame:
        raw = CACHE.get_or_set("spot_ticker_24hr_all", 20, lambda: self.ticker_24hr())
        df = pd.DataFrame(raw)
        numeric_cols = ["priceChangePercent", "lastPrice", "quoteVolume", "volume",
                         "bidPrice", "askPrice", "highPrice", "lowPrice"]
        for c in numeric_cols:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        return df

    def order_book(self, symbol: str, limit: int = 20) -> dict:
        return self._get("/api/v3/depth", {"symbol": symbol, "limit": limit})

    def recent_trades(self, symbol: str, limit: int = 500) -> list[dict]:
        return self._get("/api/v3/trades", {"symbol": symbol, "limit": limit})

    def spread_pct(self, symbol: str) -> float:
        book = self.order_book(symbol, limit=5)
        bids, asks = book.get("bids"), book.get("asks")
        if not bids or not asks:
            raise DataUnavailableError(f"no order book depth for {symbol}")
        best_bid, best_ask = float(bids[0][0]), float(asks[0][0])
        mid = (best_bid + best_ask) / 2
        if mid <= 0:
            raise DataUnavailableError(f"invalid mid price for {symbol}")
        return (best_ask - best_bid) / mid * 100


@dataclass
class DataQuality:
    status: str          # GOOD | PARTIAL | STALE | UNAVAILABLE
    source: str
    timestamp: Optional[pd.Timestamp]
    note: str = ""

    @staticmethod
    def good(source: str, ts: pd.Timestamp) -> "DataQuality":
        return DataQuality("GOOD", source, ts)

    @staticmethod
    def unavailable(source: str, note: str) -> "DataQuality":
        return DataQuality("UNAVAILABLE", source, None, note)
      """
Binance USDT-M FUTURES public REST client.

Official docs: https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data
Public, unauthenticated endpoints only.

LIQUIDATIONS NOTE (honesty, per spec section 27/33 "no fabrication"):
Binance's public REST no longer exposes a general historical force-orders feed
for arbitrary accounts. The only official real-time liquidation feed is the
WebSocket stream `!forceOrder@arr`
(https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/Liquidation-Order-Streams).
This client exposes a `stream_liquidations()` generator as an extension point;
`analysis/liquidation.py` uses OI + funding stress as a labeled PARTIAL proxy
when the live stream isn't running, and returns DATA UNAVAILABLE rather than a
guessed number when neither is available.
"""
from __future__ import annotations

from typing import Any, Optional

import pandas as pd
import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from data.cache import CACHE
from data.market_data import DataUnavailableError, KLINE_COLUMNS

BASE_URL = "https://fapi.binance.com"
REQUEST_TIMEOUT = 10


class BinanceFuturesClient:
    def __init__(self, base_url: str = BASE_URL, timeout: int = REQUEST_TIMEOUT):
        self.base_url = base_url
        self.timeout = timeout
        self.session = requests.Session()

    @retry(
        reraise=True,
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((requests.RequestException, DataUnavailableError)),
    )
    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as e:
            raise DataUnavailableError(f"network error calling {path}: {e}") from e
        if resp.status_code in (429, 418):
            raise DataUnavailableError(f"rate limited on {path} (status {resp.status_code})")
        if resp.status_code != 200:
            raise DataUnavailableError(f"{path} returned status {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as e:
            raise DataUnavailableError(f"invalid JSON from {path}: {e}") from e

    def has_futures_market(self, symbol: str) -> bool:
        info = CACHE.get_or_set("futures_exchange_info", 3600, lambda: self._get("/fapi/v1/exchangeInfo"))
        symbols = {s["symbol"] for s in info.get("symbols", [])}
        return symbol in symbols

    def klines(self, symbol: str, interval: str, limit: int = 500) -> pd.DataFrame:
        raw = self._get("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": min(limit, 1500)})
        if not raw:
            raise DataUnavailableError(f"no futures klines for {symbol} {interval}")
        df = pd.DataFrame(raw, columns=KLINE_COLUMNS)
        numeric_cols = ["open", "high", "low", "close", "volume", "quote_asset_volume",
                         "taker_buy_base_volume", "taker_buy_quote_volume"]
        df[numeric_cols] = df[numeric_cols].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        return df

    def open_interest(self, symbol: str) -> dict:
        """Current OI snapshot."""
        return self._get("/fapi/v1/openInterest", {"symbol": symbol})

    def open_interest_hist(self, symbol: str, period: str = "5m", limit: int = 30) -> pd.DataFrame:
        """period: 5m,15m,30m,1h,2h,4h,6h,12h,1d"""
        raw = self._get("/futures/data/openInterestHist", {"symbol": symbol, "period": period, "limit": limit})
        if not raw:
            raise DataUnavailableError(f"no OI history for {symbol}")
        df = pd.DataFrame(raw)
        df["sumOpenInterest"] = df["sumOpenInterest"].astype(float)
        df["sumOpenInterestValue"] = df["sumOpenInterestValue"].astype(float)
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        return df

    def funding_rate_current(self, symbol: str) -> dict:
        raw = self._get("/fapi/v1/premiumIndex", {"symbol": symbol})
        return raw

    def funding_rate_history(self, symbol: str, limit: int = 100) -> pd.DataFrame:
        raw = self._get("/fapi/v1/fundingRate", {"symbol": symbol, "limit": limit})
        if not raw:
            raise DataUnavailableError(f"no funding history for {symbol}")
        df = pd.DataFrame(raw)
        df["fundingRate"] = df["fundingRate"].astype(float)
        df["fundingTime"] = pd.to_datetime(df["fundingTime"], unit="ms", utc=True)
        return df

    def top_long_short_ratio(self, symbol: str, period: str = "1h", limit: int = 30) -> pd.DataFrame:
        """Top-trader long/short account ratio — best-effort positioning proxy."""
        raw = self._get("/futures/data/topLongShortAccountRatio",
                         {"symbol": symbol, "period": period, "limit": limit})
        if not raw:
            raise DataUnavailableError(f"no long/short ratio for {symbol}")
        df = pd.DataFrame(raw)
        for c in ("longAccount", "shortAccount", "longShortRatio"):
            df[c] = df[c].astype(float)
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        return df

    def stream_liquidations(self):
        """
        Extension point: connect to wss://fstream.binance.com/ws/!forceOrder@arr
        with a websocket client (e.g. `websockets` lib) to get live liquidation
        prints. Not opened automatically — implement in your own alerting loop
        if you need real-time liquidation data; this repo does not fabricate
        liquidation history where none is available.
        """
        raise NotImplementedError(
            "Live liquidation stream requires a persistent websocket connection; "
            "see docstring for the official stream URL. Not auto-started by this client."
  )
      """
News + tokenomics data sources.

News: CryptoPanic public API (https://cryptopanic.com/developers/api/) — free
tier, requires a token. Returns DATA UNAVAILABLE (never invented headlines) if
no key is configured or the request fails.

Tokenomics: CoinGecko public API (https://www.coingecko.com/en/api/documentation)
— free, no key required, for circulating/total/max supply, market cap, FDV.
Unlock *schedules* are not available from CoinGecko's free tier; that field is
always returned as UNKNOWN rather than guessed. Plug a paid provider in here if
you have one.
"""
from __future__ import annotations

import os
from typing import Optional

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

CRYPTOPANIC_URL = "https://cryptopanic.com/api/v1/posts/"
COINGECKO_URL = "https://api.coingecko.com/api/v3"


class NewsClient:
    def __init__(self):
        self.api_key = os.getenv("CRYPTOPANIC_API_KEY", "").strip()

    def available(self) -> bool:
        return bool(self.api_key)

    @retry(reraise=True, stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=6))
    def get_news(self, currency_code: str, limit: int = 15) -> list[dict]:
        """currency_code e.g. 'BTC'. Returns [] with no key configured — caller
        must treat that as DATA UNAVAILABLE, not 'no news'."""
        if not self.available():
            return []
        params = {"auth_token": self.api_key, "currencies": currency_code, "public": "true"}
        resp = requests.get(CRYPTOPANIC_URL, params=params, timeout=10)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        out = []
        for r in results[:limit]:
            out.append({
                "headline": r.get("title"),
                "source": (r.get("source") or {}).get("title"),
                "publication_time": r.get("published_at"),
                "url": r.get("url"),
                "votes": r.get("votes", {}),
                "kind": r.get("kind"),
            })
        return out


class TokenomicsClient:
    def __init__(self):
        self.session = requests.Session()

    @retry(reraise=True, stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=6))
    def get_coin_market_data(self, coingecko_id: str) -> Optional[dict]:
        """Returns None (-> DATA UNAVAILABLE upstream) rather than a guess if the
        lookup fails, e.g. because the Binance base-asset symbol doesn't map
        cleanly to a CoinGecko id."""
        try:
            resp = self.session.get(
                f"{COINGECKO_URL}/coins/{coingecko_id}",
                params={"localization": "false", "tickers": "false", "market_data": "true",
                        "community_data": "false", "developer_data": "false"},
                timeout=10,
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            md = data.get("market_data", {})
            return {
                "circulating_supply": md.get("circulating_supply"),
                "total_supply": md.get("total_supply"),
                "max_supply": md.get("max_supply"),
                "market_cap_usd": (md.get("market_cap") or {}).get("usd"),
                "fdv_usd": (md.get("fully_diluted_valuation") or {}).get("usd"),
                "unlock_schedule": "UNKNOWN",  # honestly not available from this free source
            }
        except requests.RequestException:
            return None
          """Standard technical indicators computed with pandas/numpy from real OHLCV.
Used as *supporting* evidence in the signal engine, never as standalone signals.
"""
from __future__ import annotations
import numpy as np
import pandas as pd


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50)


def ema(close: pd.Series, period: int) -> pd.Series:
    return close.ewm(span=period, adjust=False).mean()


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    fast_ema = ema(close, fast)
    slow_ema = ema(close, slow)
    macd_line = fast_ema - slow_ema
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return pd.DataFrame({"macd": macd_line, "signal": signal_line, "hist": hist})


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def vwap(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series) -> pd.Series:
    typical = (high + low + close) / 3
    return (typical * volume).cumsum() / volume.cumsum().replace(0, np.nan)


def bollinger_bands(close: pd.Series, period: int = 20, stddev: float = 2) -> pd.DataFrame:
    mid = close.rolling(period).mean()
    std = close.rolling(period).std()
    return pd.DataFrame({"bb_mid": mid, "bb_upper": mid + stddev * std, "bb_lower": mid - stddev * std})


def volume_ma(volume: pd.Series, period: int = 20) -> pd.Series:
    return volume.rolling(period).mean()


def compute_all(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """df must have columns: open, high, low, close, volume."""
    out = df.copy()
    out["rsi"] = rsi(out["close"], cfg.get("rsi_period", 14))
    macd_df = macd(out["close"], cfg.get("macd_fast", 12), cfg.get("macd_slow", 26), cfg.get("macd_signal", 9))
    out = pd.concat([out, macd_df], axis=1)
    for p in cfg.get("ema_periods", [20, 50, 200]):
        out[f"ema_{p}"] = ema(out["close"], p)
    out["atr"] = atr(out["high"], out["low"], out["close"], cfg.get("atr_period", 14))
    out["vwap"] = vwap(out["high"], out["low"], out["close"], out["volume"])
    bb = bollinger_bands(out["close"], cfg.get("bb_period", 20), cfg.get("bb_stddev", 2))
    out = pd.concat([out, bb], axis=1)
    out["volume_ma"] = volume_ma(out["volume"], cfg.get("volume_ma_period", 20))
    return out
"""
Market structure: swing-point detection, HH/HL/LH/LL labeling, Break of
Structure (BOS) and Change of Character (CHoCH).

Swing points are found with a simple fractal (N-bar left/right) method — no
RSI or oscillator is used to call a reversal, per spec ("never claim a
reversal simply because RSI is oversold/overbought").
"""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
import pandas as pd


class StructureState(str, Enum):
    BULLISH_STRUCTURE = "BULLISH_STRUCTURE"
    BEARISH_STRUCTURE = "BEARISH_STRUCTURE"
    RANGE = "RANGE"
    TRANSITION = "TRANSITION"
    UNCERTAIN = "UNCERTAIN"


@dataclass
class SwingPoint:
    index: int
    time: pd.Timestamp
    price: float
    kind: str  # "high" | "low"
    label: str = ""  # HH/HL/LH/LL once classified


def find_swing_points(df: pd.DataFrame, left: int = 3, right: int = 3) -> list[SwingPoint]:
    highs, lows = df["high"].values, df["low"].values
    points: list[SwingPoint] = []
    n = len(df)
    for i in range(left, n - right):
        window_h = highs[i - left: i + right + 1]
        window_l = lows[i - left: i + right + 1]
        if highs[i] == window_h.max() and (window_h == highs[i]).sum() == 1:
            points.append(SwingPoint(i, df["open_time"].iloc[i], highs[i], "high"))
        if lows[i] == window_l.min() and (window_l == lows[i]).sum() == 1:
            points.append(SwingPoint(i, df["open_time"].iloc[i], lows[i], "low"))
    points.sort(key=lambda p: p.index)
    return points


def label_swing_points(points: list[SwingPoint]) -> list[SwingPoint]:
    last_high, last_low = None, None
    for p in points:
        if p.kind == "high":
            if last_high is not None:
                p.label = "HH" if p.price > last_high else "LH"
            last_high = p.price
        else:
            if last_low is not None:
                p.label = "HL" if p.price > last_low else "LL"
            last_low = p.price
    return points


def detect_bos_choch(points: list[SwingPoint]) -> list[dict]:
    """Very direct rule: a break above the last confirmed swing high in a
    downtrend, or below the last swing low in an uptrend, is a CHoCH; a break
    that continues the existing trend direction is a BOS."""
    events = []
    trend = None  # "up" | "down"
    last_high = last_low = None
    for p in points:
        if p.kind == "high":
            if last_high is not None and p.price > last_high:
                if trend == "down":
                    events.append({"type": "CHoCH", "direction": "bullish", "time": p.time, "price": p.price})
                    trend = "up"
                else:
                    events.append({"type": "BOS", "direction": "bullish", "time": p.time, "price": p.price})
                    trend = "up"
            last_high = p.price
        else:
            if last_low is not None and p.price < last_low:
                if trend == "up":
                    events.append({"type": "CHoCH", "direction": "bearish", "time": p.time, "price": p.price})
                    trend = "down"
                else:
                    events.append({"type": "BOS", "direction": "bearish", "time": p.time, "price": p.price})
                    trend = "down"
            last_low = p.price
    return events


def classify_structure(points: list[SwingPoint]) -> StructureState:
    labeled = [p for p in points if p.label]
    if len(labeled) < 3:
        return StructureState.UNCERTAIN
    recent = labeled[-4:]
    labels = [p.label for p in recent]
    if labels.count("HH") + labels.count("HL") >= 3:
        return StructureState.BULLISH_STRUCTURE
    if labels.count("LH") + labels.count("LL") >= 3:
        return StructureState.BEARISH_STRUCTURE
    highs_flat = all(l in ("HH", "LH") for l in labels[-2:]) and len(set(labels[-2:])) > 1
    if highs_flat:
        return StructureState.TRANSITION
    return StructureState.RANGE


def support_resistance(df: pd.DataFrame, lookback: int = 50) -> dict:
    window = df.tail(lookback)
    return {
        "resistance": float(window["high"].max()),
        "support": float(window["low"].min()),
    }


def analyze_structure(df: pd.DataFrame, swing_left: int = 3, swing_right: int = 3) -> dict:
    if len(df) < (swing_left + swing_right + 5):
        return {"state": StructureState.UNCERTAIN.value, "events": [], "swing_points": [], "sr": None,
                "data_quality": "PARTIAL"}
    points = label_swing_points(find_swing_points(df, swing_left, swing_right))
    events = detect_bos_choch(points)
    state = classify_structure(points)
    sr = support_resistance(df)
    return {
        "state": state.value,
        "events": events[-5:],
        "swing_points": [(p.time.isoformat(), p.kind, p.label, p.price) for p in points[-8:]],
        "sr": sr,
        "data_quality": "GOOD",
}
"""Liquidity pool detection: equal highs/lows, previous day/session high-low,
sweeps, and distinguishing a liquidity-sweep-and-reclaim from a true breakout.
"""
from __future__ import annotations
import pandas as pd


def equal_levels(df: pd.DataFrame, col: str, tolerance_pct: float = 0.05, lookback: int = 50) -> list[dict]:
    window = df.tail(lookback).reset_index(drop=True)
    levels = []
    values = window[col].values
    times = window["open_time"].values
    used = set()
    for i in range(len(values)):
        if i in used:
            continue
        cluster = [i]
        for j in range(i + 1, len(values)):
            if abs(values[j] - values[i]) / values[i] * 100 <= tolerance_pct:
                cluster.append(j)
        if len(cluster) >= 2:
            used.update(cluster)
            levels.append({
                "price": float(sum(values[k] for k in cluster) / len(cluster)),
                "touches": len(cluster),
                "first_time": pd.Timestamp(times[cluster[0]]).isoformat(),
                "last_time": pd.Timestamp(times[cluster[-1]]).isoformat(),
            })
    return levels


def previous_period_levels(df: pd.DataFrame, periods: int = 1) -> dict:
    """Previous day high/low using open_time date grouping (works for intraday TFs)."""
    d = df.copy()
    d["date"] = d["open_time"].dt.date
    grouped = d.groupby("date").agg(high=("high", "max"), low=("low", "min")).reset_index()
    if len(grouped) < periods + 1:
        return {}
    prev = grouped.iloc[-(periods + 1)]
    return {"prev_day_high": float(prev["high"]), "prev_day_low": float(prev["low"])}


def detect_sweeps(df: pd.DataFrame, level_col: str, level_price: float, lookback: int = 50) -> list[dict]:
    """A sweep = a candle wicks beyond `level_price` then closes back on the
    other side within the next few candles (reclaim) vs. closing through it
    (true breakout)."""
    window = df.tail(lookback).reset_index(drop=True)
    events = []
    above = level_col == "high"
    for i, row in window.iterrows():
        wicked = (row["high"] > level_price) if above else (row["low"] < level_price)
        if not wicked:
            continue
        closed_back = (row["close"] < level_price) if above else (row["close"] > level_price)
        # look ahead up to 3 candles for reclaim confirmation / breakout confirmation
        confirm_window = window.iloc[i + 1: i + 4]
        if closed_back:
            kind = "LIQUIDITY_SWEEP_RECLAIM"
        elif not confirm_window.empty and (
            (confirm_window["close"] > level_price).all() if above else (confirm_window["close"] < level_price).all()
        ):
            kind = "TRUE_BREAKOUT" if above else "TRUE_BREAKDOWN"
        else:
            kind = "UNCONFIRMED"
        events.append({
            "type": kind,
            "level": level_price,
            "price": float(row["high"] if above else row["low"]),
            "time": row["open_time"].isoformat(),
            "volume": float(row["volume"]),
        })
    return events[-5:]


def analyze_liquidity(df: pd.DataFrame, lookback: int = 50) -> dict:
    if len(df) < 10:
        return {"data_quality": "PARTIAL", "equal_highs": [], "equal_lows": [], "prev_period": {}, "sweeps": []}
    eq_highs = equal_levels(df, "high", lookback=lookback)
    eq_lows = equal_levels(df, "low", lookback=lookback)
    prev_period = previous_period_levels(df)
    sweeps = []
    recent_high = float(df["high"].tail(lookback).max())
    recent_low = float(df["low"].tail(lookback).min())
    sweeps += detect_sweeps(df, "high", recent_high, lookback)
    sweeps += detect_sweeps(df, "low", recent_low, lookback)
    return {
        "data_quality": "GOOD",
        "equal_highs": eq_highs[:5],
        "equal_lows": eq_lows[:5],
        "prev_period": prev_period,
        "sweeps": sweeps,
}
"""
Spot and futures buy/sell flow, derived from Binance's documented
`taker_buy_base_volume` kline field (real exchange-reported taker-side volume,
not an approximation): taker BUY volume is trades that hit the ask (aggressive
buying); the remainder of `volume` is taker SELL volume (hit the bid).

This is genuine order-flow data — not invented — but it's still spot/futures
*taker* volume from klines, not full L2 book reconstruction; treat it as one
input among many, exactly as the spec requires ("do NOT treat one observation
as proof").
"""
from __future__ import annotations
import pandas as pd


def taker_buy_sell(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["taker_buy_volume"] = out["taker_buy_base_volume"]
    out["taker_sell_volume"] = out["volume"] - out["taker_buy_base_volume"]
    out["buy_sell_ratio"] = out["taker_buy_volume"] / out["taker_sell_volume"].replace(0, pd.NA)
    out["cvd"] = (out["taker_buy_volume"] - out["taker_sell_volume"]).cumsum()
    return out


def flow_summary(df: pd.DataFrame, lookback: int = 20) -> dict:
    if len(df) < 5 or "taker_buy_base_volume" not in df.columns:
        return {"data_quality": "UNAVAILABLE"}
    flow = taker_buy_sell(df).tail(lookback)
    buy_vol = float(flow["taker_buy_volume"].sum())
    sell_vol = float(flow["taker_sell_volume"].sum())
    total = buy_vol + sell_vol
    imbalance_pct = ((buy_vol - sell_vol) / total * 100) if total else 0.0
    price_change_pct = float((df["close"].iloc[-1] / df["close"].iloc[-lookback] - 1) * 100) if len(df) >= lookback else None

    interpretation = "NEUTRAL"
    if price_change_pct is not None:
        if price_change_pct > 0 and imbalance_pct > 5:
            interpretation = "STRONGER_CONFIRMATION_BULLISH"
        elif price_change_pct > 0 and imbalance_pct <= 5:
            interpretation = "FRAGILE_MOVE_UP"
        elif price_change_pct < 0 and imbalance_pct < -5:
            interpretation = "STRONGER_CONFIRMATION_BEARISH"
        elif price_change_pct < 0 and imbalance_pct >= -5:
            interpretation = "POSSIBLE_SELLING_EXHAUSTION"

    return {
        "data_quality": "GOOD",
        "buy_volume": buy_vol,
        "sell_volume": sell_vol,
        "imbalance_pct": round(imbalance_pct, 2),
        "cumulative_delta": float(flow["cvd"].iloc[-1]),
        "price_change_pct": round(price_change_pct, 2) if price_change_pct is not None else None,
        "interpretation": interpretation,
    }


def futures_context(spot_flow: dict, futures_flow: dict, oi_change_pct: float | None) -> dict:
    """Compares spot vs futures participation per spec section 10 examples."""
    if spot_flow.get("data_quality") != "GOOD" or futures_flow.get("data_quality") != "GOOD" or oi_change_pct is None:
        return {"data_quality": "PARTIAL", "note": "insufficient inputs to compare spot vs futures"}

    price_up = (futures_flow.get("price_change_pct") or 0) > 0
    note = "NO_CLEAR_PATTERN"
    if price_up and futures_flow["imbalance_pct"] > 5 and oi_change_pct > 0:
        note = "POTENTIALLY_STRONGER_TREND_PARTICIPATION"
    elif price_up and oi_change_pct > 5 and spot_flow["imbalance_pct"] < 2:
        note = "POTENTIALLY_LEVERAGE_DRIVEN_MOVE"
    elif not price_up and oi_change_pct < -5:
        note = "POSSIBLE_POSITION_CLOSING_DELEVERAGING"

    return {"data_quality": "GOOD", "interpretation": note}
"""Open Interest change analysis and the price-vs-OI interpretation matrix."""
from __future__ import annotations
import pandas as pd

MATRIX = {
    ("up", "up"): "Price up + OI up: new positions entering in the direction of the move — potential genuine trend participation.",
    ("up", "down"): "Price up + OI down: rally likely driven by short covering rather than fresh longs — can be fragile.",
    ("down", "up"): "Price down + OI up: new short positions entering — potential genuine downside trend participation.",
    ("down", "down"): "Price down + OI down: long liquidation/position closing rather than aggressive fresh shorting — deleveraging.",
}


def oi_change(oi_hist: pd.DataFrame, periods_back: int = 1) -> dict:
    if oi_hist is None or len(oi_hist) < periods_back + 1:
        return {"data_quality": "UNAVAILABLE"}
    latest = float(oi_hist["sumOpenInterest"].iloc[-1])
    prior = float(oi_hist["sumOpenInterest"].iloc[-(periods_back + 1)])
    change_pct = ((latest - prior) / prior * 100) if prior else None
    return {"data_quality": "GOOD", "current_oi": latest, "change_pct": round(change_pct, 2) if change_pct is not None else None}


def price_oi_matrix(price_change_pct: float, oi_change_pct: float, abnormal_threshold_pct: float = 15.0) -> dict:
    price_dir = "up" if price_change_pct >= 0 else "down"
    oi_dir = "up" if oi_change_pct >= 0 else "down"
    return {
        "combination": f"price_{price_dir}_oi_{oi_dir}",
        "interpretation": MATRIX[(price_dir, oi_dir)],
        "abnormal_oi_expansion": abs(oi_change_pct) >= abnormal_threshold_pct,
}
"""Funding rate classification — used only as a positioning/sentiment factor,
never alone as a directional signal (per spec)."""
from __future__ import annotations
import pandas as pd


def classify_funding(current_rate_pct: float, history: pd.DataFrame | None, extreme_threshold_pct: float = 0.05) -> dict:
    if current_rate_pct is None:
        return {"data_quality": "UNAVAILABLE"}

    if current_rate_pct >= extreme_threshold_pct:
        state = "EXTREME_POSITIVE"
    elif current_rate_pct <= -extreme_threshold_pct:
        state = "EXTREME_NEGATIVE"
    elif abs(current_rate_pct) >= extreme_threshold_pct / 2:
        state = "ELEVATED"
    else:
        state = "NORMAL"

    percentile = None
    if history is not None and len(history) >= 10:
        percentile = float((history["fundingRate"] * 100 <= current_rate_pct).mean() * 100)

    return {
        "data_quality": "GOOD",
        "current_rate_pct": round(current_rate_pct, 4),
        "state": state,
        "percentile": round(percentile, 1) if percentile is not None else None,
        "note": "Positioning/sentiment factor only — not used as a standalone directional signal.",
}
"""
Liquidation pressure — see the honesty note in data/futures_data.py.

Binance's free public REST does not provide a general historical force-order
feed. Rather than fabricate liquidation counts/volumes, this module:

  1. Uses OI-drop + price-move + funding-flip as a *labeled proxy* for probable
     long/short squeeze pressure (PARTIAL data quality, always disclosed).
  2. Returns DATA UNAVAILABLE for anything it can't derive this way (e.g. exact
     liquidation notional, individual liquidation prints).

If you run `data/futures_data.py`'s `!forceOrder@arr` websocket extension
point yourself and log prints to a dataframe, pass it in as `live_liquidations`
for GOOD-quality output.
"""
from __future__ import annotations
import pandas as pd


def estimate_squeeze_pressure(price_change_pct: float, oi_change_pct: float | None,
                               funding_state: str | None, live_liquidations: pd.DataFrame | None = None) -> dict:
    if live_liquidations is not None and len(live_liquidations) > 0:
        longs = float(live_liquidations.loc[live_liquidations["side"] == "long", "notional"].sum())
        shorts = float(live_liquidations.loc[live_liquidations["side"] == "short", "notional"].sum())
        imbalance = longs - shorts
        state = "LONG_SQUEEZE" if imbalance > 0 and shorts == 0 else (
            "SHORT_SQUEEZE" if shorts > longs else "MIXED")
        return {"data_quality": "GOOD", "source": "live_stream", "long_liq_notional": longs,
                "short_liq_notional": shorts, "state": state}

    if oi_change_pct is None:
        return {"data_quality": "UNAVAILABLE", "note": "no OI data to proxy liquidation pressure from"}

    # Proxy heuristic, clearly labeled PARTIAL: sharp price drop + sharp OI drop
    # suggests forced long closures (long squeeze); sharp price rise + sharp OI
    # drop suggests forced short closures (short squeeze).
    state = "NO_CLEAR_SIGNAL"
    if price_change_pct <= -3 and oi_change_pct <= -5:
        state = "PROBABLE_LONG_SQUEEZE"
    elif price_change_pct >= 3 and oi_change_pct <= -5:
        state = "PROBABLE_SHORT_SQUEEZE"
    elif abs(oi_change_pct) >= 15:
        state = "POSSIBLE_LIQUIDATION_CASCADE"

    return {
        "data_quality": "PARTIAL",
        "source": "oi_price_proxy",
        "state": state,
        "note": "Estimated from OI-drop + price-move, not actual liquidation prints. "
                "Run the live !forceOrder@arr stream (see data/futures_data.py) for real figures.",
}
"""BTC / overall market regime classification. Altcoins are never assumed to
follow BTC automatically — relative strength/weakness is computed explicitly."""
from __future__ import annotations
import pandas as pd
from analysis.indicators import ema, atr


def classify_btc_regime(btc_df: pd.DataFrame, structure_state: str) -> dict:
    if len(btc_df) < 60:
        return {"regime": "UNCERTAIN", "data_quality": "PARTIAL"}

    close = btc_df["close"]
    ema20, ema50, ema200 = ema(close, 20), ema(close, 50), ema(close, 200)
    price = close.iloc[-1]
    trend_up = price > ema50.iloc[-1] > ema200.iloc[-1]
    trend_down = price < ema50.iloc[-1] < ema200.iloc[-1]
    momentum_pct = float((close.iloc[-1] / close.iloc[-20] - 1) * 100)
    volatility = float(atr(btc_df["high"], btc_df["low"], btc_df["close"]).iloc[-1] / price * 100)

    if trend_up and structure_state == "BULLISH_STRUCTURE" and momentum_pct > 3:
        regime = "STRONG_BULLISH"
    elif trend_up or structure_state == "BULLISH_STRUCTURE":
        regime = "BULLISH"
    elif trend_down and structure_state == "BEARISH_STRUCTURE" and momentum_pct < -3:
        regime = "STRONG_BEARISH"
    elif trend_down or structure_state == "BEARISH_STRUCTURE":
        regime = "BEARISH"
    elif structure_state == "RANGE":
        regime = "NEUTRAL"
    else:
        regime = "UNCERTAIN"

    return {
        "regime": regime,
        "data_quality": "GOOD",
        "momentum_pct_20candle": round(momentum_pct, 2),
        "volatility_atr_pct": round(volatility, 2),
        "price": float(price),
    }


def relative_strength(alt_df: pd.DataFrame, btc_df: pd.DataFrame, lookback: int = 20) -> dict:
    if len(alt_df) < lookback or len(btc_df) < lookback:
        return {"data_quality": "PARTIAL"}
    alt_change = alt_df["close"].iloc[-1] / alt_df["close"].iloc[-lookback] - 1
    btc_change = btc_df["close"].iloc[-1] / btc_df["close"].iloc[-lookback] - 1
    diff_pct = (alt_change - btc_change) * 100
    label = "RELATIVE_STRENGTH" if diff_pct > 1 else ("RELATIVE_WEAKNESS" if diff_pct < -1 else "IN_LINE_WITH_BTC")
    return {"data_quality": "GOOD", "vs_btc_pct": round(diff_pct, 2), "label": label}
"""News impact scoring: direction/importance/credibility/freshness, not raw
sentiment. Confirmed vs unconfirmed/rumor is preserved from the source."""
from __future__ import annotations
from datetime import datetime, timezone

IMPORTANCE_KEYWORDS = {
    "HACK": 95, "SECURITY": 90, "DELISTING": 85, "REGULATION": 80, "ETF": 85,
    "TOKEN_UNLOCK": 60, "LISTING": 70, "PARTNERSHIP": 40, "MAINNET": 55,
    "UPGRADE": 45, "AIRDROP": 35, "ADOPTION": 40, "GOVERNANCE": 30, "OTHER": 20,
}

NEGATIVE_HINTS = ["hack", "exploit", "delist", "lawsuit", "ban", "investigation", "rug", "dump"]
POSITIVE_HINTS = ["listing", "partnership", "mainnet", "upgrade", "adoption", "etf approv", "burn"]


def categorize(headline: str) -> str:
    h = headline.lower()
    for cat, kws in {
        "HACK": ["hack", "exploit"], "SECURITY": ["vulnerab", "security"],
        "DELISTING": ["delist"], "LISTING": ["list on", "lists ", "will list"],
        "REGULATION": ["sec ", "regulat", "lawsuit", "ban"], "ETF": ["etf"],
        "TOKEN_UNLOCK": ["unlock"], "PARTNERSHIP": ["partner"], "MAINNET": ["mainnet"],
        "UPGRADE": ["upgrade", "hard fork"], "AIRDROP": ["airdrop"], "ADOPTION": ["adopt"],
        "GOVERNANCE": ["governance", "vote"],
    }.items():
        if any(k in h for k in kws):
            return cat
    return "OTHER"


def credibility(source: str | None) -> int:
    if not source:
        return 40
    trusted = ["binance", "coindesk", "the block", "reuters", "bloomberg", "cointelegraph"]
    return 85 if any(t in source.lower() for t in trusted) else 55


def freshness_score(published_iso: str | None) -> int:
    if not published_iso:
        return 0
    try:
        pub = datetime.fromisoformat(published_iso.replace("Z", "+00:00"))
    except ValueError:
        return 0
    hours_ago = (datetime.now(timezone.utc) - pub).total_seconds() / 3600
    if hours_ago <= 1:
        return 100
    if hours_ago <= 6:
        return 80
    if hours_ago <= 24:
        return 55
    if hours_ago <= 72:
        return 30
    return 10


def score_news_item(item: dict) -> dict:
    category = categorize(item.get("headline", ""))
    importance = IMPORTANCE_KEYWORDS.get(category, 20)
    cred = credibility(item.get("source"))
    fresh = freshness_score(item.get("publication_time"))
    h = (item.get("headline") or "").lower()
    direction = "NEUTRAL"
    if any(k in h for k in NEGATIVE_HINTS):
        direction = "-"
    elif any(k in h for k in POSITIVE_HINTS):
        direction = "+"
    confidence = round((importance * 0.4 + cred * 0.35 + fresh * 0.25))
    return {
        "headline": item.get("headline"),
        "source": item.get("source"),
        "url": item.get("url"),
        "category": category,
        "direction": direction,
        "importance": importance,
        "credibility": cred,
        "freshness": fresh,
        "confidence": confidence,
        "status": "CONFIRMED" if cred >= 70 else "UNCONFIRMED",
    }


def news_factor(news_items: list[dict], news_available: bool) -> dict:
    if not news_available:
        return {"data_quality": "UNAVAILABLE", "note": "CRYPTOPANIC_API_KEY not set"}
    if not news_items:
        return {"data_quality": "GOOD", "impact": "NEUTRAL", "confidence": 0, "items": []}
    scored = sorted((score_news_item(i) for i in news_items), key=lambda x: -x["confidence"])
    top = scored[0]
    impact = "+" if top["direction"] == "+" else ("-" if top["direction"] == "-" else "NEUTRAL")
    return {"data_quality": "GOOD", "impact": impact, "confidence": top["confidence"], "items": scored[:5]}
"""Tokenomics/supply flags. Unlock schedule is always UNKNOWN unless you wire
in a paid provider — never guessed (see data/news_data.py docstring)."""
from __future__ import annotations


def flag_tokenomics(market_data: dict | None) -> dict:
    if not market_data:
        return {"data_quality": "UNAVAILABLE", "flag": "UNKNOWN"}

    circ = market_data.get("circulating_supply")
    total = market_data.get("total_supply")
    flag = "UNKNOWN"
    if circ and total and total > 0:
        ratio = circ / total
        if ratio < 0.5:
            flag = "SUPPLY_EXPANSION"  # large remainder yet to enter circulation
        elif ratio > 0.95:
            flag = "SUPPLY_REDUCTION" if market_data.get("max_supply") and total < market_data["max_supply"] else "LOW_UNLOCK_RISK"
        else:
            flag = "HIGH_UNLOCK_RISK" if ratio < 0.8 else "LOW_UNLOCK_RISK"

    return {
        "data_quality": "GOOD",
        "circulating_supply": circ,
        "total_supply": total,
        "max_supply": market_data.get("max_supply"),
        "market_cap_usd": market_data.get("market_cap_usd"),
        "fdv_usd": market_data.get("fdv_usd"),
        "unlock_schedule": market_data.get("unlock_schedule", "UNKNOWN"),
        "flag": flag,
}
"""
Transparent, config-weighted signal engine.

Every factor contributes {value, interpretation, score(0-100), data_quality}.
The final Signal Confidence is the weighted sum from config/settings.yaml —
plain arithmetic, nothing hidden inside a model. Historical Win Probability is
NEVER computed here; it only ever comes from backtest/engine.py's own
walk-forward output for the exact same setup definition, and the caller must
supply it explicitly (or it's omitted).
"""
from __future__ import annotations
from dataclasses import dataclass, field


DATA_QUALITY_RANK = {"UNAVAILABLE": 0, "STALE": 1, "PARTIAL": 2, "GOOD": 3}


@dataclass
class FactorResult:
    name: str
    score: float           # 0-100, 50 = neutral
    interpretation: str
    data_quality: str
    raw: dict = field(default_factory=dict)


def _regime_score(regime: dict) -> FactorResult:
    mapping = {"STRONG_BULLISH": 100, "BULLISH": 75, "NEUTRAL": 50, "UNCERTAIN": 50,
               "BEARISH": 25, "STRONG_BEARISH": 0}
    r = regime.get("regime", "UNCERTAIN")
    return FactorResult("market_regime", mapping.get(r, 50), r, regime.get("data_quality", "UNAVAILABLE"), regime)


def _structure_score(structure: dict) -> FactorResult:
    mapping = {"BULLISH_STRUCTURE": 90, "TRANSITION": 55, "RANGE": 50, "UNCERTAIN": 50, "BEARISH_STRUCTURE": 10}
    s = structure.get("state", "UNCERTAIN")
    return FactorResult("structure", mapping.get(s, 50), s, structure.get("data_quality", "UNAVAILABLE"), structure)


def _liquidity_score(liquidity: dict) -> FactorResult:
    sweeps = liquidity.get("sweeps", [])
    if not sweeps:
        return FactorResult("liquidity", 50, "NO_RECENT_SWEEP", liquidity.get("data_quality", "UNAVAILABLE"), liquidity)
    last = sweeps[-1]
    mapping = {"LIQUIDITY_SWEEP_RECLAIM": 70, "TRUE_BREAKOUT": 80, "TRUE_BREAKDOWN": 20, "UNCONFIRMED": 50}
    return FactorResult("liquidity", mapping.get(last["type"], 50), last["type"],
                         liquidity.get("data_quality", "UNAVAILABLE"), liquidity)


def _flow_score(flow: dict, name: str) -> FactorResult:
    if flow.get("data_quality") != "GOOD":
        return FactorResult(name, 50, "NO_DATA", flow.get("data_quality", "UNAVAILABLE"), flow)
    imbalance = flow.get("imbalance_pct", 0)
    score = max(0, min(100, 50 + imbalance * 2.5))
    return FactorResult(name, score, flow.get("interpretation", "NEUTRAL"), "GOOD", flow)


def _oi_score(oi_matrix: dict, oi_change: dict) -> FactorResult:
    if oi_change.get("data_quality") != "GOOD":
        return FactorResult("open_interest", 50, "NO_DATA", "UNAVAILABLE", {})
    combo = oi_matrix.get("combination", "")
    mapping = {"price_up_oi_up": 75, "price_up_oi_down": 55, "price_down_oi_up": 25, "price_down_oi_down": 45}
    return FactorResult("open_interest", mapping.get(combo, 50), oi_matrix.get("interpretation", ""), "GOOD", oi_matrix)


def _funding_score(funding: dict) -> FactorResult:
    if funding.get("data_quality") != "GOOD":
        return FactorResult("funding", 50, "NO_DATA", "UNAVAILABLE", {})
    mapping = {"EXTREME_POSITIVE": 25, "ELEVATED": 40, "NORMAL": 50, "EXTREME_NEGATIVE": 75}
    # extreme positive funding = crowded longs = mild bearish tilt (contrarian), never standalone
    return FactorResult("funding", mapping.get(funding["state"], 50), funding["state"], "GOOD", funding)


def _liquidation_score(liq: dict) -> FactorResult:
    if liq.get("data_quality") == "UNAVAILABLE":
        return FactorResult("liquidations", 50, "NO_DATA", "UNAVAILABLE", {})
    mapping = {"PROBABLE_LONG_SQUEEZE": 30, "PROBABLE_SHORT_SQUEEZE": 70,
               "POSSIBLE_LIQUIDATION_CASCADE": 35, "NO_CLEAR_SIGNAL": 50,
               "LONG_SQUEEZE": 25, "SHORT_SQUEEZE": 75, "MIXED": 50}
    return FactorResult("liquidations", mapping.get(liq.get("state"), 50), liq.get("state", ""),
                         liq.get("data_quality", "PARTIAL"), liq)


def _volume_score(df) -> FactorResult:
    if len(df) < 20 or "volume_ma" not in df.columns:
        return FactorResult("volume", 50, "NO_DATA", "UNAVAILABLE", {})
    last_vol, avg_vol = df["volume"].iloc[-1], df["volume_ma"].iloc[-1]
    if avg_vol == 0 or avg_vol != avg_vol:
        return FactorResult("volume", 50, "NO_DATA", "PARTIAL", {})
    ratio = last_vol / avg_vol
    score = max(0, min(100, 50 + (ratio - 1) * 30))
    interp = "ABNORMAL_VOLUME" if ratio > 2 else ("ABOVE_AVERAGE" if ratio > 1.2 else "NORMAL")
    return FactorResult("volume", score, interp, "GOOD", {"ratio": round(float(ratio), 2)})


def _news_score(news: dict) -> FactorResult:
    if news.get("data_quality") != "GOOD":
        return FactorResult("news", 50, "NO_DATA", news.get("data_quality", "UNAVAILABLE"), news)
    mapping = {"+": 70, "NEUTRAL": 50, "-": 30}
    return FactorResult("news", mapping.get(news.get("impact", "NEUTRAL"), 50), news.get("impact", "NEUTRAL"), "GOOD", news)


def _tokenomics_score(tok: dict) -> FactorResult:
    if tok.get("data_quality") != "GOOD":
        return FactorResult("tokenomics", 50, "NO_DATA", tok.get("data_quality", "UNAVAILABLE"), tok)
    mapping = {"SUPPLY_REDUCTION": 65, "LOW_UNLOCK_RISK": 60, "HIGH_UNLOCK_RISK": 35, "SUPPLY_EXPANSION": 35, "UNKNOWN": 50}
    return FactorResult("tokenomics", mapping.get(tok.get("flag", "UNKNOWN"), 50), tok.get("flag", "UNKNOWN"), "GOOD", tok)


def data_quality_gate(factors: list[FactorResult], min_quality: str) -> tuple[bool, list[str]]:
    """Returns (passes, list_of_failing_factor_names). Only weighted factors count."""
    min_rank = DATA_QUALITY_RANK[min_quality]
    failing = [f.name for f in factors if DATA_QUALITY_RANK.get(f.data_quality, 0) < min_rank]
    # allow up to 2 soft factors (news/tokenomics) to be missing without forcing NO TRADE
    soft = {"news", "tokenomics", "liquidations", "funding"}
    hard_failing = [n for n in failing if n not in soft]
    return (len(hard_failing) == 0), failing


def compute_signal(factor_inputs: dict, weights: dict, thresholds: dict,
                    historical_win_probability: dict | None = None) -> dict:
    """
    factor_inputs keys expected: market_regime, structure, liquidity, spot_flow,
    futures_flow, open_interest_matrix, open_interest_change, funding,
    liquidations, df (indicator dataframe), news, tokenomics
    """
    factors = [
        _regime_score(factor_inputs["market_regime"]),
        _structure_score(factor_inputs["structure"]),
        _liquidity_score(factor_inputs["liquidity"]),
        _flow_score(factor_inputs["spot_flow"], "spot_flow"),
        _flow_score(factor_inputs["futures_flow"], "futures_flow"),
        _oi_score(factor_inputs["open_interest_matrix"], factor_inputs["open_interest_change"]),
        _funding_score(factor_inputs["funding"]),
        _liquidation_score(factor_inputs["liquidations"]),
        _volume_score(factor_inputs["df"]),
        _news_score(factor_inputs["news"]),
        _tokenomics_score(factor_inputs["tokenomics"]),
    ]

    passes_gate, failing = data_quality_gate(factors, thresholds.get("min_data_quality", "PARTIAL"))

    weighted_sum = 0.0
    total_weight = 0.0
    breakdown = []
    for f in factors:
        w = weights.get(f.name, 0)
        weighted_sum += f.score * w
        total_weight += w
        breakdown.append({
            "factor": f.name, "score": round(f.score, 1), "weight": w,
            "interpretation": f.interpretation, "data_quality": f.data_quality,
        })
    confidence = round(weighted_sum / total_weight, 1) if total_weight else 0.0

    structure_state = factor_inputs["structure"].get("state")
    sr = (factor_inputs["structure"].get("sr") or {})
    has_invalidation = bool(sr)
    conflicting_news = factor_inputs["news"].get("impact") == "-" and confidence > 50

    if not passes_gate:
        decision = "NO TRADE"
        reason = f"Data quality below '{thresholds.get('min_data_quality')}' for: {', '.join(failing)}"
    elif confidence >= thresholds.get("buy_setup_min_score", 65) and structure_state == "BULLISH_STRUCTURE" \
            and has_invalidation and not conflicting_news:
        decision = "BUY SETUP"
        reason = "Weighted confidence above threshold with bullish structure, defined invalidation, no conflicting news."
    elif (100 - confidence) >= thresholds.get("sell_setup_min_score", 65) and structure_state == "BEARISH_STRUCTURE" \
            and has_invalidation:
        decision = "SELL SETUP"
        reason = "Weighted confidence below inverse threshold with bearish structure and defined invalidation."
    elif structure_state in ("TRANSITION", "RANGE") or 35 < confidence < 65:
        decision = "HOLD / WAIT"
        reason = "Evidence conflicting or structure not yet decisive."
    else:
        decision = "NO TRADE"
        reason = "Minimum confirmation requirements not met."

    return {
        "decision": decision,
        "reason": reason,
        "signal_confidence": confidence,
        "historical_win_probability": historical_win_probability,  # None unless caller supplied real backtest stats
        "factor_breakdown": breakdown,
        "support_resistance": sr,
        "data_quality_gate_passed": passes_gate,
        "data_quality_failing_factors": failing,
}
"""Orchestrates one full per-symbol analysis pass: fetch real data from
Binance -> run every analysis module -> score with the signal engine.
This is the single place scanner/dashboard/main.py call into.
"""
from __future__ import annotations
import logging
import pandas as pd

from data.market_data import BinanceSpotClient, DataUnavailableError
from data.futures_data import BinanceFuturesClient
from data.news_data import NewsClient, TokenomicsClient
from analysis import indicators, structure, liquidity, order_flow, open_interest, funding, \
    liquidation, market_regime, news_analysis, tokenomics as tokenomics_mod, signal_engine

log = logging.getLogger(__name__)


def _safe(fn, *args, default=None, **kwargs):
    try:
        return fn(*args, **kwargs)
    except DataUnavailableError as e:
        log.info("data unavailable: %s", e)
        return default
    except Exception as e:  # noqa: BLE001 - never let one factor crash the whole scan
        log.warning("unexpected error in %s: %s", getattr(fn, "__name__", fn), e)
        return default


def analyze_symbol(symbol: str, cfg: dict, spot: BinanceSpotClient, fut: BinanceFuturesClient,
                    news_client: NewsClient, token_client: TokenomicsClient,
                    btc_df: pd.DataFrame | None = None, coingecko_id: str | None = None) -> dict:
    tf = cfg["timeframes"]["primary"][-1]  # e.g. "1h"
    df = _safe(spot.klines, symbol, tf, 300)
    if df is None:
        return {"symbol": symbol, "error": "DATA UNAVAILABLE", "decision": "NO TRADE"}

    df_ind = indicators.compute_all(df, cfg["indicators"])
    struct = structure.analyze_structure(df_ind)
    liq = liquidity.analyze_liquidity(df_ind, cfg["thresholds"].get("liquidity_sweep_lookback", 50))
    spot_flow = order_flow.flow_summary(df_ind)

    has_futures = _safe(fut.has_futures_market, symbol, default=False)
    fut_flow = {"data_quality": "UNAVAILABLE"}
    oi_change = {"data_quality": "UNAVAILABLE"}
    oi_matrix = {"combination": "", "interpretation": "", "abnormal_oi_expansion": False}
    fund = {"data_quality": "UNAVAILABLE"}
    liq_state = {"data_quality": "UNAVAILABLE"}

    if has_futures:
        fdf = _safe(fut.klines, symbol, tf, 300)
        if fdf is not None:
            fut_flow = order_flow.flow_summary(fdf)
        oi_hist = _safe(fut.open_interest_hist, symbol, "1h", 30)
        oi_change = open_interest.oi_change(oi_hist)
        if oi_change.get("data_quality") == "GOOD" and spot_flow.get("data_quality") == "GOOD":
            oi_matrix = open_interest.price_oi_matrix(
                spot_flow.get("price_change_pct") or 0, oi_change["change_pct"],
                cfg["thresholds"].get("oi_abnormal_change_pct", 15.0))
        premium = _safe(fut.funding_rate_current, symbol)
        fund_hist = _safe(fut.funding_rate_history, symbol, 100)
        if premium is not None:
            rate_pct = float(premium.get("lastFundingRate", 0)) * 100
            fund = funding.classify_funding(rate_pct, fund_hist, cfg["thresholds"].get("funding_extreme_pct", 0.05))
        liq_state = liquidation.estimate_squeeze_pressure(
            spot_flow.get("price_change_pct") or 0, oi_change.get("change_pct"), fund.get("state"))

    regime = {"regime": "UNCERTAIN", "data_quality": "UNAVAILABLE"}
    if btc_df is not None:
        btc_ind = indicators.compute_all(btc_df, cfg["indicators"])
        btc_struct = structure.analyze_structure(btc_ind)
        regime = market_regime.classify_btc_regime(btc_ind, btc_struct["state"])

    news_items = _safe(news_client.get_news, symbol.replace("USDT", ""), 15, default=[]) or []
    news = news_analysis.news_factor(news_items, news_client.available())

    tok_market = _safe(token_client.get_coin_market_data, coingecko_id) if coingecko_id else None
    tok = tokenomics_mod.flag_tokenomics(tok_market)

    factor_inputs = {
        "market_regime": regime, "structure": struct, "liquidity": liq,
        "spot_flow": spot_flow, "futures_flow": fut_flow,
        "open_interest_matrix": oi_matrix, "open_interest_change": oi_change,
        "funding": fund, "liquidations": liq_state, "df": df_ind,
        "news": news, "tokenomics": tok,
    }
    signal = signal_engine.compute_signal(factor_inputs, cfg["signal_weights"], cfg["thresholds"])

    return {
        "symbol": symbol,
        "price": float(df["close"].iloc[-1]),
        "timeframe": tf,
        **signal,
        "raw": {"structure": struct, "liquidity": liq, "spot_flow": spot_flow, "futures_flow": fut_flow,
                "open_interest": oi_change, "funding": fund, "liquidations": liq_state, "regime": regime,
                "news": news, "tokenomics": tok},
}
"""Dynamic symbol universe discovery — never a hard-coded symbol list."""
from __future__ import annotations
import re
import pandas as pd
from data.market_data import BinanceSpotClient

STABLECOIN_BASES = {"USDT", "USDC", "BUSD", "TUSD", "FDUSD", "DAI", "USDP", "EUR", "GBP", "TRY"}


def build_universe(client: BinanceSpotClient, cfg: dict) -> pd.DataFrame:
    ucfg = cfg["universe"]
    symbols = client.all_tradable_usdt_symbols(exclude_leveraged=ucfg.get("exclude_leveraged_tokens", True))
    tickers = client.ticker_24hr_all()
    tickers = tickers[tickers["symbol"].isin(symbols)].copy()

    if ucfg.get("exclude_stablecoins", True):
        tickers = tickers[~tickers["symbol"].apply(
            lambda s: re.sub(r"USDT$", "", s) in STABLECOIN_BASES)]

    tickers = tickers[tickers["quoteVolume"] >= ucfg.get("min_quote_volume_24h", 0)]
    tickers = tickers[tickers["lastPrice"] >= ucfg.get("min_price", 0)]
    tickers = tickers.sort_values("quoteVolume", ascending=False)
    tickers = tickers.head(ucfg.get("max_symbols_scanned", 60))
    return tickers.reset_index(drop=True)


def apply_spread_filter(client: BinanceSpotClient, symbols: list[str], max_spread_pct: float) -> list[str]:
    out = []
    for s in symbols:
        try:
            if client.spread_pct(s) <= max_spread_pct:
                out.append(s)
        except Exception:
            continue  # excluded, not silently included with fake spread
    return out
"""Top-N gainers by 24h % change, from the filtered universe (never illiquid
symbols). Ranked strictly by 24h gain — never labeled 'best trade' here."""
from __future__ import annotations
import pandas as pd


def top_gainers(universe_df: pd.DataFrame, n: int = 10) -> pd.DataFrame:
    return universe_df.sort_values("priceChangePercent", ascending=False).head(n).reset_index(drop=True)
"""Top-N losers by 24h % change, from the filtered universe."""
from __future__ import annotations
import pandas as pd


def top_losers(universe_df: pd.DataFrame, n: int = 10) -> pd.DataFrame:
    return universe_df.sort_values("priceChangePercent", ascending=True).head(n).reset_index(drop=True)
"""Standard backtest performance metrics computed from a trade log."""
from __future__ import annotations
import numpy as np
import pandas as pd


def compute_metrics(trades: pd.DataFrame, periods_per_year: float = 365 * 24) -> dict:
    """trades must have column 'return_pct' (per-trade, fees/slippage already applied)."""
    if trades.empty:
        return {"sample_size": 0, "note": "no trades generated for this setup/period"}

    returns = trades["return_pct"]
    wins = trades[returns > 0]
    losses = trades[returns <= 0]
    win_rate = len(wins) / len(trades) * 100
    gross_profit = wins["return_pct"].sum()
    gross_loss = -losses["return_pct"].sum()
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf") if gross_profit > 0 else 0.0

    equity_curve = (1 + returns / 100).cumprod()
    running_max = equity_curve.cummax()
    drawdown = (equity_curve - running_max) / running_max
    max_drawdown_pct = float(drawdown.min() * 100)

    mean_r, std_r = returns.mean(), returns.std(ddof=1) if len(returns) > 1 else 0.0
    sharpe = (mean_r / std_r * np.sqrt(periods_per_year)) if std_r else 0.0
    downside = returns[returns < 0]
    downside_std = downside.std(ddof=1) if len(downside) > 1 else 0.0
    sortino = (mean_r / downside_std * np.sqrt(periods_per_year)) if downside_std else 0.0

    # largest losing streak
    streak = max_streak = 0
    for r in returns:
        if r <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    return {
        "sample_size": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(win_rate, 2),
        "average_return_pct": round(float(mean_r), 3),
        "median_return_pct": round(float(returns.median()), 3),
        "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else None,
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "sharpe": round(float(sharpe), 2),
        "sortino": round(float(sortino), 2),
        "largest_losing_streak": max_streak,
}
"""
Walk-forward backtest engine — no look-ahead bias.

At each bar `t`, only data up to and including `t` is used to compute the
signal; any resulting trade is entered at bar `t+1`'s open (the next bar you
could actually have acted on), never at `t`'s own close.

SIMPLIFICATION DISCLOSED: full live-scan signal_engine.py uses spot+futures
flow, OI, funding, liquidations, live news and tokenomics — most of those
don't have deep enough free historical coverage to backtest honestly (e.g.
funding history and OI history on Binance's free API only go back a limited
window). This backtest therefore scores on the subset that has genuine deep
history: market structure + technical indicators + volume. That is stated
here and in the output, not hidden — treat these backtest stats as evidence
about the structure/indicator component of the strategy, not the full live
multi-factor signal.
"""
from __future__ import annotations
import pandas as pd

from analysis import indicators, structure


def _bar_signal(window: pd.DataFrame, cfg: dict) -> str:
    """BUY / SELL / NONE using only structure + indicators, computed strictly
    from `window` (which the caller must already have truncated to bar t)."""
    if len(window) < 60:
        return "NONE"
    ind = indicators.compute_all(window, cfg["indicators"])
    struct = structure.analyze_structure(ind)
    state = struct["state"]
    rsi_val = ind["rsi"].iloc[-1]
    above_ema50 = ind["close"].iloc[-1] > ind["ema_50"].iloc[-1]
    if state == "BULLISH_STRUCTURE" and above_ema50 and rsi_val < 75:
        return "BUY"
    if state == "BEARISH_STRUCTURE" and not above_ema50 and rsi_val > 25:
        return "SELL"
    return "NONE"


def run_backtest(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """df: full historical OHLCV, oldest-first. Returns a trade log dataframe
    with column 'return_pct' (fees + slippage applied)."""
    bcfg = cfg["backtest"]
    fee = bcfg.get("fee_pct", 0.04) / 100
    slip = bcfg.get("slippage_pct", 0.02) / 100
    max_hold = bcfg.get("max_holding_period_candles", 96)

    trades = []
    i = 60
    n = len(df)
    while i < n - 1:
        window = df.iloc[: i + 1]  # only data up to and including bar i -- no look-ahead
        sig = _bar_signal(window, cfg)
        if sig == "NONE":
            i += 1
            continue

        entry_idx = i + 1
        entry_price = float(df["open"].iloc[entry_idx]) * (1 + slip if sig == "BUY" else 1 - slip)
        atr_val = float(indicators.atr(window["high"], window["low"], window["close"]).iloc[-1])
        if sig == "BUY":
            stop = entry_price - 1.5 * atr_val
            target = entry_price + 3 * atr_val
        else:
            stop = entry_price + 1.5 * atr_val
            target = entry_price - 3 * atr_val

        exit_price, exit_idx, exit_reason = None, None, "MAX_HOLD"
        for j in range(entry_idx + 1, min(entry_idx + max_hold, n)):
            bar = df.iloc[j]
            if sig == "BUY":
                if bar["low"] <= stop:
                    exit_price, exit_idx, exit_reason = stop, j, "STOP"
                    break
                if bar["high"] >= target:
                    exit_price, exit_idx, exit_reason = target, j, "TARGET"
                    break
            else:
                if bar["high"] >= stop:
                    exit_price, exit_idx, exit_reason = stop, j, "STOP"
                    break
                if bar["low"] <= target:
                    exit_price, exit_idx, exit_reason = target, j, "TARGET"
                    break
        if exit_price is None:
            exit_idx = min(entry_idx + max_hold, n - 1)
            exit_price = float(df["close"].iloc[exit_idx])

        exit_price = exit_price * (1 - slip if sig == "BUY" else 1 + slip)
        gross_return_pct = ((exit_price / entry_price) - 1) * 100 if sig == "BUY" else ((entry_price / exit_price) - 1) * 100
        net_return_pct = gross_return_pct - (fee * 2 * 100)

        trades.append({
            "entry_time": df["open_time"].iloc[entry_idx], "exit_time": df["open_time"].iloc[exit_idx],
            "direction": sig, "entry_price": entry_price, "exit_price": exit_price,
            "exit_reason": exit_reason, "return_pct": net_return_pct,
        })
        i = exit_idx + 1

    return pd.DataFrame(trades)
"""Train/test (in-sample vs out-of-sample) split runner. The
'Historical Win Probability' shown in the dashboard should always be the
OUT-OF-SAMPLE (test) metrics, never the in-sample ones."""
from __future__ import annotations
import pandas as pd

from backtest.engine import run_backtest
from backtest.metrics import compute_metrics


def walk_forward_split(df: pd.DataFrame, cfg: dict) -> dict:
    split = cfg["backtest"].get("train_test_split", 0.7)
    cut = int(len(df) * split)
    train_df, test_df = df.iloc[:cut].reset_index(drop=True), df.iloc[cut:].reset_index(drop=True)

    train_trades = run_backtest(train_df, cfg)
    test_trades = run_backtest(test_df, cfg)

    return {
        "in_sample": {
            "period_start": train_df["open_time"].iloc[0].isoformat() if len(train_df) else None,
            "period_end": train_df["open_time"].iloc[-1].isoformat() if len(train_df) else None,
            "metrics": compute_metrics(train_trades),
        },
        "out_of_sample": {
            "period_start": test_df["open_time"].iloc[0].isoformat() if len(test_df) else None,
            "period_end": test_df["open_time"].iloc[-1].isoformat() if len(test_df) else None,
            "metrics": compute_metrics(test_trades),
        },
        "note": "Historical Win Probability should always be read from out_of_sample, never in_sample.",
}
"""Telegram alert sender. No-op (logs only) if not configured/enabled."""
from __future__ import annotations
import logging
import os
import requests

log = logging.getLogger(__name__)


def send_telegram_alert(message: str, cfg: dict) -> bool:
    if not cfg.get("alerts", {}).get("telegram", {}).get("enabled", False):
        return False
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        log.warning("Telegram alert skipped: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=10,
        )
        return resp.status_code == 200
    except requests.RequestException as e:
        log.warning("Telegram alert failed: %s", e)
        return False
"""Generic/Discord webhook alert sender. No-op (logs only) if not configured/enabled."""
from __future__ import annotations
import logging
import os
import requests

log = logging.getLogger(__name__)


def send_webhook_alert(message: str, cfg: dict) -> bool:
    if not cfg.get("alerts", {}).get("webhook", {}).get("enabled", False):
        return False
    url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        log.warning("Webhook alert skipped: DISCORD_WEBHOOK_URL not set")
        return False
    try:
        resp = requests.post(url, json={"content": message}, timeout=10)
        return resp.status_code in (200, 204)
    except requests.RequestException as e:
        log.warning("Webhook alert failed: %s", e)
        return False
      """Streamlit dashboard. Run with: streamlit run dashboard/app.py"""
from __future__ import annotations
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import streamlit as st

from config_loader import load_config
from data.market_data import BinanceSpotClient
from data.futures_data import BinanceFuturesClient
from data.news_data import NewsClient, TokenomicsClient
from analysis.pipeline import analyze_symbol
from scanner.universe import build_universe
from scanner.gainers import top_gainers
from scanner.losers import top_losers

st.set_page_config(page_title="Crypto Multi-Factor AI Market Scanner", layout="wide")
st.title("Crypto Multi-Factor AI Market Scanner")
st.caption("Research / paper-trading tool. Not a live trading bot. See README §2 for data-availability notes.")

cfg = load_config()
spot, fut = BinanceSpotClient(), BinanceFuturesClient()
news_client, token_client = NewsClient(), TokenomicsClient()

if not news_client.available():
    st.info("News factor is showing DATA UNAVAILABLE — set CRYPTOPANIC_API_KEY in .env to enable it.")

with st.spinner("Fetching BTC regime..."):
    btc_df = spot.klines("BTCUSDT", cfg["timeframes"]["primary"][-1], 300)

st.header("Market Regime (BTC)")
btc_result = analyze_symbol("BTCUSDT", cfg, spot, fut, news_client, token_client, btc_df=btc_df)
c1, c2, c3 = st.columns(3)
c1.metric("BTC Price", f"${btc_result.get('price', 0):,.2f}")
c2.metric("Regime", btc_result["raw"]["regime"].get("regime", "N/A"))
c3.metric("Decision", btc_result.get("decision"))

st.header("Core Symbols")
core_rows = [analyze_symbol(s, cfg, spot, fut, news_client, token_client, btc_df=btc_df) for s in cfg["core_symbols"]]
st.dataframe(pd.DataFrame([{
    "Symbol": r["symbol"], "Price": r.get("price"), "Decision": r.get("decision"),
    "Confidence": r.get("signal_confidence"), "Reason": r.get("reason"),
} for r in core_rows]), use_container_width=True)

st.header(f"Top {cfg['gainers_losers']['top_n']} Gainers / Losers")
with st.spinner("Building universe..."):
    universe = build_universe(spot, cfg)
n = cfg["gainers_losers"]["top_n"]
gcol, lcol = st.columns(2)

with gcol:
    st.subheader("Gainers")
    gainers = top_gainers(universe, n)
    g_rows = [analyze_symbol(row["symbol"], cfg, spot, fut, news_client, token_client, btc_df=btc_df)
              for _, row in gainers.iterrows()]
    st.dataframe(pd.DataFrame([{
        "Symbol": r["symbol"], "24h%": gainers.loc[gainers.symbol == r["symbol"], "priceChangePercent"].values[0],
        "Decision": r.get("decision"), "Confidence": r.get("signal_confidence"),
    } for r in g_rows]), use_container_width=True)

with lcol:
    st.subheader("Losers")
    losers = top_losers(universe, n)
    l_rows = [analyze_symbol(row["symbol"], cfg, spot, fut, news_client, token_client, btc_df=btc_df)
              for _, row in losers.iterrows()]
    st.dataframe(pd.DataFrame([{
        "Symbol": r["symbol"], "24h%": losers.loc[losers.symbol == r["symbol"], "priceChangePercent"].values[0],
        "Decision": r.get("decision"), "Confidence": r.get("signal_confidence"),
    } for r in l_rows]), use_container_width=True)

st.header("Symbol Deep-Dive")
symbol_choice = st.text_input("Symbol", "BTCUSDT").upper()
if st.button("Analyze"):
    result = analyze_symbol(symbol_choice, cfg, spot, fut, news_client, token_client, btc_df=btc_df)
    st.json(result)
    import pandas as pd
import numpy as np
from analysis.structure import find_swing_points, label_swing_points, detect_bos_choch, classify_structure, StructureState


def _zigzag(n, leg_len, up_amt, down_amt):
    """Builds a stair-step zigzag: each leg trends `up_amt` over `leg_len`
    bars then pulls back `down_amt`, producing clean alternating swing
    highs/lows (HH/HL in an uptrend, LH/LL in a downtrend)."""
    vals = [100.0]
    direction = 1
    for _ in range(n // (leg_len * 2) + 2):
        for _ in range(leg_len):
            vals.append(vals[-1] + direction * up_amt)
        for _ in range(leg_len):
            vals.append(vals[-1] - direction * down_amt)
    return np.array(vals[:n])


def make_uptrend_df(n=80, leg_len=6):
    rng = np.random.default_rng(42)
    close = _zigzag(n, leg_len, up_amt=3.0, down_amt=1.0)
    high = close + rng.uniform(0.2, 0.6, n)
    low = close - rng.uniform(0.2, 0.6, n)
    open_ = close - rng.uniform(-0.2, 0.2, n)
    times = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({"open_time": times, "open": open_, "high": high, "low": low,
                          "close": close, "volume": rng.uniform(100, 200, n)})


def make_downtrend_df(n=80, leg_len=6):
    rng = np.random.default_rng(43)
    close = _zigzag(n, leg_len, up_amt=-3.0, down_amt=-1.0)
    high = close + rng.uniform(0.2, 0.6, n)
    low = close - rng.uniform(0.2, 0.6, n)
    open_ = close - rng.uniform(-0.2, 0.2, n)
    times = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({"open_time": times, "open": open_, "high": high, "low": low,
                          "close": close, "volume": rng.uniform(100, 200, n)})


def test_swing_points_found_on_uptrend():
    df = make_uptrend_df()
    points = find_swing_points(df, left=3, right=3)
    assert len(points) > 0


def test_bullish_structure_classified_on_clear_uptrend():
    df = make_uptrend_df()
    points = label_swing_points(find_swing_points(df, left=3, right=3))
    state = classify_structure(points)
    assert state in (StructureState.BULLISH_STRUCTURE, StructureState.TRANSITION, StructureState.RANGE)


def test_bearish_structure_classified_on_clear_downtrend():
    df = make_downtrend_df()
    points = label_swing_points(find_swing_points(df, left=3, right=3))
    state = classify_structure(points)
    assert state in (StructureState.BEARISH_STRUCTURE, StructureState.TRANSITION, StructureState.RANGE)


def test_bos_choch_events_are_dicts_with_required_keys():
    df = make_uptrend_df()
    points = label_swing_points(find_swing_points(df, left=3, right=3))
    events = detect_bos_choch(points)
    for e in events:
        assert {"type", "direction", "time", "price"} <= e.keys()
        assert e["type"] in ("BOS", "CHoCH")
        import pandas as pd
import numpy as np
from analysis.indicators import rsi, ema, macd, atr, bollinger_bands, compute_all


def make_df(n=100):
    rng = np.random.default_rng(1)
    close = pd.Series(100 + np.cumsum(rng.normal(0, 1, n)))
    high = close + rng.uniform(0.1, 1, n)
    low = close - rng.uniform(0.1, 1, n)
    open_ = close.shift(1).fillna(close.iloc[0])
    volume = pd.Series(rng.uniform(100, 500, n))
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume})


def test_rsi_bounded_0_100():
    df = make_df()
    r = rsi(df["close"])
    assert r.min() >= 0 and r.max() <= 100


def test_ema_length_matches_input():
    df = make_df()
    e = ema(df["close"], 20)
    assert len(e) == len(df)


def test_macd_has_three_columns():
    df = make_df()
    m = macd(df["close"])
    assert set(["macd", "signal", "hist"]) <= set(m.columns)


def test_atr_non_negative():
    df = make_df()
    a = atr(df["high"], df["low"], df["close"])
    assert (a.dropna() >= 0).all()


def test_bollinger_upper_above_lower():
    df = make_df()
    bb = bollinger_bands(df["close"])
    valid = bb.dropna()
    assert (valid["bb_upper"] >= valid["bb_lower"]).all()


def test_compute_all_returns_expected_columns():
    df = make_df()
    cfg = {"rsi_period": 14, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
           "ema_periods": [20, 50], "atr_period": 14, "bb_period": 20, "bb_stddev": 2,
           "volume_ma_period": 20}
    out = compute_all(df, cfg)
    for col in ("rsi", "macd", "ema_20", "ema_50", "atr", "vwap", "bb_upper", "volume_ma"):
        assert col in out.columns
        import pandas as pd
from analysis.signal_engine import compute_signal, data_quality_gate, FactorResult

WEIGHTS = {
    "market_regime": 15, "structure": 20, "liquidity": 10, "spot_flow": 10,
    "futures_flow": 10, "open_interest": 10, "funding": 5, "liquidations": 5,
    "volume": 5, "news": 5, "tokenomics": 5,
}
THRESHOLDS = {"buy_setup_min_score": 65, "sell_setup_min_score": 65, "min_data_quality": "PARTIAL"}


def base_df():
    return pd.DataFrame({
        "volume": [100] * 25,
        "volume_ma": [90] * 25,
        "close": list(range(100, 125)),
    })


def test_no_trade_when_data_unavailable_on_hard_factor():
    inputs = {
        "market_regime": {"regime": "UNCERTAIN", "data_quality": "UNAVAILABLE"},
        "structure": {"state": "UNCERTAIN", "data_quality": "UNAVAILABLE", "sr": None},
        "liquidity": {"data_quality": "UNAVAILABLE", "sweeps": []},
        "spot_flow": {"data_quality": "UNAVAILABLE"},
        "futures_flow": {"data_quality": "UNAVAILABLE"},
        "open_interest_matrix": {}, "open_interest_change": {"data_quality": "UNAVAILABLE"},
        "funding": {"data_quality": "UNAVAILABLE"}, "liquidations": {"data_quality": "UNAVAILABLE"},
        "df": base_df(), "news": {"data_quality": "UNAVAILABLE"}, "tokenomics": {"data_quality": "UNAVAILABLE"},
    }
    result = compute_signal(inputs, WEIGHTS, THRESHOLDS)
    assert result["decision"] == "NO TRADE"
    assert result["data_quality_gate_passed"] is False


def test_buy_setup_requires_bullish_structure_and_invalidation():
    inputs = {
        "market_regime": {"regime": "STRONG_BULLISH", "data_quality": "GOOD"},
        "structure": {"state": "BULLISH_STRUCTURE", "data_quality": "GOOD", "sr": {"support": 90, "resistance": 120}},
        "liquidity": {"data_quality": "GOOD", "sweeps": [{"type": "TRUE_BREAKOUT"}]},
        "spot_flow": {"data_quality": "GOOD", "imbalance_pct": 10, "interpretation": "STRONGER_CONFIRMATION_BULLISH", "price_change_pct": 5},
        "futures_flow": {"data_quality": "GOOD", "imbalance_pct": 10, "interpretation": "STRONGER_CONFIRMATION_BULLISH", "price_change_pct": 5},
        "open_interest_matrix": {"combination": "price_up_oi_up", "interpretation": "trend"},
        "open_interest_change": {"data_quality": "GOOD", "change_pct": 8},
        "funding": {"data_quality": "GOOD", "state": "NORMAL"},
        "liquidations": {"data_quality": "PARTIAL", "state": "NO_CLEAR_SIGNAL"},
        "df": base_df(),
        "news": {"data_quality": "GOOD", "impact": "NEUTRAL"},
        "tokenomics": {"data_quality": "GOOD", "flag": "LOW_UNLOCK_RISK"},
    }
    result = compute_signal(inputs, WEIGHTS, THRESHOLDS)
    assert result["decision"] == "BUY SETUP"
    assert result["signal_confidence"] >= THRESHOLDS["buy_setup_min_score"]


def test_data_quality_gate_allows_soft_factor_gaps():
    factors = [
        FactorResult("structure", 80, "x", "GOOD"),
        FactorResult("news", 50, "x", "UNAVAILABLE"),
    ]
    passes, failing = data_quality_gate(factors, "PARTIAL")
    assert passes is True
    assert "news" in failing
    
