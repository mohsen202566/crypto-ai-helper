"""اسکن و صدور سه سطح سیگنال با پروفایل و قفل مستقل هر تایم معامله."""
from __future__ import annotations

import logging
import queue
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

import config
from behavior_engine import BEHAVIOR_VERSION, BehaviorEngine
from feature_engine import FEATURE_VERSION, FeatureEngine
from logger_setup import RejectLogger
from market_data import MarketDataClient, MarketDataError
from models import ProfileStage, Signal, SignalStatus, Tier
from news_filter import NewsFilter
from storage import Storage
from symbol_registry import SymbolRegistry
from tp_sl_engine import TP_SL_VERSION, TPSLEngine
from utils import now_ms

logger = logging.getLogger("adaptive_bot")
MODEL_VERSION = "adaptive-high-tf-v2"


class SignalEngine:
    def __init__(
        self,
        storage: Storage,
        registry: SymbolRegistry,
        market: MarketDataClient,
        feature_engine: FeatureEngine,
        behavior_engine: BehaviorEngine,
        tp_sl_engine: TPSLEngine,
        trade_queue: queue.Queue[int],
        scenario_queue: queue.Queue[int],
        notification_queue: queue.Queue[dict[str, Any]],
        reject_logger: RejectLogger,
        news_filter: NewsFilter | None = None,
    ):
        self.storage = storage
        self.registry = registry
        self.market = market
        self.features = feature_engine
        self.behaviors = behavior_engine
        self.tp_sl = tp_sl_engine
        self.trade_queue = trade_queue
        self.scenario_queue = scenario_queue
        self.notifications = notification_queue
        self.rejects = reject_logger
        self.news = news_filter

    def _profile_bootstraps(self, mapping: Any) -> tuple[str, dict[str, dict[str, Any]]]:
        """Build each timeframe profile from candles of that same timeframe."""
        if hasattr(self.market, "analysis_bundle"):
            source, bundle = self.market.analysis_bundle(mapping)
            out: dict[str, dict[str, Any]] = {}
            for tf in config.TRADE_TIMEFRAMES:
                candles = list(bundle.get(tf) or [])
                out[tf] = self.features.bootstrap_profile(candles, minimum=60)
                out[tf]["data_source"] = source
                out[tf]["trade_timeframe"] = tf
            return source, out
        # Compatibility fallback for controlled/offline test doubles.
        source, candles = self.market.candles(mapping, config.PROFILE_BAR, config.PROFILE_5M_CANDLES)
        common = self.features.bootstrap_profile(candles)
        out = {}
        for tf in config.TRADE_TIMEFRAMES:
            item = dict(common); item["data_source"] = source; item["trade_timeframe"] = tf
            out[tf] = item
        return source, out

    def prepare_profiles(self, mappings: list[Any], progress: Callable[[str], None] | None = None) -> None:
        """Gate startup until every active symbol/direction/timeframe profile is ready."""
        progress = progress or (lambda _x: None)
        active = [m for m in mappings if m.active][: config.ACTIVE_SYMBOLS]
        if len(active) != config.ACTIVE_SYMBOLS:
            raise RuntimeError(f"active universe incomplete: {len(active)}/{config.ACTIVE_SYMBOLS}")
        ready = 0
        for idx, mapping in enumerate(active, start=1):
            wanted = [(side, tf) for tf in config.TRADE_TIMEFRAMES for side in ("LONG", "SHORT")]
            profiles = [self.storage.learning.get_profile(mapping.canonical, side, tf) for side, tf in wanted]
            if all(p and p.get("ready") for p in profiles):
                ready += 1
                progress(f"پروفایل موجود {idx}/{len(active)}: {mapping.canonical}")
                continue
            progress(f"ساخت پروفایل چندتایمی {idx}/{len(active)}: {mapping.canonical}")
            _source, bootstraps = self._profile_bootstraps(mapping)
            for side, tf in wanted:
                old = self.storage.learning.get_profile(mapping.canonical, side, tf)
                cfg = (old or {}).get("config") or self.features.default_profile_config(tf)
                self.storage.learning.save_bootstrap_profile(mapping.canonical, side, bootstraps[tf], cfg, tf)
            ready += 1
        if ready != len(active):
            raise RuntimeError(f"profile gate incomplete {ready}/{len(active)}")
        self.storage.runtime.set_setting("startup_ready", True)
        self.storage.runtime.set_setting("startup_phase", "READY")

    def prepare_reserve_profiles(self, mappings: list[Any], stop_check: Callable[[], bool]) -> None:
        for mapping in [m for m in mappings if not m.active]:
            if stop_check():
                return
            try:
                wanted = [(side, tf) for tf in config.TRADE_TIMEFRAMES for side in ("LONG", "SHORT")]
                profiles = [self.storage.learning.get_profile(mapping.canonical, side, tf) for side, tf in wanted]
                if all(p and p.get("ready") for p in profiles):
                    continue
                _source, bootstraps = self._profile_bootstraps(mapping)
                for side, tf in wanted:
                    old = self.storage.learning.get_profile(mapping.canonical, side, tf)
                    cfg = (old or {}).get("config") or self.features.default_profile_config(tf)
                    self.storage.learning.save_bootstrap_profile(mapping.canonical, side, bootstraps[tf], cfg, tf)
            except Exception as exc:
                logger.warning("RESERVE_PROFILE_SKIP | %s | %s", mapping.canonical, str(exc)[:160])

    def refresh_one_stale_active_profile(self) -> str | None:
        now = now_ms()
        candidates = []
        for mapping in self.registry.active():
            profiles = [
                self.storage.learning.get_profile(mapping.canonical, side, tf)
                for tf in config.TRADE_TIMEFRAMES for side in ("LONG", "SHORT")
            ]
            profiles = [p for p in profiles if p]
            if not profiles:
                continue
            built = [int((p.get("bootstrap") or {}).get("built_at") or 0) for p in profiles]
            built_at = min(built) if built else 0
            if built_at <= 0 or now - built_at >= config.PROFILE_REFRESH_SECONDS * 1000:
                candidates.append((built_at, mapping))
        if not candidates:
            self.storage.runtime.set_health("profile_refresh", "ok", "همه پروفایل‌های فعال تازه هستند")
            return None
        _, mapping = min(candidates, key=lambda x: x[0])
        try:
            source, bootstraps = self._profile_bootstraps(mapping)
            refreshed = 0
            for tf in config.TRADE_TIMEFRAMES:
                for side in ("LONG", "SHORT"):
                    profile = self.storage.learning.get_profile(mapping.canonical, side, tf)
                    if not profile:
                        continue
                    self.storage.learning.save_bootstrap_profile(
                        mapping.canonical, side, bootstraps[tf],
                        profile.get("config") or self.features.default_profile_config(tf), tf,
                    )
                    refreshed += 1
            self.storage.learning.audit(
                mapping.canonical, None, "BOOTSTRAP_REFRESHED",
                "پروفایل‌های هر تایم از داده همان تایم بروزرسانی شدند",
                {"data_source": source, "profiles": refreshed}, "30m",
            )
            self.storage.runtime.set_health("profile_refresh", "ok", mapping.canonical)
            return mapping.canonical
        except Exception as exc:
            logger.warning("PROFILE_REFRESH_SKIP | %s | %s", mapping.canonical, str(exc)[:180])
            self.storage.runtime.set_health("profile_refresh", "warning", f"{mapping.canonical}: {str(exc)[:180]}")
            return None

    def _context(self) -> dict[str, Any]:
        context={}
        for canonical in ("BTCUSDT","ETHUSDT"):
            mapping=self.registry.get(canonical)
            if not mapping: continue
            tf="1H"
            profile=self.storage.learning.get_profile(canonical,"LONG",tf) or {"config":self.features.default_profile_config(tf),"bootstrap":{}}
            try:
                source,bundle=self.market.analysis_bundle(mapping)
                snap=self.features.analyze(canonical,source,bundle,profile,context=None,data_quality=self.market.data_quality(bundle),trade_timeframe=tf)
                context[canonical]=snap.raw.get("per_tf",{})
            except Exception as exc: logger.debug("context skip %s: %s",canonical,exc)
        return context

    def scan_once(self) -> int:
        if not self.storage.runtime.get_setting("startup_ready",False): return 0
        context=self._context(); active=self.registry.active(); emitted=0
        with ThreadPoolExecutor(max_workers=min(8,max(1,len(active)))) as pool:
            futures={pool.submit(self._analyze_symbol,mapping,context):mapping for mapping in active}
            for future in as_completed(futures):
                mapping=futures[future]
                try:
                    emitted += int(future.result() or 0); self.registry.record_data_result(mapping.canonical,True)
                except Exception as exc:
                    count=self.registry.record_data_result(mapping.canonical,False); self.rejects.event("SKIP",mapping.canonical,"DATA_ERROR",str(exc)[:180])
                    if count>=config.SYMBOL_ERROR_REPLACE_AFTER: self.registry.replace_failed_active(mapping.canonical,self.storage.runtime.has_symbol_lock(mapping.canonical))
        self.storage.runtime.set_health("scanner","ok",f"اسکن کامل؛ {emitted} سیگنال جدید"); self.storage.runtime.set_setting("last_scan_ms",now_ms()); return emitted

    def _analyze_symbol(self, mapping: Any, context: dict[str, Any]) -> int:
        canonical=mapping.canonical
        if self.registry.in_cooldown(canonical):
            self.rejects.reject(canonical,"ANY","SYMBOL_COOLDOWN"); return 0
        source,bundle=self.market.analysis_bundle(mapping); quality=self.market.data_quality(bundle)
        if quality<config.MIN_DATA_QUALITY:
            self.rejects.reject(canonical,"ANY","BAD_DATA",f"quality={quality:.0f}"); return 0
        emitted=0
        for trade_tf in config.TRADE_TIMEFRAMES:
            try:
                emitted += int(self._analyze_timeframe(mapping,source,bundle,context,quality,trade_tf))
            except Exception as exc:
                self.rejects.event("SKIP",canonical,f"TF_{trade_tf}_ERROR",str(exc)[:180])
        return emitted

    def _analyze_timeframe(self, mapping: Any, source: str, bundle: dict[str, Any], context: dict[str, Any], quality: float, trade_tf: str) -> bool:
        canonical=mapping.canonical
        if self.storage.runtime.has_symbol_lock(canonical,trade_tf):
            self.rejects.reject(canonical,trade_tf,"ACTIVE_TIMEFRAME_SIGNAL"); return False
        long_profile=self.storage.learning.get_profile(canonical,"LONG",trade_tf); short_profile=self.storage.learning.get_profile(canonical,"SHORT",trade_tf)
        if not (long_profile and short_profile and long_profile.get("ready") and short_profile.get("ready")):
            self.rejects.reject(canonical,trade_tf,"PROFILE_NOT_READY"); return False
        snap_long=self.features.analyze(canonical,source,bundle,long_profile,context=context,data_quality=quality,trade_timeframe=trade_tf)
        snap_short=self.features.analyze(canonical,source,bundle,short_profile,context=context,data_quality=quality,trade_timeframe=trade_tf)
        long_score=snap_long.long_scores["weighted"]; short_score=snap_short.short_scores["weighted"]
        if abs(long_score-short_score)<config.MIN_DIRECTION_EDGE:
            self.rejects.reject(canonical,"INITIAL",f"LOW_DIRECTION_EDGE_{trade_tf}",f"L={long_score:.1f} S={short_score:.1f}"); return False
        if long_score>=short_score: side,snapshot,profile="LONG",snap_long,long_profile
        else: side,snapshot,profile="SHORT",snap_short,short_profile
        decision=self.behaviors.decide(snapshot,profile,forced_side=side)
        if self.news is not None:
            selected=snapshot.raw.get("selected") or {}; rel_volume=float((selected.get("relative_volume") or {}).get("ratio") or 1.0); natr_state=float((selected.get("atr_natr") or {}).get("state") or 0.0)
            blocked,detail=self.news.is_blocked(market_abnormal=decision.behavior=="SHOCK" or rel_volume>=2.0 or natr_state>=1.0)
            if blocked: self.rejects.reject(canonical,trade_tf,"NEWS_BLOCK",detail); return False
        stage=str(profile.get("stage") or ProfileStage.INITIAL.value); settings=self.storage.runtime.settings(); trading_on=bool(settings.get("real_trade_enabled")); slot_free=self.storage.runtime.slot_counts()["free"]>0
        account=self.storage.runtime.account_snapshot(); account_age=max(0,now_ms()-int(account.get("updated_at") or 0)); toobit_ready=bool(account.get("connected")) and account_age<=config.TOOBIT_SNAPSHOT_MAX_AGE_SECONDS*1000
        real_profile=stage in {ProfileStage.REAL_READY.value,ProfileStage.REAL_WATCH.value}; real_blocked=bool(self.storage.learning.real_state(canonical,trade_tf).get("real_blocked"))
        if real_profile and not real_blocked and trading_on and slot_free and toobit_ready: tier=Tier.REAL.value
        elif stage in {ProfileStage.MEDIUM.value,ProfileStage.MEDIUM_RELEARN.value,ProfileStage.REAL_READY.value,ProfileStage.REAL_WATCH.value}: tier=Tier.MEDIUM.value
        else: tier=Tier.INITIAL.value
        if not self._passes_soft_gate(decision,profile,tier):
            self.rejects.reject(canonical,tier,f"SOFT_SCORE_{trade_tf}",f"final={decision.final_score:.1f} dir={decision.direction_score:.1f} entry={decision.entry_quality:.1f}"); return False
        if not self._passes_learned_entry_location(snapshot,side,profile): self.rejects.reject(canonical,tier,f"WAITING_PULLBACK_{trade_tf}"); return False
        plan=self.tp_sl.build_plan(snapshot,decision,profile,margin_usdt=float(settings["trade_margin_usdt"]),leverage=int(settings["leverage"]),tick_size=float(mapping.tick_size or 0),min_net_profit=float(settings.get("min_net_profit_usdt",config.DEFAULT_MIN_NET_PROFIT_USDT)),tier=tier)
        if not plan.valid: self.rejects.reject(canonical,tier,f"{plan.reject_reason}_{trade_tf}"); return False
        signal=Signal(id=None,canonical=canonical,exchange_symbol=mapping.toobit if tier==Tier.REAL.value else mapping.okx,side=side,tier=tier,status=SignalStatus.PENDING_OPEN.value if tier==Tier.REAL.value else SignalStatus.ACTIVE.value,created_at=now_ms(),entry=plan.entry,tp=plan.tp,sl=plan.sl,rr=plan.rr,margin_usdt=plan.margin_usdt,leverage=plan.leverage,notional_usdt=plan.notional_usdt,expected_net_profit=plan.expected_net_profit,expected_hold_minutes=decision.estimated_hold_minutes,data_source=source,profile_version=int(profile.get("champion_version") or profile.get("profile_version") or 1),model_version=MODEL_VERSION,feature_version=FEATURE_VERSION,behavior_version=BEHAVIOR_VERSION,tp_sl_version=TP_SL_VERSION,decision=decision.to_dict(),features=snapshot.to_dict(),trade_timeframe=trade_tf,entry_timeframe=decision.entry_timeframe,metadata={"profile_stage":stage,"expected_cost":plan.expected_cost,"trade_timeframe":trade_tf,"entry_timeframe":decision.entry_timeframe})
        if tier==Tier.REAL.value:
            signal_id=self.storage.runtime.create_real_signal_and_reserve(signal)
            if signal_id is None:
                signal.tier=Tier.MEDIUM.value; signal.status=SignalStatus.ACTIVE.value; signal.exchange_symbol=mapping.okx
                signal_id=self.storage.runtime.create_official_signal(signal)
                if signal_id is None: return False
                self.scenario_queue.put(signal_id)
            else: self.trade_queue.put(signal_id)
        else:
            signal_id=self.storage.runtime.create_official_signal(signal)
            if signal_id is None: return False
            self.scenario_queue.put(signal_id)
        self.notifications.put({"type":"signal","signal_id":signal_id}); return True

    def _passes_learned_entry_location(snapshot: Any, side: str, profile: dict[str, Any]) -> bool:
        """Apply a promoted pullback preference without creating a hanging limit signal.

        LAB can test a lower/higher entry by ATR offset. Once proven, future signals are
        emitted only when price has already retraced that much, so the official entry is
        still the executable current price and the whole-symbol lock never hangs waiting.
        """
        required = float((profile.get("config") or {}).get("entry_atr_offset", 0.0) or 0.0)
        if required <= 0:
            return True
        selected = snapshot.raw.get("selected") or {}
        atr = float((selected.get("atr_natr") or {}).get("atr") or 0.0)
        last = float(selected.get("last") or 0.0)
        if atr <= 0 or last <= 0:
            return False
        if side == "LONG":
            retrace = max(0.0, float(selected.get("recent_high") or last) - last) / atr
        else:
            retrace = max(0.0, last - float(selected.get("recent_low") or last)) / atr
        return retrace >= required

    @staticmethod
    def _passes_soft_gate(decision: Any, profile: dict[str, Any], tier: str) -> bool:
        cfg = profile.get("config") or {}
        if tier == Tier.INITIAL.value:
            min_score = float(cfg.get("initial_min_score", config.INITIAL_MIN_SCORE))
            return (
                decision.final_score >= min_score
                and decision.direction_score >= config.INITIAL_MIN_DIRECTION
                and decision.entry_quality >= config.INITIAL_MIN_ENTRY
            )
        if tier == Tier.MEDIUM.value:
            min_score = float(cfg.get("medium_min_score", config.MEDIUM_MIN_SCORE))
            return (
                decision.final_score >= min_score
                and decision.direction_score >= config.MEDIUM_MIN_DIRECTION
                and decision.entry_quality >= config.MEDIUM_MIN_ENTRY
            )
        min_score = float(cfg.get("real_min_score", config.REAL_MIN_SCORE))
        # Real-ready has already proved profitability and win rate.  Real is only a
        # *slightly* tighter current-opportunity sanity check than Medium, not a second
        # wall of indicators.
        return (
            decision.final_score >= min_score
            and decision.direction_score >= config.REAL_MIN_DIRECTION
            and decision.entry_quality >= config.REAL_MIN_ENTRY
        )
