"""
Base Strategy Architecture
==========================
Defines the abstract BaseStrategy lifecycle, signal generation interface,
parameter discovery, and diagnostic contracts for all modular trading strategies.
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from kcex.engine.models import TradeSignal, TradeOutcome


class BaseStrategy(ABC):
    """Abstract base class for all modular strategies."""

    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    def should_generate_signal(self, current_time: float) -> bool:
        """Determines if the strategy is ready to evaluate and emit a trade signal."""
        pass

    @abstractmethod
    def generate_signal(self, symbol: str) -> Optional[TradeSignal]:
        """Generates a directional trade signal if entry criteria are met."""
        pass

    @abstractmethod
    def on_trade_completed(self, outcome: TradeOutcome) -> None:
        """Callback invoked when an active position has closed."""
        pass

    @abstractmethod
    def get_remaining_cooldown(self, current_time: float) -> float:
        """Returns the remaining cooldown time in seconds."""
        pass

    def start(self) -> None:
        """Starts any background resources (e.g. WebSocket feeds)."""
        pass

    def stop(self) -> None:
        """Stops background resources."""
        pass

    def get_parameters(self) -> Dict[str, Any]:
        """Returns strategy configuration hyperparameters for reporting and analytics."""
        return {"strategy": self.name}

    def get_diagnostics(self) -> Dict[str, Any]:
        """Returns real-time diagnostics, indicator values, and state metrics."""
        return {}


# Backwards compatibility alias
BaseSubStrategy = BaseStrategy
