"""
Performance Metrics & Statistical Analysis
===========================================
Calculates institutional-grade backtesting performance metrics:
Win Rate, Profit Factor, Sharpe/Sortino/Calmar Ratios, Max Drawdown,
Dual-Currency Net Returns, Consecutive Streaks, and Exit Breakdown.
"""

import math
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

from kcex.engine.models import TradeOutcome, OrderDirection, ExitReason


@dataclass
class PerformanceSummary:
    symbol: str
    total_trades: int
    winning_trades: int
    losing_trades: int
    scratch_trades: int
    win_rate_pct: float

    # Financial returns in USDT
    initial_balance_usdt: float
    final_balance_usdt: float
    net_pnl_usdt: float
    net_roi_pct: float
    gross_profit_usdt: float
    gross_loss_usdt: float
    profit_factor: float
    total_fees_usdt: float

    # Financial returns in INR
    initial_balance_inr: float
    final_balance_inr: float
    net_pnl_inr: float
    gross_profit_inr: float
    gross_loss_inr: float
    total_fees_inr: float

    # Risk & Drawdown
    max_drawdown_usdt: float
    max_drawdown_pct: float
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float

    # Trade Stats
    avg_trade_pnl_usdt: float
    avg_win_pnl_usdt: float
    avg_loss_pnl_usdt: float
    win_loss_ratio: float
    avg_duration_seconds: float
    max_consecutive_wins: int
    max_consecutive_losses: int

    # Directional Breakdown
    long_trades: int
    long_wins: int
    long_win_rate_pct: float
    short_trades: int
    short_wins: int
    short_win_rate_pct: float

    # Exit Breakdown
    exit_reasons: Dict[str, int] = field(default_factory=dict)


class PerformanceCalculator:
    """
    Computes performance statistics from a list of TradeOutcome objects.
    """

    @staticmethod
    def calculate(
        outcomes: List[TradeOutcome],
        initial_balance_usdt: float = 100.0,
        inr_rate: float = 94.45
    ) -> PerformanceSummary:
        if not outcomes:
            return PerformanceSummary(
                symbol="",
                total_trades=0,
                winning_trades=0,
                losing_trades=0,
                scratch_trades=0,
                win_rate_pct=0.0,
                initial_balance_usdt=initial_balance_usdt,
                final_balance_usdt=initial_balance_usdt,
                net_pnl_usdt=0.0,
                net_roi_pct=0.0,
                gross_profit_usdt=0.0,
                gross_loss_usdt=0.0,
                profit_factor=0.0,
                total_fees_usdt=0.0,
                initial_balance_inr=initial_balance_usdt * inr_rate,
                final_balance_inr=initial_balance_usdt * inr_rate,
                net_pnl_inr=0.0,
                gross_profit_inr=0.0,
                gross_loss_inr=0.0,
                total_fees_inr=0.0,
                max_drawdown_usdt=0.0,
                max_drawdown_pct=0.0,
                sharpe_ratio=0.0,
                sortino_ratio=0.0,
                calmar_ratio=0.0,
                avg_trade_pnl_usdt=0.0,
                avg_win_pnl_usdt=0.0,
                avg_loss_pnl_usdt=0.0,
                win_loss_ratio=0.0,
                avg_duration_seconds=0.0,
                max_consecutive_wins=0,
                max_consecutive_losses=0,
                long_trades=0,
                long_wins=0,
                long_win_rate_pct=0.0,
                short_trades=0,
                short_wins=0,
                short_win_rate_pct=0.0,
                exit_reasons={}
            )

        symbol = outcomes[0].symbol
        total_trades = len(outcomes)
        wins = [o for o in outcomes if o.realized_pnl_usdt > 1e-8]
        losses = [o for o in outcomes if o.realized_pnl_usdt < -1e-8]
        scratches = [o for o in outcomes if abs(o.realized_pnl_usdt) <= 1e-8]

        winning_trades = len(wins)
        losing_trades = len(losses)
        scratch_trades = len(scratches)
        win_rate = (winning_trades / total_trades * 100.0) if total_trades > 0 else 0.0

        gross_profit = sum(o.realized_pnl_usdt for o in wins)
        gross_loss = abs(sum(o.realized_pnl_usdt for o in losses))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)

        total_fees_usdt = sum(o.fee_total_usdt for o in outcomes)
        net_pnl_usdt = sum(o.realized_pnl_usdt for o in outcomes)
        final_balance_usdt = initial_balance_usdt + net_pnl_usdt
        net_roi_pct = (net_pnl_usdt / initial_balance_usdt * 100.0) if initial_balance_usdt > 0 else 0.0

        # INR translations
        initial_balance_inr = initial_balance_usdt * inr_rate
        final_balance_inr = final_balance_usdt * inr_rate
        net_pnl_inr = net_pnl_usdt * inr_rate
        gross_profit_inr = gross_profit * inr_rate
        gross_loss_inr = gross_loss * inr_rate
        total_fees_inr = total_fees_usdt * inr_rate

        # Averages
        avg_trade_pnl = net_pnl_usdt / total_trades if total_trades > 0 else 0.0
        avg_win_pnl = (gross_profit / winning_trades) if winning_trades > 0 else 0.0
        avg_loss_pnl = (gross_loss / losing_trades) if losing_trades > 0 else 0.0
        win_loss_ratio = (avg_win_pnl / avg_loss_pnl) if avg_loss_pnl > 0 else 0.0
        avg_duration = sum(o.duration_seconds for o in outcomes) / total_trades if total_trades > 0 else 0.0

        # Drawdown calculation
        peak_balance = initial_balance_usdt
        current_balance = initial_balance_usdt
        max_dd_usdt = 0.0
        max_dd_pct = 0.0

        equity_points = [initial_balance_usdt]
        pnl_returns = []

        for o in outcomes:
            current_balance += o.realized_pnl_usdt
            equity_points.append(current_balance)
            ret = o.realized_pnl_usdt / (current_balance - o.realized_pnl_usdt) if (current_balance - o.realized_pnl_usdt) > 0 else 0.0
            pnl_returns.append(ret)

            if current_balance > peak_balance:
                peak_balance = current_balance
            dd_usdt = peak_balance - current_balance
            dd_pct = (dd_usdt / peak_balance * 100.0) if peak_balance > 0 else 0.0
            if dd_usdt > max_dd_usdt:
                max_dd_usdt = dd_usdt
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct

        # Sharpe & Sortino (per-trade basis)
        sharpe = 0.0
        sortino = 0.0
        if len(pnl_returns) > 1:
            mean_ret = sum(pnl_returns) / len(pnl_returns)
            var_ret = sum((r - mean_ret) ** 2 for r in pnl_returns) / (len(pnl_returns) - 1)
            std_ret = math.sqrt(var_ret) if var_ret > 0 else 0.0
            if std_ret > 0:
                sharpe = (mean_ret / std_ret) * math.sqrt(252 * 24) # Annualized estimate

            downside = [r for r in pnl_returns if r < 0]
            if downside:
                downside_var = sum(r ** 2 for r in downside) / len(downside)
                downside_std = math.sqrt(downside_var) if downside_var > 0 else 0.0
                if downside_std > 0:
                    sortino = (mean_ret / downside_std) * math.sqrt(252 * 24)

        calmar = (net_roi_pct / max_dd_pct) if max_dd_pct > 0 else 0.0

        # Streaks
        cur_w_streak = 0
        max_w_streak = 0
        cur_l_streak = 0
        max_l_streak = 0
        for o in outcomes:
            if o.realized_pnl_usdt > 1e-8:
                cur_w_streak += 1
                cur_l_streak = 0
                if cur_w_streak > max_w_streak:
                    max_w_streak = cur_w_streak
            elif o.realized_pnl_usdt < -1e-8:
                cur_l_streak += 1
                cur_w_streak = 0
                if cur_l_streak > max_l_streak:
                    max_l_streak = cur_l_streak
            else:
                cur_w_streak = 0
                cur_l_streak = 0

        # Directional breakdown
        longs = [o for o in outcomes if o.direction == OrderDirection.LONG]
        shorts = [o for o in outcomes if o.direction == OrderDirection.SHORT]
        long_wins = len([o for o in longs if o.realized_pnl_usdt > 1e-8])
        short_wins = len([o for o in shorts if o.realized_pnl_usdt > 1e-8])
        long_wr = (long_wins / len(longs) * 100.0) if longs else 0.0
        short_wr = (short_wins / len(shorts) * 100.0) if shorts else 0.0

        # Exit reasons breakdown
        exit_reasons: Dict[str, int] = {}
        for o in outcomes:
            r_str = str(o.exit_reason.value if hasattr(o.exit_reason, "value") else o.exit_reason)
            exit_reasons[r_str] = exit_reasons.get(r_str, 0) + 1

        return PerformanceSummary(
            symbol=symbol,
            total_trades=total_trades,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            scratch_trades=scratch_trades,
            win_rate_pct=win_rate,
            initial_balance_usdt=initial_balance_usdt,
            final_balance_usdt=final_balance_usdt,
            net_pnl_usdt=net_pnl_usdt,
            net_roi_pct=net_roi_pct,
            gross_profit_usdt=gross_profit,
            gross_loss_usdt=gross_loss,
            profit_factor=profit_factor,
            total_fees_usdt=total_fees_usdt,
            initial_balance_inr=initial_balance_inr,
            final_balance_inr=final_balance_inr,
            net_pnl_inr=net_pnl_inr,
            gross_profit_inr=gross_profit_inr,
            gross_loss_inr=gross_loss_inr,
            total_fees_inr=total_fees_inr,
            max_drawdown_usdt=max_dd_usdt,
            max_drawdown_pct=max_dd_pct,
            sharpe_ratio=sharpe,
            sortino_ratio=sortino,
            calmar_ratio=calmar,
            avg_trade_pnl_usdt=avg_trade_pnl,
            avg_win_pnl_usdt=avg_win_pnl,
            avg_loss_pnl_usdt=avg_loss_pnl,
            win_loss_ratio=win_loss_ratio,
            avg_duration_seconds=avg_duration,
            max_consecutive_wins=max_w_streak,
            max_consecutive_losses=max_l_streak,
            long_trades=len(longs),
            long_wins=long_wins,
            long_win_rate_pct=long_wr,
            short_trades=len(shorts),
            short_wins=short_wins,
            short_win_rate_pct=short_wr,
            exit_reasons=exit_reasons
        )
