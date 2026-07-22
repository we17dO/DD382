import unittest
from dataclasses import replace

from xauusd_alert import (
    Bar,
    Monitor,
    Settings,
    detect_signals,
    find_completed_waves,
    find_pivots,
    maximum_internal_move,
)


BASE_TIME = 1_700_000_000


def bar(index, high, low):
    return Bar(BASE_TIME + index * 300, low, high, low, (high + low) / 2)


def valid_long_bars():
    # Pivot Low=index 2, Pivot High=index 6; indices 7-8 confirm; 9-16 consolidate.
    values = [
        (102, 100),
        (101, 99),
        (100, 90),
        (104, 94),
        (108, 98),
        (112, 102),
        (120, 110),
        (116, 111),
        (115, 110),
    ]
    values += [(118, 109)] * 8
    return [bar(index, high, low) for index, (high, low) in enumerate(values)]


def valid_short_bars():
    # Pivot High=index 2, Pivot Low=index 6; indices 7-8 confirm; 9-16 consolidate.
    values = [
        (110, 100),
        (115, 105),
        (120, 110),
        (116, 106),
        (112, 102),
        (108, 98),
        (100, 90),
        (101, 94),
        (102, 95),
    ]
    values += [(101, 91)] * 8
    return [bar(index, high, low) for index, (high, low) in enumerate(values)]


class StrategyTests(unittest.TestCase):
    def test_strict_pivot_n2(self):
        bars = valid_long_bars()
        self.assertIn((2, -1), find_pivots(bars))
        self.assertIn((6, 1), find_pivots(bars))
        equal_low = list(bars)
        equal_low[1] = bar(1, 101, 90)
        self.assertNotIn((2, -1), find_pivots(equal_low))

    def test_valid_long_after_confirmation_plus_eight_bars(self):
        bars = valid_long_bars()
        signals = detect_signals(bars, 118, 0, set())
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].direction, "多头")
        self.assertAlmostEqual(signals[0].take_profit, 138.54)
        self.assertEqual(signals[0].wave_end, bars[8].time + 300)
        self.assertEqual(signals[0].consolidation_start, bars[9].time)
        self.assertEqual(signals[0].consolidation_end, bars[16].time + 300)

    def test_no_signal_until_eighth_consolidation_bar_closes(self):
        self.assertEqual(detect_signals(valid_long_bars()[:-1], 118, 0, set()), [])

    def test_long_invalid_if_any_consolidation_low_below_fib(self):
        bars = valid_long_bars()
        bars[12] = bar(12, 118, 108)
        self.assertEqual(detect_signals(bars, 118, 0, set()), [])

    def test_valid_short_and_duplicate_filter(self):
        bars = valid_short_bars()
        signals = detect_signals(bars, 95, 0, set())
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].direction, "空头")
        self.assertAlmostEqual(signals[0].take_profit, 71.46)
        self.assertLess(signals[0].take_profit, signals[0].wave_min)
        self.assertEqual(detect_signals(bars, 95, 0, {signals[0].signal_id}), [])

    def test_m15_and_h1_use_their_own_bar_spacing(self):
        source = valid_long_bars()
        for timeframe, seconds in (("M15", 900), ("H1", 3600)):
            bars = [replace(item, time=BASE_TIME + index * seconds) for index, item in enumerate(source)]
            signals = detect_signals(bars, 118, 0, set(), timeframe=timeframe)
            self.assertEqual(len(signals), 1)
            self.assertEqual(signals[0].timeframe, timeframe)
            self.assertIn(f"XAUUSD:{timeframe}:", signals[0].signal_id)
            self.assertEqual(signals[0].consolidation_end, bars[16].time + seconds)

    def test_k_and_r_filters(self):
        bars = valid_long_bars()
        self.assertEqual(list(find_completed_waves(bars, min_wave_bars=6)), [])
        self.assertEqual(list(find_completed_waves(bars, min_wave_ratio=0.5)), [])

    def test_internal_retracement_filter(self):
        bars = valid_long_bars()
        # Keeps endpoints adjacent pivots but creates 16 points of pullback after high=116.
        bars[4] = bar(4, 116, 91)
        bars[5] = bar(5, 112, 100)
        wave = bars[2:7]
        self.assertGreater(maximum_internal_move(wave, 1), 15)
        self.assertEqual(list(find_completed_waves(bars)), [])

    def test_monitor_starts_with_zero_server_offset(self):
        monitor = Monitor(lambda: Settings(), lambda _message: None)
        self.assertEqual(monitor.server_offset_seconds, 0)


if __name__ == "__main__":
    unittest.main()
