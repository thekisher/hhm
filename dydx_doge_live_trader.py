#!/usr/bin/env python3
"""
dYdX DOGE Live Trader - Hybrid Heatmap Strategy
Trades DOGE on dYdX mainnet with Coinglass Binance liquidation heatmap
Places POST_ONLY maker orders to earn -0.011% rebates
"""
import asyncio
import random
import time
from datetime import datetime
from typing import Optional

from dydx_v4_client.network import make_mainnet
from dydx_v4_client.node.client import NodeClient
from dydx_v4_client.indexer.rest.indexer_client import IndexerClient
from dydx_v4_client.indexer.socket.websocket import IndexerSocket
from dydx_v4_client.wallet import Wallet
from dydx_v4_client.node.market import Market
from dydx_v4_client import MAX_CLIENT_ID, OrderFlags
from dydx_v4_client.indexer.rest.constants import OrderType
from v4_proto.dydxprotocol.clob.order_pb2 import Order
import random

from hybrid_heatmap_no_rebates import HybridHeatmapNoRebates


class DydxDogeLiveTrader:
    def __init__(self, private_key: str, address: str):
        self.private_key = private_key
        self.address = address
        
        # Strategy with Coinglass heatmap
        self.strategy = HybridHeatmapNoRebates(initial_capital=100.0, live_mode=True)
        self.strategy.leverage = 50
        self.strategy.trading_fee_rate = -0.00011  # dYdX maker rebate
        self.strategy.position_size_pct = 0.18
        
        # dYdX network (mainnet)
        network_config = make_mainnet(
            node_url="dydx-ops-grpc.kingnodes.com:443",
            rest_indexer="https://indexer.dydx.trade",
            websocket_indexer="wss://indexer.dydx.trade/v4/ws",
        )
        self.network = network_config.node
        self.rest_indexer = network_config.rest_indexer
        self.websocket_indexer = network_config.websocket_indexer
        
        self._node: Optional[NodeClient] = None
        self._indexer: Optional[IndexerClient] = None
        self._wallet: Optional[Wallet] = None
        self._socket: Optional[IndexerSocket] = None
        
        print("=" * 80)
        print("🚀 DYDX DOGE LIVE TRADER - HYBRID HEATMAP STRATEGY")
        print("=" * 80)
        print(f"💰 Starting Capital: $100")
        print(f"⚡ Leverage: 50x")
        print(f"🐶 Market: DOGE-USD on dYdX mainnet")
        print(f"📊 Heatmap: Binance DOGE liquidations from Coinglass")
        print(f"💎 POST_ONLY Maker Orders: -0.011% rebate")
        print(f"🔄 Heatmap updates every 2 minutes")
        print("=" * 80)
        print()
        
        self.tick_count = 0
        self.last_status_time = time.time()
        self.last_trade_count = 0
        self.start_time = time.time()
        self.last_message_time = time.time()
        
        # Order tracking
        self.open_orders = {}  # client_id -> order_info
        self.market_helper = None
        self.subaccount_number = 0
        self.processed_trade_ids = set()  # Track all processed trade IDs to avoid duplicates
        self.poll_count = 0
    
    async def connect(self):
        """Connect to dYdX mainnet"""
        print("📡 Connecting to dYdX mainnet...")
        
        # Node client first
        self._node = await NodeClient.connect(self.network)
        print("✅ Node client connected")
        
        # Derive the actual address from the mnemonic using KeyPair
        from dydx_v4_client.key_pair import KeyPair
        key_pair = KeyPair.from_mnemonic(self.private_key)
        # Create temporary wallet to get address
        temp_wallet = Wallet(key_pair, 0, 0)
        derived_address = temp_wallet.address
        print(f"🔑 Derived address from mnemonic: {derived_address}")
        
        # Wallet - initialize from mnemonic (requires node, mnemonic, address)
        self._wallet = await Wallet.from_mnemonic(self._node, self.private_key, derived_address)
        print(f"✅ Wallet initialized: {derived_address}")

        # Indexer client
        self._indexer = IndexerClient(self.rest_indexer)
        print("✅ Indexer client connected")
        balances=self._indexer.get.get_account_balances(temp_wallet.address)
        print(balances)
        return
        # Initialize Market helper for DOGE-USD
        market_data = await self._indexer.markets.get_perpetual_markets("DOGE-USD")
        self.market_helper = Market(market_data["markets"]["DOGE-USD"])
        print("✅ Market helper initialized for DOGE-USD")
        print()
    
    def websocket_handler(self, ws: IndexerSocket, message: dict):
        """Handle WebSocket messages from dYdX"""
        # Only print non-channel_data messages to reduce spam
        msg_type = message.get('type', 'unknown')
        if msg_type != 'channel_data':
            print(f"📨 WebSocket message: {msg_type}")
        
        # Update last message time
        self.last_message_time = time.time()
        
        if message.get("type") == "connected":
            # Subscribe to DOGE trades
            ws.trades.subscribe("DOGE-USD", batched=False)
            print("✅ Subscribed to DOGE-USD trades - receiving ALL trades in real-time\n")
            return
        
        if message.get("channel") == "v4_trades" and "contents" in message:
            contents = message["contents"]
            trades = contents.get("trades", [])
            
            if len(trades) > 10:
                print(f"\n📦 Received batch of {len(trades)} trades (likely historical data)")
            
            for trade in trades:
                try:
                    price = float(trade["price"])
                    size = float(trade["size"])
                    timestamp = time.time()
                    
                    self.tick_count += 1
                    
                    # Feed to strategy (includes Coinglass heatmap)
                    signal = self.strategy.add_tick(price, timestamp, volume=size)
                    
                    # Print status every 100 ticks
                    if self.tick_count % 100 == 0:
                        self.print_status_sync(price)
                    
                    # Handle trade signals (but don't place orders from WebSocket handler)
                    if signal:
                        # Store signal for processing in main loop
                        self.pending_signal = (signal, price)
                except Exception as e:
                    print(f"⚠️  Error processing trade: {e}")
            
            if len(trades) > 10:
                print(f"✅ Batch processed. Now waiting for live trades...")
                print(f"   (DOGE may have low volume - new trades will appear as they happen)")
    
    async def run(self, duration_minutes=15):
        print(f"📊 Coinglass heatmap will update every 2 minutes")
        print(f"⏰ Started at: {datetime.now().strftime('%H:%M:%S')}")
        print(f"⏱️  Will run for {duration_minutes} minutes")
        print()
        
        # Connect to dYdX
        await self.connect()
        
        end_time = time.time() + (duration_minutes * 60)
        
        try:
            # Connect WebSocket
            print("🔌 Connecting to WebSocket...")
            self._socket = IndexerSocket(
                self.websocket_indexer,
                on_message=self.websocket_handler
            ).connect()
            print("✅ WebSocket connected! Waiting for messages...\n")
            
            # Run - process signals in main loop
            self.pending_signal = None
            last_heartbeat = time.time()
            #print("outside while loop")
            while time.time() < end_time:
                try:
                    #print("inside while loop")
                    # Process any pending signals
                    if self.pending_signal:
                        signal, price = self.pending_signal
                        self.pending_signal = None
                        await self.handle_signal(signal, price)
                    
                    # Check if data is still flowing - poll REST API if WebSocket is quiet
                    if time.time() - self.last_message_time > 0.5:
                        self.poll_count += 1
                        
                        # Debug: Show polling status every 10 polls
                        if self.poll_count % 10 == 1:
                            print(f"\n🔍 Polling REST API (poll #{self.poll_count})...")
                        
                        # Fetch latest trades via REST API as fallback
                        try:
                            #print("inside try statement")
                            trades_data = await self._indexer.markets.get_perpetual_market_trades("DOGE-USD")
                            #print(trades_data)
                            # Debug first poll
                            if self.poll_count == 1:
                                print(f"   API Response keys: {list(trades_data.keys()) if trades_data else 'None'}")
                                if trades_data and 'trades' in trades_data:
                                    print(f"   Found {len(trades_data['trades'])} trades in response")
                            
                            if trades_data and 'trades' in trades_data:
                                new_trades = []
                                for trade in trades_data['trades']:  # Check all trades
                                    trade_id = trade.get('id')
                                    if trade_id and trade_id not in self.processed_trade_ids:
                                        new_trades.append(trade)
                                        self.processed_trade_ids.add(trade_id)
                                        # Keep set size manageable (last 1000 trades)
                                        if len(self.processed_trade_ids) > 1000:
                                            self.processed_trade_ids = set(list(self.processed_trade_ids)[-500:])
                                
                                if new_trades:
                                    print(f"\n🔄 Fetched {len(new_trades)} new trades via REST API (poll #{self.poll_count})")
                                    for trade in reversed(new_trades):  # Process oldest first
                                        price = float(trade['price'])
                                        size = float(trade['size'])
                                        timestamp = time.time()
                                        
                                        self.tick_count += 1
                                        signal = self.strategy.add_tick(price, timestamp, volume=size)
                                        
                                        if self.tick_count % 100 == 0:
                                            self.print_status_sync(price)
                                        
                                        if signal:
                                            self.pending_signal = (signal, price)
                                elif self.poll_count % 10 == 1:
                                    print(f"   No new trades (tracking {len(self.processed_trade_ids)} IDs)")
                        except Exception as e:
                            print(f"⚠️  REST API fetch failed (poll #{self.poll_count}): {e}")
                            import traceback
                            traceback.print_exc()
                        
                        self.last_message_time = time.time()  # Reset timer
                    
                    # Heartbeat every 60 seconds
                    if time.time() - last_heartbeat > 60:
                        elapsed = (time.time() - self.start_time) / 60
                        print(f"\n❤️  Heartbeat: {elapsed:.1f} min elapsed, {self.tick_count:,} ticks processed")
                        last_heartbeat = time.time()
                    
                    await asyncio.sleep(0.01)  # Check every 10ms for faster response
                except KeyboardInterrupt:
                    break
                    
        except Exception as e:
            print(f"\n❌ Error: {e}")
        finally:
            if self._socket:
                await self._socket.close()
                
        
        await self.print_final()
    
    def print_status_sync(self, price):
        """Synchronous status printing for WebSocket handler"""
        s = self.strategy.get_status()
        runtime = time.time() - self.start_time
        
        print(f"\n📊 Tick #{self.tick_count:,} | {datetime.now().strftime('%H:%M:%S')} | DOGE ${price:.6f}")
        print(f"💰 ${s['current_capital']:,.2f} → ${s['total_capital']:,.2f} ({s['total_return_pct']:+.2f}%)")
        print(f"⏱️  Runtime: {runtime/60:.1f} min | Bias: {s['market_bias']}")
        
        if s['total_trades'] > 0:
            print(f"🎯 {s['total_trades']} trades | {s['win_rate_pct']:.1f}% WR")
            print(f"📊 {s['liquidation_clusters']} Coinglass clusters")
        else:
            warmup = max(0, 250 - len(self.strategy.price_history))
            if warmup > 0:
                print(f"🎯 Warming up... ({len(self.strategy.price_history)}/250)")
            else:
                print(f"🎯 Ready! Waiting for high-confidence signal (70%+)")
    
    async def print_status(self, price):
        s = self.strategy.get_status()
        runtime = time.time() - self.start_time
        
        print(f"\n📊 Tick #{self.tick_count:,} | {datetime.now().strftime('%H:%M:%S')} | DOGE ${price:.6f}")
        print(f"💰 ${s['current_capital']:,.2f} → ${s['total_capital']:,.2f} ({s['total_return_pct']:+.2f}%)")
        print(f"⏱️  Runtime: {runtime/60:.1f} min | Bias: {s['market_bias']}")
        
        if s['total_trades'] > 0:
            print(f"🎯 {s['total_trades']} trades | {s['win_rate_pct']:.1f}% WR")
            print(f"📊 {s['liquidation_clusters']} Coinglass clusters")
        else:
            warmup = max(0, 250 - len(self.strategy.price_history))
            if warmup > 0:
                print(f"🎯 Warming up... ({len(self.strategy.price_history)}/250)")
            else:
                print(f"🎯 Ready! Waiting for high-confidence signal (70%+)")
    
    async def handle_signal(self, signal, price):
        action = signal.get('action')
        
        if action in ['long', 'short']:
            await self.open_position(signal, price)
            
        elif action == 'close':
            await self.close_position(signal, price)
    
    async def open_position(self, signal: dict, price: float):
        """Place POST_ONLY maker order on dYdX"""
        try:
            # Check if we already have an open position
            if self.open_orders:
                print("⚠️ Already have open order, skipping")
                return None
            
            side = Order.Side.SIDE_BUY if signal['action'] == 'long' else Order.Side.SIDE_SELL
            
            # Calculate size in DOGE
            position_value = self.strategy.current_capital * self.strategy.position_size_pct
            size = (position_value * self.strategy.leverage) / price
            size = round(size, 1)  # Round to 1 decimal for DOGE
            
            # Minimum size check
            if size < 1:
                print(f"⚠️ Size too small: {size} DOGE (min 1)")
                return None
            
            # Generate unique client ID
            client_id = random.randint(0, MAX_CLIENT_ID)
            
            # Create order ID
            order_id = self.market_helper.order_id(
                self.address,
                self.subaccount_number,
                client_id,
                OrderFlags.SHORT_TERM
            )
            
            # Get current block
            current_block = await self._node.latest_block_height()
            good_til_block = current_block + 20  # Valid for ~20 blocks (~2 minutes)
            
            # Create order with POST_ONLY flag
            new_order = self.market_helper.order(
                order_id=order_id,
                order_type=OrderType.LIMIT,
                side=side,
                size=size,
                price=price,
                time_in_force=Order.TimeInForce.TIME_IN_FORCE_POST_ONLY,
                reduce_only=False,
                good_til_block=good_til_block,
            )
            
            # Place order
            print(f"\n{'='*75}")
            print(f"📤 PLACING {signal['action'].upper()} ORDER")
            print(f"   Price: ${price:.6f}")
            print(f"   Size: {size} DOGE")
            print(f"   Confidence: {signal.get('confidence', 0):.1%}")
            print(f"   POST_ONLY (Maker Rebate: -0.011%)")
            
            result = await self._node.place_order(
                wallet=self._wallet,
                order=new_order,
            )
            
            # Increment wallet sequence
            self._wallet.sequence += 1
            
            # Track order
            self.open_orders[client_id] = {
                'signal': signal,
                'order_id': order_id,
                'side': signal['action'],
                'size': size,
                'price': price,
                'timestamp': time.time()
            }
            
            print(f"   ✅ Order placed! Client ID: {client_id}")
            print(f"   TX Hash: {result.tx_hash if hasattr(result, 'tx_hash') else 'N/A'}")
            print(f"{'='*75}")
            
            return result
            
        except Exception as e:
            print(f"\n❌ Order placement error: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    async def close_position(self, signal: dict, price: float):
        """Close position by placing opposite order"""
        try:
            if not self.open_orders:
                print("⚠️ No open orders to close")
                return
            
            # Get first open order
            client_id = list(self.open_orders.keys())[0]
            order_info = self.open_orders[client_id]
            
            # Place opposite order to close
            opposite_side = Order.Side.SIDE_SELL if order_info['side'] == 'long' else Order.Side.SIDE_BUY
            size = order_info['size']
            
            # Generate new client ID for close order
            close_client_id = random.randint(0, MAX_CLIENT_ID)
            
            close_order_id = self.market_helper.order_id(
                self.address,
                self.subaccount_number,
                close_client_id,
                OrderFlags.SHORT_TERM
            )
            
            current_block = await self._node.latest_block_height()
            
            # Market order to close quickly
            close_order = self.market_helper.order(
                order_id=close_order_id,
                order_type=OrderType.MARKET,
                side=opposite_side,
                size=size,
                price=price * 0.95 if opposite_side == Order.Side.SIDE_SELL else price * 1.05,  # Safety price
                time_in_force=Order.TimeInForce.TIME_IN_FORCE_IOC,  # Immediate or cancel
                reduce_only=True,
                good_til_block=current_block + 5,
            )
            
            print(f"\n{'='*75}")
            print(f"📤 CLOSING {order_info['side'].upper()} POSITION")
            print(f"   Entry: ${order_info['price']:.6f}")
            print(f"   Exit: ${price:.6f}")
            print(f"   Reason: {signal.get('reason', 'unknown')}")
            
            result = await self._node.place_order(
                wallet=self._wallet,
                order=close_order,
            )
            
            self._wallet.sequence += 1
            
            # Calculate PnL
            pnl = signal.get('net_pnl', 0)
            emoji = "✅" if pnl > 0 else "❌"
            
            print(f"   {emoji} Position closed! PnL: ${pnl:+.2f}")
            print(f"{'='*75}")
            
            # Remove from tracking
            del self.open_orders[client_id]
            
        except Exception as e:
            print(f"\n❌ Close position error: {e}")
            import traceback
            traceback.print_exc()
    
    async def print_final(self):
        s = self.strategy.get_status()
        runtime = time.time() - self.start_time
        
        print("\n" + "=" * 80)
        print("🏁 DOGE TRADING SESSION COMPLETE")
        print("=" * 80)
        print(f"📊 Ticks: {self.tick_count:,} | Runtime: {runtime/60:.1f} min")
        print(f"💼 Trades: {s['total_trades']} | Win Rate: {s['win_rate_pct']:.1f}%")
        print(f"💰 $100 → ${s['total_capital']:,.2f} ({s['total_return_pct']:+.2f}%)")
        print(f"📊 Market Bias: {s['market_bias']}")
        print(f"📊 Coinglass Clusters: {s['liquidation_clusters']}")
        print("=" * 80)


async def main():
    print("\n🔥 DYDX DOGE LIVE TRADER WITH COINGLASS HEATMAP")
    print("🐶 Trading DOGE on dYdX with Binance liquidation heatmap")
    print("💎 POST_ONLY maker orders to earn -0.011% rebates")
    print("Press Ctrl+C to stop early\n")
    
    # Your dYdX credentials
    MNEMONIC = "still endorse use choose monkey equal jungle ketchup obscure put stumble eye minimum ritual follow neck rally coin funny sock broccoli bracket kite render"
    WALLET_ADDRESS = "dydx1kydnehq9hfqqrt28jc2nhaxux33hk2sh9zw84q"
    
    trader = DydxDogeLiveTrader(private_key=MNEMONIC, address=WALLET_ADDRESS)
    await trader.run(duration_minutes=15)  # Run for 15 minutes


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n⚠️ Stopped by user")
