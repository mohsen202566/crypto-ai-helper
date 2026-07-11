"""UEM V2.0 - موتور یک‌ساعته تشخیص جهت، قدرت و شروع حرکت.

رابط عمومی این فایل با نسخه قبلی سازگار نگه داشته شده است:
StrategySignal, WatchCandidate, WatchState, WatchEvaluation,
detect_watch_candidate, evaluate_watch, analyze_symbol.

تحلیل کندلی فقط از OKX می‌آید و این فایل هیچ تماس شبکه‌ای ندارد.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Any
import time

import config


@dataclass
class StrategySignal:
    symbol_id: str
    okx_symbol: str
    toobit_symbol: str
    side: str
    entry: float
    strength: str
    strength_score: float
    compression_score: float
    flow_bias: float
    absorption_score: float
    reason: str
    model: str = "pressure_transfer"
    signal_class: str = "A"
    direction_score: float = 0.0
    origin_score: float = 0.0
    continuation_score: float = 0.0
    path_score: float = 0.0
    suggested_sl_pct: float = 0.0


@dataclass
class WatchCandidate:
    side: str
    trigger: str
    start_price: float
    early_flow: float
    compression_score: float
    volume_ratio: float
    range_ratio: float
    expected_move_pct: float
    late_limit_pct: float
    pre_move_score: float = 0.0
    long_score: float = 0.0
    short_score: float = 0.0
    conflict_score: float = 0.0
    watch_confidence: float = 0.0
    model: str = "pressure_transfer"
    signal_class: str = "B+"
    origin_score: float = 0.0
    strength_score: float = 0.0
    continuation_score: float = 0.0
    path_score: float = 0.0
    suggested_sl_pct: float = 0.0
    details: dict[str, float | str] = field(default_factory=dict)


@dataclass
class WatchState:
    symbol_id: str
    okx_symbol: str
    toobit_symbol: str
    side: str
    trigger: str
    start_price: float
    created_at: float
    expected_move_pct: float
    late_limit_pct: float
    early_flow: float
    compression_score: float
    direction_locked: bool = False
    side_changes: int = 0
    confirm_count: int = 0
    bad_count: int = 0
    last_price: float = 0.0
    last_update: float = 0.0
    pre_move_score: float = 0.0
    long_direction_score: float = 0.0
    short_direction_score: float = 0.0
    watch_confidence: float = 0.0
    conflict_score: float = 0.0
    direction_gap: float = 0.0
    persistence_count: int = 0
    locked_side: str = "UNCERTAIN"
    observations: list[float] = field(default_factory=list)
    max_favorable_pct: float = 0.0
    max_adverse_pct: float = 0.0
    weakness_count: int = 0
    locked_at: float = 0.0
    proof_seen: bool = False
    proof_count: int = 0
    model: str = "pressure_transfer"
    signal_class: str = "B+"
    origin_score: float = 0.0
    organic_strength: float = 0.0
    continuation_score: float = 0.0
    path_score: float = 0.0
    suggested_sl_pct: float = 0.0


@dataclass
class WatchEvaluation:
    action: str
    reason_fa: str
    side: str
    signal: StrategySignal | None
    metrics: dict[str, float | str]


@dataclass
class StrategyAnalysisResult:
    signal: StrategySignal | None
    reject_reason: str
    details: dict[str, float | str]


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(x)))


def _score(x: float, lo: float, hi: float) -> float:
    if hi <= lo:
        return 0.0
    return 100.0 * _clamp((x - lo) / (hi - lo))


def _med(xs: list[float], default: float = 0.0) -> float:
    return median(xs) if xs else default


def _volume(c: dict[str, float]) -> float:
    return max(float(c.get("vol_quote") or c.get("volume") or 0.0), 0.0)


def pct_range(c: dict[str, float]) -> float:
    close = float(c.get("close") or 0.0)
    return (float(c["high"]) - float(c["low"])) / close * 100.0 if close > 0 else 0.0


def _body_signed(c: dict[str, float]) -> float:
    r = max(float(c["high"]) - float(c["low"]), 1e-12)
    return max(-1.0, min(1.0, (float(c["close"]) - float(c["open"])) / r))


def _close_loc(c: dict[str, float]) -> float:
    r = max(float(c["high"]) - float(c["low"]), 1e-12)
    return _clamp((float(c["close"]) - float(c["low"])) / r)


def _normalize(inp: Any) -> tuple[list[dict[str, float]], list[dict[str, float]]]:
    if isinstance(inp, dict):
        pk = str(getattr(config, "OKX_PRIMARY_BAR", "1H"))
        ck = str(getattr(config, "OKX_CONTEXT_BAR", "4H"))
        primary = inp.get(pk) or inp.get("primary") or []
        context = inp.get(ck) or inp.get("context") or primary
        return list(primary), list(context)
    arr = list(inp or [])
    return arr, arr


def _atr_pct(candles: list[dict[str, float]], n: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    trs: list[float] = []
    for i in range(max(1, len(candles)-n), len(candles)):
        c, p = candles[i], candles[i-1]
        tr = max(float(c["high"])-float(c["low"]), abs(float(c["high"])-float(p["close"])), abs(float(c["low"])-float(p["close"])))
        close = max(float(c["close"]), 1e-12)
        trs.append(tr / close * 100.0)
    return _med(trs, 0.0)


def pre_move_flow_bias(candles: list[dict[str, float]]) -> float:
    recent = candles[-max(4, int(getattr(config, "FLOW_BIAS_LOOKBACK", 8))):]
    total = sum(_volume(c) for c in recent) or 1.0
    val = 0.0
    for c in recent:
        loc = (_close_loc(c)-0.5)*2.0
        val += (0.65*_body_signed(c)+0.35*loc) * (_volume(c)/total)
    return max(-1.0, min(1.0, val))


def _features(primary: list[dict[str, float]], context: list[dict[str, float]]) -> dict[str, float]:
    recent = primary[-12:]
    short = primary[-6:]
    closes = [float(c["close"]) for c in recent]
    highs = [float(c["high"]) for c in short]
    lows = [float(c["low"]) for c in short]
    ranges = [pct_range(c) for c in recent]
    atr = max(_atr_pct(primary), 1e-6)
    net = (closes[-1]-closes[0])/max(closes[0],1e-12)*100.0
    efficiency = abs(net) / max(sum(ranges), 1e-9)
    flow = pre_move_flow_bias(primary)
    vol_med = _med([_volume(c) for c in primary[-30:-6]], 1.0) or 1.0
    vol_ratio = _med([_volume(c) for c in short], 0.0)/vol_med
    recent_rng = _med([pct_range(c) for c in short], atr)
    base_rng = _med([pct_range(c) for c in primary[-30:-6]], atr) or atr
    compression = recent_rng/base_rng
    hold_long = sum(1 for c in short if _close_loc(c) >= .55)/len(short)
    hold_short = sum(1 for c in short if _close_loc(c) <= .45)/len(short)
    higher_lows = sum(lows[i]>=lows[i-1] for i in range(1,len(lows)))/max(1,len(lows)-1)
    lower_highs = sum(highs[i]<=highs[i-1] for i in range(1,len(highs)))/max(1,len(highs)-1)
    up_effort = sum(pct_range(c) for c in recent if float(c["close"])>=float(c["open"]))
    dn_effort = sum(pct_range(c) for c in recent if float(c["close"])<float(c["open"]))
    buy_result = max(net,0.0); sell_result=max(-net,0.0)
    buy_eff = buy_result/max(up_effort, atr*.2)
    sell_eff = sell_result/max(dn_effort, atr*.2)
    ctx = 0.0
    if len(context)>=5:
        ctx = (float(context[-1]["close"])-float(context[-5]["close"]))/max(float(context[-5]["close"]),1e-12)*100.0
    return dict(atr=atr,net=net,efficiency=efficiency,flow=flow,vol_ratio=vol_ratio,compression=compression,
                hold_long=hold_long,hold_short=hold_short,higher_lows=higher_lows,lower_highs=lower_highs,
                buy_eff=buy_eff,sell_eff=sell_eff,ctx=ctx,base_rng=base_rng,recent_rng=recent_rng)


def _score_models(f: dict[str,float], primary: list[dict[str,float]]) -> dict[str,Any]:
    atr=f["atr"]; net=f["net"]; flow=f["flow"]
    long_state = .30*_score(net,-atr*.8,atr*1.4)+.20*_score(flow,-.12,.35)+.20*(100*f["higher_lows"])+.15*(100*f["hold_long"])+.15*_score(f["ctx"],-atr,atr*2)
    short_state = .30*_score(-net,-atr*.8,atr*1.4)+.20*_score(-flow,-.12,.35)+.20*(100*f["lower_highs"])+.15*(100*f["hold_short"])+.15*_score(-f["ctx"],-atr,atr*2)
    long_transition = .45*_score(f["buy_eff"]-f["sell_eff"],-.15,.45)+.25*_score(flow,-.05,.30)+.30*(100*f["higher_lows"])
    short_transition = .45*_score(f["sell_eff"]-f["buy_eff"],-.15,.45)+.25*_score(-flow,-.05,.30)+.30*(100*f["lower_highs"])
    long=max(long_state,long_transition*.92); short=max(short_state,short_transition*.92)
    side="LONG" if long>=short else "SHORT"
    direction=max(long,short); gap=abs(long-short); conflict=max(0.0,55-gap)

    largest=max([pct_range(c) for c in primary[-8:]] or [0.0]); total=sum(pct_range(c) for c in primary[-8:]) or 1.0
    concentration=largest/total
    retention = f["hold_long"] if side=="LONG" else f["hold_short"]
    organic=.35*_score(f["efficiency"],.035,.20)+.30*(100*retention)+.20*_score(f["vol_ratio"],.65,1.8)+.15*(100*(1-_clamp((concentration-.38)/.42)))
    compression_quality=.45*_score(1/f["compression"],.95,1.55)+.30*(100*retention)+.25*_score(abs(flow),.02,.28)
    pressure=.45*(long_transition if side=="LONG" else short_transition)+.30*direction+.25*(100*(1-_clamp((f["compression"]-.75)/.75)))
    persistent=.45*organic+.30*direction+.25*_score(f["efficiency"],.04,.18)
    impulse=.35*_score(f["recent_rng"]/max(f["base_rng"],1e-9),1.0,2.2)+.30*direction+.20*(100*retention)+.15*_score(f["vol_ratio"],.9,2.2)
    models={"pressure_transfer":pressure,"compression_ignition":compression_quality,"persistent_expansion":persistent,"impulse_cascade":impulse}
    model=max(models,key=models.get); origin=models[model]
    age_penalty=_score(abs(net)/atr,1.8,4.2)
    energy=.55*organic+.45*origin
    continuation=.45*energy+.30*(100-age_penalty)+.25*_score(abs(net)+atr,.5*atr,3.0*atr)
    path=max(0.0,min(100.0,.6*continuation+.4*(100-conflict)))
    signal_class="A" if direction>=76 and origin>=76 and continuation>=70 and gap>=16 else "B+"
    return {"side":side,"long":long,"short":short,"direction":direction,"gap":gap,"conflict":conflict,
            "organic":organic,"origin":origin,"continuation":continuation,"path":path,"model":model,"models":models,
            "signal_class":signal_class}


def detect_watch_candidate(candles: Any, profile: dict[str,Any]|None=None) -> tuple[WatchCandidate|None,str,dict[str,float|str]]:
    primary,context=_normalize(candles)
    if len(primary)<max(40,int(getattr(config,"UEM_MIN_BARS",40))):
        return None,"داده کندلی کافی نیست",{"تعداد_کندل":len(primary)}
    f=_features(primary,context); s=_score_models(f,primary)
    current=primary[-1]; entry=float(current["close"])
    profile=profile or {}
    expected=float(profile.get("tp_p70") or profile.get("tp_median") or max(f["atr"]*2.2,.8))
    late=max(float(getattr(config,"UEM_LATE_MIN_PCT",.18)),min(float(getattr(config,"UEM_LATE_MAX_PCT",1.6)),expected*.55))
    current_move=abs(float(current["close"])-float(current["open"]))/max(float(current["open"]),1e-9)*100
    suggested=max(float(profile.get("min_sl_pct") or 0.0), f["atr"]*float(getattr(config,"UEM_SL_ATR_MULT",1.05)), float(getattr(config,"RISK_FALLBACK_MIN_SL_PCT",.55)))
    watch=.30*s["direction"]+.30*s["origin"]+.22*s["organic"]+.18*s["continuation"]
    details={"UEM":round(watch,1),"Model":s["model"],"Class":s["signal_class"],"Long":round(s["long"],1),"Short":round(s["short"],1),
             "Gap":round(s["gap"],1),"Conflict":round(s["conflict"],1),"Direction":round(s["direction"],1),"Origin":round(s["origin"],1),
             "Strength":round(s["organic"],1),"Continuation":round(s["continuation"],1),"Path":round(s["path"],1),"ATR%":round(f["atr"],4),
             "Compression":round(f["compression"],3),"VolumeRatio":round(f["vol_ratio"],3),"CurrentMove%":round(current_move,4)}
    # فقط موارد واقعاً ضعیف رد می‌شوند؛ مسیر Impulse اجازه شروع سریع‌تر دارد.
    min_watch=float(getattr(config,"UEM_WATCH_MIN",64.0))
    min_origin=float(getattr(config,"UEM_ORIGIN_WATCH_MIN",62.0))
    min_dir=float(getattr(config,"UEM_DIRECTION_WATCH_MIN",58.0))
    if watch<min_watch or s["origin"]<min_origin or s["direction"]<min_dir or s["gap"]<float(getattr(config,"UEM_MIN_GAP_WATCH",8.0)):
        return None,"نشانه یکپارچه جهت/قدرت/آغاز حرکت کافی نبود",details
    if current_move>late*1.45 and s["model"]!="impulse_cascade":
        return None,"حرکت برای ورود عادی بیش‌ازحد جلو رفته بود",details
    trigger={"pressure_transfer":"انتقال فشار نزدیک مبدأ","compression_ignition":"آزادسازی فشردگی جهت‌دار","persistent_expansion":"ادامه روند جوان و کارا","impulse_cascade":"آغاز پامپ/دامپ زنجیره‌ای"}[s["model"]]
    return WatchCandidate(side=s["side"],trigger=trigger,start_price=entry,early_flow=f["flow"],compression_score=_clamp(1.15-f["compression"]),
        volume_ratio=f["vol_ratio"],range_ratio=f["recent_rng"]/max(f["base_rng"],1e-9),expected_move_pct=expected,late_limit_pct=late,
        pre_move_score=watch,long_score=s["long"],short_score=s["short"],conflict_score=s["conflict"],watch_confidence=watch,
        model=s["model"],signal_class=s["signal_class"],origin_score=s["origin"],strength_score=s["organic"],continuation_score=s["continuation"],
        path_score=s["path"],suggested_sl_pct=suggested,details=details),"ورود به واچ UEM",details


def _update_path(state:WatchState,price:float)->tuple[float,float]:
    state.observations.append(price)
    state.observations=state.observations[-int(getattr(config,"UEM_WATCH_OBSERVATIONS",20)):]
    sign=1 if state.side=="LONG" else -1
    response=sign*(price-state.start_price)/max(state.start_price,1e-9)*100
    state.max_favorable_pct=max(state.max_favorable_pct,response)
    state.max_adverse_pct=max(state.max_adverse_pct,-response)
    return response, (state.max_favorable_pct-response)/max(state.max_favorable_pct,1e-9) if state.max_favorable_pct>0 else 0.0


def evaluate_watch(state:WatchState,snapshot:dict[str,Any],now:float|None=None)->WatchEvaluation:
    now=now or time.time(); price=float(snapshot.get("mid_price") or snapshot.get("last_price") or 0.0)
    if price<=0:return WatchEvaluation("KEEP","قیمت معتبر دریافت نشد",state.side,None,{})
    if now-state.created_at>float(getattr(config,"WATCH_TTL_SECONDS",3600)):
        return WatchEvaluation("REMOVE","واچ منقضی شد",state.side,None,{"Age":round(now-state.created_at,1)})
    trade=float(snapshot.get("trade_imbalance") or 0.0); book=float(snapshot.get("book_imbalance") or 0.0); intensity=float(snapshot.get("intensity_acceleration") or 0.0)
    side=state.side if state.side in ("LONG","SHORT") else ("LONG" if state.long_direction_score>=state.short_direction_score else "SHORT")
    state.side=side; sign=1 if side=="LONG" else -1
    response,pullback=_update_path(state,price)
    micro=50+sign*trade*24+sign*book*16+_score(response,0,max(state.late_limit_pct*.65,.08))*.28+_score(intensity,-.1,.8)*.12
    micro=max(0,min(100,micro))
    opposite=max(0,-sign*trade)+max(0,-sign*book)
    hold=100*(1-_clamp(pullback))
    ignition=.42*micro+.24*hold+.20*state.origin_score+.14*state.organic_strength
    metrics={"Model":state.model,"Class":state.signal_class,"Direction":round(max(state.long_direction_score,state.short_direction_score),1),
             "Origin":round(state.origin_score,1),"Strength":round(state.organic_strength,1),"Continuation":round(state.continuation_score,1),
             "Micro":round(micro,1),"Ignition":round(ignition,1),"Response%":round(response,4),"Pullback":round(pullback,3),
             "Trade":round(trade,3),"Book":round(book,3),"Intensity":round(intensity,3)}
    if opposite>float(getattr(config,"UEM_STRONG_OPPOSITE",.42)) and response<0:
        state.bad_count+=1
    else: state.bad_count=max(0,state.bad_count-1)
    if state.bad_count>=int(getattr(config,"WATCH_BAD_OBSERVATIONS_TO_REMOVE",3)):
        return WatchEvaluation("REMOVE","فشار زنده چند بار خلاف جهت غالب شد",side,None,metrics)
    min_response=max(float(getattr(config,"UEM_MIN_START_RESPONSE_PCT",.035)),state.late_limit_pct*float(getattr(config,"UEM_START_FRACTION",.12)))
    # Impulse سریع‌تر تایید می‌شود؛ مدل‌های دقیق‌تر حفظ کوتاه می‌خواهند.
    needed=1 if state.model=="impulse_cascade" and ignition>=86 else int(getattr(config,"UEM_CONFIRMATIONS_REQUIRED",2))
    valid = response>=min_response and ignition>=float(getattr(config,"UEM_IGNITION_MIN",72.0)) and pullback<=float(getattr(config,"UEM_MAX_PULLBACK",.52)) and opposite<.34
    if valid: state.confirm_count+=1
    else: state.confirm_count=max(0,state.confirm_count-1)
    if state.confirm_count<needed:
        return WatchEvaluation("KEEP","جهت و مبدأ مناسب‌اند؛ منتظر آزادسازی زنده و حفظ کوتاه",side,None,metrics)
    final=.24*max(state.long_direction_score,state.short_direction_score)+.22*state.organic_strength+.24*state.origin_score+.18*state.continuation_score+.12*micro
    if final<float(getattr(config,"UEM_FINAL_SIGNAL_MIN",70.0)):
        return WatchEvaluation("KEEP","آزادسازی دیده شد اما کیفیت یکپارچه نهایی هنوز کافی نیست",side,None,metrics)
    strength="خیلی قوی" if final>=86 else ("قوی" if final>=77 else "متوسط")
    signal=StrategySignal(state.symbol_id,state.okx_symbol,state.toobit_symbol,side,price,strength,round(final,2),round(state.compression_score*100,2),round(trade,4),round(max(state.long_direction_score,state.short_direction_score),2),
        reason=(f"UEM_V2 model={state.model} class={state.signal_class} side={side} direction={max(state.long_direction_score,state.short_direction_score):.1f} "
                f"origin={state.origin_score:.1f} strength={state.organic_strength:.1f} continuation={state.continuation_score:.1f} path={state.path_score:.1f} "
                f"ignition={ignition:.1f} trade={trade:.3f} book={book:.3f} response={response:.4f}%"),
        model=state.model,signal_class=state.signal_class,direction_score=max(state.long_direction_score,state.short_direction_score),origin_score=state.origin_score,
        continuation_score=state.continuation_score,path_score=state.path_score,suggested_sl_pct=state.suggested_sl_pct)
    return WatchEvaluation("SIGNAL","جهت، قدرت و آغاز حرکت به‌صورت یکپارچه تأیید شدند",side,signal,metrics)


def analyze_symbol_detailed(symbol_id:str,okx_symbol:str,toobit_symbol:str,candles:Any)->StrategyAnalysisResult:
    candidate,reason,details=detect_watch_candidate(candles,None)
    if not candidate:return StrategyAnalysisResult(None,reason,details)
    score=.4*max(candidate.long_score,candidate.short_score)+.3*candidate.origin_score+.3*candidate.strength_score
    sig=StrategySignal(symbol_id,okx_symbol,toobit_symbol,candidate.side,candidate.start_price,"قوی" if score>=77 else "متوسط",round(score,2),round(candidate.compression_score*100,2),candidate.early_flow,max(candidate.long_score,candidate.short_score),
        f"UEM historical proxy model={candidate.model}",candidate.model,candidate.signal_class,max(candidate.long_score,candidate.short_score),candidate.origin_score,candidate.continuation_score,candidate.path_score,candidate.suggested_sl_pct)
    return StrategyAnalysisResult(sig,"accepted",details)


def analyze_symbol(symbol_id:str,okx_symbol:str,toobit_symbol:str,candles:Any)->StrategySignal|None:
    return analyze_symbol_detailed(symbol_id,okx_symbol,toobit_symbol,candles).signal
