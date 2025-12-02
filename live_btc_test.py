#!/usr/bin/env python3
"""
Live BTC Test - Using HTTP polling (more reliable)
"""
import asyncio
import requests
import time
from datetime import datetime
from hybrid_heatmap_no_rebates import HybridHeatmapNoRebates

class LiveBTCHTTP:
    def __init__(self):
        self.strategy = HybridHeatmapNoRebates(initial_capital=800.0)
        
        # dYdX 50x configuration
        self.strategy.leverage = 50
        self.strategy.trading_fee_rate = -0.00011
        self.strategy.position_size_pct = 0.18
        
        print("=" * 80)
        print("🚀 LIVE BTC TEST - 50X STRATEGY (HTTP)")
        print("=" * 80)
        print(f"💰 Starting Capital: $800")
        print(f"⚡ Leverage: 50x")
        print(f"💎 Maker Rebate: 0.011%")
        print("=" * 80)
        print()
        
        self.tick_count = 0
        self.last_status_time = time.time()
        self.last_trade_count = 0
        self.start_time = time.time()
        
    async def run(self, duration_minutes=15):
        print(f"📡 Fetching live BTC prices from Binance...")
        print(f"⏰ Started at: {datetime.now().strftime('%H:%M:%S')}")
        print(f"⏱️  Will run for {duration_minutes} minutes")
        print()
        
        end_time = time.time() + (duration_minutes * 60)
        
        try:
            while time.time() < end_time:
                try:
                    # Get current BTC price
                    response = requests.get(
                        'https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT',
                        timeout=5
                    )
                    
                    if response.status_code == 200:
                        data = response.json()
                        price = float(data['price'])
                        timestamp = time.time()
                        
                        self.tick_count += 1
                        
                        # Feed to strategy (use price as volume proxy)
                        signal = self.strategy.add_tick(price, timestamp, volume=1.0)
                        
                        # Print status every 10 ticks
                        if self.tick_count % 10 == 0:
                            await self.print_status(price)
                        
                        # Handle trade signals
                        if signal:
                            await self.handle_signal(signal, price)
                    
                    # Poll every 0.5 seconds
                    await asyncio.sleep(0.5)
                    
                except KeyboardInterrupt:
                    break
                except Exception as e:
                    print(f"Error: {e}")
                    await asyncio.sleep(1)
                    
        except Exception as e:
            print(f"\n❌ Error: {e}")
        
        await self.print_final()
    
    async def print_status(self, price):
        s = self.strategy.get_status()
        runtime = time.time() - self.start_time
        
        print(f"\n📊 Tick #{self.tick_count:,} | {datetime.now().strftime('%H:%M:%S')} | ${price:,.2f}")
        print(f"💰 ${s['current_capital']:,.2f} → ${s['total_capital']:,.2f} ({s['total_return_pct']:+.2f}%)")
        print(f"⏱️  Runtime: {runtime/60:.1f} min")
        
        if s['total_trades'] > 0:
            print(f"🎯 {s['total_trades']} trades | {s['win_rate_pct']:.1f}% WR")
        else:
            warmup = max(0, 250 - len(self.strategy.price_history))
            if warmup > 0:
                print(f"🎯 Warming up... ({len(self.strategy.price_history)}/250)")
            else:
                print(f"🎯 Ready! Waiting for signal...")
    
    async def handle_signal(self, signal, price):
        action = signal.get('action')
        
        if action in ['long', 'short']:
            conf = signal.get('confidence', 0)
            print(f"\n{'='*75}")
            print(f"🎯 OPEN {action.upper()} @ ${price:,.2f} | Confidence: {conf:.1%}")
            print(f"{'='*75}")
            
        elif action == 'close':
            pnl = signal.get('net_pnl', 0)
            emoji = "✅" if pnl > 0 else "❌"
            print(f"\n{'='*75}")
            print(f"{emoji} CLOSE @ ${price:,.2f} | PnL: ${pnl:+.2f}")
            print(f"{'='*75}")
    
    async def print_final(self):
        s = self.strategy.get_status()
        runtime = time.time() - self.start_time
        
        print("\n" + "=" * 80)
        print("🏁 SESSION COMPLETE")
        print("=" * 80)
        print(f"📊 Ticks: {self.tick_count:,} | Runtime: {runtime/60:.1f} min")
        print(f"💼 Trades: {s['total_trades']} | Win Rate: {s['win_rate_pct']:.1f}%")
        print(f"💰 $800 → ${s['total_capital']:,.2f} ({s['total_return_pct']:+.2f}%)")
        print("=" * 80)

async def main():
    print("\n🔥 LIVE BTC STRATEGY TEST (HTTP)")
    print("Press Ctrl+C to stop early\n")
    
    trader = LiveBTCHTTP()
    await trader.run(duration_minutes=15)  # Run for 15 minutes

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n⚠️ Stopped by user")
