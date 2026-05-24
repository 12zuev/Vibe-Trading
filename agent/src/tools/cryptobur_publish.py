"""Publish a research signal from Vibe-Trading → CryptoБур /api/cron/inject-signal.

Sacred role separation (see docs/PIPE_DESIGN.md):
  - Vibe-Trading produces SIGNALS (research artifact with provenance + expiry).
  - CryptoБур consumes signals through its conflict resolver and synthesizes
    DECISIONS that route through portfolio gate → broker.

This tool is the publisher contract. It:
  1. Validates the structured fields locally
  2. Computes deterministic signal_id = sha256(source|run_id|ticker|horizon)
  3. Computes payload_hash for tamper-evident transport
  4. POSTs to CRYPTOBUR_SIGNAL_INGEST_URL with bearer auth

Idempotency: identical payload returns persisted=false. Retries are safe.

Env vars required:
  CRYPTOBUR_SIGNAL_INGEST_URL     — endpoint URL
  CRYPTOBUR_SIGNAL_INGEST_SECRET  — bearer token
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from typing import Any

from src.agent.tools import BaseTool


SOURCE = "vibe-trading"
DEFAULT_TTL_HOURS = 1  # signal valid for 1h by default — tunable per call


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _canonical_payload_json(p: dict[str, Any]) -> str:
    """Mirror the server's canonical-JSON computation in signal-ingest.ts."""
    ordered = {
        "conviction": p.get("conviction"),
        "direction": p["direction"],
        "horizon_hours": p["horizon_hours"],
        "model_version": p.get("model_version"),
        "observed_market_ts": p["observed_market_ts"],
        "probability_up_horizon": p.get("probability_up_horizon"),
        "prompt_version": p.get("prompt_version"),
        "risk_factors": p.get("risk_factors"),
        "source": p["source"],
        "source_preset": p.get("source_preset"),
        "source_run_id": p["source_run_id"],
        "thesis": p.get("thesis"),
        "ticker": p["ticker"],
    }
    # Match JS JSON.stringify behaviour: no separators padding
    return json.dumps(ordered, separators=(",", ":"), ensure_ascii=False)


def _err(message: str, **extra: Any) -> str:
    return json.dumps({"status": "error", "message": message, **extra}, ensure_ascii=False, indent=2)


def _ok(payload: Any, **extra: Any) -> str:
    body = {"status": "ok", **extra}
    if isinstance(payload, dict):
        body.update(payload)
    else:
        body["result"] = payload
    return json.dumps(body, ensure_ascii=False, indent=2)


class CryptoburPublishSignalTool(BaseTool):
    """Publish a research signal from Vibe-Trading to CryptoБур execution pipeline.

    Server validates: schema, signal_id determinism, payload_hash, freshness,
    and applies per-source rate limit. Returns accepted with persisted flag.
    """

    name = "cryptobur_publish_signal"
    description = (
        "Publish a research signal to CryptoБур's execution pipeline via "
        "POST /api/cron/inject-signal. The signal becomes an opt-in input for "
        "users who enabled this source. CryptoБур synthesizes the final "
        "decision via its conflict resolver (native analysts ∪ external sources)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "Ticker symbol (e.g. BTC-USD, AAPL, NVDA). Will be uppercased.",
            },
            "direction": {
                "type": "string",
                "enum": ["long", "short", "flat"],
                "description": "Directional bias for the horizon. 'flat' = no actionable signal.",
            },
            "conviction": {
                "type": "integer",
                "description": "1-5; how strong is the setup quality.",
                "minimum": 1,
                "maximum": 5,
            },
            "horizon_hours": {
                "type": "integer",
                "description": "Horizon over which the directional view applies, in hours.",
                "minimum": 1,
                "maximum": 8760,
            },
            "thesis": {
                "type": "string",
                "description": "Short markdown explanation. Used by audit + UI; keep concise.",
            },
            "probability_up_horizon": {
                "type": "number",
                "description": "Calibrated P(price up over horizon), [0,1]. Optional but improves resolver.",
            },
            "risk_factors": {
                "type": "array",
                "description": "List of 1-line risk factor strings.",
                "items": {"type": "string"},
            },
            "ttl_hours": {
                "type": "integer",
                "description": f"How long the signal is valid for execution (default {DEFAULT_TTL_HOURS}h, max 24h).",
            },
            "source_run_id": {
                "type": "string",
                "description": "Optional swarm run id; auto-generated if omitted.",
            },
            "source_preset": {
                "type": "string",
                "description": "Optional preset name that produced this signal.",
            },
        },
        "required": ["ticker", "direction", "horizon_hours", "thesis"],
    }
    repeatable = True

    @classmethod
    def check_available(cls) -> bool:
        return bool(
            os.getenv("CRYPTOBUR_SIGNAL_INGEST_URL")
            and os.getenv("CRYPTOBUR_SIGNAL_INGEST_SECRET")
        )

    def execute(self, **kwargs: Any) -> str:
        import httpx

        url = os.getenv("CRYPTOBUR_SIGNAL_INGEST_URL", "").strip()
        secret = os.getenv("CRYPTOBUR_SIGNAL_INGEST_SECRET", "").strip()
        if not url or not secret:
            return _err("Missing CRYPTOBUR_SIGNAL_INGEST_URL or CRYPTOBUR_SIGNAL_INGEST_SECRET in env.")

        ticker = str(kwargs.get("ticker", "")).strip().upper()
        direction = str(kwargs.get("direction", "")).strip().lower()
        thesis = str(kwargs.get("thesis", "")).strip()
        if not ticker or direction not in {"long", "short", "flat"} or not thesis:
            return _err("ticker, direction (long|short|flat), thesis are required.")

        try:
            horizon_hours = int(kwargs["horizon_hours"])
        except (KeyError, TypeError, ValueError):
            return _err("horizon_hours must be a positive integer.")
        if horizon_hours < 1 or horizon_hours > 8760:
            return _err("horizon_hours out of [1, 8760].")

        conviction = kwargs.get("conviction")
        if conviction is not None:
            try:
                conviction = int(conviction)
            except (TypeError, ValueError):
                return _err("conviction must be int 1-5.")
            if conviction < 1 or conviction > 5:
                return _err("conviction out of [1, 5].")

        prob = kwargs.get("probability_up_horizon")
        if prob is not None:
            try:
                prob = float(prob)
            except (TypeError, ValueError):
                return _err("probability_up_horizon must be a number in [0,1].")
            if prob < 0 or prob > 1:
                return _err("probability_up_horizon out of [0,1].")

        risk_factors = kwargs.get("risk_factors")
        if risk_factors is not None:
            if not isinstance(risk_factors, list) or not all(isinstance(s, str) for s in risk_factors):
                return _err("risk_factors must be a list of strings.")

        ttl_hours = int(kwargs.get("ttl_hours") or DEFAULT_TTL_HOURS)
        if ttl_hours < 1 or ttl_hours > 24:
            return _err("ttl_hours out of [1, 24].")

        now_sec = int(time.time())
        observed_market_ts = now_sec
        expires_at = now_sec + ttl_hours * 3600

        source_run_id = str(kwargs.get("source_run_id") or f"adhoc-{uuid.uuid4().hex[:12]}")
        source_preset = kwargs.get("source_preset") or None
        model_version = os.getenv("LANGCHAIN_MODEL_NAME") or None
        prompt_version = kwargs.get("prompt_version") or None

        # Compute deterministic IDs
        id_basis = f"{SOURCE}|{source_run_id}|{ticker}|{horizon_hours}"
        signal_id = _sha256_hex(id_basis)

        body_for_hash = {
            "source": SOURCE,
            "source_run_id": source_run_id,
            "source_preset": source_preset,
            "ticker": ticker,
            "direction": direction,
            "probability_up_horizon": prob,
            "conviction": conviction,
            "horizon_hours": horizon_hours,
            "observed_market_ts": observed_market_ts,
            "thesis": thesis,
            "risk_factors": risk_factors,
            "model_version": model_version,
            "prompt_version": prompt_version,
        }
        payload_hash = _sha256_hex(_canonical_payload_json(body_for_hash))

        post_body = {
            **body_for_hash,
            "signal_id": signal_id,
            "expires_at": expires_at,
            "payload_hash": payload_hash,
        }

        try:
            with httpx.Client(timeout=15) as client:
                resp = client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {secret}",
                        "Content-Type": "application/json",
                    },
                    json=post_body,
                )
        except Exception as e:
            return _err(f"HTTP transport error: {e}")

        try:
            server_payload = resp.json()
        except Exception:
            server_payload = {"raw": resp.text[:500]}

        if resp.status_code not in (200, 202):
            return _err(
                f"CryptoБур rejected signal (HTTP {resp.status_code}).",
                server=server_payload,
                signal_id=signal_id,
            )

        return _ok(
            {
                "server": server_payload,
                "signal_id": signal_id,
                "ticker": ticker,
                "direction": direction,
                "horizon_hours": horizon_hours,
                "expires_at": expires_at,
            }
        )
