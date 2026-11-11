"""Multi-symbol, multi-timeframe market scanner (spec Part 6.3 workflow)."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.claude_agent import AgentError, ClaudeAgent
from app.analysis import regime as rg
from app.analysis import sessions as ss
from app.core.config import get_settings
from app.db.models import (AgentConversation, AgentDecision, AnalysisResult, ClosedPosition, Strategy,
                           StrategyConfiguration, StrategyPerformance, Symbol, TradeOrder, TradingSignal)
from app.db.session import session_scope
from app.execution.bridge import BridgeClient
from app.notifications.service import notify
from app.signals.engine import Calibrator, MarketContext, SignalConfig, build_signal
from app.strategies import registry
from app.strategies.base import CAP_REAL_VOLUME, CAP_TICK_VOLUME, StrategyContext
from app.services.events import agent_activity, publish

log = logging.getLogger("scanner")
UTC = timezone.utc
HTF_OF = {"M1": "M15", "M5": "H1", "M15": "H4", "M30": "H4", "H1": "D1", "H4": "D1", "D1": "W1", "W1": "MN1"}


def bars_to_df(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df.index = pd.to_datetime(df["time"], unit="s", utc=True)
    df["volume"] = df["real_volume"] if "real_volume" in df and df["real_volume"].sum() > 0 else df.get("tick_volume", 0)
    return df[["open", "high", "low", "close", "volume"] + [c for c in ("spread", "tick_volume", "real_volume") if c in df]]


def drop_forming_bar(df: pd.DataFrame, tf_minutes: int, now: datetime) -> pd.DataFrame:
    """MT5 returns the still-forming bar last; strategies must only see closed bars."""
    if len(df) and df.index[-1] + pd.Timedelta(minutes=tf_minutes) > pd.Timestamp(now):
        return df.iloc[:-1]
    return df


def enabled_strategies(db: Session, mode: str) -> dict[str, dict]:
    out = {}
    rows = {s.code: s for s in db.scalars(select(Strategy))}
    cfgs = {c.strategy_id: c for c in db.scalars(select(StrategyConfiguration).where(
        StrategyConfiguration.account_mode == mode, StrategyConfiguration.symbol_id.is_(None)))}
    for code in registry.all_strategies():
        s = rows.get(code)
        if s is None or not s.enabled or s.stage != "PRODUCTION":
            continue
        cfg = cfgs.get(s.id)
        if cfg is not None and not cfg.enabled:
            continue
        out[code] = (cfg.params if cfg else {}) or {}
    return out


def strategy_stats(db: Session) -> dict[str, dict]:
    """OOS / walk-forward / paper / live evidence per strategy (backtest IS results are excluded)."""
    out: dict[str, dict] = {}
    q = select(Strategy.code, StrategyPerformance).join(Strategy, Strategy.id == StrategyPerformance.strategy_id) \
        .where(StrategyPerformance.source.in_(["OOS", "WALKFORWARD", "PAPER", "LIVE"]))
    for code, p in db.execute(q):
        d = out.setdefault(code, {"trades": 0, "wins": 0, "gp": 0.0, "gl": 0.0})
        d["trades"] += p.trades
        d["wins"] += p.wins
        d["gp"] += p.gross_profit
        d["gl"] += p.gross_loss
    for d in out.values():
        d["win_rate"] = d["wins"] / d["trades"] if d["trades"] else None
        d["profit_factor"] = d["gp"] / d["gl"] if d["gl"] else None
    return out


def calibrator(db: Session) -> Calibrator:
    """Fit score -> win probability on realised outcomes of executed signals (demo/paper/live):
    ClosedPosition.execution_id -> TradeOrder.signal_id -> TradingSignal.score."""
    rows = db.execute(select(TradingSignal.score, ClosedPosition.profit)
                      .join(TradeOrder, TradeOrder.signal_id == TradingSignal.id)
                      .join(ClosedPosition, ClosedPosition.execution_id == TradeOrder.execution_id)).all()
    if not rows:
        return Calibrator()
    return Calibrator.fit([float(s) for s, _ in rows], [p > 0 for _, p in rows])


def analyse(db: Session, bridge: BridgeClient, sym: Symbol, tf: str, enabled: dict[str, dict], stats: dict,
            calib: Calibrator, cfg: SignalConfig) -> dict | None:
    s = get_settings()
    now = datetime.now(UTC)
    from app.signals.engine import TF_MINUTES
    df = drop_forming_bar(bars_to_df(bridge.rates(sym.broker_symbol, tf, s.scan_bars)), TF_MINUTES[tf], now)
    if len(df) < 120:
        return None
    htf_tf = HTF_OF.get(tf)
    htf = {}
    if htf_tf:
        h = drop_forming_bar(bars_to_df(bridge.rates(sym.broker_symbol, htf_tf, 300)), TF_MINUTES[htf_tf], now)
        if len(h):
            htf[htf_tf] = h
    tick = bridge.tick(sym.broker_symbol)
    caps = {CAP_TICK_VOLUME}
    if "real_volume" in df and df["real_volume"].sum() > 0:
        caps.add(CAP_REAL_VOLUME)
    ctx = StrategyContext(df, sym.broker_symbol, tf, htf=htf, point=float(sym.point), data_caps=caps)
    reg = rg.classify(df)
    sigs, diag = registry.evaluate_all(ctx, enabled)
    from app.news.service import status_for
    news, _ = status_for(db, sym, sym.broker_symbol)
    metas = registry.all_strategies()
    spread = (float(tick.get("ask", 0)) - float(tick.get("bid", 0))) if tick else 0.0
    mctx = MarketContext(sym.broker_symbol, tf, float(tick.get("bid") or df.close.iloc[-1]), df.index[-1].to_pydatetime(),
                         ctx.atr_last(), spread, reg.primary, reg.tags, ctx.htf_bias(), ss.current_sessions(pd.Timestamp(now)),
                         news, {k: metas[k].regimes for k in enabled}, stats)
    res = build_signal(sigs, mctx, cfg, calib)
    ar = AnalysisResult(symbol_id=sym.id, timeframe=tf, bar_time=df.index[-1].to_pydatetime(), regime=reg.primary,
                        structure={"regime": reg.dict(), "htf_bias": mctx.htf_bias, "sessions": mctx.sessions,
                                   "news": news, "atr": mctx.atr, "price": mctx.price,
                                   "spread_points": round(spread / float(sym.point), 2) if float(sym.point) else None},
                        strategy_outputs=[s.dict() for s in sigs] + [d for d in diag if d["status"] == "error"],
                        score=res.dict())
    db.add(ar)
    db.flush()
    agent_activity("market_scanner", "evaluating", f"{sym.broker_symbol} {tf}: {len(sigs)} setups, "
                                                   f"{res.decision} {res.grade} ({res.score})")
    return {"analysis": ar, "result": res, "symbol": sym, "regime": reg, "price": mctx.price, "spread": spread}


def persist_signal(db: Session, item: dict) -> TradingSignal | None:
    r = item["result"]
    if r.decision not in ("BUY", "SELL", "WAIT"):
        return None
    sig = TradingSignal(symbol_id=item["symbol"].id, analysis_id=item["analysis"].id, decision=r.decision, grade=r.grade,
                        timeframe=r.timeframe, entry=r.entry, stop_loss=r.stop_loss, tp1=r.tp1, tp2=r.tp2, tp3=r.tp3,
                        risk_reward=r.risk_reward, score=r.score, calibrated_confidence=r.calibrated_confidence,
                        confidence_sample=r.confidence_sample, strategies=r.strategies, regime=r.regime,
                        explanation=r.explanation, expires_at=r.expires_at)
    db.add(sig)
    db.flush()
    publish("signal", {"id": sig.id, "symbol": item["symbol"].broker_symbol, "decision": sig.decision, "grade": sig.grade,
                       "score": sig.score, "timeframe": sig.timeframe})
    if r.grade == "A+":
        notify(db, "SIGNAL_A_PLUS", f"A+ {r.decision} {item['symbol'].broker_symbol} {r.timeframe}", r.explanation)
    elif r.decision in ("BUY", "SELL"):
        notify(db, f"SIGNAL_{r.decision}", f"{r.decision} {item['symbol'].broker_symbol} {r.timeframe} ({r.grade})",
               r.explanation)
    return sig


def claude_review(db: Session, agent: ClaudeAgent, sig: TradingSignal, item: dict, extra: dict) -> None:
    r = item["result"]
    if not agent.throttle_ok(item["symbol"].broker_symbol, r.timeframe):
        return
    candidate = {**r.dict(), "symbol": item["symbol"].broker_symbol, "broker_symbol": item["symbol"].broker_symbol,
                 "current_price": item["price"], "spread": item["spread"], "regime_detail": item["regime"].dict(),
                 "structure": item["analysis"].structure, **extra}
    conv = AgentConversation(purpose="SIGNAL_REVIEW", model=agent.model, request={"signal_id": sig.id})
    db.add(conv)
    db.flush()
    try:
        review, meta = agent.review(candidate)
    except AgentError as exc:
        conv.error = str(exc)
        notify(db, "CLAUDE_ERROR", "Claude review failed", str(exc))
        agent_activity("claude_agent", "error", str(exc))
        # Without a review the candidate is not auto-executed (conservative)
        sig.ai_review = {"decision": "UNREVIEWED", "error": str(exc)}
        return
    conv.response, conv.input_tokens, conv.output_tokens = review.model_dump(), meta["input_tokens"], meta["output_tokens"]
    conv.latency_ms, conv.model = meta["latency_ms"], meta["model"]
    sig.ai_review = review.model_dump()
    db.add(AgentDecision(conversation_id=conv.id, signal_id=sig.id, symbol=item["symbol"].broker_symbol,
                         decision=review.decision, payload=review.model_dump()))
    if review.decision == "NO_TRADE":
        sig.decision, sig.status = "NO_TRADE", "REJECTED"
    elif review.decision == "WAIT_FOR_CONFIRMATION" and sig.decision in ("BUY", "SELL"):
        sig.decision = "WAIT"


def scan_once(bridge: BridgeClient, agent: ClaudeAgent | None, auto_execute) -> dict:
    s = get_settings()
    with session_scope() as db:
        from app.services.trading import get_state
        mode = get_state(db).active_mode
        enabled = enabled_strategies(db, mode)
        stats = strategy_stats(db)
        calib = calibrator(db)
        symbols = db.scalars(select(Symbol).where(Symbol.scan_enabled.is_(True), Symbol.tradable.is_(True))
                             .order_by(Symbol.priority, Symbol.canonical)).all()
        sym_ids = [x.id for x in symbols]
    agent_activity("market_scanner", "processing", f"Scanning {len(sym_ids)} symbols x {s.timeframes}")
    results = []

    def work(sid: int, tf: str):
        with session_scope() as db:
            sym = db.get(Symbol, sid)
            try:
                item = analyse(db, bridge, sym, tf, enabled, stats, calib, SignalConfig())
            except Exception as exc:  # noqa: BLE001 - one symbol failing must not stop the scan
                log.exception("scan %s %s failed", sym.broker_symbol, tf)
                return {"symbol": sym.broker_symbol, "tf": tf, "error": str(exc)}
            if not item:
                return None
            sig = persist_signal(db, item)
            return {"signal_id": sig.id if sig else None, "score": item["result"].score, "decision": item["result"].decision,
                    "symbol": sym.broker_symbol, "tf": tf}

    with ThreadPoolExecutor(max_workers=s.scan_max_parallel) as ex:
        futs = [ex.submit(work, sid, tf) for sid in sym_ids for tf in s.timeframes]
        for f in futs:
            r = f.result()
            if r:
                results.append(r)
    ranked = sorted([r for r in results if r.get("signal_id") and r["decision"] in ("BUY", "SELL")],
                    key=lambda r: r["score"], reverse=True)[: s.claude_max_candidates_per_scan]
    for r in ranked:
        with session_scope() as db:
            sig = db.get(TradingSignal, r["signal_id"])
            if agent is not None:
                ar = db.get(AnalysisResult, sig.analysis_id)
                sym = db.get(Symbol, sig.symbol_id)
                item = {"result": _ResultView(sig, ar), "symbol": sym, "analysis": ar,
                        "price": float(sig.entry or 0), "spread": 0.0, "regime": _RegimeView(ar)}
                claude_review(db, agent, sig, item, {})
            elif not s.anthropic_api_key:
                sig.ai_review = {"decision": "UNREVIEWED", "error": "Claude API key not configured"}
        auto_execute(r["signal_id"])
    agent_activity("market_scanner", "waiting", f"Scan complete: {len(results)} analyses, {len(ranked)} candidates")
    return {"analyses": len(results), "candidates": len(ranked), "errors": sum(1 for r in results if r.get("error"))}


class _ResultView:
    """Adapts a stored signal to the shape claude_review expects."""

    def __init__(self, sig: TradingSignal, ar: AnalysisResult):
        self._d = dict(ar.score)
        self.timeframe = sig.timeframe

    def dict(self):
        return self._d


class _RegimeView:
    def __init__(self, ar: AnalysisResult):
        self._d = ar.structure.get("regime", {})

    def dict(self):
        return self._d


def expire_signals(db: Session) -> int:
    n = 0
    for sig in db.scalars(select(TradingSignal).where(TradingSignal.status == "NEW",
                                                      TradingSignal.expires_at < datetime.now(UTC))):
        sig.status = "EXPIRED"
        n += 1
    return n
_ = timedelta
