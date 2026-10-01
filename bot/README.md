# Bybit testnet execution bot

Score is pnl_usd after fees on a flattened testnet fill. A run with no fill is a failure. Live stays off until the operator sets LIVE_TRADING_ACK. Do not write a mission, a slice, or a verdict.

Order path: TradingEngine.execute -> BybitClient.place_order. Flatten: TradingEngine.close_position. Spot flat means BTC balance is dust, not get_position.

Run from the repo root, in the Codespace that has the keys:

    BYBIT_VENUE=testnet python3 bot/tools/testnet_session.py

Exit 0 only if flat and cash_ledger.json exists. Exit 2 is zero fills. allows_live stays false.

To rerun the same session on a timer instead of by hand:

    bot/scripts/testnet_session_timer.sh [interval_seconds] [max_ticks]

It just loops the command above and sleeps between ticks (default 900s, runs forever unless max_ticks is set). No new scheduler, no state beyond what testnet_session.py already writes each tick.
