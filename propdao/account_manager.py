"""
PropDAO Account Discovery & Challenge Tracker
=============================================
Manages account lifecycle:
- Detects active evaluation vs funded accounts
- Tracks challenge targets, equity progress, daily drawdown floors, and static floors
- Calculates fee impacts and net profitability metrics
"""

from __future__ import annotations
import logging
from typing import Optional, Dict, Any, List
from propdao.client import PropDAOClient
from propdao.models import AccountState, AccountStatus, Stage

logger = logging.getLogger("PropDAOAccountManager")


class PropDAOAccountManager:
    """
    Monitors PropDAO accounts, challenge stages, and evaluation progress.
    """

    def __init__(self, client: PropDAOClient):
        self.client = client
        self._accounts: List[Dict[str, Any]] = []

    def load_accounts(self) -> List[Dict[str, Any]]:
        """Fetches all accounts owned by the authenticated key."""
        self._accounts = self.client.get_accounts()
        logger.info("Found %d accounts on PropDAO.", len(self._accounts))
        return self._accounts

    def get_preferred_account(
        self,
        requested_id: Optional[str] = None,
        mode: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Retrieves the target PropDAO account.
        If requested_id is provided, matches case-insensitively.
        If no account id is provided, NEVER defaults to the first active account;
        instead prompts the user interactively, or raises a clear error in non-interactive mode.
        """
        import sys

        accounts = self.load_accounts()
        if not accounts:
            raise ValueError(
                "No PropDAO accounts found for this API key. "
                "Please verify that your PROPDAO_API_KEY is active on https://app.propdao.finance."
            )

        # 1. If an account ID was explicitly provided, match case-insensitively
        if requested_id and str(requested_id).strip():
            clean_req = str(requested_id).strip().lower()
            for a in accounts:
                if str(a.get("account_id", "")).lower() == clean_req:
                    ch_name = a.get("challenge", {}).get("name", a.get("live_stage", "Challenge"))
                    logger.info("Using PropDAO Account: %s (%s)", a.get("account_id").upper(), ch_name)
                    return a

            # Not found among user's accounts
            avail_str = ", ".join([f"{a.get('account_id').upper()} ({a.get('challenge', {}).get('name', 'Challenge')})" for a in accounts])
            raise ValueError(
                f"Requested account '{requested_id}' not found for this API key.\n"
                f"Your available PropDAO accounts are: {avail_str}"
            )

        # 2. No account ID provided -> DO NOT default to first account!
        # Check if running in an interactive terminal
        is_interactive = sys.stdin and hasattr(sys.stdin, "isatty") and sys.stdin.isatty()
        if is_interactive:
            print("\n" + "=" * 76)
            print("                 PROPDAO ACCOUNT SELECTION REQUIRED")
            print("=" * 76)
            print(f"No account ID was specified. Found {len(accounts)} accounts for your API key:\n")

            for idx, a in enumerate(accounts, 1):
                acct_id = str(a.get("account_id", "")).upper()
                ch = a.get("challenge", {})
                ch_name = ch.get("name", a.get("live_stage", "Challenge"))
                is_trial = "trial" in str(ch.get("id", "")).lower() or "free trial" in str(ch_name).lower()
                acct_badge = "[DEMO / TRIAL (Paper)]" if is_trial else "[LIVE / EVALUATION]"
                equity = float(a.get("live_equity", a.get("live_starting_balance", 0.0)))
                status = str(a.get("status", "active")).upper()

                rec_tag = ""
                if mode == "paper" and is_trial:
                    rec_tag = " <-- RECOMMENDED FOR PAPER MODE"
                elif mode == "live" and not is_trial:
                    rec_tag = " <-- RECOMMENDED FOR LIVE EVALUATION"

                print(f"  [{idx}] {acct_id:<16} {ch_name:<20} {acct_badge:<22} Equity: ${equity:>10,.2f} ({status}){rec_tag}")

            print("=" * 76)

            while True:
                try:
                    user_input = input(f"Please select account (1-{len(accounts)}) or enter Account ID: ").strip()
                except (EOFError, KeyboardInterrupt):
                    raise ValueError("Account selection cancelled by user.")

                if not user_input:
                    continue

                # Check numerical index (1, 2...)
                if user_input.isdigit():
                    c_idx = int(user_input) - 1
                    if 0 <= c_idx < len(accounts):
                        chosen = accounts[c_idx]
                        print(f"-> Selected Account: {chosen.get('account_id').upper()} ({chosen.get('challenge', {}).get('name')})\n")
                        return chosen
                    else:
                        print(f"Invalid choice. Enter a number between 1 and {len(accounts)}.")
                        continue

                # Check account ID string (e.g. PROP-L6AS4Z5H)
                clean_in = user_input.lower()
                for a in accounts:
                    if str(a.get("account_id", "")).lower() == clean_in:
                        print(f"-> Selected Account: {a.get('account_id').upper()} ({a.get('challenge', {}).get('name')})\n")
                        return a

                print(f"Account ID '{user_input}' not recognized. Please choose from the list above.")

        # 3. Non-interactive environment (CI, background daemon, script) without account ID
        lines = []
        for a in accounts:
            ch_name = a.get("challenge", {}).get("name", "Challenge")
            is_trial = "trial" in str(a.get("challenge_id", "")).lower() or "trial" in ch_name.lower()
            tag = "DEMO / TRIAL (Paper)" if is_trial else "LIVE / EVALUATION"
            lines.append(f"  - {a.get('account_id').upper()}: {ch_name} [{tag}]")

        account_list_str = "\n".join(lines)
        raise ValueError(
            f"No PropDAO account ID specified.\n"
            f"Please set PROPDAO_ACCOUNT in your .env file or pass --account <ID>.\n"
            f"Your available accounts:\n{account_list_str}"
        )

    def get_challenge_progress(self, account_id: str) -> Dict[str, Any]:
        """
        Calculates challenge status, distance to profit target, and drawdown cushion.
        """
        acct = self.client.get_account(account_id)
        risk = self.client.get_risk(account_id)

        starting_bal = float(acct.get("startingBalance", 25000.0))
        target_pct = float(acct.get("profitTargetPct", 10.0))
        target_equity = starting_bal * (1.0 + (target_pct / 100.0))

        current_equity = float(risk.get("equity", starting_bal))
        profit_usd = current_equity - starting_bal
        profit_pct = (profit_usd / starting_bal) * 100.0 if starting_bal > 0 else 0.0

        daily_anchor = float(acct.get("dailyAnchorEquity", starting_bal))
        daily_floor = float(risk.get("dailyFloor", daily_anchor * 0.98))
        max_floor = float(risk.get("maxFloor", starting_bal * 0.95))
        room_usd = float(risk.get("roomUsd", 0.0))

        status = acct.get("status", "active")
        is_passed = status == "passed" or (status == "active" and current_equity >= target_equity)

        return {
            "account_id": account_id,
            "status": status,
            "stage": acct.get("stage", "Evaluation"),
            "starting_balance": starting_bal,
            "current_equity": current_equity,
            "current_balance": float(acct.get("balance", starting_bal)),
            "profit_usd": profit_usd,
            "profit_pct": profit_pct,
            "target_pct": target_pct,
            "target_equity": target_equity,
            "remaining_to_target_usd": max(0.0, target_equity - current_equity),
            "is_passed": is_passed,
            "room_usd": room_usd,
            "room_pct": float(risk.get("roomPct", 0.0)),
            "binding_floor": float(risk.get("floor", max_floor)),
            "floor_kind": str(risk.get("floorKind", "max")),
            "daily_anchor_equity": daily_anchor,
            "daily_floor": daily_floor,
            "max_floor": max_floor,
            "breached": bool(risk.get("breached", False)),
            "open_positions_count": int(risk.get("openPositions", 0)),
        }
