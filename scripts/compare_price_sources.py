#!/usr/bin/env python3
"""Deterministic FMP-vs-Robinhood price cross-check (verifier-only; never picks a source silently).

Compares the FMP primary quote against the Robinhood verifier quote ONLY when they are genuinely
comparable: same symbol, same target session date, comparable trading session (NEVER premarket vs
previous_close), and both timestamps valid/fresh within a bounded window. It computes absolute /
percentage / timestamp differences + the RH spread in bps and returns a verdict:

  PRICE_CROSS_VERIFIED        — within the documented divergence threshold; price independently verified
  ROBINHOOD_PRICE_DISAGREEMENT — exceeds threshold; BLOCK Buy, permit Watch/No-Trade, disclose
  PRIMARY_STALE_SECONDARY_LIVE_NOT_COMPARABLE
                              — the FMP PRIMARY is stale (previous_close/eod) while the Robinhood
                                SECONDARY is a fresh live session (premarket/regular). The two are
                                explicitly NOT from a comparable session, so NO divergence % is
                                computed as an equivalent-session confirmation and NO source is
                                silently selected. Both timestamps/sessions are reported for context;
                                Buy is BLOCKED and Official status is unchanged (the stale primary is
                                still stale). Preliminary reporting may use the RH quote as LIVE
                                supplemental context only.
  NOT_COMPARABLE              — different session / missing-or-stale input; price NOT verified -> no Buy

It NEVER chooses a "winner": a disagreement or non-comparability blocks Buy; it does not let RH
override FMP or vice-versa. FMP remains the deterministic primary. A fresh secondary NEVER makes a
stale primary fresh and NEVER creates Official eligibility.

Std-lib only (optional PyYAML). CLI:
  compare_price_sources.py --fmp FMP.json --rh RH_SNAPSHOT.json --target YYYY-MM-DD
  (FMP.json = a data_preflight snapshot OR a {price,market_timestamp,vintage} object; RH = the
   sanitized validate_robinhood_snapshot.py artifact.)
"""
from __future__ import annotations
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import tzutil  # noqa: E402

CONFIG = ROOT / "config" / "robinhood_verifier.yaml"
DEFAULTS = {"divergence_block_pct": 0.0050, "max_timestamp_diff_minutes": 15,
            "require_same_session": True, "max_fmp_age_minutes": 90}

VERDICT_PASS = "PRICE_CROSS_VERIFIED"
VERDICT_DISAGREE = "ROBINHOOD_PRICE_DISAGREEMENT"
VERDICT_NOT_COMPARABLE = "NOT_COMPARABLE"
VERDICT_PRIMARY_STALE_SECONDARY_LIVE = "PRIMARY_STALE_SECONDARY_LIVE_NOT_COMPARABLE"

# Normalized session buckets that count as a fresh, tradable LIVE session for the SECONDARY.
_LIVE_SESSIONS = {"premarket", "regular"}
# Normalized session buckets that mean the PRIMARY quote is stale (not same-day live).
_STALE_SESSIONS = {"prior"}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        import yaml  # type: ignore
        cc = (yaml.safe_load(CONFIG.read_text()) or {}).get("cross_check", {}) or {}
        cfg["divergence_block_pct"] = float(cc.get("divergence_block_pct", cfg["divergence_block_pct"]))
        cfg["max_timestamp_diff_minutes"] = float(cc.get("max_timestamp_diff_minutes", cfg["max_timestamp_diff_minutes"]))
        cfg["require_same_session"] = bool(cc.get("require_same_session", cfg["require_same_session"]))
        cfg["max_fmp_age_minutes"] = float(cc.get("max_fmp_age_minutes", cfg["max_fmp_age_minutes"]))
    except Exception:
        pass
    return cfg


_SESSION = {"regular": "regular", "regular_or_eod": "regular", "premarket": "premarket",
            "afterhours": "afterhours", "previous_close": "prior", "eod": "prior",
            "stale_prior_session": "prior"}


def _norm_session(v):
    return _SESSION.get(str(v or "").lower())


def _num(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def _parse_et_or_iso(ts):
    """Accept an ISO8601 (with tz) OR an ET 'YYYY-MM-DD HH:MM:SS EDT/EST' string; return UTC dt."""
    if not isinstance(ts, str) or not ts.strip():
        return None
    s = ts.strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            return tzutil.to_utc(dt)
    except Exception:
        pass
    # ET-stamped "2026-06-30 08:45:00 EDT"
    for suf in (" EDT", " EST", " UTC"):
        if s.endswith(suf):
            try:
                base = datetime.strptime(s[: -len(suf)].strip(), "%Y-%m-%d %H:%M:%S")
                tz = tzutil.ET if suf in (" EDT", " EST") else tzutil.UTC
                return tzutil.to_utc(base.replace(tzinfo=tz))
            except Exception:
                return None
    return None


def compare(fmp: dict, rh: dict, target: str, cfg: dict, now: datetime | None = None) -> dict:
    now = now or tzutil.now_utc()
    fmp_price = _num(fmp.get("price")) if fmp else None
    fmp_ts = _parse_et_or_iso((fmp or {}).get("market_timestamp"))
    fmp_sess = _norm_session((fmp or {}).get("vintage"))
    rhq = (rh or {}).get("spy_quote", {}) or {}
    rh_price = _num(rhq.get("last"))
    rh_bid, rh_ask = _num(rhq.get("bid")), _num(rhq.get("ask"))
    rh_ts = _parse_et_or_iso(rhq.get("market_timestamp_utc") or rhq.get("market_timestamp_et"))
    rh_sess = _norm_session(rhq.get("vintage"))
    rh_status = (rh or {}).get("status")

    # ABSOLUTE FMP freshness — NOT the vintage label alone. The rest of the system defines "stale
    # primary" as `same_day == False`, which requires the market timestamp to be within the age cap
    # (providers.py) AND an Official morning vintage. A quote can carry a premarket/regular vintage
    # LABEL yet be age-stale (feed lag); trusting the label alone would let such a stale primary be
    # cross-VERIFIED against a fresh secondary. So we treat the primary as stale when its own
    # `same_day` flag is False, OR the vintage is a prior-session vintage, OR its market timestamp is
    # older than the age cap. (Consistent with data_preflight / snapshot_gate freshness.)
    cap = cfg.get("max_fmp_age_minutes", 90)
    fmp_age_min = ((tzutil.to_utc(now) - tzutil.to_utc(fmp_ts)).total_seconds() / 60.0
                   if fmp_ts is not None else None)
    _sd = fmp.get("same_day") if isinstance(fmp, dict) else None
    if _sd is True:
        fmp_stale = False
    elif _sd is False:
        fmp_stale = True
    else:
        fmp_stale = (fmp_sess in _STALE_SESSIONS) or (fmp_age_min is not None and fmp_age_min > cap)

    # PRIMARY STALE (checked FIRST, by absolute freshness). A stale FMP primary can NEVER be
    # cross-VERIFIED — no divergence % is computed as confirmation and no source is auto-selected.
    #   * If the Robinhood secondary is a genuinely FRESH LIVE quote (status PASSED freshness —
    #     VERIFIED or ACCOUNT_CONTEXT_UNAVAILABLE — and a live session), return the specific
    #     PRIMARY_STALE_SECONDARY_LIVE verdict with both timestamps/sessions + RH spread for context.
    #   * Otherwise (secondary missing/stale/unavailable), return NOT_COMPARABLE naming the stale
    #     primary. Either way Buy is BLOCKED and Official is untouched (the stale primary stays stale).
    _RH_FRESH_LIVE = {"ROBINHOOD_VERIFIED", "ROBINHOOD_ACCOUNT_CONTEXT_UNAVAILABLE"}
    if fmp_stale and fmp_price and fmp_price > 0 and fmp_ts is not None:
        secondary_live = (rh_sess in _LIVE_SESSIONS and rh_status in _RH_FRESH_LIVE
                          and rh_price and rh_price > 0 and rh_ts is not None)
        if secondary_live:
            rh_spread_bps = round(((rh_ask - rh_bid) / ((rh_ask + rh_bid) / 2.0)) * 10000, 2) \
                if (rh_bid and rh_ask and (rh_ask + rh_bid) > 0) else None
            fmp_mt = (fmp or {}).get("market_timestamp")
            rh_mt = rhq.get("market_timestamp_utc") or rhq.get("market_timestamp_et")
            return _verdict(VERDICT_PRIMARY_STALE_SECONDARY_LIVE,
                            reasons=[f"primary_stale(vintage={fmp_sess},age={None if fmp_age_min is None else round(fmp_age_min,1)})_secondary_live({rh_sess},{rh_status})"],
                            block_buy=True, comparable=False, price_divergence_computed=False, pct_diff=None,
                            fmp_price=fmp_price, rh_price=rh_price,
                            fmp_session=fmp_sess, rh_session=rh_sess,
                            fmp_market_timestamp=fmp_mt, rh_market_timestamp=rh_mt,
                            rh_spread_bps=rh_spread_bps,
                            note_override="Primary (FMP) is STALE; secondary (Robinhood) is a fresh live "
                                          "session. NOT comparable: no divergence confirmation, no source "
                                          "auto-selected, Buy BLOCKED, Official unchanged. RH is LIVE "
                                          "supplemental context only and never rescues the stale primary.")
        return _verdict(VERDICT_NOT_COMPARABLE,
                        reasons=[f"fmp_primary_stale(vintage={fmp_sess},age={None if fmp_age_min is None else round(fmp_age_min,1)})"],
                        block_buy=True, fmp_price=fmp_price, rh_price=rh_price)

    reasons = []
    if rh_status and rh_status != "ROBINHOOD_VERIFIED":
        reasons.append(f"rh_not_verified({rh_status})")
    if fmp_price is None or fmp_price <= 0:
        reasons.append("fmp_price_missing")
    if rh_price is None or rh_price <= 0:
        reasons.append("rh_price_missing")
    if fmp_ts is None:
        reasons.append("fmp_timestamp_missing")
    if rh_ts is None:
        reasons.append("rh_timestamp_missing")
    if reasons:
        return _verdict(VERDICT_NOT_COMPARABLE, reasons=reasons, block_buy=True)

    # session comparability — NEVER compare premarket vs previous_close as equivalent
    if cfg["require_same_session"] and fmp_sess != rh_sess:
        return _verdict(VERDICT_NOT_COMPARABLE,
                        reasons=[f"session_mismatch(fmp={fmp_sess},rh={rh_sess})"], block_buy=True,
                        fmp_price=fmp_price, rh_price=rh_price)

    ts_diff_min = abs((fmp_ts - rh_ts).total_seconds()) / 60.0
    if ts_diff_min > cfg["max_timestamp_diff_minutes"]:
        return _verdict(VERDICT_NOT_COMPARABLE,
                        reasons=[f"timestamp_diff_too_large({ts_diff_min:.1f}m>{cfg['max_timestamp_diff_minutes']}m)"],
                        block_buy=True, fmp_price=fmp_price, rh_price=rh_price, ts_diff_min=round(ts_diff_min, 2))

    mid = (fmp_price + rh_price) / 2.0
    abs_diff = abs(fmp_price - rh_price)
    pct_diff = abs_diff / mid if mid > 0 else 1.0
    rh_spread_bps = round(((rh_ask - rh_bid) / ((rh_ask + rh_bid) / 2.0)) * 10000, 2) if (rh_bid and rh_ask) else None

    if pct_diff > cfg["divergence_block_pct"]:
        return _verdict(VERDICT_DISAGREE,
                        reasons=[f"divergence({pct_diff:.4%}>{cfg['divergence_block_pct']:.2%})"],
                        block_buy=True, fmp_price=fmp_price, rh_price=rh_price, abs_diff=round(abs_diff, 4),
                        pct_diff=round(pct_diff, 6), ts_diff_min=round(ts_diff_min, 2), rh_spread_bps=rh_spread_bps,
                        session=fmp_sess)
    return _verdict(VERDICT_PASS, reasons=[], block_buy=False, fmp_price=fmp_price, rh_price=rh_price,
                    abs_diff=round(abs_diff, 4), pct_diff=round(pct_diff, 6), ts_diff_min=round(ts_diff_min, 2),
                    rh_spread_bps=rh_spread_bps, session=fmp_sess)


def _verdict(verdict, *, reasons, block_buy, **extra):
    note = extra.pop("note_override", None) or (
        "Verifier-only: a disagreement or non-comparability BLOCKS Buy (Watch/No-Trade ok); "
        "no source is silently chosen; FMP remains the deterministic primary.")
    out = {"verdict": verdict, "price_cross_verified": verdict == VERDICT_PASS,
           "block_buy": block_buy, "reasons": reasons, "note": note}
    out.update(extra)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="FMP-vs-Robinhood price cross-check (verifier-only).")
    ap.add_argument("--fmp", required=True, help="FMP snapshot JSON or {price,market_timestamp,vintage}.")
    ap.add_argument("--rh", required=True, help="Sanitized Robinhood verifier artifact JSON.")
    ap.add_argument("--target", required=True)
    ap.add_argument("--symbol", default="SPY")
    args = ap.parse_args()
    cfg = load_config()
    fmp_raw = json.loads(Path(args.fmp).read_text())
    rh = json.loads(Path(args.rh).read_text())
    # Extract the FMP anchor quote from a full snapshot, or accept a bare quote object.
    if "fields" in fmp_raw:
        fld = next((f for f in fmp_raw["fields"] if str(f.get("symbol", "")).upper() == args.symbol
                    and f.get("category") == "index_quote" and f.get("status") == "ok"), None)
        # Carry the authoritative `same_day` flag through so compare judges staleness by real
        # freshness (age + vintage), not the vintage LABEL alone (feed-lag anomaly defense).
        fmp = {"price": (fld or {}).get("value"), "market_timestamp": (fld or {}).get("market_timestamp"),
               "vintage": (fld or {}).get("vintage"), "same_day": (fld or {}).get("same_day")} if fld else {}
    else:
        fmp = fmp_raw
    res = compare(fmp, rh, args.target, cfg)
    print(json.dumps(res, indent=2, sort_keys=True))
    return 0 if res["verdict"] == VERDICT_PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
