"""
KCEX Dual-Currency & Trade Outcome Loggers
==========================================
Provides real-time beautifully formatted logging with dual-currency (USDT & INR)
reporting, plus a dedicated trade outcome journal that records detailed execution
cards and cumulative performance statistics.
"""

import os
import sys
import time
import json
import logging
from datetime import datetime
from typing import Optional, Any
from kcex.engine.models import TradeOutcome, CumulativeStats


ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


class DualCurrencyLogger:
    """
    Real-time logger that outputs to both console and a realtime log file.
    Includes built-in dual-currency conversion helpers (USDT <-> INR).
    """

    def __init__(
        self,
        log_file: str = "logs/engine_realtime.log",
        inr_rate: float = 94.45,
        log_level: int = logging.INFO
    ):
        if not os.path.isabs(log_file):
            log_file = os.path.join(ROOT_DIR, log_file)
        self.log_file = log_file
        self.inr_rate = inr_rate
        self._ensure_dir()

        # Set up standard logger
        self.logger = logging.getLogger("KCEXEngine")
        self.logger.setLevel(log_level)
        self.logger.propagate = False

        # Clear existing handlers if re-initialized
        if self.logger.hasHandlers():
            self.logger.handlers.clear()

        # Console Handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(log_level)
        console_fmt = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        console_handler.setFormatter(console_fmt)
        self.logger.addHandler(console_handler)

        # File Handler (UTF-8, immediate flush)
        file_handler = logging.FileHandler(self.log_file, encoding="utf-8", mode="a")
        file_handler.setLevel(log_level)
        file_fmt = logging.Formatter(
            fmt="%(asctime)s [%(levelname)-7s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        file_handler.setFormatter(file_fmt)
        self.logger.addHandler(file_handler)

        # Single-line dynamic telemetry & price deduplication state
        self._in_place_active = False
        self._has_active_positions: bool = False
        self._last_status_msg: Optional[str] = None
        self._last_status_price: Optional[float] = None
        self._last_status_time: float = 0.0
        self._same_price_count: int = 0
        self._is_tty: bool = sys.stdout.isatty()
        self._is_cloud_ci: bool = bool(
            os.environ.get("RAILWAY_ENVIRONMENT") or
            os.environ.get("RAILWAY_ENVIRONMENT_NAME") or
            os.environ.get("RAILWAY_SERVICE_NAME") or
            os.environ.get("RAILWAY_PROJECT_ID") or
            os.environ.get("RAILWAY_DEPLOYMENT_ID") or
            os.environ.get("RAILWAY_STATIC_URL") or
            os.environ.get("GITHUB_ACTIONS") or
            os.environ.get("CI") or
            not sys.stdout.isatty()
        )

    def set_has_active_positions(self, has_active: bool) -> None:
        """Sets whether any position is currently open across any tracked pair."""
        self._has_active_positions = bool(has_active)

    def _ensure_dir(self) -> None:
        directory = os.path.dirname(self.log_file)
        if directory:
            os.makedirs(directory, exist_ok=True)

    def set_inr_rate(self, rate: float) -> None:
        """Updates the current exchange rate."""
        if rate > 0:
            self.inr_rate = rate

    def format_dual(self, usdt_val: float, precision: int = 4) -> str:
        """Formats a value in both USDT and INR."""
        inr_val = usdt_val * self.inr_rate
        sign = "+" if usdt_val > 0 else ""
        return f"{sign}{usdt_val:.{precision}f} USDT ({sign}INR {inr_val:.2f})"

    def format_price(self, price: float, precision: int = 4) -> str:
        """Formats a price in USDT and INR equivalent."""
        inr_val = price * self.inr_rate
        return f"{price:.{precision}f} USDT (INR {inr_val:.2f})"

    def clear_status_line(self) -> None:
        """Clears any in-place status line currently printed on console."""
        if self._in_place_active:
            try:
                sys.stdout.write("\n")
                sys.stdout.flush()
            except Exception:
                pass
            self._in_place_active = False

    def update_status_line(
        self,
        msg: str,
        price: Optional[float] = None,
        tag: str = "STATUS",
        force: bool = False,
        heartbeat_sec: float = 30.0
    ) -> bool:
        """
        Updates telemetry in a single line.
        - On local interactive TTY: Updates the current line in-place using carriage return (\\r).
        - On Cloud / CI (Railway, Docker without TTY):
          Throttles periodic status & price telemetry lines to prevent messy log spamming:
          * Every 10 minutes (600s) if NO position is active on any pair
          * Every 5 minutes (300s) if ANY position is open on any pair
          * Immediate logging when force=True
        - Background scanning and important trading events (signals, fills, TPs, SLs) continue logging immediately.
        """
        now = time.time()
        raw_price = price if price is not None else None
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Required interval: 5m (300s) if in position, 10m (600s) if idle
        required_interval = 300.0 if self._has_active_positions else 600.0

        # Cloud/CI environment (Railway, Docker, non-interactive)
        if self._is_cloud_ci or not self._is_tty:
            # If not forced and interval has not elapsed, suppress repetitive status lines
            if not force and (now - self._last_status_time < required_interval):
                return False

            self.clear_status_line()
            self.logger.info(msg)
            if raw_price is not None:
                self._last_status_price = raw_price
            self._last_status_time = now
            self._last_status_msg = msg
            self._write_file_log(now_str, "INFO", msg)
            return True

        # Interactive Local TTY: update in-place on the same line
        try:
            clean_msg = f"\r{now_str} [INFO] {msg}"
            sys.stdout.write(f"{clean_msg:<140}")
            sys.stdout.flush()
            self._in_place_active = True
        except Exception:
            self.logger.info(msg)

        # Record to log file conditionally at heartbeat interval (5m in trade, 10m idle)
        if (now - self._last_status_time >= required_interval) or force:
            self._write_file_log(now_str, "INFO", msg)
            self._last_status_time = now
            if raw_price is not None:
                self._last_status_price = raw_price

        self._last_status_msg = msg
        return True

    def _write_file_log(self, timestamp_str: str, level: str, msg: str) -> None:
        """Helper to append a clean formatted line directly to log file."""
        try:
            line = f"{timestamp_str} [{level:<7s}] {msg}\n"
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self.clear_status_line()
        self.logger.info(msg, *args, **kwargs)

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self.clear_status_line()
        self.logger.warning(msg, *args, **kwargs)

    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self.clear_status_line()
        self.logger.error(msg, *args, **kwargs)

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self.logger.debug(msg, *args, **kwargs)

    def exception(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self.clear_status_line()
        self.logger.exception(msg, *args, **kwargs)

    def section(self, title: str) -> None:
        self.clear_status_line()
        border = "=" * 76
        self.logger.info(border)
        self.logger.info(f"   {title}")
        self.logger.info(border)


class TradeOutcomeLogger:
    """
    Dedicated logger that records every closed trade outcome to both
    a human-readable text file and a structured JSONL file.
    Tracks and displays running cumulative performance statistics.
    """

    def __init__(
        self,
        txt_file: str = "logs/trade_outcomes.txt",
        jsonl_file: str = "logs/trade_outcomes.jsonl"
    ):
        if not os.path.isabs(txt_file):
            txt_file = os.path.join(ROOT_DIR, txt_file)
        if not os.path.isabs(jsonl_file):
            jsonl_file = os.path.join(ROOT_DIR, jsonl_file)
        self.txt_file = txt_file
        self.jsonl_file = jsonl_file
        self.cumulative = CumulativeStats()
        self._ensure_dir()

    def _ensure_dir(self) -> None:
        for f in (self.txt_file, self.jsonl_file):
            d = os.path.dirname(f)
            if d:
                os.makedirs(d, exist_ok=True)

    def log_outcome(self, outcome: TradeOutcome) -> str:
        """
        Appends trade outcome to files and returns formatted text card.
        """
        self.cumulative.update(outcome)
        timestamp_str = datetime.fromtimestamp(outcome.close_time).strftime("%Y-%m-%d %H:%M:%S")
        open_time_str = datetime.fromtimestamp(outcome.open_time).strftime("%Y-%m-%d %H:%M:%S")

        pnl_sign = "+" if outcome.realized_pnl_usdt > 0 else ""
        roe_sign = "+" if outcome.roe_percentage > 0 else ""

        mode_badge = "[🔴 LIVE TRADING]" if outcome.mode.value == "live" else "[🟢 SIMULATED / DRY-RUN]"

        tp_offset = abs(outcome.min_profit_tp_price - outcome.entry_price)
        tp_ticks = round(tp_offset / outcome.price_unit) if outcome.price_unit > 0 else 1
        sl_offset = abs(outcome.stop_loss_price - outcome.entry_price)

        sl_ticks = round(sl_offset / outcome.price_unit) if outcome.price_unit > 0 else 0
        sl_pct = (sl_offset / outcome.entry_price * 100.0) if outcome.entry_price > 0 else 0.0
        sl_roe = sl_pct * outcome.leverage

        ps = getattr(outcome, "price_precision", 4) or 4
        base_coin = getattr(outcome, "base_coin", "") or outcome.symbol.split("_")[0]
        fee_badge = "[Zero-Fee Pair]" if outcome.fee_total_usdt <= 1e-8 else f"[Trading Fees: {outcome.fee_total_usdt:.6f} USDT]"

        card_lines = [
            "=" * 78,
            f"TRADE #{outcome.trade_id} OUTCOME JOURNAL | Closed at: {timestamp_str} | {mode_badge}",
            "=" * 78,
            f"Execution Mode     : {mode_badge}",
            f"Symbol & Direction : {outcome.symbol} [{outcome.direction.value}] ({outcome.leverage}x isolated)",
            f"Strategy Name      : {outcome.sub_strategy_name}",
            f"Volume Executed    : {outcome.vol_contracts} contract(s) ({outcome.underlying_quantity:g} {base_coin})",
            f"Trade Quantity     : {outcome.notional_value_usdt:.4f} USDT (INR {outcome.notional_value_inr:.2f}) [Notional exposure]",
            f"Margin Committed   : {outcome.margin_used_usdt:.4f} USDT (INR {outcome.margin_used_inr:.2f}) [Trade Qty / {outcome.leverage}x leverage]",
            f"Entry Price        : {outcome.entry_price:.{ps}f} USDT (Opened: {open_time_str})",
            f"Exit Price         : {outcome.exit_price:.{ps}f} USDT (Duration: {outcome.duration_seconds:.2f}s)",
            f"Tick Size (pu)     : {outcome.price_unit:.{ps}f} USDT",
            f"Min-Profit TP Target: {outcome.min_profit_tp_price:.{ps}f} USDT (Offset: +{tp_ticks} pu / +{tp_offset:.{ps}f} USDT)",
            f"Stop Loss Level    : {outcome.stop_loss_price:.{ps}f} USDT (Offset: -{sl_ticks} pu / -{sl_offset:.{ps}f} USDT | -{sl_pct:.3f}% price | -{sl_roe:.1f}% ROE)",
            f"Exit Reason        : {outcome.exit_reason.value}",
        ]

        if getattr(outcome, "ml_confidence", None) is not None:
            p_b = getattr(outcome, "ml_prob_buy", 0.0) or 0.0
            p_s = getattr(outcome, "ml_prob_sell", 0.0) or 0.0
            p_w = getattr(outcome, "ml_prob_wait", 0.0) or 0.0
            card_lines.append(
                f"ML Model Alpha     : Conviction {outcome.ml_confidence:.1%} | P(BUY)={p_b:.1%} | P(SELL)={p_s:.1%} | P(WAIT)={p_w:.1%}"
            )

        if getattr(outcome, "smc_zone_id", None) or getattr(outcome, "smc_zone_type", None):
            zh = getattr(outcome, 'smc_zone_high', 0.0) or 0.0
            zl = getattr(outcome, 'smc_zone_low', 0.0) or 0.0
            zm = getattr(outcome, 'smc_zone_mid', None) or ((zh + zl) / 2.0 if (zh and zl) else 0.0)
            z_time = getattr(outcome, 'smc_zone_creation_time_utc', None) or "N/A"
            z_bar = getattr(outcome, 'smc_zone_creation_bar_idx', None)
            bar_desc = f"Bar #{z_bar} | {z_time}" if z_bar is not None else f"{z_time}"
            bos_desc = f" | BOS Bar #{outcome.smc_bos_bar_idx}" if getattr(outcome, 'smc_bos_bar_idx', None) is not None else ""
            t1 = getattr(outcome, 'smc_target_1to1', None)
            t2 = getattr(outcome, 'smc_target_1to2', None)
            t1_str = f"{t1:.{ps}f} USDT" if t1 else "N/A"
            t2_str = f"{t2:.{ps}f} USDT" if t2 else "N/A"
            partial_badge = " [1:1 Partial TP Hit]" if getattr(outcome, 'smc_partial_tp_hit', False) else ""

            card_lines.extend([
                f"SMC Identified OB  : {outcome.smc_zone_type or 'ORDER_BLOCK'} (#{outcome.smc_zone_id}){partial_badge}",
                f"  • OB Candle Loc  : {bar_desc}{bos_desc}",
                f"  • Zone Boundary  : Low: {zl:.{ps}f} <---> Mid: {zm:.{ps}f} <---> High: {zh:.{ps}f} USDT",
                f"  • Profit Targets : 1:1 TP: {t1_str} | 1:2 TP: {t2_str}"
            ])

        card_lines.extend([
            "------------------------------------------------------------------------------",
            f"REALIZED PnL       : {pnl_sign}{outcome.realized_pnl_usdt:.6f} USDT ({pnl_sign}INR {outcome.realized_pnl_inr:.4f})",
            f"Return on Equity   : {roe_sign}{outcome.roe_percentage:.2f}% (Price move: {pnl_sign}{outcome.pnl_percentage:.3f}%)",
            f"Trading Fees       : {outcome.fee_total_usdt:.6f} USDT (INR {outcome.fee_total_inr:.4f}) {fee_badge}",
        ])

        if outcome.balance_after_trade_usdt is not None:
            card_lines.append(
                f"ACCOUNT BALANCE    : {outcome.balance_after_trade_usdt:.4f} USDT (INR {outcome.balance_after_trade_inr:.2f})"
            )

        card_lines.extend([
            "------------------------------------------------------------------------------",
            f"CUMULATIVE STATS   : Total Trades: {self.cumulative.total_trades} | Wins: {self.cumulative.winning_trades} | Losses: {self.cumulative.losing_trades} | Scratch: {self.cumulative.scratch_trades}",
            f"Win Rate           : {self.cumulative.win_rate_pct:.1f}%",
            f"Net Cumulative PnL : {'+' if self.cumulative.total_pnl_usdt >= 0 else ''}{self.cumulative.total_pnl_usdt:.6f} USDT ({'+' if self.cumulative.total_pnl_inr >= 0 else ''}INR {self.cumulative.total_pnl_inr:.4f})",
            f"Live USD/INR Rate  : INR {outcome.inr_rate:.2f} per USD",
            "=" * 78,
            ""
        ])

        card_text = "\n".join(card_lines)

        # Write to human-readable TXT file
        try:
            with open(self.txt_file, "a", encoding="utf-8") as f:
                f.write(card_text)
        except Exception as e:
            logging.getLogger("KCEXEngine").error("Failed to write to %s: %s", self.txt_file, e)

        # Write to JSONL file
        try:
            json_record = {
                "trade_id": outcome.trade_id,
                "close_time": outcome.close_time,
                "timestamp_str": timestamp_str,
                "symbol": outcome.symbol,
                "base_coin": base_coin,
                "direction": outcome.direction.value,
                "sub_strategy": outcome.sub_strategy_name,
                "mode": outcome.mode.value,
                "vol_contracts": outcome.vol_contracts,
                "underlying_quantity": outcome.underlying_quantity,
                "entry_price": outcome.entry_price,
                "exit_price": outcome.exit_price,
                "min_profit_tp_price": outcome.min_profit_tp_price,
                "stop_loss_price": outcome.stop_loss_price,
                "price_unit": outcome.price_unit,
                "price_precision": ps,
                "duration_seconds": outcome.duration_seconds,
                "notional_value_usdt": outcome.notional_value_usdt,
                "notional_value_inr": outcome.notional_value_inr,
                "margin_used_usdt": outcome.margin_used_usdt,
                "margin_used_inr": outcome.margin_used_inr,
                "realized_pnl_usdt": outcome.realized_pnl_usdt,
                "realized_pnl_inr": outcome.realized_pnl_inr,
                "roe_percentage": outcome.roe_percentage,
                "pnl_percentage": outcome.pnl_percentage,
                "fee_total_usdt": outcome.fee_total_usdt,
                "fee_total_inr": outcome.fee_total_inr,
                "exit_reason": outcome.exit_reason.value,
                "balance_after_trade_usdt": outcome.balance_after_trade_usdt,
                "balance_after_trade_inr": outcome.balance_after_trade_inr,
                "inr_rate": outcome.inr_rate,
                "order_id": outcome.order_id,
                "position_id": outcome.position_id,
                "ml_confidence": getattr(outcome, "ml_confidence", None),
                "ml_prob_buy": getattr(outcome, "ml_prob_buy", None),
                "ml_prob_sell": getattr(outcome, "ml_prob_sell", None),
                "ml_prob_wait": getattr(outcome, "ml_prob_wait", None),
                "ml_tp_ticks": getattr(outcome, "ml_tp_ticks", None),
                "ml_sl_ticks": getattr(outcome, "ml_sl_ticks", None),
                "ml_atr_14": getattr(outcome, "ml_atr_14", None),
                "smc_zone_id": getattr(outcome, "smc_zone_id", None),
                "smc_zone_type": getattr(outcome, "smc_zone_type", None),
                "smc_zone_high": getattr(outcome, "smc_zone_high", None),
                "smc_zone_low": getattr(outcome, "smc_zone_low", None),
                "smc_zone_mid": getattr(outcome, "smc_zone_mid", None),
                "smc_zone_creation_bar_idx": getattr(outcome, "smc_zone_creation_bar_idx", None),
                "smc_zone_creation_time_utc": getattr(outcome, "smc_zone_creation_time_utc", None),
                "smc_bos_bar_idx": getattr(outcome, "smc_bos_bar_idx", None),
                "smc_bos_price": getattr(outcome, "smc_bos_price", None),
                "smc_target_1to1": getattr(outcome, "smc_target_1to1", None),
                "smc_target_1to2": getattr(outcome, "smc_target_1to2", None),
                "smc_partial_tp_hit": getattr(outcome, "smc_partial_tp_hit", None),
                "cumulative_trades": self.cumulative.total_trades,
                "cumulative_win_rate": self.cumulative.win_rate_pct,
                "cumulative_net_pnl_usdt": self.cumulative.total_pnl_usdt,
                "cumulative_net_pnl_inr": self.cumulative.total_pnl_inr
            }
            with open(self.jsonl_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(json_record) + "\n")
        except Exception as e:
            logging.getLogger("KCEXEngine").error("Failed to write to %s: %s", self.jsonl_file, e)

        return card_text
