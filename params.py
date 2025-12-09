#!/usr/bin/python3


market="bitcoin"

market_params={
    "bitcoin": {"ticker": "BTC_USD", "leverage": "50", "symbol":"BTC", "cgSymbol":"BTCUSD"}, 
    "doge": {"ticker": "DOGE_USD", "leverage": "10", "symbol": "DOGE", "cgSymbol": "DOGEUSD"}
trade_params={
    "name":market,
    "mnemonic":"still endorse use choose monkey equal jungle ketchup obscure put stumble eye minimum ritual follow neck rally coin funny sock broccoli bracket kite render",  
    "market": market_params[market]
    }
