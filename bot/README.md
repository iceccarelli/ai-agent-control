# Bybit testnet execution bot

Score is pnl_usd after fees on a flattened testnet fill. A run with no fill is a failure. Live stays off until the operator sets LIVE_TRADING_ACK. Do not write a mission, a slice, or a verdict.

Order path: TradingEngine.execute -> BybitClient.place_order. Flatten: TradingEngine.close_position. Spot flat means BTC balance is dust, not get_position.

Run from the repo root, in the Codespace that has the keys:

    BYBIT_VENUE=testnet python3 bot/tools/testnet_session.py

Exit 0 only if flat and cash_ledger.json exists. Exit 2 is zero fills. allows_live stays false.
