"""
Microstructure Alpha & Order Book Imbalance (OBI) Engine
=========================================================
Implements Domain 1 microstructure alpha signals:
1. VPINCalculator: Volume-Synchronized Probability of Toxicity
2. TickRunLengthDetector: Consecutive aggressive tick momentum tracker
3. OrderFlowImbalance: Rolling 1s / 5s buyer-maker vs seller-maker flow ratio
"""

import math
from typing import List, Dict, Any, Optional, Tuple
from collections import deque


class VPINCalculator:
    """
    Volume-Synchronized Probability of Toxicity (VPIN)
    Calculates toxic flow imbalance across constant volume buckets:
    VPIN = sum(|V_buy - V_sell|) / (N * V_bucket)
    """

    def __init__(self, bucket_volume: float = 50.0, num_buckets: int = 20):
        self.bucket_volume = bucket_volume
        self.num_buckets = num_buckets
        self.buckets = deque(maxlen=num_buckets)
        
        # Current accumulating bucket
        self.curr_buy_vol = 0.0
        self.curr_sell_vol = 0.0

    def update(self, price: float, qty: float, is_buyer_maker: bool) -> float:
        """
        Ingests a trade tick and returns the current rolling VPIN [0.0, 1.0].
        If is_buyer_maker is True -> Seller is aggressive taker -> Sell volume.
        If is_buyer_maker is False -> Buyer is aggressive taker -> Buy volume.
        """
        remaining_qty = qty
        is_buy = not is_buyer_maker

        while remaining_qty > 0:
            curr_total = self.curr_buy_vol + self.curr_sell_vol
            space = self.bucket_volume - curr_total

            if remaining_qty <= space:
                if is_buy:
                    self.curr_buy_vol += remaining_qty
                else:
                    self.curr_sell_vol += remaining_qty
                remaining_qty = 0.0
            else:
                # Fill current bucket to completion
                if is_buy:
                    self.curr_buy_vol += space
                else:
                    self.curr_sell_vol += space
                remaining_qty -= space
                
                # Push completed bucket imbalance
                imbalance = abs(self.curr_buy_vol - self.curr_sell_vol)
                self.buckets.append(imbalance)
                
                # Reset for next bucket
                self.curr_buy_vol = 0.0
                self.curr_sell_vol = 0.0

        if not self.buckets:
            return 0.5

        return sum(self.buckets) / (len(self.buckets) * self.bucket_volume)


class TickRunLengthDetector:
    """
    Tracks trade tick run length (consecutive aggressive buys vs sells).
    Triggers micro-momentum continuation or exhaustion fade signals.
    """

    def __init__(self, run_threshold: int = 4):
        self.run_threshold = run_threshold
        self.current_direction: int = 0  # +1 = buy run, -1 = sell run, 0 = neutral
        self.current_run_length: int = 0
        self.last_price: Optional[float] = None

    def update(self, price: float, is_buyer_maker: bool) -> Tuple[int, int]:
        """
        Updates run length.
        Returns: (current_direction, current_run_length)
        """
        trade_dir = -1 if is_buyer_maker else +1

        if trade_dir == self.current_direction:
            self.current_run_length += 1
        else:
            self.current_direction = trade_dir
            self.current_run_length = 1

        self.last_price = price
        return self.current_direction, self.current_run_length

    def is_momentum_breakout(self) -> bool:
        """Returns True if current run length exceeds continuation threshold."""
        return self.current_run_length >= self.run_threshold

    def is_exhaustion_candidate(self, max_run: int = 8) -> bool:
        """Returns True if run length is extremely extended (exhaustion fade candidate)."""
        return self.current_run_length >= max_run


class OrderFlowImbalance:
    """
    Rolling time-window order flow imbalance (OFI):
    OFI = (V_buy - V_sell) / (V_buy + V_sell)
    Ranges from -1.0 (pure aggressive selling) to +1.0 (pure aggressive buying).
    """

    def __init__(self, window_seconds: float = 5.0):
        self.window_seconds = window_seconds
        self.trades = deque()  # stores (timestamp_sec, buy_vol, sell_vol)

    def update(self, timestamp_sec: float, qty: float, is_buyer_maker: bool) -> float:
        """Adds a trade tick, purges expired ticks, and calculates current OFI."""
        buy_vol = 0.0 if is_buyer_maker else qty
        sell_vol = qty if is_buyer_maker else 0.0
        
        self.trades.append((timestamp_sec, buy_vol, sell_vol))

        cutoff = timestamp_sec - self.window_seconds
        while self.trades and self.trades[0][0] < cutoff:
            self.trades.popleft()

        total_buy = sum(t[1] for t in self.trades)
        total_sell = sum(t[2] for t in self.trades)
        total_vol = total_buy + total_sell

        if total_vol <= 1e-9:
            return 0.0

        return (total_buy - total_sell) / total_vol
