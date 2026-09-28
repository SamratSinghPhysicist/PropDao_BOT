"""
1-Minute ML Trading Strategy Adapter for KCEX
==============================================
Bridges the Scikit-Learn HistGradientBoosting Alpha Engine (ML_1M_MODEL)
with the KCEX BaseStrategy interface.

Compatible with:
- Live Automated Trading (run_engine.py --mode live)
- Simulated Dry-Run Execution (run_engine.py --mode dry-run)
- High-Fidelity Backtesting (BACKTESTER/run_backtest.py)
- Semi-Autonomous Terminal Assistant (semi_auto_trader.py)
"""

import os
import sys
import time
import threading
import logging
import warnings
from typing import Optional, Dict, Any, List
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)
try:
    from sklearn.exceptions import InconsistentVersionWarning
    warnings.filterwarnings("ignore", category=InconsistentVersionWarning)
except ImportError:
    pass

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from strategies.base import BaseStrategy
from kcex.engine.models import OrderDirection, TradeSignal, TradeOutcome
from ML_1M_MODEL.config import MODELS_DIR, normalize_symbol_name, get_model_config, get_tick_spec
from ML_1M_MODEL.model import TradingModel
from ML_1M_MODEL.features import extract_features

logger = logging.getLogger("KCEXEngine")


class MLStrategy(BaseStrategy):
    """
    1-Minute Machine Learning Tactical Alpha Strategy.
    
    Generates high-conviction BUY, SELL, or WAIT signals using multi-horizon
    technicals, Carter squeeze expansion, and microstructure order-flow dynamics.
    Outputs volatility-calibrated dynamic Take-Profit and Stop-Loss boundaries.
    """

    def __init__(
        self,
        market: Any,
        symbol: str = "TRUMP_USDT",
        model_path: Optional[str] = None,
        timeframe: str = "1m",
        cooldown_seconds: float = 10.0,
        warmup_candles: int = 150,
        confidence_threshold: Optional[float] = None,
        confidence_threshold_sell: Optional[float] = None,
        edge_threshold: Optional[float] = None,
        preferred_direction: Optional[OrderDirection] = None,
        require_closed_candle: bool = True,
        interval: Optional[str] = None,
        auto_start_feed: bool = False,
        **kwargs: Any
    ):
        super().__init__(name="ML_1M_MODEL")
        self.market = market
        self.symbol = symbol
        self.clean_symbol = normalize_symbol_name(symbol)
        self.timeframe = interval or timeframe
        self.cooldown_seconds = cooldown_seconds
        self.warmup_candles = warmup_candles
        self.preferred_direction = preferred_direction
        self.require_closed_candle = require_closed_candle

        self.last_trade_time: float = 0.0
        self.trade_in_progress: bool = False
        self.last_prediction: Optional[Dict[str, Any]] = None

        # Kline cache and rate-limit backoff state
        self.kline_cache_interval: float = float(kwargs.get("kline_cache_interval", 2.5))
        self._cached_candles: List[Any] = []
        self._last_kline_fetch_ts: float = 0.0
        self._rate_limit_backoff_until: float = 0.0
        self.last_data_source: str = "INITIAL"

        # Resolve model path
        if model_path is None:
            model_path = os.path.join(MODELS_DIR, f"ml_1m_model_{self.clean_symbol.lower()}.pkl")
            if not os.path.exists(model_path):
                alt_path = os.path.join(MODELS_DIR, f"ml_1m_model_{self.symbol.lower()}.pkl")
                if os.path.exists(alt_path):
                    model_path = alt_path

        self.model_path = model_path
        self.model: Optional[TradingModel] = None
        self._load_model()

        # Apply runtime threshold overrides if provided
        if self.model and self.model.cfg:
            if confidence_threshold is not None:
                self.model.cfg.confidence_threshold = confidence_threshold
            if confidence_threshold_sell is not None:
                self.model.cfg.confidence_threshold_sell = confidence_threshold_sell
            if edge_threshold is not None:
                self.model.cfg.edge_threshold = edge_threshold

        # Real-time WebSocket Feed support (from Network_logs_by_codex)
        self._candles_lock = threading.Lock()
        self.candles: List[Dict[str, Any]] = []
        self.latest_deal_price: Optional[float] = None
        self.feed = None

        if auto_start_feed or getattr(self.market, "is_live", True):
            self._init_websocket_feed(auto_start=auto_start_feed)

        # Seed initial candles from REST once at startup
        self._seed_initial_candles()

    def _init_websocket_feed(self, auto_start: bool = False) -> None:
        """Initializes KCEX real-time WebSocket feed for live klines & deals."""
        try:
            from kcex.feed import KCEXWebSocketFeed
            self.feed = KCEXWebSocketFeed(
                symbol=self.symbol,
                kline_interval=self.timeframe or "Min1",
                on_kline=self._on_ws_kline,
                on_deal=self._on_ws_deal,
                subscribe_kline=True,
                subscribe_deals=True,
                subscribe_depth=False,
                subscribe_ticker=False
            )
            if auto_start:
                self.feed.start()
        except Exception as e:
            logger.warning("[MLStrategy] Could not initialize WebSocket feed: %s", e)

    def _seed_initial_candles(self) -> None:
        """Seeds in-memory candles buffer with historical klines from REST once at startup."""
        try:
            bars = self.market.get_klines(self.symbol, interval="Min1", limit=300)
            if bars and len(bars) >= 50:
                with self._candles_lock:
                    self.candles = list(bars)
                logger.info(f"[MLStrategy] Seeded initial {len(bars)} 1m candles for {self.symbol}.")
        except Exception as e:
            logger.warning(f"[MLStrategy] Failed to seed initial candles: {e}")

    def _on_ws_kline(self, k: Dict[str, Any]) -> None:
        """Processes real-time 1m candle updates from KCEX WebSocket push.kline stream."""
        with self._candles_lock:
            if not self.candles:
                self.candles.append(k)
                return
            last_candle = self.candles[-1]
            last_ts = int(last_candle.get("timestamp", 0))
            new_ts = int(k.get("timestamp", 0))

            if new_ts == last_ts:
                # Update currently forming 1-minute candle
                last_candle["high"] = max(float(last_candle.get("high", 0.0)), float(k.get("high", 0.0)))
                last_candle["low"] = min(float(last_candle.get("low", float("inf"))), float(k.get("low", 0.0)))
                last_candle["close"] = float(k.get("close", last_candle.get("close", 0.0)))
                last_candle["volume"] = float(k.get("volume", last_candle.get("volume", 0.0)))
                last_candle["amount"] = float(k.get("amount", last_candle.get("amount", 0.0)))
            elif new_ts > last_ts:
                # New 1-minute candle finalized!
                k_rec = dict(k)
                if "taker_buy_volume" not in k_rec:
                    k_rec["taker_buy_volume"] = k_rec["volume"] * 0.5
                self.candles.append(k_rec)
                if len(self.candles) > 350:
                    self.candles = self.candles[-300:]

    def _on_ws_deal(self, price: float, volume: float, side: str, ts: float) -> None:
        """Processes real-time deal ticks to update latest price without polling."""
        self.latest_deal_price = price
        with self._candles_lock:
            if not self.require_closed_candle and self.candles:
                self.candles[-1]["close"] = price

    def start(self) -> None:
        """Starts the real-time WebSocket feed listener thread."""
        if self.feed and not self.feed.is_connected:
            self.feed.start()

    def stop(self) -> None:
        """Stops the real-time WebSocket feed listener thread."""
        if self.feed:
            self.feed.stop()

    def _load_model(self) -> None:
        """Loads serialized model artifact from disk."""
        if os.path.exists(self.model_path):
            try:
                self.model = TradingModel.load(self.model_path)
                logger.info(
                    f"[MLStrategy] Loaded ML model for {self.clean_symbol} from {self.model_path}"
                )
            except Exception as e:
                logger.error(f"[MLStrategy] Error loading model bundle from {self.model_path}: {e}")
                self.model = None
        else:
            logger.warning(
                f"[MLStrategy] Model artifact not found at {self.model_path}. Signals will remain paused until model is trained."
            )
            self.model = None

    def should_generate_signal(self, current_time: float) -> bool:
        """Checks whether the strategy is permitted to emit a signal."""
        if self.trade_in_progress:
            return False
        if (current_time - self.last_trade_time) < self.cooldown_seconds:
            return False
        if self.model is None or not self.model.is_trained:
            return False
        return True

    def get_remaining_cooldown(self, current_time: float) -> float:
        """Returns remaining cooldown time in seconds."""
        elapsed = current_time - self.last_trade_time
        return max(0.0, self.cooldown_seconds - elapsed)

    def generate_signal(self, symbol: str) -> Optional[TradeSignal]:
        """
        Polls recent 1-minute klines from market feed, extracts features,
        evaluates directional probability, and returns a TradeSignal if actionable.
        """
        now = time.time()
        if not self.should_generate_signal(now):
            return None

        # Check rate-limit cooldown
        if now < self._rate_limit_backoff_until:
            return None

        # 1. Prefer in-memory real-time candles from WebSocket feed IF WS is active AND candles are fresh
        raw_candles = None
        data_source = "REST"
        with self._candles_lock:
            if len(self.candles) >= min(self.warmup_candles, 50):
                last_c = self.candles[-1]
                last_ts = int(last_c.get("timestamp", 0) if isinstance(last_c, dict) else getattr(last_c, "timestamp", 0))
                last_ts_s = (last_ts / 1000.0) if last_ts > 1e11 else float(last_ts)
                is_fresh = (now - last_ts_s) <= 120.0
                ws_active = bool(self.feed and getattr(self.feed, "is_connected", False))
                feed_running = bool(self.feed and getattr(self.feed, "_running", False))
                # In-memory candles are accepted if fresh AND (WS is connected OR feed is not running in background)
                if is_fresh and (ws_active or not feed_running):
                    raw_candles = list(self.candles)
                    data_source = "WS"

        # 2. If WebSocket feed is not connected, blocked (Cloudflare 403), or candles are stale,
        # poll fresh 1m klines from REST with rate-limit throttling (kline_cache_interval).
        if not raw_candles:
            if (now - self._last_kline_fetch_ts < self.kline_cache_interval) and self._cached_candles:
                raw_candles = self._cached_candles
                data_source = "REST_CACHED"
            else:
                try:
                    fresh_bars = self.market.get_klines(symbol, interval="Min1", limit=300)
                    if fresh_bars and len(fresh_bars) >= 50:
                        self._cached_candles = fresh_bars
                        self._last_kline_fetch_ts = now
                        self._consecutive_rate_limits = 0
                        with self._candles_lock:
                            self.candles = list(fresh_bars)
                        raw_candles = fresh_bars
                        data_source = "REST_LIVE"
                    else:
                        # Fallback to cached candles if API returned empty
                        raw_candles = self._cached_candles
                        data_source = "REST_CACHED"
                except Exception as e:
                    is_rate_limit = ("510" in str(e)) or (getattr(e, "code", None) in (510, 429))
                    if not hasattr(self, "_consecutive_rate_limits"):
                        self._consecutive_rate_limits = 0
                    if is_rate_limit:
                        self._consecutive_rate_limits += 1
                        backoff = min(30.0, 5.0 * (1.5 ** min(self._consecutive_rate_limits - 1, 4)))
                        self._rate_limit_backoff_until = now + backoff
                        logger.warning(
                            f"[MLStrategy] Hit API rate limit (510/429) fetching klines for {symbol}. "
                            f"Engaging {backoff:.1f}s backoff (strike {self._consecutive_rate_limits}): {e}"
                        )
                    else:
                        logger.warning(f"[MLStrategy] Error fetching klines for {symbol}: {e}")
                    raw_candles = self._cached_candles
                    data_source = "REST_CACHED"

        if not raw_candles or len(raw_candles) < min(self.warmup_candles, 50):
            logger.debug(f"[MLStrategy] Insufficient candles for warmup ({len(raw_candles) if raw_candles else 0}).")
            return None

        # Parse candles to standardized DataFrame
        records = []
        for c in raw_candles:
            if isinstance(c, dict):
                ts = int(c.get("open_time", c.get("timestamp", c.get("time", 0))))
                o = float(c.get("open", 0.0))
                h = float(c.get("high", 0.0))
                l = float(c.get("low", 0.0))
                cl = float(c.get("close", 0.0))
                vol = float(c.get("volume", c.get("vol", 0.0)))
                tbv = float(c.get("taker_buy_volume", c.get("taker_buy_vol", vol * 0.5)))
            else:
                # Support dataclass object (e.g. BACKTESTER Candle)
                ts = int(getattr(c, "open_time_ms", getattr(c, "timestamp", 0)))
                o = float(getattr(c, "open", 0.0))
                h = float(getattr(c, "high", 0.0))
                l = float(getattr(c, "low", 0.0))
                cl = float(getattr(c, "close", 0.0))
                vol = float(getattr(c, "volume", 0.0))
                tbv = float(getattr(c, "taker_buy_volume", vol * 0.5))

            records.append({
                "timestamp": ts,
                "open": o,
                "high": h,
                "low": l,
                "close": cl,
                "volume": vol,
                "taker_buy_volume": tbv
            })

        df_ohlcv = pd.DataFrame(records).drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
        if len(df_ohlcv) < 50:
            return None

        # Feature Extraction using official pipeline
        try:
            df_feats, _ = extract_features(df_ohlcv, df_orderflow=None)
        except Exception as e:
            logger.error(f"[MLStrategy] Error extracting features: {e}")
            return None

        # Determine evaluation candle index:
        # If require_closed_candle is True, candle[-1] is still forming, so evaluate closed candle at index -2.
        latest_idx = len(df_feats) - 2 if self.require_closed_candle and len(df_feats) >= 2 else len(df_feats) - 1
        latest_row = df_feats.iloc[[latest_idx]]
        curr_price = float(df_ohlcv["close"].iloc[latest_idx])
        curr_atr = float(df_feats["atr_14"].iloc[latest_idx])

        # Model Inference
        try:
            decisions = self.model.predict_decision(
                X=latest_row,
                current_prices=np.array([curr_price]),
                current_atrs=np.array([curr_atr]),
                symbol=self.clean_symbol
            )
            dec = decisions[0]
            dec["curr_price"] = curr_price
            dec["curr_atr"] = curr_atr
            try:
                pu_val = getattr(self.market, "get_tick_size", lambda s: 0.001)(symbol)
                pu = float(pu_val) if pu_val and isinstance(pu_val, (int, float)) else 0.001
            except Exception:
                pu = 0.001
            dec["atr_ticks"] = (curr_atr / pu) if pu > 0 else 0.0
            self.last_prediction = dec
        except Exception as e:
            logger.error(f"[MLStrategy] Prediction execution error: {e}")
            return None

        action = dec["action"]
        confidence = dec["confidence"]

        self.last_data_source = data_source

        # Internal ML Radar Telemetry (logged at DEBUG level; central executor presents consolidated info)
        if not hasattr(self, "_last_radar_log_time"):
            self._last_radar_log_time = 0.0
        if now - self._last_radar_log_time >= 5.0:
            self._last_radar_log_time = now
            try:
                p_buy = float(dec.get("prob_buy", 0.0))
                p_sell = float(dec.get("prob_sell", 0.0))
                p_wait = float(dec.get("prob_wait", 0.0))
                raw_tb = getattr(self.model.cfg, "confidence_threshold", 0.38) if hasattr(self, "model") and hasattr(self.model, "cfg") else 0.38
                thresh_buy = float(raw_tb) if isinstance(raw_tb, (int, float)) else 0.38
                raw_ts = getattr(self.model.cfg, "confidence_threshold_sell", thresh_buy) if hasattr(self, "model") and hasattr(self.model, "cfg") else thresh_buy
                thresh_sell = float(raw_ts) if isinstance(raw_ts, (int, float)) else thresh_buy
                try:
                    pu_val = getattr(self.market, "get_tick_size", lambda s: 0.001)(symbol)
                    pu = float(pu_val) if pu_val and isinstance(pu_val, (int, float)) else 0.001
                except Exception:
                    pu = 0.001
                atr_ticks = (curr_atr / pu) if pu > 0 else 0
                logger.debug(
                    f"[ML RADAR] {symbol} Price: {curr_price:.4f} USDT [{data_source}] | ATR(14): {curr_atr:.4f} ({atr_ticks:.1f}t) | "
                    f"P(BUY): {p_buy:.1%} [T:{thresh_buy:.1%}] | P(SELL): {p_sell:.1%} [T:{thresh_sell:.1%}] | "
                    f"P(WAIT): {p_wait:.1%} | Action: {action}"
                )
            except Exception as e:
                logger.debug(f"[ML RADAR] Telemetry format error: {e}")

        # Apply preferred direction lock if configured
        if self.preferred_direction is not None:
            if self.preferred_direction == OrderDirection.LONG and action == "SELL":
                return None
            if self.preferred_direction == OrderDirection.SHORT and action == "BUY":
                return None

        if action in ("BUY", "SELL"):
            direction = OrderDirection.LONG if action == "BUY" else OrderDirection.SHORT
            tp_ticks = dec["tp_ticks"]
            sl_ticks = dec["sl_ticks"]
            try:
                pu_val = getattr(self.market, "get_tick_size", lambda s: 0.001)(symbol)
                pu = float(pu_val) if pu_val and isinstance(pu_val, (int, float)) else 0.001
            except Exception:
                pu = 0.001

            metadata = {
                "target_ticks": tp_ticks,
                "target_sl_ticks": sl_ticks,
                "suggested_tp": dec["suggested_tp"],
                "suggested_sl": dec["suggested_sl"],
                "tp_price_exact": dec.get("tp_price_exact", dec["suggested_tp"]),
                "sl_price_exact": dec.get("sl_price_exact", dec["suggested_sl"]),
                "confidence": confidence,
                "prob_buy": dec["prob_buy"],
                "prob_sell": dec["prob_sell"],
                "prob_wait": dec["prob_wait"],
                "risk_reward_ratio": dec["risk_reward_ratio"],
                "atr_14": round(curr_atr, 6),
                "entry_price": curr_price,
                "model_action": action,
                "strategy": "ML_1M_MODEL"
            }

            self.trade_in_progress = True
            sig = TradeSignal(
                symbol=symbol,
                direction=direction,
                sub_strategy_name="ML_1M_MODEL",
                timestamp=now,
                metadata=metadata
            )
            logger.info(
                f"==============================================================================\n"
                f"🔥 [ML ALPHA TRIGGER] HIGH-CONVICTION {action} SIGNAL DETECTED 🔥\n"
                f"   • Conviction     : {confidence:.1%} (P({action})={confidence:.1%} vs P(WAIT)={dec['prob_wait']:.1%})\n"
                f"   • Reference Price: {curr_price:.4f} USDT | 1m ATR = {curr_atr:.4f} USDT ({(curr_atr / pu if pu > 0 else 0):.1f} ticks)\n"
                f"   • Dynamic Target : TP = {dec['suggested_tp']} (+{tp_ticks} ticks / ~{getattr(self.model.cfg, 'tp_atr_mult', 3.0):.1f}x ATR)\n"
                f"   • Dynamic Stop   : SL = {dec['suggested_sl']} (-{sl_ticks} ticks / ~{getattr(self.model.cfg, 'sl_atr_mult', 1.5):.1f}x ATR)\n"
                f"   • Reward / Risk  : {dec['risk_reward_ratio']} : 1 | Horizon = {getattr(self.model.cfg, 'horizon_bars', 15)} bars (15m)\n"
                f"=============================================================================="
            )
            return sig

        return None

    def on_trade_completed(self, outcome: TradeOutcome) -> None:
        """Callback invoked when position closes."""
        self.trade_in_progress = False
        self.last_trade_time = outcome.close_time if outcome.close_time > 0 else time.time()
        logger.info(
            f"[MLStrategy] Trade #{outcome.trade_id} closed [{outcome.exit_reason.value}]. "
            f"PnL: {outcome.realized_pnl_usdt:+.4f} USDT ({outcome.roe_percentage:+.2f}% ROE). Engaging cooldown ({self.cooldown_seconds}s)."
        )

    def on_trade_rejected(self) -> None:
        """Callback invoked when signal was rejected or suppressed by a filter."""
        self.trade_in_progress = False

    def get_parameters(self) -> Dict[str, Any]:
        """Returns strategy hyperparameters for reporting."""
        params = {
            "strategy": self.name,
            "symbol": self.symbol,
            "clean_symbol": self.clean_symbol,
            "timeframe": self.timeframe,
            "cooldown_seconds": self.cooldown_seconds,
            "warmup_candles": self.warmup_candles,
            "require_closed_candle": self.require_closed_candle,
            "model_path": self.model_path,
            "model_loaded": (self.model is not None and self.model.is_trained)
        }
        if self.model and self.model.cfg:
            params.update({
                "horizon_bars": self.model.cfg.horizon_bars,
                "tp_atr_mult": self.model.cfg.tp_atr_mult,
                "sl_atr_mult": self.model.cfg.sl_atr_mult,
                "confidence_threshold": self.model.cfg.confidence_threshold,
                "confidence_threshold_sell": getattr(self.model.cfg, "confidence_threshold_sell", self.model.cfg.confidence_threshold),
                "edge_threshold": self.model.cfg.edge_threshold
            })
        return params

    def get_diagnostics(self) -> Dict[str, Any]:
        """Returns real-time diagnostics and latest prediction probabilities."""
        feed_stats = self.feed.stats if self.feed else {"connected": False}
        diag = {
            "strategy": self.name,
            "symbol": self.symbol,
            "trade_in_progress": self.trade_in_progress,
            "model_loaded": (self.model is not None and self.model.is_trained),
            "remaining_cooldown_sec": round(self.get_remaining_cooldown(time.time()), 1),
            "feed": feed_stats,
            "data_source": getattr(self, "last_data_source", "REST"),
            "in_memory_candles": len(self.candles),
            "last_prediction": self.last_prediction
        }
        return diag


# Standard alias
MLSubStrategy = MLStrategy
