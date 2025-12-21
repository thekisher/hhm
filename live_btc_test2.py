#!/usr/bin/env python3
"""
Live BTC Test - Real-time pricing from Binance
Run the 50x strategy with live market data
"""
import asyncio
import websockets
import json
import time
from datetime import datetime
from hybrid_heatmap_no_rebates import HybridHeatmapNoRebates

class LiveBTCTest:
    def __init__(self):
        # Initialize strategy
        self.strategy = HybridHeatmapNoRebates(initial_capital=800.0)
        
        # dYdX 50x configuration
        self.strategy.leverage = 50
        self.strategy.trading_fee_rate = -0.00011  # -0.011% maker rebate
        self.strategy.position_size_pct = 0.18
        
        print("=" * 80)
        print("🚀 LIVE BTC TEST - 50X STRATEGY")
        print("=" * 80)
        print(f"💰 Starting Capital: $800")
        print(f"⚡ Leverage: 50x")
        print(f"💎 Maker Rebate: 0.011% (GET PAID!)")
        print(f"📊 Position Size: 18%")
        print("=" * 80)
        print()
        
        self.ws_url = "wss://stream.binance.com:9443/ws/btcusdt@trade"
        self.tick_count = 0
        self.last_status_time = time.time()
        self.last_trade_count = 0
        self.start_time = time.time()
        
    async def run(self):
        print(f"📡 Connecting to Binance live feed...")
        print(f"⏰ Started at: {datetime.now().strftime('%H:%M:%S')}")
        print()
        
        try:
            async with websockets.connect(self.ws_url) as ws:
                print(f"✅ Connected! Receiving live BTC prices\n")
                
                while True:
                    try:
                        msg = await ws.recv()
                        data = json.loads(msg)
                        
                        price = float(data['p'])
                        size = float(data['q'])
                        timestamp = data['T'] / 1000
                        
                        self.tick_count += 1
                        
                        # Feed to strategy
                        signal = self.strategy.add_tick(price, timestamp, size)
                        
                        # Print status every 100 ticks
                        if self.tick_count % 100 == 0:
                            await self.print_status(price)
                        
                        # Handle trade signals
                        if signal:
                            await self.handle_signal(signal, price)
                            
                    except KeyboardInterrupt:
                        break
                    except Exception as e:
                        print(f"Error processing tick: {e}")
                        
        except websockets.exceptions.InvalidStatus as e:
            print(f"\n❌ WebSocket Error: {e}")
            print("Binance may be blocking connections. Try again in a moment.")
        except Exception as e:
            print(f"\n❌ Connection Error: {e}")
        
        await self.print_final()
    
    async def print_status(self, price):
        """Print current status"""
        s = self.strategy.get_status()
        elapsed = time.time() - self.last_status_time
        tps = 100 / elapsed if elapsed > 0 else 0
        runtime = time.time() - self.start_time
        
        print(f"\n📊 Tick #{self.tick_count:,} | {datetime.now().strftime('%H:%M:%S')} | ${price:,.2f}")
        print(f"💰 ${s['current_capital']:,.2f} → ${s['total_capital']:,.2f} ({s['total_return_pct']:+.2f}%)")
        print(f"⏱️  Runtime: {runtime/60:.1f} min | Speed: {tps:.1f} ticks/sec")
        
        if s['total_trades'] > 0:
            new_trades = s['total_trades'] - self.last_trade_count
            if new_trades > 0:
                print(f"🎯 {s['total_trades']} trades | {s['winning_trades']} wins | {s['win_rate_pct']:.1f}% WR | +{new_trades} new")
            else:
                print(f"🎯 {s['total_trades']} trades | {s['winning_trades']} wins | {s['win_rate_pct']:.1f}% WR")
            self.last_trade_count = s['total_trades']
        else:
            warmup_needed = max(0, 250 - len(self.strategy.price_history))
            if warmup_needed > 0:
                print(f"🎯 Warming up... ({len(self.strategy.price_history)}/250 ticks)")
            else:
                print(f"🎯 Ready! Waiting for high-confidence signal (70%+)")
        
        self.last_status_time = time.time()
    
    async def handle_signal(self, signal, price):
        """Handle trading signals"""
        action = signal.get('action')
        
        if action in ['long', 'short']:
            conf = signal.get('confidence', 0)
            
            print(f"\n{'='*75}")
            print(f"🎯 TRADE: OPEN {action.upper()} @ ${price:,.2f}")
            print(f"   📊 Confidence: {conf:.1%}")
            print(f"   💎 Using POST-ONLY orders = Maker Rebate (-0.011%)")
            print(f"   💰 Position: ${self.strategy.current_capital * 0.18 * 50:,.2f} notional")
            print(f"   📈 Market Bias: {signal.get('market_bias', 'N/A')}")
            print(f"   ⏰ Time: {datetime.now().strftime('%H:%M:%S')}")
            print(f"{'='*75}")
            
        elif action == 'close':
            pnl = signal.get('net_pnl', 0)
            reason = signal.get('reason', 'unknown')
            hold_time = signal.get('hold_time', 0)
            
            emoji = "✅" if pnl > 0 else "❌"
            print(f"\n{'='*75}")
            print(f"{emoji} CLOSE POSITION @ ${price:,.2f}")
            print(f"   📊 Reason: {reason}")
            print(f"   💵 PnL: ${pnl:+.2f}")
            print(f"   ⏱️  Hold Time: {hold_time:.0f}s")
            print(f"   📈 New Capital: ${self.strategy.current_capital:,.2f}")
            print(f"   ⏰ Time: {datetime.now().strftime('%H:%M:%S')}")
            print(f"{'='*75}")
    
    async def print_final(self):
        """Final stats"""
        s = self.strategy.get_status()
        runtime = time.time() - self.start_time
        
        print("\n" + "=" * 80)
        print("🏁 SESSION COMPLETE")
        print("=" * 80)
        print(f"📊 Total Ticks: {self.tick_count:,}")
        print(f"⏱️  Runtime: {runtime/60:.1f} minutes")
        print(f"💼 Total Trades: {s['total_trades']}")
        
        if s['total_trades'] > 0:
            print(f"✅ Wins: {s['winning_trades']}")
            print(f"📈 Win Rate: {s['win_rate_pct']:.1f}%")
            print(f"📊 Trades/Hour: {s['total_trades'] / (runtime/3600):.1f}")
        
        print(f"\n💰 Starting: $800.00")
        print(f"💰 Ending: ${s['current_capital']:,.2f}")
        print(f"💎 Reserves: ${s['reserves']:,.2f}")
        print(f"📈 Total: ${s['total_capital']:,.2f}")
        print(f"📊 Return: {s['total_return_pct']:+.2f}%")
        print("=" * 80)
        
        # Assessment
        if s['total_trades'] >= 5:
            if s['win_rate_pct'] >= 90:
                print("\n✅ EXCELLENT! Strategy performing as expected (90%+ WR)")
            elif s['win_rate_pct'] >= 70:
                print("\n⚠️  GOOD but below target. Continue monitoring.")
            else:
                print("\n❌ Below expectations. Review market conditions.")

async def main():
    print("\n🔥 LIVE BTC STRATEGY TEST")
    print("Using proven 50x + maker rebates configuration")
    print("Press Ctrl+C to stop\n")
    
    trader = LiveBTCTest()
    await trader.run()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n⚠️ Stopped by user")
