import asyncio
import random
import time
import numpy as np
import requests
import httpx
import websockets
import json
import logging
from collections import deque, defaultdict
from typing import Dict, List, Optional, Tuple, Callable


# dYdX V4 Client Imports
from dydx_v4_client import MAX_CLIENT_ID, OrderFlags
from dydx_v4_client.node.client import NodeClient
from dydx_v4_client.indexer.rest.indexer_client import IndexerClient
from dydx_v4_client.indexer.rest.constants import OrderType
from v4_proto.dydxprotocol.clob.order_pb2 import Order
from dydx_v4_client.network import make_mainnet
from dydx_v4_client.node.market import since_now
from dydx_v4_client.wallet import Wallet


logging.basicConfig(level=logging.INFO  )
logger = logging.getLogger(__name__)


# --- Custom WebSocket Implementation (from your working hb.py code) ---


class DydxLiveWebSocket:
    """Raw WebSocket client for dYdX v4 live tick data"""
    
    def __init__(self, market: str = "BTC-USD", on_tick: Optional[Callable] = None):
        # dYdX v4 mainnet WebSocket endpoint
        self.ws_url = "wss://indexer.dydx.trade/v4/ws"
        self.market = market
        self.on_tick = on_tick
        
        # Connection state
        self.websocket = None
        self.running = False
        self.connection_id = None
        
        # Market data
        self.best_bid = None
        self.best_ask = None
        self.last_trade_price = None
        self.last_update = None
        
        logger.info(f"🔌 dYdX Live WebSocket for {market}")
        logger.info(f"📡 Endpoint: {self.ws_url}")
    
    async def connect(self):
        """Connect to dYdX WebSocket and subscribe to live data"""
        try:
            logger.info("🔗 Connecting to dYdX WebSocket...")
            self.websocket = await websockets.connect(self.ws_url)
            self.running = True
            
            # Start message handler
            asyncio.create_task(self._message_handler())
            
            # Wait for connection confirmation
            await asyncio.sleep(1)
            
            # Subscribe to orderbook updates (for bid/ask)
            await self._subscribe_orderbook()
            
            # Subscribe to trades (for actual tick prices)
            await self._subscribe_trades()
            
            logger.info(f"✅ Connected and subscribed to {self.market}")
            
        except Exception as e:
            logger.error(f"❌ Failed to connect: {e}")
            raise
    
    async def disconnect(self):
        """Disconnect from WebSocket"""
        self.running = False
        if self.websocket:
            await self.websocket.close()
        logger.info("🛑 Disconnected from dYdX WebSocket")
    
    async def _subscribe_orderbook(self):
        """Subscribe to orderbook updates"""
        subscribe_msg = {
            "type": "subscribe",
            "channel": "v4_orderbook",
            "id": self.market,
            "batched": False  # Get every update immediately
        }
        await self.websocket.send(json.dumps(subscribe_msg))
        logger.info(f"📊 Subscribed to {self.market} orderbook")
    
    async def _subscribe_trades(self):
        """Subscribe to trade updates (actual tick prices)"""
        subscribe_msg = {
            "type": "subscribe", 
            "channel": "v4_trades",
            "id": self.market,
            "batched": False  # Get every trade immediately
        }
        await self.websocket.send(json.dumps(subscribe_msg))
        logger.info(f"💱 Subscribed to {self.market} trades")
    
    async def _message_handler(self):
        """Handle incoming WebSocket messages"""
        try:
            async for message in self.websocket:
                if not self.running:
                    break
                
                try:
                    data = json.loads(message)
                    await self._process_message(data)
                except json.JSONDecodeError as e:
                    logger.error(f"JSON decode error: {e}")
                except Exception as e:
                    logger.error(f"Message processing error: {e}")
                    
        except websockets.exceptions.ConnectionClosed:
            logger.warning("🔒 WebSocket connection closed")
            # Reconnect logic would go here
        except Exception as e:
            logger.error(f"❌ Error in message handler: {e}")
        finally:
            self.running = False
            
    async def _process_message(self, data: Dict):
        """Process a single message from the WebSocket"""
        
        # Handle subscription confirmation
        if data.get('type') == 'subscribed':
            if data.get('channel') == 'v4_orderbook':
                logger.info(f"✅ Orderbook subscription confirmed for {data.get('id')}")
            elif data.get('channel') == 'v4_trades':
                logger.info(f"✅ Trades subscription confirmed for {data.get('id')}")
            return
        
        # Handle initial snapshot or update
        channel = data.get('channel')
        
        if channel == 'v4_orderbook':
            await self._handle_orderbook_update(data)
        elif channel == 'v4_trades':
            await self._handle_trade_update(data)
        else:
            # logger.debug(f"Unhandled message channel: {channel}")
            pass

    async def _handle_orderbook_update(self, data: Dict):
        """Process orderbook updates to get best bid/ask"""
        contents = data.get('contents', {})
        
        # Snapshot or Update
        if 'bids' in contents and 'asks' in contents:
            bids = contents['bids']
            asks = contents['asks']
            
            if bids:
                self.best_bid = self._extract_price(bids[0])
            if asks:
                self.best_ask = self._extract_price(asks[0])
            
            self.last_update = time.time()
            # logger.debug(f"Orderbook update: Bid={self.best_bid}, Ask={self.best_ask}")

    def _extract_price(self, item: List) -> float:
        """Extracts price from a [price, size] list"""
        return float(item[0])

    async def _handle_trade_update(self, data: Dict):
        """Process trade updates to get the last trade price (tick)"""
        trades = data.get('contents', {}).get('trades', [])
        
        for trade in trades:
            price, size, timestamp = self._extract_trade(trade)
            
            if price is not None:
                self.last_trade_price = price
                self.last_update = time.time()
                
                # Call the strategy's tick handler
                if self.on_tick:
                    # The on_tick function in the strategy is async, so we await it
                    await self.on_tick(price, timestamp, size)
                
                # logger.debug(f"Trade update: Price={price}, Size={size}")

    def _extract_trade(self, trade_data: dict) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """Extracts price, size, and timestamp from a single trade message."""
        try:
            price = float(trade_data.get('price'))
            size = float(trade_data.get('size'))
            # The timestamp in v4_trades is in milliseconds
            timestamp = float(trade_data.get('time')) / 1000.0
            return price, size, timestamp
        except (TypeError, ValueError) as e:
            logger.error(f"Error extracting trade data: {e} in {trade_data}")
            return None, None, None

    def get_best_bid_ask(self) -> Tuple[Optional[float], Optional[float]]:
        """Returns the current best bid and ask"""
        return self.best_bid, self.best_ask

    def get_last_price(self) -> Optional[float]:
        """Returns the last trade price"""
        return self.last_trade_price

    def is_connected(self) -> bool:
        """Returns connection status"""
        return self.running


# --- HybridHeatmapDydx Class (Strategy) ---

class HybridHeatmapDydx:
    def __init__(self, wallet: Wallet, node_client: NodeClient, indexer_client: IndexerClient, network):
        self.wallet = wallet
        self.node_client = node_client
        self.indexer_client = indexer_client
        self.network = network
        
        # --- Strategy Parameters ---
        self.initial_capital = 800.0
        self.current_capital = self.initial_capital
        self.reserves = 0.0
        self.leverage = 50
        self.trading_fee_rate = 0.00011  # Maker fee is positive in dYdX V4
        self.position_size_pct = 0.18
        self.confidence_threshold = 0.7  # 70%
        self.entry_zones = (25, 75)  # Bottom 25% and Top 25% of the range
        self.range_lookbacks = [15, 25, 40]
        self.range_size_limits = (0.015, 0.6) # 0.015% to 0.6% range size
        
        # --- State Variables ---
        self.price_history = deque(maxlen=250) # Max length for all lookbacks
        self.current_position = None
        self.position_entry_time = None
        self.position_entry_price = None
        self.total_trades = 0
        self.winning_trades = 0
        self.recent_accuracy = deque(maxlen=10)
        self.market_bias = 'neutral'
        self.liquidity_zones = []
        self.liquidation_clusters = []
        self.support_levels = []
        self.resistance_levels = []
        
        # --- dYdX V4 Specifics ---
        self.market_id = "BTC-USD" # Corrected market ID
        self.order_client_id = 0
        self.ws_client = None # Will be set in run_bot
        
        print("\n" + "="*80)
        print("Hybrid Heatmap dYdX V4 Bot Initialized")
        print(f"Market: {self.market_id}")
        print(f"Leverage: {self.leverage}x | Position Size: {self.position_size_pct:.1%}")
        print(f"Confidence Threshold: {self.confidence_threshold:.1%}")
        print("="*80 + "\n")

    async def setup_market(self):
        """Fetches initial market data and account balance."""
        # 1. Get account balance (from your working hb.py code)
        try:
            # The IndexerClient is used to get the subaccount balance
            subaccount = await self.indexer_client.get_subaccount(
                address=self.wallet.address,
                subaccount_number=0 # Assuming subaccount 0
            )
            
            # Find the USDC balance
            usdc_balance = 0.0
            for asset in subaccount.subaccount.assets:
                if asset.symbol == "USDC":
                    usdc_balance = float(asset.total_balance)
                    break
            
            logger.info(f"💰 Account Balance (USDC): {usdc_balance:,.2f}")
            self.current_capital = usdc_balance
            self.initial_capital = usdc_balance
            
        except Exception as e:
            logger.error(f"❌ Failed to fetch account balance: {e}")
            # Continue with default capital if balance fetch fails
            
        # 2. Get initial market data (optional)
        pass

    # --- HEATMAP LOGIC (Stubs from your hb.py code) ---

    async def fetch_coinglass_heatmap(self):
        """Stub for fetching external heatmap data."""
        # In your original code, this would fetch data from Coinglass or a similar service.
        # For now, we return a placeholder.
        logger.debug("Fetching Coinglass heatmap data (stub)")
        await asyncio.sleep(1) # Simulate network delay
        return {'liquidity': [], 'liquidation': []}

    async def analyze_heatmap(self, heatmap_data: Dict):
        """Stub for analyzing heatmap data to update zones."""
        # In your original code, this would populate self.liquidity_zones, etc.
        logger.debug("Analyzing heatmap data (stub)")
        self.liquidity_zones = [10000, 15000] # Placeholder
        self.liquidation_clusters = [20000, 25000] # Placeholder

    async def update_market_bias(self):
        """Stub for determining market bias based on heatmap and other factors."""
        # In your original code, this would set self.market_bias to 'bullish', 'bearish', or 'neutral'.
        logger.debug("Updating market bias (stub)")
        
        # Simple random bias for now to allow the logic to flow
        self.market_bias = random.choice(['bullish', 'bearish', 'neutral'])
        logger.info(f"Market Bias updated to: {self.market_bias}")

    async def _heatmap_updater(self):
        """Background task to periodically update heatmap data and market bias."""
        while True:
            try:
                heatmap_data = await self.fetch_coinglass_heatmap()
                await self.analyze_heatmap(heatmap_data)
                await self.update_market_bias()
            except Exception as e:
                logger.error(f"Error in heatmap updater: {e}")
            
            # Update every 60 seconds as per typical heatmap update frequency
            await asyncio.sleep(60)

    # --- CORE TRADING LOGIC (Fixed Range/Pattern) ---

    async def add_tick(self, price: float, timestamp: float, size: float):
        """Processes a new price tick and runs the strategy logic."""
        self.price_history.append(price)
        
        # Analyze for a trade signal
        signal = self.analyze_trading_signal(price, timestamp)
        
        if signal:
            if signal['action'] in ['long', 'short']:
                await self.place_maker_order(signal)
            elif signal['action'] == 'close':
                await self.cancel_all_orders()
        
        # Check for exit conditions (even if no new signal was generated)
        exit_signal = self.check_exit_conditions(price, timestamp)
        if exit_signal and exit_signal['action'] == 'close':
            await self.cancel_all_orders()

    def detect_micro_ranges(self, prices: List[float]) -> Dict:
        """Enhanced micro-range detection (using raw prices, no SMA)"""
        ranges = {}
        
        for lookback in self.range_lookbacks:
            if len(prices) < lookback:
                continue
            
            recent = prices[-lookback:]
            range_high = max(recent)
            range_low = min(recent)
            
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

    def analyze_tick_data(self, prices: List[float], lookback: int = 50) -> Optional[Dict]:
        """Analyzes tick data for sequence patterns."""
        if len(prices) < lookback:
            return None

        recent_prices = prices[-lookback:]
        changes = [(recent_prices[i] - recent_prices[i-1]) / recent_prices[i-1] * 100 
                   for i in range(1, len(recent_prices))]
        
        pattern_length = 5
        min_occurrences = 3
        patterns = defaultdict(lambda: {'count': 0, 'next_change': []})
        
        for i in range(len(changes) - pattern_length):
            pattern = tuple(changes[i:i+pattern_length]) 
            next_change = changes[i+pattern_length]
            
            patterns[pattern]['count'] += 1
            patterns[pattern]['next_change'].append(next_change)
            
        valid_patterns = {p: data for p, data in patterns.items() if data['count'] >= min_occurrences}
        
        if not valid_patterns:
            logger.debug("Filter Fail - No Sequence Pattern found.")
            return None
        
        most_frequent_pattern = max(valid_patterns.items(), key=lambda item: item[1]['count'])
        pattern, data = most_frequent_pattern
        avg_next_change = sum(data['next_change']) / len(data['next_change'])
        
        if avg_next_change > 0:
            correct_predictions = sum(1 for change in data['next_change'] if change > 0)
            predicted_action = 'long'
        else:
            correct_predictions = sum(1 for change in data['next_change'] if change < 0)
            predicted_action = 'short'
            
        confidence = correct_predictions / len(data['next_change'])
        
        if confidence < 0.6:
            logger.debug(f"Filter Fail - Pattern Confidence too low ({confidence:.1%}).")
            return None
        
        return {
            'action': predicted_action,
            'confidence': confidence,
            'pattern': pattern,
            'avg_next_change': avg_next_change
        }

    def analyze_trading_signal(self, current_price: float, timestamp: float) -> Optional[Dict]:
        """Runs the hybrid strategy logic."""
        if len(self.price_history) < max(self.range_lookbacks):
            return None
        
        if self.current_position:
            return self.check_exit_conditions(current_price, timestamp)
        
        prices = list(self.price_history)
        
        # 1. Range Consensus Filter
        ranges = self.detect_micro_ranges(prices)
        if not ranges:
            logger.debug("Filter Fail - No Consensus Range found.")
            return None
        
        # 2. Sequence Pattern Filter
        pattern_signal = self.analyze_tick_data(prices)
        if not pattern_signal:
            return None
        
        # 3. Hybrid Signal Generation
        long_signals = 0
        short_signals = 0
        
        for timeframe, range_data in ranges.items():
            position_pct = range_data['current_position_pct']
            
            if position_pct <= self.entry_zones[0]:
                long_signals += 1
            elif position_pct >= self.entry_zones[1]:
                short_signals += 1
        
        min_consensus = 2
        signal = None
        
        if long_signals >= min_consensus and pattern_signal['action'] == 'long':
            confidence = self.calculate_trade_confidence(ranges, 'long')
            if confidence >= self.confidence_threshold:
                # CRITICAL: Now uses the updated market_bias from the background task
                if self.market_bias in ['bullish', 'neutral']:
                    signal = {
                        'action': 'long',
                        'confidence': confidence,
                        'market_bias': self.market_bias,
                        'entry_price': current_price,
                        'timestamp': timestamp,
                        'range_consensus': long_signals
                    }
        
        elif short_signals >= min_consensus and pattern_signal['action'] == 'short':
            confidence = self.calculate_trade_confidence(ranges, 'short')
            if confidence >= self.confidence_threshold:
                # CRITICAL: Now uses the updated market_bias from the background task
                if self.market_bias in ['bearish', 'neutral']:
                    signal = {
                        'action': 'short',
                        'confidence': confidence,
                        'market_bias': self.market_bias,
                        'entry_price': current_price,
                        'timestamp': timestamp,
                        'range_consensus': short_signals
                    }
        
        if signal:
            self.execute_trade(signal)
        
        return signal

    def calculate_trade_confidence(self, ranges: Dict, signal_type: str) -> float:
        """Calculate confidence score for trade signal (Simplified for dYdX V4)"""
        if not ranges:
            return 0.0
        
        confidence_factors = []
        agreement_score = len([r for r in ranges.values() 
                             if (signal_type == 'long' and r['current_position_pct'] <= self.entry_zones[0]) or
                                (signal_type == 'short' and r['current_position_pct'] >= self.entry_zones[1])])
        confidence_factors.append(agreement_score / len(ranges))
        
        avg_strength = sum(r['strength'] for r in ranges.values()) / len(ranges)
        confidence_factors.append(min(1.0, avg_strength * 2))
        
        if (signal_type == 'long' and self.market_bias == 'bullish') or \
           (signal_type == 'short' and self.market_bias == 'bearish'):
            confidence_factors.append(1.0)
        elif self.market_bias == 'neutral':
            confidence_factors.append(0.7)
        else:
            confidence_factors.append(0.3)
        
        confidence_factors.append(0.8)
        
        return sum(confidence_factors) / len(confidence_factors)

    def execute_trade(self, signal: Dict):
        """Execute trade (simplified for logging and state management)"""
        base_position_pct = self.position_size_pct
        confidence_multiplier = min(1.2, signal['confidence'] / 0.8)
        adjusted_position_pct = base_position_pct * confidence_multiplier
        
        position_value = self.current_capital * adjusted_position_pct
        leveraged_value = position_value * self.leverage
        
        self.current_position = {
            'side': signal['action'],
            'entry_price': signal['entry_price'],
            'position_value': position_value,
            'leveraged_value': leveraged_value,
            'timestamp': signal['timestamp'],
            'confidence': signal['confidence'],
            'market_bias': signal['market_bias'],
            'adjusted_size': adjusted_position_pct
        }
        
        self.position_entry_time = signal['timestamp']
        self.position_entry_price = signal['entry_price']
        
        logger.info(f"🎯 SIGNAL: OPEN {signal['action'].upper()} @ ${signal['entry_price']:.6f}")
        logger.info(f"   Confidence: {signal['confidence']:.1%}")
        logger.info(f"   Size Adj: {adjusted_position_pct:.1%}")

    def check_exit_conditions(self, current_price: float, timestamp: float) -> Optional[Dict]:
        """Enhanced exit conditions (simplified for dYdX V4)"""
        if not self.current_position:
            return None
        
        entry_price = self.current_position['entry_price']
        side = self.current_position['side']
        
        if side == 'long':
            price_change_pct = (current_price - entry_price) / entry_price
        else:
            price_change_pct = (entry_price - current_price) / entry_price
        
        pnl_pct = price_change_pct * self.leverage
        
        exit_reason = None
        
        if pnl_pct >= 0.005:
            exit_reason = 'profit_target'
        elif pnl_pct <= -0.002:
            exit_reason = 'stop_loss'
        elif timestamp - self.position_entry_time > 300:
            exit_reason = 'timeout'
        
        if exit_reason:
            logger.info(f"✅ SIGNAL: CLOSE {side.upper()} @ ${current_price:.6f} | Reason: {exit_reason}")
            self.current_position = None
            self.position_entry_time = None
            self.position_entry_price = None
            return {'action': 'close', 'reason': exit_reason}
        
        return None

    # --- dYdX V4 Order Placement/Cancellation ---

    async def place_maker_order(self, signal: Dict):
        """Places a POST_ONLY maker order on dYdX V4."""
        side = signal['action']
        is_buy = side == 'long'
        current_price = signal['entry_price']
        price_offset = current_price * 0.0001 
        
        if is_buy:
            limit_price = current_price - price_offset
        else:
            limit_price = current_price + price_offset
            
        size = self.current_position['leveraged_value'] / limit_price
        self.order_client_id = (self.order_client_id + 1) % MAX_CLIENT_ID
        
        try:
            tx_hash = await self.node_client.place_order(
                wallet=self.wallet,
                market_id=self.market_id,
                client_id=self.order_client_id,
                order_type=OrderType.LIMIT,
                side=Order.Side.BUY if is_buy else Order.Side.SELL,
                price=limit_price,
                size=size,
                time_in_force=Order.TimeInForce.GTT,
                flags=OrderFlags.POST_ONLY,
                expiration_epoch_seconds=since_now(60 * 60 * 24)
            )
            logger.info(f"✅ ORDER PLACED: {side.upper()} {size:.4f} @ ${limit_price:.2f} | Tx: {tx_hash}")
            
        except HTTPStatusError as e:
            logger.error(f"❌ Failed to place order: {e.response.text}")
        except Exception as e:
            logger.error(f"❌ An unexpected error occurred during order placement: {e}")

    async def cancel_all_orders(self):
        """Cancels all open orders for the market."""
        try:
            open_orders = await self.indexer_client.get_open_orders(
                address=self.wallet.address,
                market_id=self.market_id
            )
            
            if not open_orders.orders:
                logger.debug("No open orders to cancel.")
                return
            
            orders_to_cancel = [
                {
                    'market_id': self.market_id,
                    'client_id': order.client_id,
                    'order_flags': order.flags
                }
                for order in open_orders.orders
            ]
            
            tx_hash = await self.node_client.batch_cancel_orders(
                wallet=self.wallet,
                cancellation_requests=orders_to_cancel
            )
            logger.info(f"❌ ORDER CANCELLED: {len(orders_to_cancel)} orders cancelled. Tx: {tx_hash}")
            
        except HTTPStatusError as e:
            logger.error(f"❌ Failed to cancel orders: {e.response.text}")
        except Exception as e:
            logger.error(f"❌ An unexpected error occurred during order cancellation: {e}")

    async def run_bot(self):
        """Main entry point for the bot, connects to the dYdX WebSocket."""
        
        # 1. Setup market and get initial balance
        await self.setup_market()
        
        # 2. Start the background heatmap updater task
        asyncio.create_task(self._heatmap_updater())
        
        # 3. Connect to dYdX WebSocket for trades
        self.ws_client = DydxLiveWebSocket(
            market=self.market_id,
            on_tick=self.add_tick
        )
        
        try:
            await self.ws_client.connect()
            
            # Keep the main task running while the WebSocket is connected
            while self.ws_client.is_connected():
                await asyncio.sleep(1)
                
        except Exception as e:
            logger.error(f"Fatal WebSocket connection error: {e}")
        finally:
            if self.ws_client:
                await self.ws_client.disconnect()


# --- Main Execution ---

async def main():
    # CRITICAL FIX: Load mnemonic from file as per your original setup
    try:
        with open("mnemonic.txt", "r") as f:
            mnemonic = f.read().strip()
    except FileNotFoundError:
        logger.error("❌ CRITICAL ERROR: mnemonic.txt not found. Please create this file with your dYdX mnemonic.")
        return
    except Exception as e:
        logger.error(f"❌ CRITICAL ERROR: Could not read mnemonic.txt: {e}")
        return

    try:
        # NOTE: The Node URL is provided without the http(s ):// prefix as per the library's requirement
        network = make_mainnet(
            node_url="oegs.dydx.trade:443",
            rest_indexer="https://indexer.dydx.trade",
            websocket_indexer="wss://indexer.dydx.trade/v4/ws"
         )
        
        node_client = await NodeClient.connect(network.node)
        indexer_client = IndexerClient(network.rest_indexer)
        
        # FINAL FIX: Explicitly pass the address, which is required by your library version.
        # NOTE: The address "dydx1kydnehq9hfqqrt28jc2nhaxux33hk2sh9zw84q" is a placeholder and should be correct for your setup.
        wallet = await Wallet.from_mnemonic(node_client, mnemonic, "dydx1kydnehq9hfqqrt28jc2nhaxux33hk2sh9zw84q")
        
        print("\n" + "="*80)
        print(f"dYdX V4 Hybrid Heatmap Bot Initialized.")
        print(f"Wallet Address: {wallet.address}")
        print("="*80 + "\n")
        
        strategy = HybridHeatmapDydx(wallet, node_client, indexer_client, network)
        await strategy.run_bot()
        
    except Exception as e:
        logger.error(f"Fatal error in main execution: {e}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n⚠️ Stopped by user")
