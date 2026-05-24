"""Alpaca paper / live broker tools — execution layer for Vibe-Trading.

Sacred constraint (mirrored from CryptoBur ``worker/src/risk/portfolio-gate.ts``):
    ``ALPACA_READ_ONLY=1`` is the DEFAULT. Place-order and close-position calls
    are rejected until the operator explicitly sets ``ALPACA_READ_ONLY=0`` AND
    a per-process ``ALPACA_KILL_SWITCH`` is not engaged.

Tools registered:
    - ``alpaca_account``         — cash / equity / buying_power / status
    - ``alpaca_positions``       — open positions with unrealized P/L
    - ``alpaca_orders``          — recent orders (filterable by status)
    - ``alpaca_place_order``     — submit a market/limit order through risk gate
    - ``alpaca_close_position``  — flatten one symbol through risk gate

Risk gate (enforced in code, not in prompt):
    1. ``ALPACA_READ_ONLY``       (default "1")  → reject all writes
    2. ``ALPACA_KILL_SWITCH``     (default "0")  → reject all writes when "1"
    3. ``ALPACA_MAX_NOTIONAL_USD``(default 1000) → per-order cap
    4. ``ALPACA_MAX_POSITION_PCT``(default 0.05) → 5% of equity per symbol
    5. ``ALPACA_CASH_RESERVE_PCT``(default 0.20) → keep 20% cash unallocated
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from src.agent.tools import BaseTool


_DEFAULT_BASE_URL = "https://paper-api.alpaca.markets"
_TIMEOUT_SECONDS = 15


# ---------------------------------------------------------------------------
# Config + client
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _AlpacaConfig:
    key: str
    secret: str
    base_url: str
    read_only: bool
    kill_switch: bool
    max_notional_usd: float
    max_position_pct: float
    cash_reserve_pct: float

    @classmethod
    def from_env(cls) -> "_AlpacaConfig":
        def _flag(name: str, default: str) -> bool:
            return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}

        def _float(name: str, default: float) -> float:
            try:
                return float(os.getenv(name, str(default)))
            except (TypeError, ValueError):
                return default

        return cls(
            key=os.getenv("ALPACA_KEY", "").strip(),
            secret=os.getenv("ALPACA_SECRET", "").strip(),
            base_url=os.getenv("ALPACA_BASE_URL", _DEFAULT_BASE_URL).strip().rstrip("/"),
            read_only=_flag("ALPACA_READ_ONLY", "1"),  # sacred default
            kill_switch=_flag("ALPACA_KILL_SWITCH", "0"),
            max_notional_usd=_float("ALPACA_MAX_NOTIONAL_USD", 1000.0),
            max_position_pct=_float("ALPACA_MAX_POSITION_PCT", 0.05),
            cash_reserve_pct=_float("ALPACA_CASH_RESERVE_PCT", 0.20),
        )


def _client_headers(cfg: _AlpacaConfig) -> dict[str, str]:
    return {
        "APCA-API-KEY-ID": cfg.key,
        "APCA-API-SECRET-KEY": cfg.secret,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _http_request(
    method: str,
    path: str,
    cfg: _AlpacaConfig,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    """Synchronous HTTP via ``httpx`` (already a Vibe-Trading dep)."""
    import httpx

    url = f"{cfg.base_url}{path}"
    with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
        resp = client.request(
            method,
            url,
            headers=_client_headers(cfg),
            params=params,
            json=body,
        )
    try:
        payload: Any = resp.json()
    except Exception:
        payload = {"raw": resp.text[:500]}
    return resp.status_code, payload


def _err(message: str, **extra: Any) -> str:
    return json.dumps({"status": "error", "message": message, **extra}, ensure_ascii=False, indent=2)


def _ok(payload: Any, **extra: Any) -> str:
    body = {"status": "ok", **extra}
    if isinstance(payload, dict):
        body.update(payload)
    else:
        body["result"] = payload
    return json.dumps(body, ensure_ascii=False, indent=2)


def _missing_creds(cfg: _AlpacaConfig) -> str | None:
    if not cfg.key or not cfg.secret:
        return "Missing ALPACA_KEY / ALPACA_SECRET in env."
    return None


# ---------------------------------------------------------------------------
# Risk gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _GateResult:
    approved: bool
    reasons: tuple[str, ...]
    approved_notional_usd: float
    checks: tuple[dict[str, Any], ...]


def _evaluate_risk_gate(
    cfg: _AlpacaConfig,
    *,
    intended_notional_usd: float,
    symbol: str,
    cash_usd: float,
    equity_usd: float,
    existing_symbol_position_usd: float,
) -> _GateResult:
    """Deterministic risk gate. Mirrors CryptoBur's ``evaluatePortfolioGate``.

    Reductions cascade: each check may lower ``approved_notional_usd`` or
    reject outright. Final value is the maximum size we'll send to Alpaca.
    """
    reasons: list[str] = []
    checks: list[dict[str, Any]] = []
    approved = max(0.0, float(intended_notional_usd))

    # 1. Sacred read-only constraint
    if cfg.read_only:
        reasons.append("read_only_enabled")
        checks.append({"name": "read_only", "status": "fail",
                       "message": "ALPACA_READ_ONLY=1 — write operations disabled."})
        return _GateResult(False, tuple(reasons), 0.0, tuple(checks))
    checks.append({"name": "read_only", "status": "pass"})

    # 2. Kill switch
    if cfg.kill_switch:
        reasons.append("kill_switch_enabled")
        checks.append({"name": "kill_switch", "status": "fail",
                       "message": "ALPACA_KILL_SWITCH=1 — execution suspended."})
        return _GateResult(False, tuple(reasons), 0.0, tuple(checks))
    checks.append({"name": "kill_switch", "status": "pass"})

    # 3. Per-order notional cap
    per_order_cap = max(0.0, cfg.max_notional_usd)
    if approved > per_order_cap:
        checks.append({"name": "max_notional_usd", "status": "warn",
                       "message": f"Reduced to per-order cap ${per_order_cap:.2f}.",
                       "from": approved, "to": per_order_cap})
        approved = per_order_cap
    else:
        checks.append({"name": "max_notional_usd", "status": "pass",
                       "value": approved, "limit": per_order_cap})

    # 4. Cash reserve
    reserve_usd = max(0.0, equity_usd * cfg.cash_reserve_pct)
    spendable_cash = max(0.0, cash_usd - reserve_usd)
    if approved > spendable_cash:
        if spendable_cash > 0:
            checks.append({"name": "cash_reserve", "status": "warn",
                           "message": "Reduced to preserve cash reserve.",
                           "from": approved, "to": spendable_cash,
                           "reserve_usd": reserve_usd})
            approved = spendable_cash
        else:
            reasons.append("cash_reserve_violation")
            checks.append({"name": "cash_reserve", "status": "fail",
                           "message": "No spendable cash after reserve.",
                           "cash_usd": cash_usd, "reserve_usd": reserve_usd})
            approved = 0.0
    else:
        checks.append({"name": "cash_reserve", "status": "pass",
                       "value": approved, "limit": spendable_cash})

    # 5. Per-symbol position cap (incremental + existing must fit)
    symbol_cap = max(0.0, equity_usd * cfg.max_position_pct)
    room_for_symbol = max(0.0, symbol_cap - existing_symbol_position_usd)
    if approved > room_for_symbol:
        if room_for_symbol > 0:
            checks.append({"name": "max_position_pct", "status": "warn",
                           "message": f"Reduced to per-symbol cap for {symbol}.",
                           "from": approved, "to": room_for_symbol,
                           "symbol_cap": symbol_cap,
                           "existing_position_usd": existing_symbol_position_usd})
            approved = room_for_symbol
        else:
            reasons.append("position_cap_violation")
            checks.append({"name": "max_position_pct", "status": "fail",
                           "message": f"Symbol cap already filled for {symbol}.",
                           "symbol_cap": symbol_cap,
                           "existing_position_usd": existing_symbol_position_usd})
            approved = 0.0
    else:
        checks.append({"name": "max_position_pct", "status": "pass",
                       "value": approved, "limit": room_for_symbol})

    if approved <= 0 and not reasons:
        reasons.append("approved_notional_zero")

    return _GateResult(len(reasons) == 0 and approved > 0, tuple(reasons), approved, tuple(checks))


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


_EXECUTION_TOOL_NAMES = {"AlpacaPlaceOrderTool", "AlpacaClosePositionTool"}


def _execution_disabled() -> bool:
    """Honor sacred constraint: VIBE_TRADING_EXECUTION_DISABLED=1 hard-disables
    Place/Close tools so they're invisible to the agent registry. CryptoБур
    is the single account-owner once the signal pipe is live."""
    return os.getenv("VIBE_TRADING_EXECUTION_DISABLED", "0").strip().lower() in {"1", "true", "yes", "on"}


class _AlpacaToolBase(BaseTool):
    """Common availability check: require ALPACA_KEY/SECRET in env."""

    @classmethod
    def check_available(cls) -> bool:
        if cls.__name__ in _EXECUTION_TOOL_NAMES and _execution_disabled():
            return False
        return bool(os.getenv("ALPACA_KEY") and os.getenv("ALPACA_SECRET"))


class AlpacaAccountTool(_AlpacaToolBase):
    """Get Alpaca account snapshot: cash, equity, buying power, status."""

    name = "alpaca_account"
    description = (
        "Fetch the Alpaca account snapshot (cash, equity, buying_power, "
        "portfolio_value, status, pattern_day_trader, trading_blocked). "
        "Works with paper or live depending on ALPACA_BASE_URL."
    )
    parameters = {"type": "object", "properties": {}}
    repeatable = True

    def execute(self, **kwargs: Any) -> str:
        cfg = _AlpacaConfig.from_env()
        missing = _missing_creds(cfg)
        if missing:
            return _err(missing)
        code, payload = _http_request("GET", "/v2/account", cfg)
        if code != 200:
            return _err(f"Alpaca account request failed (HTTP {code}).", payload=payload)
        if not isinstance(payload, dict):
            return _err("Unexpected account payload.", payload=payload)
        slim = {
            "account_number": payload.get("account_number"),
            "status": payload.get("status"),
            "currency": payload.get("currency"),
            "cash": payload.get("cash"),
            "equity": payload.get("equity"),
            "portfolio_value": payload.get("portfolio_value"),
            "buying_power": payload.get("buying_power"),
            "pattern_day_trader": payload.get("pattern_day_trader"),
            "trading_blocked": payload.get("trading_blocked"),
            "transfers_blocked": payload.get("transfers_blocked"),
            "account_blocked": payload.get("account_blocked"),
            "base_url": cfg.base_url,
            "mode": "paper" if "paper" in cfg.base_url else "live",
        }
        return _ok(slim)


class AlpacaPositionsTool(_AlpacaToolBase):
    """List Alpaca open positions with quantities and unrealized P/L."""

    name = "alpaca_positions"
    description = (
        "List all open Alpaca positions: symbol, qty, avg_entry_price, "
        "market_value, unrealized_pl, unrealized_plpc, current_price."
    )
    parameters = {"type": "object", "properties": {}}
    repeatable = True

    def execute(self, **kwargs: Any) -> str:
        cfg = _AlpacaConfig.from_env()
        missing = _missing_creds(cfg)
        if missing:
            return _err(missing)
        code, payload = _http_request("GET", "/v2/positions", cfg)
        if code != 200:
            return _err(f"Alpaca positions request failed (HTTP {code}).", payload=payload)
        if not isinstance(payload, list):
            return _err("Unexpected positions payload.", payload=payload)
        slim = [
            {
                "symbol": p.get("symbol"),
                "asset_class": p.get("asset_class"),
                "side": p.get("side"),
                "qty": p.get("qty"),
                "avg_entry_price": p.get("avg_entry_price"),
                "current_price": p.get("current_price"),
                "market_value": p.get("market_value"),
                "unrealized_pl": p.get("unrealized_pl"),
                "unrealized_plpc": p.get("unrealized_plpc"),
            }
            for p in payload
        ]
        return _ok({"count": len(slim), "positions": slim})


class AlpacaOrdersTool(_AlpacaToolBase):
    """List recent Alpaca orders (default: last 20 across all statuses)."""

    name = "alpaca_orders"
    description = (
        "List recent Alpaca orders. Default returns the latest 20 across all "
        "statuses. Use status='open' or 'closed' to filter."
    )
    parameters = {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": "open / closed / all (default all)",
                "default": "all",
            },
            "limit": {"type": "integer", "default": 20, "description": "Max rows (default 20)."},
        },
    }
    repeatable = True

    def execute(self, **kwargs: Any) -> str:
        cfg = _AlpacaConfig.from_env()
        missing = _missing_creds(cfg)
        if missing:
            return _err(missing)
        status = str(kwargs.get("status", "all")).strip().lower()
        if status not in {"open", "closed", "all"}:
            status = "all"
        limit = max(1, min(int(kwargs.get("limit", 20)), 100))
        code, payload = _http_request(
            "GET", "/v2/orders", cfg,
            params={"status": status, "limit": limit, "direction": "desc"},
        )
        if code != 200:
            return _err(f"Alpaca orders request failed (HTTP {code}).", payload=payload)
        if not isinstance(payload, list):
            return _err("Unexpected orders payload.", payload=payload)
        slim = [
            {
                "id": o.get("id"),
                "symbol": o.get("symbol"),
                "side": o.get("side"),
                "type": o.get("type"),
                "qty": o.get("qty"),
                "notional": o.get("notional"),
                "status": o.get("status"),
                "submitted_at": o.get("submitted_at"),
                "filled_at": o.get("filled_at"),
                "filled_avg_price": o.get("filled_avg_price"),
                "filled_qty": o.get("filled_qty"),
            }
            for o in payload
        ]
        return _ok({"count": len(slim), "status_filter": status, "orders": slim})


def _account_state(cfg: _AlpacaConfig) -> tuple[float, float] | str:
    """Returns (cash_usd, equity_usd) or an error string."""
    code, payload = _http_request("GET", "/v2/account", cfg)
    if code != 200 or not isinstance(payload, dict):
        return _err(f"Account lookup failed (HTTP {code}).", payload=payload)
    try:
        return float(payload.get("cash", 0)), float(payload.get("equity", 0))
    except (TypeError, ValueError):
        return _err("Could not parse cash/equity.", payload=payload)


def _existing_position_value(cfg: _AlpacaConfig, symbol: str) -> float:
    code, payload = _http_request("GET", f"/v2/positions/{symbol.upper()}", cfg)
    if code == 200 and isinstance(payload, dict):
        try:
            return abs(float(payload.get("market_value", 0)))
        except (TypeError, ValueError):
            return 0.0
    return 0.0  # 404 = no position; treated as zero


class AlpacaPlaceOrderTool(_AlpacaToolBase):
    """Submit a market or limit order through the risk gate.

    Refuses to execute when ALPACA_READ_ONLY=1 or kill switch is on.
    Sizes are clamped by per-order cap, cash reserve, and per-symbol cap.
    Pass ``notional`` (USD amount) — qty is computed by Alpaca server-side.
    """

    name = "alpaca_place_order"
    description = (
        "Submit a paper/live order on Alpaca with risk-gate enforcement. "
        "Specify notional in USD (preferred) or qty. The gate rejects when "
        "ALPACA_READ_ONLY=1 or kill switch is on, and clamps size to fit "
        "per-order, cash-reserve, and per-symbol caps."
    )
    parameters = {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Alpaca symbol, e.g. AAPL, BTC/USD."},
            "side": {"type": "string", "description": "'buy' or 'sell'."},
            "notional": {
                "type": "number",
                "description": "Order notional in USD (preferred for fractional / crypto).",
            },
            "qty": {
                "type": "number",
                "description": "Order quantity in shares/units (alternative to notional).",
            },
            "type": {
                "type": "string",
                "description": "'market' (default) or 'limit'.",
                "default": "market",
            },
            "limit_price": {
                "type": "number",
                "description": "Required when type='limit'.",
            },
            "time_in_force": {
                "type": "string",
                "description": "'day' (default), 'gtc', 'ioc'.",
                "default": "day",
            },
            "client_order_id": {
                "type": "string",
                "description": "Optional idempotency key.",
            },
        },
        "required": ["symbol", "side"],
    }
    repeatable = True

    def execute(self, **kwargs: Any) -> str:
        cfg = _AlpacaConfig.from_env()
        missing = _missing_creds(cfg)
        if missing:
            return _err(missing)

        symbol = str(kwargs.get("symbol", "")).strip().upper()
        side = str(kwargs.get("side", "")).strip().lower()
        if not symbol or side not in {"buy", "sell"}:
            return _err("symbol and side='buy'|'sell' are required.")

        order_type = str(kwargs.get("type", "market")).strip().lower()
        if order_type not in {"market", "limit"}:
            return _err("type must be 'market' or 'limit'.")
        time_in_force = str(kwargs.get("time_in_force", "day")).strip().lower()

        intended_notional = float(kwargs.get("notional") or 0.0)
        qty = kwargs.get("qty")
        if intended_notional <= 0 and qty is None:
            return _err("Provide notional (USD) or qty.")

        # If qty given without notional, estimate notional from latest trade.
        if intended_notional <= 0 and qty is not None:
            code, ptrade = _http_request(
                "GET", f"/v2/stocks/{symbol}/trades/latest", cfg,
            )
            price = 0.0
            if code == 200 and isinstance(ptrade, dict):
                try:
                    price = float(ptrade.get("trade", {}).get("p", 0))
                except (TypeError, ValueError):
                    price = 0.0
            intended_notional = float(qty) * price
            if intended_notional <= 0:
                return _err("Could not estimate notional from qty; pass notional explicitly.")

        # Pre-trade snapshot for risk gate.
        acct = _account_state(cfg)
        if isinstance(acct, str):
            return acct
        cash_usd, equity_usd = acct
        held_usd = _existing_position_value(cfg, symbol) if side == "buy" else 0.0

        gate = _evaluate_risk_gate(
            cfg,
            intended_notional_usd=intended_notional,
            symbol=symbol,
            cash_usd=cash_usd,
            equity_usd=equity_usd,
            existing_symbol_position_usd=held_usd,
        )

        if not gate.approved:
            return _err(
                "Risk gate rejected the order.",
                gate={
                    "approved": False,
                    "reasons": list(gate.reasons),
                    "approved_notional_usd": gate.approved_notional_usd,
                    "checks": list(gate.checks),
                },
                intended_notional_usd=intended_notional,
                snapshot={"cash_usd": cash_usd, "equity_usd": equity_usd,
                          "existing_position_usd_for_symbol": held_usd},
            )

        body: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "time_in_force": time_in_force,
        }
        # Send notional unless an explicit qty was approved by gate equivalent.
        body["notional"] = round(gate.approved_notional_usd, 2)
        if order_type == "limit":
            lp = kwargs.get("limit_price")
            if lp is None:
                return _err("limit_price required when type='limit'.")
            body["limit_price"] = float(lp)
            # Alpaca limit orders don't accept notional — switch to qty estimate.
            est_qty = round(gate.approved_notional_usd / float(lp), 4)
            if est_qty <= 0:
                return _err("Estimated qty after limit_price is zero — increase notional or lower price.")
            body.pop("notional", None)
            body["qty"] = est_qty
        if kwargs.get("client_order_id"):
            body["client_order_id"] = str(kwargs["client_order_id"])[:128]

        code, payload = _http_request("POST", "/v2/orders", cfg, body=body)
        if code not in (200, 201):
            return _err(
                f"Alpaca order submission failed (HTTP {code}).",
                payload=payload,
                gate={"approved_notional_usd": gate.approved_notional_usd,
                      "checks": list(gate.checks)},
            )
        return _ok(
            {
                "submitted": payload,
                "gate": {
                    "approved": True,
                    "approved_notional_usd": gate.approved_notional_usd,
                    "checks": list(gate.checks),
                },
            }
        )


class AlpacaClosePositionTool(_AlpacaToolBase):
    """Flatten an open Alpaca position by symbol. Honors read_only/kill_switch."""

    name = "alpaca_close_position"
    description = (
        "Close (flatten) an open Alpaca position by symbol. Submits a market "
        "sell/cover sized to the full current quantity. Rejected when "
        "ALPACA_READ_ONLY=1 or kill switch is on."
    )
    parameters = {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Symbol to flatten, e.g. AAPL."},
        },
        "required": ["symbol"],
    }
    repeatable = True

    def execute(self, **kwargs: Any) -> str:
        cfg = _AlpacaConfig.from_env()
        missing = _missing_creds(cfg)
        if missing:
            return _err(missing)
        if cfg.read_only:
            return _err("ALPACA_READ_ONLY=1 — close_position disabled.",
                        gate={"reasons": ["read_only_enabled"]})
        if cfg.kill_switch:
            return _err("ALPACA_KILL_SWITCH=1 — execution suspended.",
                        gate={"reasons": ["kill_switch_enabled"]})

        symbol = str(kwargs.get("symbol", "")).strip().upper()
        if not symbol:
            return _err("symbol is required.")
        code, payload = _http_request("DELETE", f"/v2/positions/{symbol}", cfg)
        if code not in (200, 207):
            return _err(f"Alpaca close_position failed (HTTP {code}).", payload=payload)
        return _ok({"closed": payload})
