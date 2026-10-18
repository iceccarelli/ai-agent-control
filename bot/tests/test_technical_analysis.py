

class TestTwoQuietFixes:
    def test_a_flat_window_does_not_cast_a_bollinger_vote(self):
        import technical_analysis as ta
        n = 60
        flat = tuple([1.0001] * n)
        candles = ta.Candles(open=flat, high=tuple(x * 1.0005 for x in flat),
                             low=tuple(x * 0.9995 for x in flat), close=flat,
                             volume=tuple([100.0] * n), timeframe="60")
        vote = ta.TechnicalAnalysis()._vote_bollinger(candles)
        assert vote.direction == 0 and vote.strength == 0.0

    def test_a_failed_fetch_clears_the_previous_symbols_bars(self):
        import technical_analysis as ta

        class Client:
            def get_klines(self, symbol, interval, limit):
                if symbol == "BAD":
                    raise RuntimeError("timeout")
                return [[str(1_600_000_000_000 + i * 3_600_000), "100", "101", "99",
                         "100", "10", "1000"] for i in range(150)][::-1]
        m = ta.MarketStrategy(Client())
        m.signal_for("GOOD")
        assert m.last_rows is not None
        m.signal_for("BAD")
        assert m.last_rows is None and m.last_candles is None
