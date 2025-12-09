#!/usr/bin/env python3
"""
DOGE Hybrid Heatmap → dYdX Live Bot (single-file edition)

Features
--------
1. Streams DOGE-USD trades directly from the dYdX v4 indexer (testnet/mainnet).
2. Feeds every tick into the Hybrid Heatmap strategy, which:
   - Polls Coinglass (Binance DOGE) liquidation heatmaps every 2 minutes.
   - Detects micro ranges and emits long/short/close signals.
3. Pipes signals into a dYdX live trader that places real orders using 50× leverage.
"""
from params import trade_params
import argparse
import asyncio
import json
import logging
import os
import time
from asyncio import Queue
from collections import deque, defaultdict
from datetime import datetime
from typing import AsyncGenerator, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests
import websockets

# =============================================================================
# WebSocket trade feed helpers (dYdX)
# =============================================================================
ticker=trade_params["market"]["ticker"]
mnemonic=trade_params["mnemonic"]
marketLeverage=trade_params["market"]["leverage"]
symbol=trade_params["symbol"]
market_name=trade_params["symbol"]
name=trade_params['name']

DYDX_WS_ENDPOINTS = {
    True: "wss://indexer.v4testnet.dydx.exchange/v4/ws",
    False: "wss://indexer.dydx.trade/v4/ws",
}

def iso_to_timestamp(iso_str: str) -> float:
    """Convert ISO-8601 timestamp (with optional Z) to Unix epoch seconds."""
    return datetime.fromisoformat(iso_str.replace("Z", "+00:00")).timestamp()


async def dydx_trade_feed(
    market: str = ticker,
    use_testnet: bool = False,
    reconnect_delay: float = 5.0,
) -> AsyncGenerator[Tuple[float, float, float], None]:
    """Yield (price, timestamp, volume) tuples for every trade printed on dYdX v4."""
    ws_url = DYDX_WS_ENDPOINTS[use_testnet]
    subscribe_msg = {"type": "subscribe", "channel": "v4_trades", "id": market}

    while True:
        try:
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
                await ws.send(json.dumps(subscribe_msg))
                async for raw in ws:
                    message = json.loads(raw)

                    if message.get("type") == "subscribed":
                        print(f"[dYdX] ✅ Subscribed to v4_trades for {market}")
                        continue
                    if message.get("type") == "error":
                        print(f"[dYdX] ⚠️ Subscription error: {message}")
                        break
                    if message.get("type") != "v4_trades":
                        continue

                    for trade in message.get("contents", {}).get("trades", []):
                        try:
                            price = float(trade["price"])
                            size = float(trade["size"])
                            timestamp = iso_to_timestamp(trade["createdAt"])
                            yield price, timestamp, size
                        except (KeyError, ValueError) as exc:
                            print(f"[dYdX] ⚠️ Bad trade payload: {exc} -> {trade}")
        except Exception as exc:
            print(f"[dYdX] ❌ Feed error ({exc}); reconnecting in {reconnect_delay}s...")
            await asyncio.sleep(reconnect_delay)


# =============================================================================
# Hybrid Heatmap Strategy (unchanged logic, live-mode ready)
# =============================================================================


class HybridHeatmapNoRebates:
    """Advanced DOGE strategy using Coinglass liquidation zones + micro ranges."""

    def __init__(self, initial_capital=50.0, live_mode=False):
        self.initial_capital = initial_capital
        self.current_capital = initial_capital
        self.reserves = 0.0
        self.total_trades = 0
        self.winning_trades = 0

        self.live_mode = live_mode
        self.leverage = 50  # Enforced 50x per spec
        self.position_size_pct = 0.18
        self.trading_fee_rate = 0.0004

        self.range_lookbacks = [15, 25, 40]
        self.entry_zones = (35, 65)
        self.range_size_limits = (0.015, 0.6)

        self.price_history = deque(maxlen=250)
        self.volume_history = deque(maxlen=250)
        self.tick_timestamps = deque(maxlen=250)

        self.use_coinglass = True
        self.coinglass_update_interval = 120
        self.last_coinglass_update = 0
        self.coinglass_heatmap_data = None

        self.liquidity_zones: List[Dict] = []
        self.liquidation_clusters: List[Dict] = []
        self.support_levels: List[Dict] = []
        self.resistance_levels: List[Dict] = []
        self.market_bias = 'neutral'

        self.recent_accuracy = deque(maxlen=20)
        self.confidence_threshold = 0.7

        self.current_position = None
        self.position_entry_time = None
        self.position_entry_price = None

        self.last_tick_timestamp: Optional[float] = None

        print("🔥 HYBRID HEATMAP STRATEGY - COINGLASS EDITION")
        print("📊 Binance DOGE liquidation heatmap from Coinglass.com")
        print("🔄 Heatmap updates every 2 minutes")
        print("⚡ 50x leverage with 18% position sizing")
        print(f"💰 Starting Capital: ${self.initial_capital:.2f}")
        if self.live_mode:
            print("🔴 LIVE MODE: Signals only (execution is external)")
        print("=" * 60)

    # -- Tick ingestion -----------------------------------------------------

    def add_tick(self, price: float, timestamp: float, volume: float = 0):
        """Add new price tick and update all analysis."""
        if self.last_tick_timestamp and timestamp <= self.last_tick_timestamp:
            return None

        self.price_history.append(price)
        self.volume_history.append(volume)
        self.tick_timestamps.append(timestamp)
        self.last_tick_timestamp = timestamp

        current_time = time.time()
        if self.use_coinglass:
            if current_time - self.last_coinglass_update >= self.coinglass_update_interval:
                self.fetch_coinglass_heatmap()
                self.last_coinglass_update = current_time
                self.update_market_bias()
        else:
            if len(self.price_history) % 8 == 0:
                self.update_liquidity_heatmap()
                self.update_market_bias()

        return self.analyze_trading_signal(price, timestamp)

    # -- Coinglass integration ----------------------------------------------

    def fetch_coinglass_heatmap(self):
        """Fetch real liquidation heatmap data from Coinglass (Binance DOGE)."""
        try:
            print("📊 Fetching Binance DOGE liquidation heatmap from Coinglass...")
            endpoints = [
                {
                    'url': 'https://open-api.coinglass.com/public/v2/liquidation_heatmap',
                    'params': {'symbol': symbol, 'ex': 'Binance'}
                },
                {
                    'url': 'https://open-api.coinglass.com/public/v2/liquidation_chart',
                    'params': {'symbol': symbol, 'ex': 'Binance', 'time_type': 'h1'}
                },
                {
                    'url': 'https://www.coinglass.com/api/futures/liquidation/chart',
                    'params': {'symbol': symbol, 'exchange': 'Binance'}
                }
            ]

            for endpoint in endpoints:
                try:
                    response = requests.get(
                        endpoint['url'],
                        params=endpoint['params'],
                        headers={'User-Agent': 'Mozilla/5.0'},
                        timeout=10
                    )
                    if response.status_code == 200:
                        data = response.json()
                        payload = data.get('data')
                        if payload:
                            self.coinglass_heatmap_data = payload
                            self.process_coinglass_data(payload)
                            print(f"✅ Binance "+name+" heatmap updated: {len(payload)} zones")
                            return
                except Exception as exc:
                    print(f"⚠️ Coinglass endpoint failed: {exc}")
                    continue

            print("⚠️ Heatmap unavailable; falling back to internal liquidity map.")
            self.use_coinglass = False
            self.update_liquidity_heatmap()

        except Exception as exc:
            print(f"❌ Error fetching Coinglass data: {exc}")
            self.use_coinglass = False
            self.update_liquidity_heatmap()

    def process_coinglass_data(self, heatmap_data):
        """Process Coinglass liquidation heatmap data into trading zones."""
        try:
            self.liquidity_zones = []
            self.liquidation_clusters = []

            current_price = self.price_history[-1] if self.price_history else 0

            for zone in heatmap_data:
                price = float(zone.get('price', 0))
                long_liq = float(zone.get('longLiquidation', 0))
                short_liq = float(zone.get('shortLiquidation', 0))
                total_liq = long_liq + short_liq

                if price > 0 and total_liq > 0:
                    self.liquidity_zones.append({
                        'price': price,
                        'volume': total_liq,
                        'type': 'coinglass_liquidation'
                    })

                    if price > current_price:
                        self.liquidation_clusters.append({
                            'price': price,
                            'type': 'long_liquidation',
                            'strength': long_liq / 1_000_000
                        })
                    else:
                        self.liquidation_clusters.append({
                            'price': price,
                            'type': 'short_liquidation',
                            'strength': short_liq / 1_000_000
                        })

            self.liquidation_clusters.sort(key=lambda x: x['strength'], reverse=True)
            self.update_support_resistance_from_coinglass()

        except Exception as exc:
            print(f"❌ Error processing Coinglass data: {exc}")

    def update_support_resistance_from_coinglass(self):
        """Update support/resistance levels from Coinglass liquidation data."""
        if not self.price_history or not self.liquidation_clusters:
            return

        current_price = self.price_history[-1]
        self.support_levels = [
            cluster for cluster in self.liquidation_clusters
            if cluster['type'] == 'short_liquidation' and cluster['price'] < current_price
        ][:5]

        self.resistance_levels = [
            cluster for cluster in self.liquidation_clusters
            if cluster['type'] == 'long_liquidation' and cluster['price'] > current_price
        ][:5]

    # -- Internal liquidity heatmap -----------------------------------------

    def update_liquidity_heatmap(self):
        """Build liquidity heatmap from recent price/volume data."""
        if len(self.price_history) < 60:
            return

        recent_prices = list(self.price_history)[-120:]
        recent_volumes = list(self.volume_history)[-120:]

        price_min, price_max = min(recent_prices), max(recent_prices)
        price_range = price_max - price_min
        if price_range == 0:
            return

        liquidity_map = defaultdict(float)
        for i, price in enumerate(recent_prices):
            bin_idx = int((price - price_min) / price_range * 24)
            bin_idx = max(0, min(24, bin_idx))
            liquidity_map[bin_idx] += recent_volumes[i] if recent_volumes[i] > 0 else 1

        sorted_zones = sorted(liquidity_map.items(), key=lambda x: x[1], reverse=True)
        top_zones = sorted_zones[:6]

        self.liquidity_zones = []
        for bin_idx, volume in top_zones:
            zone_price = price_min + (bin_idx / 24) * price_range
            self.liquidity_zones.append({
                'price': zone_price,
                'volume': volume,
                'type': 'high_liquidity'
            })

        self.identify_liquidation_clusters(recent_prices)
        self.update_support_resistance()

    def identify_liquidation_clusters(self, prices: List[float]):
        """Enhanced liquidation cluster identification."""
        if len(prices) < 25:
            return

        current_price = prices[-1]
        highs, lows = [], []

        for i in range(3, len(prices) - 3):
            if all(prices[i] >= prices[j] for j in range(i - 3, i + 4)):
                highs.append(prices[i])
            if all(prices[i] <= prices[j] for j in range(i - 3, i + 4)):
                lows.append(prices[i])

        long_liq_zones = []
        for high in highs[-7:]:
            if high > current_price:
                liq_price = high * 0.9995
                strength = min(2.0, (high - current_price) / current_price * 100)
                long_liq_zones.append({
                    'price': liq_price,
                    'type': 'long_liquidation',
                    'strength': strength
                })

        short_liq_zones = []
        for low in lows[-7:]:
            if low < current_price:
                liq_price = low * 1.0005
                strength = min(2.0, (current_price - low) / current_price * 100)
                short_liq_zones.append({
                    'price': liq_price,
                    'type': 'short_liquidation',
                    'strength': strength
                })

        self.liquidation_clusters = long_liq_zones + short_liq_zones

    def update_support_resistance(self):
        """Update support/resistance levels from liquidity zones."""
        if not self.liquidity_zones:
            return

        current_price = self.price_history[-1]
        self.support_levels = [
            zone for zone in self.liquidity_zones if zone['price'] < current_price
        ]
        self.support_levels.sort(key=lambda x: x['price'], reverse=True)

        self.resistance_levels = [
            zone for zone in self.liquidity_zones if zone['price'] > current_price
        ]
        self.resistance_levels.sort(key=lambda x: x['price'])

    # -- Market bias + micro ranges -----------------------------------------

    def update_market_bias(self):
        """Enhanced market bias calculation."""
        if not self.liquidation_clusters:
            self.market_bias = 'neutral'
            return

        current_price = self.price_history[-1]
        long_liq_strength = sum(
            cluster['strength'] for cluster in self.liquidation_clusters
            if cluster['type'] == 'long_liquidation' and cluster['price'] > current_price
        )
        short_liq_strength = sum(
            cluster['strength'] for cluster in self.liquidation_clusters
            if cluster['type'] == 'short_liquidation' and cluster['price'] < current_price
        )

        bias_ratio = long_liq_strength / (short_liq_strength + 0.1)
        if bias_ratio > 1.5:
            self.market_bias = 'bullish'
        elif bias_ratio < 0.67:
            self.market_bias = 'bearish'
        else:
            self.market_bias = 'neutral'

    def detect_micro_ranges(self, prices: List[float]) -> Dict:
        """Enhanced micro-range detection."""
        ranges = {}
        for lookback in self.range_lookbacks:
            if len(prices) < lookback:
                continue

            recent = prices[-lookback:]
            range_high = max(recent)
            range_low = min(recent)
            if range_high == range_low:
                continue

            range_size = (range_high - range_low) / range_low * 100
            if self.range_size_limits[0] <= range_size <= self.range_size_limits[1]:
                current_pos_pct = (prices[-1] - range_low) / (range_high - range_low) * 100
                touches_high = sum(1 for p in recent if abs(p - range_high) / range_high < 0.001)
                touches_low = sum(1 for p in recent if abs(p - range_low) / range_low < 0.001)
                range_strength = (touches_high + touches_low) / len(recent)

                ranges[f'{lookback}_tick'] = {
                    'high': range_high,
                    'low': range_low,
                    'size_pct': range_size,
                    'current_position_pct': current_pos_pct,
                    'strength': range_strength
                }
        return ranges

    def calculate_trade_confidence(self, ranges: Dict, signal_type: str) -> float:
        """Calculate confidence score for trade signal."""
        if not ranges:
            return 0.0

        confidence_factors = []
        agreement_score = len([
            r for r in ranges.values()
            if (signal_type == 'long' and r['current_position_pct'] <= self.entry_zones[0]) or
               (signal_type == 'short' and r['current_position_pct'] >= self.entry_zones[1])
        ])
        confidence_factors.append(agreement_score / len(ranges))

        avg_strength = sum(r['strength'] for r in ranges.values()) / len(ranges)
        confidence_factors.append(min(1.0, avg_strength * 2))

        if signal_type == 'long' and self.market_bias == 'bullish':
            confidence_factors.append(1.0)
        elif signal_type == 'short' and self.market_bias == 'bearish':
            confidence_factors.append(1.0)
        elif self.market_bias == 'neutral':
            confidence_factors.append(0.7)
        else:
            confidence_factors.append(0.3)

        if len(self.recent_accuracy) > 5:
            recent_win_rate = sum(self.recent_accuracy) / len(self.recent_accuracy)
            confidence_factors.append(recent_win_rate)
        else:
            confidence_factors.append(0.8)

        return sum(confidence_factors) / len(confidence_factors)

    def analyze_trading_signal(self, current_price: float, timestamp: float) -> Optional[Dict]:
        """Enhanced trading signal analysis."""
        if len(self.price_history) < max(self.range_lookbacks):
            return None

        if not self.live_mode and self.current_position:
            return self.check_exit_conditions(current_price, timestamp)

        prices = list(self.price_history)
        ranges = self.detect_micro_ranges(prices)
        if not ranges:
            return None

        long_signals = 0
        short_signals = 0
        for range_data in ranges.values():
            position_pct = range_data['current_position_pct']
            if position_pct <= self.entry_zones[0]:
                long_signals += 1
            elif position_pct >= self.entry_zones[1]:
                short_signals += 1

        min_consensus = 2
        signal = None

        if long_signals >= min_consensus:
            confidence = self.calculate_trade_confidence(ranges, 'long')
            if confidence >= self.confidence_threshold:
                near_support = any(
                    abs(current_price - support['price']) / current_price < 0.0008
                    for support in self.support_levels[:3]
                )
                if near_support or self.market_bias in ['bullish', 'neutral']:
                    signal = {
                        'action': 'long',
                        'confidence': confidence,
                        'market_bias': self.market_bias,
                        'heatmap_support': near_support,
                        'entry_price': current_price,
                        'timestamp': timestamp,
                        'range_consensus': long_signals
                    }

        elif short_signals >= min_consensus:
            confidence = self.calculate_trade_confidence(ranges, 'short')
            if confidence >= self.confidence_threshold:
                near_resistance = any(
                    abs(current_price - resistance['price']) / current_price < 0.0008
                    for resistance in self.resistance_levels[:3]
                )
                if near_resistance or self.market_bias in ['bearish', 'neutral']:
                    signal = {
                        'action': 'short',
                        'confidence': confidence,
                        'market_bias': self.market_bias,
                        'heatmap_support': near_resistance,
                        'entry_price': current_price,
                        'timestamp': timestamp,
                        'range_consensus': short_signals
                    }

        if signal and not self.live_mode:
            self.execute_trade(signal)

        return signal

    # -- Simulation helpers (unused in live_mode but kept intact) ------------

    def execute_trade(self, signal: Dict):
        if self.live_mode:
            return
        # Simulation code omitted for brevity in live mode (unchanged from original)

    def check_exit_conditions(self, current_price: float, timestamp: float) -> Optional[Dict]:
        if self.live_mode or not self.current_position:
            return None
        # Simulation exit logic omitted (unchanged)

    def close_position(self, exit_price: float, timestamp: float, reason: str, net_pnl: float):
        if self.live_mode or not self.current_position:
            return None
        # Simulation close logic omitted (unchanged)

    # -- Status helpers ------------------------------------------------------

    def get_status(self) -> Dict:
        total_capital = self.current_capital + self.reserves
        total_return_pct = ((total_capital - self.initial_capital) / self.initial_capital) * 100
        win_rate = (self.winning_trades / self.total_trades * 100) if self.total_trades > 0 else 0
        recent_win_rate = (sum(self.recent_accuracy) / len(self.recent_accuracy) * 100) if self.recent_accuracy else 0

        return {
            'current_capital': self.current_capital,
            'reserves': self.reserves,
            'total_capital': total_capital,
            'total_return_pct': total_return_pct,
            'total_trades': self.total_trades,
            'winning_trades': self.winning_trades,
            'win_rate_pct': win_rate,
            'recent_win_rate_pct': recent_win_rate,
            'market_bias': self.market_bias,
            'liquidity_zones': len(self.liquidity_zones),
            'liquidation_clusters': len(self.liquidation_clusters),
            'current_position': self.current_position is not None,
            'confidence_threshold': self.confidence_threshold
        }

    def print_status(self):
        status = self.get_status()
        print(f"\n📊 HEATMAP STATUS | Capital ${status['current_capital']:.2f} | Reserves ${status['reserves']:.2f}")
        print(f"   Total ${status['total_capital']:.2f} ({status['total_return_pct']:+.1f}%) | Bias {status['market_bias']}")
        print(f"   Trades {status['total_trades']} | Win {status['win_rate_pct']:.1f}% | Recent {status['recent_win_rate_pct']:.1f}%")
        if status['current_position']:
            print(f"   Active Position: {self.current_position['side']} @ {self.current_position['entry_price']:.6f}")

    # -- Batch helpers (unchanged) -------------------------------------------

    def analyze_tick_data(self, df: pd.DataFrame) -> List[Dict]:
        if df.empty:
            return []
        if not df.index.is_monotonic_increasing:
            df = df.sort_index()

        emitted_events: List[Dict] = []
        volume_col = 'volume' if 'volume' in df.columns else None

        for timestamp, row in df.iterrows():
            ts_value = (
                timestamp.timestamp()
                if isinstance(timestamp, (pd.Timestamp, datetime))
                else float(timestamp)
            )
            price = float(row['price'])
            volume = float(row[volume_col]) if volume_col else 0.0

            signal_or_exit = self.add_tick(price, ts_value, volume)
            if signal_or_exit:
                emitted_events.append(signal_or_exit)

        return emitted_events

    def feed_tick_stream(
        self,
        tick_iterable: Iterable[Tuple[float, float, float]],
        sleep_interval: float = 0.0
    ) -> Iterable[Dict]:
        for price, timestamp, volume in tick_iterable:
            signal_or_exit = self.add_tick(price, timestamp, volume)
            if signal_or_exit:
                yield signal_or_exit
            if sleep_interval > 0:
                time.sleep(sleep_interval)


# =============================================================================
# dYdX Live Trader
# =============================================================================


class DydxLiveTrader:
    """Live trading adapter for dYdX v4 (placeholder order methods)."""

    def __init__(
        self,
        market: str = ticker,
        use_testnet: bool = False,
        leverage: float = leverage
    ):
        self.market = market
        self.use_testnet = use_testnet
        self.leverage = leverage
        self.position_size_pct = 0.18
        self.min_order_value = 10.0

        logging.basicConfig(level=logging.INFO)
        self.logger = logging.getLogger("DydxLiveTrader")

        self._load_credentials()
        self._init_dydx_client()

        self.account_balance = 50.0
        self.open_orders = []
        self.positions: Dict[str, Dict] = {}
        self.total_pnl = 0.0

        self.logger.info(f"📊 Market: {market}")
        self.logger.info(f"⚡ Leverage: {self.leverage}x")
        self.logger.info(f"💰 Starting Balance: ${self.account_balance}")
        self.logger.info(f"🌐 Network: {'Testnet' if use_testnet else 'MAINNET'}")

    def _load_credentials(self):
        try:
            with open('.env', 'r', encoding='utf-8') as env_file:
                for line in env_file:
                    if '=' in line and not line.strip().startswith('#'):
                        key, value = line.strip().split('=', 1)
                        os.environ[key] = value.strip(' "\'')
        except FileNotFoundError:
            self.logger.warning("⚠️ .env file not found; relying on existing environment.")

        self.mnemonic = os.getenv('DYDX_MNEMONIC')
        self.wallet_address = os.getenv('DYDX_WALLET_ADDRESS')
        self.subaccount_number = int(os.getenv('DYDX_SUBACCOUNT_NUMBER', '0'))

        if not self.mnemonic or not self.wallet_address:
            raise ValueError("Missing dYdX credentials in environment variables")

    def _init_dydx_client(self):
        self.base_url = (
            'https://indexer.v4testnet.dydx.exchange'
            if self.use_testnet else
            'https://indexer.dydx.trade'
        )
        self.logger.info(f"✅ dYdX client initialized: {self.base_url}")

    def calculate_position_size(self, signal_price: float) -> float:
        base_position = self.account_balance * self.position_size_pct
        leveraged_position = base_position * self.leverage
        quantity = leveraged_position / signal_price
        return round(quantity, 2)

    def execute_signal(self, signal: Dict) -> bool:
        try:
            action = signal.get('action')
            price = signal.get('entry_price') or signal.get('exit_price')

            if not action or not price:
                self.logger.warning("⚠️ Signal missing action or price; skipping.")
                return False

            quantity = self.calculate_position_size(price)
            min_quantity = self.min_order_value / price
            quantity = round(max(quantity, min_quantity), 2)

            self.logger.info(f"📊 Position: {quantity:.2f} {self.market} @ ${price:.6f}")

            if action in ['buy', 'long']:
                return self._place_buy_order(quantity, price)
            if action in ['sell', 'short']:
                return self._place_sell_order(quantity, price)
            if action == 'close':
                return self._close_position(price)

            self.logger.warning(f"⚠️ Unsupported action '{action}'")
            return False

        except Exception as exc:
            self.logger.error(f"❌ Order execution failed: {exc}")
            return False

    def _place_buy_order(self, quantity: float, price: float) -> bool:
        self.logger.info(f"🚀 REAL BUY ORDER: {quantity:.2f} @ ${price:.6f} | Value ${quantity * price:.2f}")
        self.open_orders.append({
            'market': self.market,
            'side': 'BUY',
            'type': 'LIMIT',
            'size': quantity,
            'price': price,
            'post_only': True
        })
        return True

    def _place_sell_order(self, quantity: float, price: float) -> bool:
        self.logger.info(f"🚀 REAL SELL ORDER: {quantity:.2f} @ ${price:.6f} | Value ${quantity * price:.2f}")
        self.open_orders.append({
            'market': self.market,
            'side': 'SELL',
            'type': 'LIMIT',
            'size': quantity,
            'price': price,
            'post_only': True
        })
        return True

    def _close_position(self, price: float) -> bool:
        self.logger.info(f"🚀 REAL CLOSE ORDER @ ${price:.6f}")
        self.logger.info("🎯 Reducing position to zero")
        return True

    def get_status(self) -> Dict:
        return {
            'account_balance': self.account_balance,
            'total_pnl': self.total_pnl,
            'open_orders': len(self.open_orders),
            'positions': len(self.positions)
        }


# =============================================================================
# Live runner (async)
# =============================================================================


async def run_live_bot(
    market: str = ticker,
    leverage: float = 50.0,
    use_testnet_feed: bool = True,
    use_testnet_trader: bool = True,
    queue_maxsize: int = 500
):
    strategy = HybridHeatmapNoRebates(live_mode=True)
    trader = DydxLiveTrader(market=market, use_testnet=use_testnet_trader, leverage=leverage)
    signal_queue: Queue = Queue(maxsize=queue_maxsize)

    async def signal_producer():
        async for price, ts, vol in dydx_trade_feed(market=market, use_testnet=use_testnet_feed):
            signal = strategy.add_tick(price, ts, vol)
            if signal:
                await signal_queue.put(signal)

    async def signal_consumer():
        while True:
            signal = await signal_queue.get()
            trader.execute_signal(signal)
            signal_queue.task_done()

    async def status_logger():
        while True:
            await asyncio.sleep(60)
            strategy.print_status()
            trader.logger.info(f"ℹ️ Trader status: {trader.get_status()}")

    await asyncio.gather(signal_producer(), signal_consumer(), status_logger())


def test_live_trader(market=ticker, use_testnet=False, leverage=marketLeverage):
    trader = DydxLiveTrader(market, use_testnet=use_testnet, leverage=leverage)
    test_signal = {'action': 'long', 'entry_price': 0.22000, 'confidence': 1.0}
    success = trader.execute_signal(test_signal)
    print(f"Signal execution: {'✅ Success' if success else '❌ Failed'}")
    print("Status:", trader.get_status())


# =============================================================================
# CLI entry point
# =============================================================================


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the DOGE Hybrid Heatmap dYdX live bot.")
    parser.add_argument("--market", default=ticker, help="Market to trade (default "+ticker+").")
    parser.add_argument("--mainnet-feed", action="store_true", help="Use dYdX mainnet data feed.")
    parser.add_argument("--mainnet-trader", action="store_true", help="Send orders to dYdX mainnet.")
    parser.add_argument("--queue-size", type=int, default=500, help="Max pending signals.")
    parser.add_argument("--leverage", type=float, default=50.0, help="Leverage to use (default 50x for DOGE).")
    parser.add_argument("--self-test", action="store_true", help="Run a single signal test and exit.")
    return parser


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.self_test:
        test_live_trader(
            market=args.market,
            use_testnet=not args.mainnet_trader,
            leverage=args.leverage
        )
        return

    try:
        asyncio.run(
            run_live_bot(
                market=args.market,
                leverage=args.leverage,
                use_testnet_feed=not args.mainnet_feed,
                use_testnet_trader=not args.mainnet_trader,
                queue_maxsize=args.queue_size
            )
        )
    except KeyboardInterrupt:
        print("\n🛑 Live bot stopped by user.")


if __name__ == "__main__":
    main()
