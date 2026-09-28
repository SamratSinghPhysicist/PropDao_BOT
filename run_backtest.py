"""
PropDAO High-Fidelity Backtester Entrypoint
===========================================
Invokes the complete KCEX-grade high-fidelity backtesting engine located in BACKTESTER.
Supports both interactive wizard configuration and full CLI automation.
"""

import os
import sys

# Ensure project root is in sys.path
ROOT_DIR = os.path.abspath(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from BACKTESTER.run_backtest import main

if __name__ == "__main__":
    main()
