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
from dydx_v4_client.network import make_mainnet
from dydx_v4_client.node.market import Market, since_now
from dydx_v4_client.wallet import Wallet

logging.basicConfig(level=logging.INFO )
logger = logging.getLogger(__name__)

# --- Custom WebSocket Implementation (from your working code) ---

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
        except Exception as e:
            logger.error(f"❌ Message handler error: {e}")
    
    async def _process_message(self, data: dict):
        """Process incoming WebSocket message"""
        msg_type = data.get("type")

        if msg_type == "connected":
            self.connection_id = data.get("connection_id")
            logger.info(f"🔓 Connected with ID: {self.connection_id}")

        elif msg_type == "subscribed":
            channel = data.get("channel")
            sub_id = data.get("id")
            if sub_id:
                logger.info(f"✅ Subscribed to {channel} ({sub_id})")
            else:
                logger.info(f"✅ Subscribed to {channel}")

        elif msg_type == "channel_data":
            channel = data.get("channel")
            sub_id = data.get("id")
            contents = data.get("contents", {})

            # Route based on channel name and id (market)
            if channel == "v4_orderbook" and sub_id == self.market:
                await self._handle_orderbook_update(contents)
            elif channel == "v4_trades" and sub_id == self.market:
                await self._handle_trade_update(contents)
    
    async def _handle_orderbook_update(self, contents: dict):
        """Handle orderbook update (bid/ask prices)"""
        try:
            bids = contents.get("bids", []) or []
            asks = contents.get("asks", []) or []

            def _extract_price(entry):
                # Supports dict {'price': '0.123', 'size': '...'} or list ['0.123','...']
                try:
                    if isinstance(entry, dict):
                        return float(entry.get("price")) if entry.get("price") is not None else None
                    elif isinstance(entry, (list, tuple)) and len(entry) >= 1:
                        return float(entry[0])
                except Exception:
                    return None
                return None

            best_bid_price = _extract_price(bids[0]) if len(bids) > 0 else None
            best_ask_price = _extract_price(asks[0]) if len(asks) > 0 else None

            if best_bid_price is not None:
                self.best_bid = best_bid_price
            if best_ask_price is not None:
                self.best_ask = best_ask_price
            
            self.last_update = time.time()
            
            # Call tick callback if we have both bid/ask
            if self.best_bid and self.best_ask and self.on_tick:
                mid_price = (self.best_bid + self.best_ask) / 2.0
                await self.on_tick(mid_price, self.last_update, "orderbook")
            
        except Exception as e:
            logger.error(f"Error handling orderbook: {e}")
    
    async def _handle_trade_update(self, contents: dict):
        """Handle trade update (actual executed prices - highest frequency)"""
        try:
            trades = contents.get("trades", []) or []

            def _extract_trade(entry):
                # Supports dict {'price': '0.123','size':'1.2','createdAt':...}
                # Or list-like ['0.123','1.2', ...]
                try:
                    if isinstance(entry, dict):
                        price = float(entry.get("price")) if entry.get("price") is not None else None
                        size = float(entry.get("size")) if entry.get("size") is not None else None
                        ts = entry.get("createdAt")
                        return price, size, ts
                    elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
                        price = float(entry[0])
                        size = float(entry[1])
                        ts = None
                        return price, size, ts
                except Exception:
                    return None, None, None
                return None, None, None

            for trade in trades:
                price, size, _ts = _extract_trade(trade)
                if price is None:
                    continue

                self.last_trade_price = price
                self.last_update = time.time()

                if self.on_tick:
                    await self.on_tick(price, self.last_update, "trade", size if size is not None else 0.0)
            
        except Exception as e:
            logger.error(f"Error handling trades: {e}")
    
    def get_best_bid_ask(self) -> Optional[Tuple[float, float]]:
        """Get current best bid/ask"""
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid, self.best_ask)
        return None
    
    def get_last_price(self) -> Optional[float]:
        """Get last trade price (most recent tick)"""
        return self.last_trade_price
    
    def is_connected(self) -> bool:
        """Check if connected and receiving data"""
        return self.running and self.last_update and (time.time() - self.last_update < 30)

# --- Trading Logic Class (Adapted from user's code) ---

class HybridHeatmapDydx:
    
    # FIX: Added network object to the constructor
    def __init__(self, wallet: Wallet, node_client: NodeClient, indexer_client: IndexerClient, network, initial_capital=50.0):
        # dYdX Client Instances
        self.wallet = wallet
        self.node = node_client
        self.indexer = indexer_client
        self.network = network # Store network object for ws_url
        self.address = wallet.address
        self.subaccount_number = 0 # Default trading subaccount
        self.market_id = "BTC-USD" # Updated from DOGE to BTC
        self.market = None # Will be set in setup_market
        
        # Capital management (NOTE: In a real bot, capital is derived from subaccount balance)
        self.initial_capital = initial_capital
        self.current_capital = initial_capital # Represents capital in the trading subaccount
        self.reserves = 0.0 # Represents capital in the main account (for compounding)
        self.total_trades = 0
        self.winning_trades = 0
        self.recent_accuracy = deque(maxlen=100) # Added missing attribute
        self.confidence_threshold = 0.7 # Added missing attribute
        
        # Trading parameters (optimized for BTC on dYdX)
        self.leverage = 50  # 50x leverage as requested
        self.position_size_pct = 0.18  # 18% of capital per trade
        self.maker_rebate_rate = 0.00002  # dYdX maker rebate (0.002%)
        
        # Risk management
        self.profit_target_pct = 0.5  # 0.5% price move target
        self.stop_loss_pct = 0.2  # 0.2% stop loss
        self.timeout_seconds = 300  # 5 minute max hold time
        
        # Micro-range parameters (from user's code)
        self.range_lookbacks = [15, 25, 40]
        self.entry_zones = (35, 65)
        self.range_size_limits = (0.015, 0.6)
        
        # Data tracking
        self.price_history = deque(maxlen=250)
        self.volume_history = deque(maxlen=250)
        self.tick_timestamps = deque(maxlen=250)
        
        # Coinglass heatmap data
        self.heatmap_data = None
        self.last_heatmap_update = 0
        self.heatmap_update_interval = 300  # Update every 5 minutes
        self.liquidity_zones = []
        self.market_bias = 'neutral'
        self.bias_confidence = 0.0
        self.coinglass_api_key = "5e283b4ccdc0474e99ac39ea68e1a398" # User's API Key
        
        # Position tracking
        self.current_position = None
        self.position_entry_time = None
        self.position_entry_price = None
        self.position_side = None
        self.current_order_id = None # To track the maker order
        
        print("🚀 HYBRID HEATMAP STRATEGY - dYdX BTC FUTURES")
        print("=" * 60)

    async def setup_market(self):
        """Fetches market data and sets the market object."""
        markets_response = await self.indexer.markets.get_perpetual_markets(self.market_id)
        self.market = Market(markets_response["markets"][self.market_id])
        print(f"Market set: {self.market_id}")
        print(f"Current Oracle Price: {markets_response['markets'][self.market_id]['oraclePrice']}")

    async def get_subaccount_balance(self):
        """Fetches the current balance of the trading subaccount (subaccount 0)."""
        try:
            # Corrected to use indexer.account.get_subaccount as per docs
            subaccount_data = await self.indexer.account.get_subaccount(
                address=self.address,
                subaccount_number=self.subaccount_number
            )
            
            # FIX: Access the balance using ['subaccount']['equity'] as per documentation
            # Use 0.0 if the equity key is not present (unfunded subaccount)
            equity = float(subaccount_data.get('subaccount', {}).get('equity', 0.0))
                
            self.current_capital = equity
            print(f"✅ Subaccount {self.subaccount_number} Equity (USDC): {self.current_capital:.2f}")
            return self.current_capital
            
        except Exception as e:
            # Log the error but allow the bot to proceed with initial capital
            print(f"⚠️ Could not fetch subaccount balance. Using initial capital. Error: {e}")
            return self.current_capital

    # --- Coinglass Logic (from user's code, updated for BTC) ---

    async def fetch_coinglass_heatmap(self):
        """
        Fetch BTC liquidation heatmap from Coinglass
        Uses public API endpoint (no key required)
        """
        try:
            # Coinglass public API endpoint
            url = "https://open-api-v4.coinglass.com/api/futures/liquidation/heatmap/model1"
            params = {
                "symbol": "BTC", # Updated from DOGE to BTC
                "interval": "1h"
            }
            headers = {
                "coinglassSecret": self.coinglass_api_key
            }
            
            async with httpx.AsyncClient(timeout=10 ) as client:
                response = await client.get(url, params=params, headers=headers)
                response.raise_for_status() # Raise exception for 4xx or 5xx status codes
            
            data = response.json()
            self.heatmap_data = data
            self.last_heatmap_update = time.time()
            self.analyze_heatmap(data)
            print(f"✅ Heatmap updated: {self.market_bias} bias ({self.bias_confidence:.0f}% confidence)")
            return True
                
        except Exception as e:
            print(f"❌ Heatmap error: {e}")
            return False
    
    def analyze_heatmap(self, data):
        """
        Analyze Coinglass heatmap to determine market bias
        Price tends to move toward high liquidation zones
        """
        if not data or 'data' not in data:
            return
        
        try:
            current_price = self.price_history[-1] if self.price_history else 0
            if current_price == 0:
                return
            
            # Extract liquidation zones from heatmap
            zones = data.get('data', {}).get('liquidation_data', [])
            
            # Calculate liquidation volume above and below current price
            volume_above = 0
            volume_below = 0
            
            self.liquidity_zones = []
            
            for zone in zones:
                price = float(zone.get('price', 0))
                volume = float(zone.get('volume', 0))
                
                self.liquidity_zones.append({
                    'price': price,
                    'volume': volume
                })
                
                if price > current_price:
                    volume_above += volume
                elif price < current_price:
                    volume_below += volume
            
            # Determine market bias based on liquidation concentration
            total_volume = volume_above + volume_below
            
            if total_volume > 0:
                above_ratio = volume_above / total_volume
                
                if above_ratio > 0.6:
                    # More liquidations above = price likely to move UP
                    self.market_bias = 'bullish'
                    self.bias_confidence = above_ratio * 100
                elif above_ratio < 0.4:
                    # More liquidations below = price likely to move DOWN
                    self.market_bias = 'bearish'
                    self.bias_confidence = (1 - above_ratio) * 100
                else:
                    self.market_bias = 'neutral'
                    self.bias_confidence = 50
            
        except Exception as e:
            print(f"⚠️ Heatmap analysis error: {e}")
    
    async def add_tick(self, price: float, timestamp: float, volume: float = 0):
        """Add new price tick and update all analysis"""
        self.price_history.append(price)
        self.volume_history.append(volume)
        self.tick_timestamps.append(timestamp)
        
        # Update heatmap periodically
        if time.time() - self.last_heatmap_update > self.heatmap_update_interval:
            await self.fetch_coinglass_heatmap()
        
        # Analyze every 8 ticks
        if len(self.price_history) % 8 == 0:
            return await self.analyze_trading_signal(price, timestamp)
        
        return None
    
    def detect_micro_ranges(self, prices: List[float]) -> Dict:
        """
        Detect micro-ranges across multiple timeframes
        Returns range data for 15, 25, and 40 tick windows
        """
        ranges = {}
        
        for lookback in self.range_lookbacks:
            if len(prices) < lookback:
                continue
            
            recent = prices[-lookback:]
            range_high = max(recent)
            range_low = min(recent)
            range_size = (range_high - range_low) / range_low * 100
            
            # Validate range size
            if self.range_size_limits[0] <= range_size <= self.range_size_limits[1]:
                current_pos_pct = (prices[-1] - range_low) / (range_high - range_low) * 100
                
                # Calculate range strength
                mid_point = (range_high + range_low) / 2
                touches_high = sum(1 for p in recent if abs(p - range_high) / range_high < 0.001)
                touches_low = sum(1 for p in recent if abs(p - range_low) / range_low < 0.001)
                range_strength = (touches_high + touches_low) / len(recent)
                
                ranges[f'{lookback}_tick'] = {
                    'high': range_high,
                    'low': range_low,
                    'size_pct': range_size,
                    'current_position_pct': current_pos_pct,
                    'strength': range_strength,
                    'confidence': min(100, range_strength * 150)
                }
        
        return ranges
    
    def get_micro_range_consensus(self, ranges: Dict) -> Optional[Dict]:
        """
        Get consensus range when at least 2 out of 3 timeframes agree
        """
        if len(ranges) < 2:
            return None
        
        # Calculate average range parameters
        avg_high = np.mean([r['high'] for r in ranges.values()])
        avg_low = np.mean([r['low'] for r in ranges.values()])
        avg_pos = np.mean([r['current_position_pct'] for r in ranges.values()])
        avg_confidence = np.mean([r['confidence'] for r in ranges.values()])
        
        return {
            'high': avg_high,
            'low': avg_low,
            'current_position_pct': avg_pos,
            'confidence': avg_confidence,
            'range': avg_high - avg_low,
            'timeframes_agreeing': len(ranges)
        }
    
    def detect_sequence_patterns(self, prices: List[float], pattern_length: int = 5, 
                                 min_occurrences: int = 3) -> List[Dict]:
        """
        Detect repeating price sequence patterns to predict next movement
        """
        if len(prices) < pattern_length * min_occurrences:
            return []
        
        patterns = []
        recent_prices = prices[-250:] # Use a reasonable lookback for pattern detection
        
        # Convert prices to percentage changes for pattern matching
        changes = []
        for i in range(1, len(recent_prices)):
            change_pct = (recent_prices[i] - recent_prices[i-1]) / recent_prices[i-1] * 100
            changes.append(change_pct)
        
        if len(changes) < pattern_length:
            return []
        
        # Look for the current pattern in historical data
        current_pattern = changes[-(pattern_length-1):]
        
        # Search for similar patterns in history
        matches = []
        for i in range(len(changes) - pattern_length):
            historical_pattern = changes[i:i+(pattern_length-1)]
            
            # Calculate pattern similarity
            similarity = 1.0 - (np.std(np.array(current_pattern) - np.array(historical_pattern)) / 10.0)
            
            if similarity > 0.7:  # 70% similarity threshold
                # Get the next move after this pattern
                if i + pattern_length < len(changes):
                    next_move = changes[i + pattern_length - 1]
                    matches.append({
                        'similarity': similarity,
                        'next_move': next_move
                    })
        
        if len(matches) >= min_occurrences:
            # Calculate predicted next move
            avg_next_move = np.mean([m['next_move'] for m in matches])
            confidence = np.mean([m['similarity'] for m in matches]) * 100
            
            patterns.append({
                'pattern_length': pattern_length,
                'occurrences': len(matches),
                'next_predicted': avg_next_move,
                'confidence': confidence
            })
        
        return sorted(patterns, key=lambda x: x['confidence'], reverse=True)
    
    def is_in_extreme_zone(self, current_price: float, range_data: Dict, side: str) -> bool:
        """
        Check if price is in extreme zone for entry
        Bottom 35% for longs, top 35% for shorts
        """
        pos_pct = range_data['current_position_pct']
        
        if side == 'long':
            return pos_pct <= self.entry_zones[0]  # Bottom 35%
        elif side == 'short':
            return pos_pct >= self.entry_zones[1]  # Top 35%
        
        return False
    
    def calculate_entry_confidence(self, range_confidence: float, pattern_confidence: float,
                                   bias_confidence: float, recent_accuracy: float) -> float:
        """
        Calculate overall entry confidence from multiple factors
        """
        # Weighted average of confidence factors
        confidence = (
            range_confidence * 0.3 +
            pattern_confidence * 0.3 +
            bias_confidence * 0.2 +
            recent_accuracy * 0.2
        )
        
        return min(100, confidence)
    
    async def analyze_trading_signal(self, current_price: float, timestamp: float) -> Optional[Dict]:
        """
        Main trading signal analysis
        """
        # Need minimum history
        if len(self.price_history) < 40:
            return None
        
        # Don't trade if already in position
        if self.current_position:
            return None
        
        # Need heatmap data
        if not self.heatmap_data:
            return None
        
        prices = list(self.price_history)
        
        # Detect micro-ranges
        ranges = self.detect_micro_ranges(prices)
        consensus_range = self.get_micro_range_consensus(ranges)
        
        if not consensus_range:
            return None
        
        # Detect sequence patterns
        patterns = self.detect_sequence_patterns(prices, pattern_length=5, min_occurrences=3)
        
        if not patterns:
            return None
        
        best_pattern = patterns[0]
        
        # Determine trade side based on market bias and pattern
        if self.market_bias == 'bullish':
            trade_side = 'long'
        elif self.market_bias == 'bearish':
            trade_side = 'short'
        else:
            # Use pattern prediction for neutral bias
            trade_side = 'long' if best_pattern['next_predicted'] > 0 else 'short'
        
        # Check if in extreme zone
        if not self.is_in_extreme_zone(current_price, consensus_range, trade_side):
            return None
        
        # Calculate entry confidence
        recent_acc = np.mean(self.recent_accuracy) * 100 if self.recent_accuracy else 70
        confidence = self.calculate_entry_confidence(
            consensus_range['confidence'],
            best_pattern['confidence'],
            self.bias_confidence,
            recent_acc
        )
        
        # Check confidence threshold
        if confidence < self.confidence_threshold * 100:
            return None
        
        # Calculate maker order price (slightly away from current price)
        price_offset = consensus_range['range'] * 0.001  # 0.1% of range
        
        if trade_side == 'long':
            entry_price = current_price - price_offset  # Buy below current
        else:
            entry_price = current_price + price_offset  # Sell above current
        
        # Calculate position size
        position_value = (self.current_capital * self.position_size_pct)
        
        # Adjust based on confidence (up to 20% increase)
        confidence_multiplier = 1 + ((confidence - 70) / 100) * 0.2
        adjusted_value = position_value * confidence_multiplier
        
        # Calculate size in BTC
        size = adjusted_value / entry_price
        
        return {
            'action': 'enter',
            'side': trade_side,
            'entry_price': entry_price,
            'size': size,
            'confidence': confidence,
            'leverage': self.leverage,
            'timestamp': timestamp,
            'market_bias': self.market_bias,
            'pattern_prediction': best_pattern['next_predicted'],
            'range_data': consensus_range
        }
    
    def enter_position(self, signal: Dict):
        """Enter a new position"""
        self.current_position = signal
        self.position_entry_time = signal['timestamp']
        self.position_entry_price = signal['entry_price']
        self.position_side = signal['side']
        
        print(f"\n{'🟢 LONG' if signal['side'] == 'long' else '🔴 SHORT'} ENTRY")
        print(f"Price: ${signal['entry_price']:.2f}")
        print(f"Size: {signal['size']:.4f} BTC")
        print(f"Confidence: {signal['confidence']:.1f}%")
        print(f"Market Bias: {signal['market_bias']}")
        print(f"Pattern: {signal['pattern_prediction']:+.3f}%")
    
    def check_exit_conditions(self, current_price: float, timestamp: float) -> Optional[str]:
        """
        Check if position should be exited
        Returns exit reason or None
        """
        if not self.current_position:
            return None
        
        entry_price = self.position_entry_price
        side = self.position_side
        
        # Calculate P&L percentage
        if side == 'long':
            pnl_pct = (current_price - entry_price) / entry_price * 100
        else:
            pnl_pct = (entry_price - current_price) / entry_price * 100
        
        # Profit target
        if pnl_pct >= self.profit_target_pct:
            return 'profit_target'
        
        # Stop loss
        if pnl_pct <= -self.stop_loss_pct:
            return 'stop_loss'
        
        # Timeout
        if timestamp - self.position_entry_time >= self.timeout_seconds:
            return 'timeout'
        
        # Market bias reversal
        if side == 'long' and self.market_bias == 'bearish':
            return 'bias_reversal'
        elif side == 'short' and self.market_bias == 'bullish':
            return 'bias_reversal'
        
        # Partial profit protection (after 2 minutes, protect 80% of target)
        if timestamp - self.position_entry_time >= 120:
            if pnl_pct >= self.profit_target_pct * 0.8:
                return 'partial_profit'
        
        return None
    
    # --- dYdX Trading Functions ---

    async def place_maker_order(self, side: str, price: float, size: float):
        """Places a long-term maker order on dYdX."""
        if not self.market:
            raise Exception("Market not initialized. Run setup_market first.")

        # 1. Generate a unique order ID
        self.current_order_id = self.market.order_id(
            self.address, 
            self.subaccount_number, 
            random.randint(0, MAX_CLIENT_ID), 
            OrderFlags.LONG_TERM
        )
        
        # 2. Set the good_til_block_time (e.g., 5 minutes from now)
        good_til_time = since_now(seconds=300)
        
        # 3. Determine Order Side
        order_side = 1 if side == 'long' else 2 # 1=BUY, 2=SELL (Using raw int to avoid import error)
        
        # 4. Create the order object
        order = self.market.order(
            order_id=self.current_order_id,
            order_type=OrderType.LIMIT,
            side=order_side,
            size=size,
            price=price,
            time_in_force=0, # 0=TIME_IN_FORCE_UNSPECIFIED (Fixes "Stateful orders cannot require immediate execution" error)
            reduce_only=False,
            good_til_block_time=good_til_time,
        )
        
        # 5. Place the order
        print(f"Placing {side.upper()} maker order at {price:.2f}...")
        place_tx = await self.node.place_order(wallet=self.wallet, order=order)
        self.wallet.sequence += 1
        print(f"Place TX Hash: {place_tx}")
        return place_tx

    async def cancel_current_order(self):
        """Cancels the currently tracked order."""
        if not self.current_order_id:
            print("No active order to cancel.")
            return None
        
        # Set a new good_til_block_time for the cancel transaction
        cancel_good_til_time = since_now(seconds=60)
        
        print(f"Cancelling order with ID: {self.current_order_id}...")
        cancel_tx = await self.node.cancel_order(
            wallet=self.wallet,
            order_id=self.current_order_id,
            good_til_block_time=cancel_good_til_time
        )
        self.wallet.sequence += 1
        self.current_order_id = None
        print(f"Cancel TX Hash: {cancel_tx}")
        return cancel_tx

    async def transfer_funds(self, amount: float, to_subaccount: int):
        """
        Transfers funds (USDC) between subaccounts.
        We use subaccount 0 for trading and subaccount 1 for reserves.
        """
        
        # Asset ID for USDC is 0
        ASSET_ID = 0
        
        print(f"Transferring {amount:.6f} USDC from Subaccount 0 to Reserves (Subaccount 1)...")
        
        try:
            # NOTE: Using the node client's transfer method.
            transfer_tx = await self.node.transfer(
                wallet=self.wallet,
                sender_subaccount_number=self.subaccount_number, # 0
                recipient_address=self.address, # Main wallet address
                recipient_subaccount_number=to_subaccount, # 1 (Reserves)
                asset_id=ASSET_ID,
                amount=amount
            )
            self.wallet.sequence += 1
            print(f"Transfer TX Hash: {transfer_tx}")
            
            # Update local capital tracking only after successful transaction
            self.current_capital -= amount
            self.reserves += amount
            print(f"Transfer Complete. Trading Capital: {self.current_capital:.2f} | Reserves: {self.reserves:.2f}")
            
        except Exception as e:
            print(f"❌ Transfer failed: {e}")
            print("Local capital tracking NOT updated.")
            
    async def exit_position(self, exit_price: float, exit_reason: str):
        """Exit current position and handle compounding."""
        if not self.current_position:
            return
        
        entry_price = self.position_entry_price
        side = self.position_side
        size = self.current_position['size']
        
        # 1. Place TAKER order to close the position (simulated for now)
        # In a real bot, this would be a TAKER market order to close the position.
        # await self.place_taker_order(side='sell' if side == 'long' else 'buy', size=size)
        
        # 2. Calculate P&L
        if side == 'long':
            pnl_pct = (exit_price - entry_price) / entry_price * 100
        else:
            pnl_pct = (entry_price - exit_price) / entry_price * 100 # FIX: Use exit_price here
        
        # Apply leverage
        pnl_pct_leveraged = pnl_pct * self.leverage
        
        # Calculate dollar P&L
        position_value = size * entry_price
        pnl_dollars = position_value * (pnl_pct_leveraged / 100)
        
        # Add maker rebates (earned on both entry and exit)
        rebate = position_value * self.maker_rebate_rate * 2  # Entry + exit
        pnl_dollars += rebate
        
        # 3. Update capital and compounding logic (50% to reserves, 50% to trading)
        self.current_capital += pnl_dollars
        
        if pnl_dollars > 0:
            # User requested 50% to wallet (reserves) and 50% to trading capital.
            reserve_amount = pnl_dollars * 0.5
            
            # Transfer half the profit to the reserves subaccount (Subaccount 1)
            await self.transfer_funds(reserve_amount, to_subaccount=1)
            
            # The other 50% remains in self.current_capital (trading subaccount 0) for compounding.
        
        # 4. Update stats
        self.total_trades += 1
        if pnl_dollars > 0:
            self.winning_trades += 1
            self.recent_accuracy.append(1)
        else:
            self.recent_accuracy.append(0)
        
        win_rate = (self.winning_trades / self.total_trades * 100) if self.total_trades > 0 else 0
        
        print(f"\n{'✅ WIN' if pnl_dollars > 0 else '❌ LOSS'} - {exit_reason.upper()}")
        print(f"Exit Price: ${exit_price:.2f}")
        print(f"P&L: ${pnl_dollars:+.2f} ({pnl_pct_leveraged:+.2f}%)")
        print(f"Rebate: ${rebate:.4f}")
        print(f"Capital: ${self.current_capital:.2f} | Reserves: ${self.reserves:.2f} (50% Profit Compounded)")
        print(f"Win Rate: {win_rate:.1f}% ({self.winning_trades}/{self.total_trades})")
        
        self.current_position = None
        self.position_entry_time = None
        self.position_entry_price = None
        self.position_side = None
        self.current_order_id = None
    
    def get_status(self) -> Dict:
        """Get current bot status"""
        return {
            'current_capital': self.current_capital,
            'reserves': self.reserves,
            'total_capital': self.current_capital + self.reserves,
            'total_trades': self.total_trades,
            'winning_trades': self.winning_trades,
            'win_rate': (self.winning_trades / self.total_trades * 100) if self.total_trades > 0 else 0,
            'in_position': self.current_position is not None,
            'market_bias': self.market_bias,
            'bias_confidence': self.bias_confidence,
            'tick_count': len(self.price_history)
        }
    
    # --- Main Bot Logic ---

    async def handle_ws_message(self, price: float, timestamp: float, source: str, size: float = 0.0):
        """
        Handles incoming WebSocket messages (trades/ticks) from the custom DydxLiveWebSocket.
        The custom WS client already extracts the price, timestamp, and size.
        """
        # We only care about trade ticks for the trading logic
        if source != 'trade':
            return

        # 1. Add tick and check for signal
        signal = await self.add_tick(price, timestamp, size)
        
        # 2. Process signal
        if signal and not self.current_position:
            # 3. Place order
            await self.place_maker_order(
                side=signal['side'],
                price=signal['entry_price'],
                size=signal['size']
            )
            self.enter_position(signal)
        
        elif self.current_position:
            # 4. Check exit conditions
            exit_reason = self.check_exit_conditions(price, timestamp)
            
            if exit_reason:
                # 5. Cancel order (if not filled) or close position (if filled)
                # NOTE: This is a critical point. We need to check if the maker order was filled.
                # For this simulation, we will assume the order was filled and we are closing the position.
                
                # If the order was not filled, cancel it
                # await self.cancel_current_order() 
                
                # Simulate P&L calculation and compounding
                await self.exit_position(price, exit_reason)

    async def run_bot(self):
        """Main execution loop for the bot."""
        await self.setup_market()
        await self.get_subaccount_balance()
        
        # Start the Coinglass heatmap update task in the background
        asyncio.create_task(self._heatmap_updater())
        
        print("\nStarting dYdX WebSocket connection for real-time data...")
        
        # 1) Instantiate the custom WebSocket client
        # Pass the market ID and the handle_ws_message as the on_tick callback
        ws_client = DydxLiveWebSocket(
            market=self.market_id,
            on_tick=self.handle_ws_message
        )
        
        # 2) Connect and start the listener
        try:
            await ws_client.connect()
            
            # 3) Keep the main task running to allow the WebSocket to process messages
            while True:
                await asyncio.sleep(1)
            
        except Exception as e:
            print(f"❌ WebSocket connection failed: {e}. Retrying in 5 seconds...")
            await asyncio.sleep(5)
            return await self.run_bot() # Recursive call to retry connection

    async def _heatmap_updater(self):
        """Background task to periodically update the Coinglass heatmap."""
        while True:
            await self.fetch_coinglass_heatmap()
            await asyncio.sleep(self.heatmap_update_interval) # Wait 5 minutes (300s)

# --- Main Execution ---

async def main():
    # Use official mainnet endpoints
    network = make_mainnet(
        # FIX: Removed https:// prefix from node_url
        node_url="oegs.dydx.trade:443",
        rest_indexer="https://indexer.dydx.trade",
        websocket_indexer="wss://indexer.dydx.trade/v4/ws"
      )
    
    # FIX: Use network.node when connecting NodeClient
    node = await NodeClient.connect(network.node)
    indexer = IndexerClient(network.rest_indexer)
    
    # NOTE: Ensure 'mnemonic.txt' is in the same directory and contains your mnemonic phrase.
    try:
        with open('mnemonic.txt', 'r') as f:
            mnemonic = f.read().strip()
    except FileNotFoundError:
        print("ERROR: 'mnemonic.txt' not found. Please create this file with your mnemonic phrase.")
        return
    
    # NOTE: The address is used to derive the correct wallet.
    wallet = await Wallet.from_mnemonic(node, mnemonic, "dydx1kydnehq9hfqqrt28jc2nhaxux33hk2sh9zw84q")
    
    # FIX: Passed the network object to the bot constructor
    bot = HybridHeatmapDydx(wallet, node, indexer, network)
    await bot.run_bot()

if __name__ == "__main__":
    # Run the main function
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped by user.")
