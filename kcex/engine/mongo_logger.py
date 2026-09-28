"""
KCEX MongoDB Trade Logger
=========================
Logs every live trade (executed and cancelled) to MongoDB Atlas in real-time.
Each trade is stored as an individual document immediately after verification
from the KCEX live account. Gracefully degrades if MongoDB is unreachable.

Collections:
  - trades: All executed trade outcomes (verified from KCEX)
  - cancelled_orders: Limit orders that timed out before fill
  - sessions: Session start/end summaries
"""

import os
import uuid
import logging
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

logger = logging.getLogger("KCEXEngine")

# MongoDB Database & Collection Names
DB_NAME = "kcex_trading_bot"
COLLECTION_TRADES = "trades"
COLLECTION_CANCELLED = "cancelled_orders"
COLLECTION_SESSIONS = "sessions"


class MongoTradeLogger:
    """
    Real-time MongoDB trade logger for KCEX live trading.
    
    Connects lazily on first use. If MongoDB is unreachable, all logging
    methods fail silently with a warning to avoid interrupting live trading.
    """

    def __init__(self, mongodb_uri: Optional[str] = None):
        """
        Initialize the MongoDB logger.
        
        Args:
            mongodb_uri: MongoDB connection string. Falls back to MONGODB_URI env var.
        """
        if not mongodb_uri and not os.getenv("MONGODB_URI"):
            try:
                from kcex.config import load_env_file
                load_env_file()
            except Exception:
                pass

        self._uri = mongodb_uri or os.getenv("MONGODB_URI", "")
        self._client = None
        self._db = None
        self._connected = False
        self._connection_attempted = False

        # Session identification
        self.session_id = str(uuid.uuid4())[:12]
        self.session_start_time = datetime.now(timezone.utc)

        # Detect execution environment
        self.execution_env = "github_actions" if os.getenv("GITHUB_ACTIONS") else "local"
        self.github_run_id = os.getenv("GITHUB_RUN_ID", "")
        self.github_run_number = os.getenv("GITHUB_RUN_NUMBER", "")

    def _connect(self) -> bool:
        """Lazy connection to MongoDB Atlas. Returns True if connected."""
        if self._connected:
            return True
        if self._connection_attempted:
            return False
        self._connection_attempted = True

        if not self._uri:
            logger.warning("[MONGO] No MONGODB_URI configured. Trade logging to MongoDB disabled.")
            return False

        try:
            try:
                import dns.resolver
                res = dns.resolver.get_default_resolver()
                for ns in reversed(["8.8.8.8", "1.1.1.1"]):
                    if ns not in res.nameservers:
                        res.nameservers.append(ns)
            except Exception:
                pass

            from pymongo import MongoClient
            self._client = MongoClient(
                self._uri,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=5000,
                socketTimeoutMS=10000,
                retryWrites=True,
                w="majority"
            )
            # Verify connectivity
            self._client.admin.command("ping")
            self._db = self._client[DB_NAME]

            # Create indexes for efficient querying
            self._db[COLLECTION_TRADES].create_index("session_id")
            self._db[COLLECTION_TRADES].create_index("entry_time")
            self._db[COLLECTION_TRADES].create_index("symbol")
            self._db[COLLECTION_TRADES].create_index("execution_env")
            self._db[COLLECTION_CANCELLED].create_index("session_id")
            self._db[COLLECTION_CANCELLED].create_index("timestamp")

            self._connected = True
            logger.info(
                f"[MONGO] ✅ Connected to MongoDB Atlas | DB: {DB_NAME} | "
                f"Session: {self.session_id} | Env: {self.execution_env}"
            )
            return True
        except Exception as e:
            logger.warning(f"[MONGO] ⚠️ Failed to connect to MongoDB: {e}. Trade logging to MongoDB disabled.")
            return False

    def ping(self) -> bool:
        """Test MongoDB connectivity."""
        return self._connect()

    # =========================================================================
    # EXECUTED TRADE LOGGING
    # =========================================================================

    def log_executed_trade(
        self,
        outcome,  # TradeOutcome
        config=None,   # ExecutionConfig or Dict or None
        balance_before_usdt: Optional[float] = None,
        balance_before_inr: Optional[float] = None
    ) -> Optional[str]:
        """
        Log a verified executed trade to MongoDB immediately.
        
        Args:
            outcome: The TradeOutcome dataclass from the executor
            config: The ExecutionConfig used for this session (optional)
            balance_before_usdt: Wallet balance before trade entry
            balance_before_inr: Wallet balance before trade entry (INR)
            
        Returns:
            The MongoDB document _id as string, or None on failure.
        """
        if not self._connect():
            return None

        try:
            doc = outcome.to_mongo_dict() if hasattr(outcome, "to_mongo_dict") else dict(outcome)

            # Enrich with session and environment metadata
            doc["session_id"] = self.session_id
            doc["logged_at"] = datetime.now(timezone.utc)

            # Balance before trade (fallback to outcome attributes if not explicitly passed)
            if balance_before_usdt is not None:
                doc["balance_before_trade_usdt"] = balance_before_usdt
            elif doc.get("balance_before_trade_usdt") is None:
                doc["balance_before_trade_usdt"] = getattr(outcome, "balance_before_trade_usdt", None)

            if balance_before_inr is not None:
                doc["balance_before_trade_inr"] = balance_before_inr
            elif doc.get("balance_before_trade_inr") is None:
                doc["balance_before_trade_inr"] = getattr(outcome, "balance_before_trade_inr", None)

            # Strategy & Configuration snapshot (safely handled if config is None or dict)
            if hasattr(config, "to_config_snapshot"):
                doc["config_snapshot"] = config.to_config_snapshot()
            elif isinstance(config, dict):
                doc["config_snapshot"] = config
            else:
                doc["config_snapshot"] = getattr(outcome, "config_snapshot", {})

            # Execution environment
            doc["execution_env"] = self.execution_env
            doc["github_run_id"] = self.github_run_id
            doc["github_run_number"] = self.github_run_number

            result = self._db[COLLECTION_TRADES].insert_one(doc)
            doc_id = str(result.inserted_id)

            trade_id = getattr(outcome, "trade_id", doc.get("trade_id", "?"))
            pnl_val = getattr(outcome, "realized_pnl_usdt", doc.get("realized_pnl_usdt", 0.0))
            pnl_sign = "+" if pnl_val > 0 else ""
            sym = getattr(outcome, "symbol", doc.get("symbol", "?"))
            logger.info(
                f"[MONGO] 📝 Trade #{trade_id} [{sym}] logged to MongoDB | "
                f"PnL: {pnl_sign}{pnl_val:.6f} USDT | "
                f"Doc ID: {doc_id}"
            )
            return doc_id

        except Exception as e:
            trade_id = getattr(outcome, "trade_id", "?")
            logger.warning(f"[MONGO] ⚠️ Failed to log trade #{trade_id} to MongoDB: {e}")
            return None

    def log_trade(
        self,
        outcome,
        config=None,
        balance_before_usdt: Optional[float] = None,
        balance_before_inr: Optional[float] = None
    ) -> Optional[str]:
        """Backward-compatible alias for log_executed_trade."""
        return self.log_executed_trade(
            outcome=outcome,
            config=config,
            balance_before_usdt=balance_before_usdt,
            balance_before_inr=balance_before_inr
        )


    # =========================================================================
    # CANCELLED ORDER LOGGING
    # =========================================================================

    def log_cancelled_order(
        self,
        symbol: str,
        direction: str,
        intended_entry_price: float,
        order_id: Optional[str],
        timeout_seconds: float,
        strategy_name: str,
        config,  # ExecutionConfig
        market_snapshot: Optional[Dict[str, float]] = None,
        balance_usdt: Optional[float] = None,
        balance_inr: Optional[float] = None,
        inr_rate: float = 94.45
    ) -> Optional[str]:
        """
        Log a cancelled limit order (timed out before fill) to MongoDB.
        
        Args:
            symbol: Trading pair
            direction: "LONG" or "SHORT"
            intended_entry_price: The limit price that was set
            order_id: KCEX order ID (if available)
            timeout_seconds: How long we waited before cancelling
            strategy_name: Active strategy name
            config: ExecutionConfig snapshot
            market_snapshot: {bid1, ask1, last_price} at time of cancellation
            balance_usdt: Current wallet balance
            balance_inr: Current wallet balance in INR
            inr_rate: USD/INR exchange rate
            
        Returns:
            The MongoDB document _id as string, or None on failure.
        """
        if not self._connect():
            return None

        try:
            doc = {
                "type": "CANCELLED",
                "cancel_reason": "QUEUE_TIMEOUT_CANCELLED",
                "session_id": self.session_id,
                "timestamp": datetime.now(timezone.utc),
                "symbol": symbol,
                "direction": direction,
                "intended_entry_price": intended_entry_price,
                "order_id": order_id,
                "timeout_seconds": timeout_seconds,
                "strategy_name": strategy_name,
                "config_snapshot": config.to_config_snapshot(),
                "market_snapshot": market_snapshot or {},
                "balance_usdt": balance_usdt,
                "balance_inr": balance_inr,
                "inr_rate": inr_rate,
                "execution_env": self.execution_env,
                "github_run_id": self.github_run_id,
                "github_run_number": self.github_run_number,
                "logged_at": datetime.now(timezone.utc),
            }

            result = self._db[COLLECTION_CANCELLED].insert_one(doc)
            doc_id = str(result.inserted_id)

            logger.info(
                f"[MONGO] 📝 Cancelled order logged to MongoDB | "
                f"{symbol} {direction} @ {intended_entry_price} | "
                f"Timeout: {timeout_seconds}s | Doc ID: {doc_id}"
            )
            return doc_id

        except Exception as e:
            logger.warning(f"[MONGO] ⚠️ Failed to log cancelled order to MongoDB: {e}")
            return None

    # =========================================================================
    # SESSION LOGGING
    # =========================================================================

    def log_session_start(self, config=None) -> Optional[str]:
        """Log session start to MongoDB with full configuration."""
        if not self._connect():
            return None

        try:
            if hasattr(config, "to_config_snapshot"):
                cfg_snap = config.to_config_snapshot()
            elif isinstance(config, dict):
                cfg_snap = config
            else:
                cfg_snap = {}

            doc = {
                "session_id": self.session_id,
                "event": "SESSION_START",
                "start_time": self.session_start_time,
                "execution_env": self.execution_env,
                "github_run_id": self.github_run_id,
                "github_run_number": self.github_run_number,
                "config_snapshot": cfg_snap,
                "logged_at": datetime.now(timezone.utc),
            }
            result = self._db[COLLECTION_SESSIONS].insert_one(doc)
            return str(result.inserted_id)
        except Exception as e:
            logger.warning(f"[MONGO] ⚠️ Failed to log session start: {e}")
            return None

    def log_session_end(
        self,
        total_trades: int,
        winning_trades: int,
        losing_trades: int,
        scratch_trades: int,
        cancelled_orders: int,
        total_pnl_usdt: float,
        total_pnl_inr: float,
        win_rate: float,
        best_trade_usdt: float,
        worst_trade_usdt: float,
        total_fees_usdt: float,
        total_fees_inr: float,
        final_balance_usdt: Optional[float] = None,
        final_balance_inr: Optional[float] = None,
    ) -> Optional[str]:
        """Log session end summary to MongoDB."""
        if not self._connect():
            return None

        try:
            session_end_time = datetime.now(timezone.utc)
            duration = (session_end_time - self.session_start_time).total_seconds()

            doc = {
                "session_id": self.session_id,
                "event": "SESSION_END",
                "start_time": self.session_start_time,
                "end_time": session_end_time,
                "duration_seconds": duration,
                "duration_human": f"{int(duration // 3600)}h {int((duration % 3600) // 60)}m {int(duration % 60)}s",
                "execution_env": self.execution_env,
                "github_run_id": self.github_run_id,
                "github_run_number": self.github_run_number,
                # Performance summary
                "total_trades": total_trades,
                "winning_trades": winning_trades,
                "losing_trades": losing_trades,
                "scratch_trades": scratch_trades,
                "cancelled_orders": cancelled_orders,
                "win_rate_pct": win_rate,
                "total_pnl_usdt": total_pnl_usdt,
                "total_pnl_inr": total_pnl_inr,
                "best_trade_usdt": best_trade_usdt,
                "worst_trade_usdt": worst_trade_usdt,
                "total_fees_usdt": total_fees_usdt,
                "total_fees_inr": total_fees_inr,
                "final_balance_usdt": final_balance_usdt,
                "final_balance_inr": final_balance_inr,
                "logged_at": datetime.now(timezone.utc),
            }
            result = self._db[COLLECTION_SESSIONS].insert_one(doc)
            logger.info(
                f"[MONGO] 📝 Session end logged | Trades: {total_trades} | "
                f"PnL: {'+' if total_pnl_usdt >= 0 else ''}{total_pnl_usdt:.6f} USDT"
            )
            return str(result.inserted_id)
        except Exception as e:
            logger.warning(f"[MONGO] ⚠️ Failed to log session end: {e}")
            return None

    # =========================================================================
    # ANALYTICS QUERIES
    # =========================================================================

    def _build_mode_match(self, mode_filter: Optional[str]) -> Optional[Dict[str, Any]]:
        """Constructs case-insensitive mode query condition."""
        if not mode_filter or mode_filter.upper() == "ALL":
            return None
        m = mode_filter.strip().lower()
        return {"$in": [m, m.upper()]}

    def get_traded_symbols(self, mode_filter: str = "live") -> List[str]:
        """Fetch distinct symbols of traded pairs from MongoDB."""
        if not self._connect():
            return []
        try:
            query: Dict[str, Any] = {"type": "EXECUTED"}
            mode_cond = self._build_mode_match(mode_filter)
            if mode_cond:
                query["mode"] = mode_cond
            symbols = self._db[COLLECTION_TRADES].distinct("symbol", query)
            return sorted([s for s in symbols if s])
        except Exception as e:
            logger.warning(f"[MONGO] ⚠️ Failed to fetch traded symbols: {e}")
            return []

    def get_traded_pairs_summary(self, mode_filter: str = "live") -> List[Dict[str, Any]]:
        """Fetch distinct traded pairs with trade count, base coin, and net PnL from MongoDB."""
        if not self._connect():
            return []
        try:
            match_stage: Dict[str, Any] = {"type": "EXECUTED"}
            mode_cond = self._build_mode_match(mode_filter)
            if mode_cond:
                match_stage["mode"] = mode_cond
            pipeline = [
                {"$match": match_stage},
                {
                    "$group": {
                        "_id": "$symbol",
                        "symbol": {"$first": "$symbol"},
                        "base_coin": {"$first": "$base_coin"},
                        "trade_count": {"$sum": 1},
                        "pnl_usdt": {"$sum": "$realized_pnl_usdt"},
                        "pnl_inr": {"$sum": "$realized_pnl_inr"},
                        "wins": {
                            "$sum": {
                                "$cond": [{"$gt": ["$realized_pnl_usdt", 0]}, 1, 0]
                            }
                        }
                    }
                },
                {"$sort": {"trade_count": -1}}
            ]
            results = list(self._db[COLLECTION_TRADES].aggregate(pipeline))
            for r in results:
                if not r.get("base_coin") and r.get("symbol"):
                    r["base_coin"] = r["symbol"].split("_")[0]
            return results
        except Exception as e:
            logger.warning(f"[MONGO] ⚠️ Failed to fetch traded pairs summary: {e}")
            return []

    def get_all_trades(self, mode_filter: str = "live", symbol_filter: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch all executed trades from MongoDB, sorted by entry_time."""
        if not self._connect():
            return []
        try:
            query: Dict[str, Any] = {"type": "EXECUTED"}
            mode_cond = self._build_mode_match(mode_filter)
            if mode_cond:
                query["mode"] = mode_cond
            if symbol_filter and symbol_filter.upper() != "ALL":
                sym = symbol_filter.strip().upper()
                query["$or"] = [
                    {"symbol": sym},
                    {"symbol": f"{sym}_USDT"},
                    {"base_coin": sym}
                ]
            return list(self._db[COLLECTION_TRADES].find(query).sort("entry_time", 1))
        except Exception as e:
            logger.warning(f"[MONGO] ⚠️ Failed to query trades: {e}")
            return []

    def get_all_cancelled_orders(self, symbol_filter: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch all cancelled orders from MongoDB."""
        if not self._connect():
            return []
        try:
            query = {}
            if symbol_filter:
                sym = symbol_filter.strip().upper()
                query["$or"] = [
                    {"symbol": sym},
                    {"symbol": f"{sym}_USDT"}
                ]
            return list(self._db[COLLECTION_CANCELLED].find(query).sort("timestamp", 1))
        except Exception as e:
            logger.warning(f"[MONGO] ⚠️ Failed to query cancelled orders: {e}")
            return []

    def get_session_trades(self, session_id: str) -> List[Dict[str, Any]]:
        """Fetch trades for a specific session."""
        if not self._connect():
            return []
        try:
            return list(
                self._db[COLLECTION_TRADES]
                .find({"session_id": session_id})
                .sort("entry_time", 1)
            )
        except Exception as e:
            logger.warning(f"[MONGO] ⚠️ Failed to query session trades: {e}")
            return []

    def get_all_sessions(self) -> List[Dict[str, Any]]:
        """Fetch all session records."""
        if not self._connect():
            return []
        try:
            return list(
                self._db[COLLECTION_SESSIONS]
                .find({"event": "SESSION_END"})
                .sort("start_time", -1)
            )
        except Exception as e:
            logger.warning(f"[MONGO] ⚠️ Failed to query sessions: {e}")
            return []

    def get_trade_count(self) -> int:
        """Get total number of executed trades in MongoDB."""
        if not self._connect():
            return 0
        try:
            return self._db[COLLECTION_TRADES].count_documents({"type": "EXECUTED"})
        except Exception:
            return 0

    def close(self):
        """Close MongoDB connection."""
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._connected = False
