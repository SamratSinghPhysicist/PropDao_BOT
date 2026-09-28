"""
Analytics Data Models & Serialization Schemas
============================================
Lightweight structured schemas for backtest runs, performance scorecards,
directional analytics, exit attributions, and downsampled equity curves.
"""

from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional


@dataclass
class RunMetadata:
    run_id: str
    run_name: str
    timestamp_utc: str = ""
    symbol: str = ""
    base_asset: str = ""
    quote_asset: str = ""
    timeframe: str = ""
    strategy: str = ""
    strategy_desc: str = ""
    strategy_preset: str = ""
    date_range: str = ""
    start_date: str = ""
    end_date: str = ""
    high_fidelity_ticks: bool = True
    slippage_ticks: int = 0
    sizing_mode: str = "MULTIPLIER"
    volume_desc: str = ""
    contracts: float = 1.0
    leverage: int = 50
    starting_capital_usdt: float = 100.0
    starting_capital_inr: float = 9445.0
    tp_target_desc: str = ""
    tp_ticks: int = 2
    sl_rule_desc: str = ""
    sl_mode: str = "ROE"
    sl_value: float = 25.0
    contract_size: float = 1.0
    price_unit: float = 0.0001
    source: str = "local"
    fee_mode: str = "ZERO"
    maker_fee_pct: float = 0.0
    taker_fee_pct: float = 0.0
    price_precision: int = 4
    min_volume: float = 1.0
    max_leverage: int = 75
    has_csv: bool = False
    has_jsonl: bool = False
    has_md: bool = False
    has_zip: bool = False
    zip_filename: Optional[str] = None
    csv_size_mb: float = 0.0
    jsonl_size_mb: float = 0.0
    zip_size_mb: float = 0.0
    parameters: Dict[str, Any] = field(default_factory=dict)
    filters: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RunMetadata":
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)


@dataclass
class RunScorecard:
    initial_capital_usdt: float = 0.0
    final_balance_usdt: float = 0.0
    net_pnl_usdt: float = 0.0
    net_pnl_inr: float = 0.0
    net_roi_pct: float = 0.0
    gross_profit_usdt: float = 0.0
    gross_loss_usdt: float = 0.0
    total_fees_usdt: float = 0.0
    profit_factor: float = 0.0
    win_loss_payoff: float = 0.0
    max_drawdown_usdt: float = 0.0
    max_drawdown_pct: float = 0.0
    win_rate_pct: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    scratch_trades: int = 0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    calmar_ratio: float = 0.0
    avg_trade_pnl_usdt: float = 0.0
    avg_win_pnl_usdt: float = 0.0
    avg_loss_pnl_usdt: float = 0.0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0
    avg_duration_seconds: float = 0.0
    fastest_fill_seconds: float = 0.0
    longest_fill_seconds: float = 0.0
    cumulative_in_position_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RunScorecard":
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)


@dataclass
class DirectionalStats:
    long_trades: int = 0
    long_wins: int = 0
    long_losses: int = 0
    long_win_rate_pct: float = 0.0
    long_gross_profit: float = 0.0
    long_gross_loss: float = 0.0
    long_net_pnl_usdt: float = 0.0
    long_profit_factor: float = 0.0

    short_trades: int = 0
    short_wins: int = 0
    short_losses: int = 0
    short_win_rate_pct: float = 0.0
    short_gross_profit: float = 0.0
    short_gross_loss: float = 0.0
    short_net_pnl_usdt: float = 0.0
    short_profit_factor: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DirectionalStats":
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)


@dataclass
class ExitAttribution:
    reason: str
    count: int = 0
    pct_of_trades: float = 0.0
    total_pnl_usdt: float = 0.0
    win_rate_pct: float = 0.0
    avg_duration_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DownsampledPoint:
    trade_id: int
    time_utc: str
    balance_usdt: float
    roi_pct: float
    drawdown_pct: float
    cum_pnl_usdt: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DetailedAnalytics:
    duration_buckets: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    hourly_distribution: List[Dict[str, Any]] = field(default_factory=list)
    day_of_week_distribution: List[Dict[str, Any]] = field(default_factory=list)
    pnl_distribution: Dict[str, float] = field(default_factory=dict)
    roe_distribution: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DetailedAnalytics":
        return cls(
            duration_buckets=data.get("duration_buckets", {}),
            hourly_distribution=data.get("hourly_distribution", []),
            day_of_week_distribution=data.get("day_of_week_distribution", []),
            pnl_distribution=data.get("pnl_distribution", {}),
            roe_distribution=data.get("roe_distribution", {}),
        )


@dataclass
class BacktestRunRecord:
    metadata: RunMetadata
    scorecard: RunScorecard
    directional: DirectionalStats
    exit_attributions: List[ExitAttribution] = field(default_factory=list)
    detailed: Optional[DetailedAnalytics] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metadata": self.metadata.to_dict(),
            "scorecard": self.scorecard.to_dict(),
            "directional": self.directional.to_dict(),
            "exit_attributions": [e.to_dict() for e in self.exit_attributions],
            "detailed": self.detailed.to_dict() if self.detailed else None,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BacktestRunRecord":
        return cls(
            metadata=RunMetadata.from_dict(data.get("metadata", {})),
            scorecard=RunScorecard.from_dict(data.get("scorecard", {})),
            directional=DirectionalStats.from_dict(data.get("directional", {})),
            exit_attributions=[
                ExitAttribution(**e) if isinstance(e, dict) else e
                for e in data.get("exit_attributions", [])
            ],
            detailed=DetailedAnalytics.from_dict(data["detailed"]) if data.get("detailed") else None,
        )
