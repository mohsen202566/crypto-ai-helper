from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class SymbolMap:
    id: str
    okx: str
    toobit: str
    base: str
    quote: str = 'USDT'
    group: str = 'ALT'
    active: bool = True

# 40 liquid USDT perpetual candidates. Availability is still validated at runtime by the exchange clients;
# a temporarily unavailable market is logged and skipped instead of stopping the scanner.
SYMBOLS = [
    SymbolMap('BTC','BTC-USDT-SWAP','BTCUSDT','BTC',group='MAJOR'),
    SymbolMap('ETH','ETH-USDT-SWAP','ETHUSDT','ETH',group='MAJOR'),
    SymbolMap('BNB','BNB-USDT-SWAP','BNBUSDT','BNB',group='MAJOR'),
    SymbolMap('SOL','SOL-USDT-SWAP','SOLUSDT','SOL',group='LIQUID_VOL'),
    SymbolMap('XRP','XRP-USDT-SWAP','XRPUSDT','XRP',group='LIQUID_VOL'),
    SymbolMap('DOGE','DOGE-USDT-SWAP','DOGEUSDT','DOGE',group='LIQUID_VOL'),
    SymbolMap('ADA','ADA-USDT-SWAP','ADAUSDT','ADA'),
    SymbolMap('LINK','LINK-USDT-SWAP','LINKUSDT','LINK'),
    SymbolMap('AVAX','AVAX-USDT-SWAP','AVAXUSDT','AVAX',group='LIQUID_VOL'),
    SymbolMap('SUI','SUI-USDT-SWAP','SUIUSDT','SUI',group='LIQUID_VOL'),
    SymbolMap('LTC','LTC-USDT-SWAP','LTCUSDT','LTC'),
    SymbolMap('BCH','BCH-USDT-SWAP','BCHUSDT','BCH'),
    SymbolMap('DOT','DOT-USDT-SWAP','DOTUSDT','DOT'),
    SymbolMap('NEAR','NEAR-USDT-SWAP','NEARUSDT','NEAR',group='HIGH_VOL'),
    SymbolMap('APT','APT-USDT-SWAP','APTUSDT','APT',group='HIGH_VOL'),
    SymbolMap('ATOM','ATOM-USDT-SWAP','ATOMUSDT','ATOM'),
    SymbolMap('INJ','INJ-USDT-SWAP','INJUSDT','INJ',group='HIGH_VOL'),
    SymbolMap('ARB','ARB-USDT-SWAP','ARBUSDT','ARB',group='HIGH_VOL'),
    SymbolMap('OP','OP-USDT-SWAP','OPUSDT','OP',group='HIGH_VOL'),
    SymbolMap('FIL','FIL-USDT-SWAP','FILUSDT','FIL',group='HIGH_VOL'),
    SymbolMap('TRX','TRX-USDT-SWAP','TRXUSDT','TRX'),
    SymbolMap('TON','TON-USDT-SWAP','TONUSDT','TON',group='LIQUID_VOL'),
    SymbolMap('UNI','UNI-USDT-SWAP','UNIUSDT','UNI'),
    SymbolMap('AAVE','AAVE-USDT-SWAP','AAVEUSDT','AAVE'),
    SymbolMap('ETC','ETC-USDT-SWAP','ETCUSDT','ETC'),
    SymbolMap('XLM','XLM-USDT-SWAP','XLMUSDT','XLM'),
    SymbolMap('HBAR','HBAR-USDT-SWAP','HBARUSDT','HBAR'),
    SymbolMap('ICP','ICP-USDT-SWAP','ICPUSDT','ICP'),
    SymbolMap('ALGO','ALGO-USDT-SWAP','ALGOUSDT','ALGO'),
    SymbolMap('VET','VET-USDT-SWAP','VETUSDT','VET'),
    SymbolMap('SEI','SEI-USDT-SWAP','SEIUSDT','SEI',group='HIGH_VOL'),
    SymbolMap('TIA','TIA-USDT-SWAP','TIAUSDT','TIA',group='HIGH_VOL'),
    SymbolMap('WIF','WIF-USDT-SWAP','WIFUSDT','WIF',group='HIGH_VOL'),
    SymbolMap('PEPE','PEPE-USDT-SWAP','PEPEUSDT','PEPE',group='HIGH_VOL'),
    SymbolMap('SHIB','SHIB-USDT-SWAP','SHIBUSDT','SHIB',group='HIGH_VOL'),
    SymbolMap('RENDER','RENDER-USDT-SWAP','RENDERUSDT','RENDER',group='HIGH_VOL'),
    SymbolMap('JUP','JUP-USDT-SWAP','JUPUSDT','JUP',group='HIGH_VOL'),
    SymbolMap('PYTH','PYTH-USDT-SWAP','PYTHUSDT','PYTH',group='HIGH_VOL'),
    SymbolMap('GALA','GALA-USDT-SWAP','GALAUSDT','GALA',group='HIGH_VOL'),
    SymbolMap('LDO','LDO-USDT-SWAP','LDOUSDT','LDO',group='HIGH_VOL'),
]

BY_ID = {s.id: s for s in SYMBOLS}
BY_OKX = {s.okx: s for s in SYMBOLS}
BY_TOOBIT = {s.toobit: s for s in SYMBOLS}

def get_symbol(symbol_id: str):
    return BY_ID.get(symbol_id.upper())

# Extra mappings used only when one of the primary 40 is unavailable on OKX.
FALLBACK_SYMBOLS = [
    SymbolMap('CRV','CRV-USDT-SWAP','CRVUSDT','CRV',group='HIGH_VOL'),
    SymbolMap('SAND','SAND-USDT-SWAP','SANDUSDT','SAND',group='HIGH_VOL'),
    SymbolMap('MANA','MANA-USDT-SWAP','MANAUSDT','MANA',group='HIGH_VOL'),
    SymbolMap('DYDX','DYDX-USDT-SWAP','DYDXUSDT','DYDX',group='HIGH_VOL'),
    SymbolMap('ORDI','ORDI-USDT-SWAP','ORDIUSDT','ORDI',group='HIGH_VOL'),
]

def select_valid_symbols(valid_okx_ids: set[str], target: int = 40):
    """Return exactly target unique OKX-valid mappings when enough candidates exist."""
    selected = []
    seen = set()
    for sym in [*SYMBOLS, *FALLBACK_SYMBOLS]:
        if sym.okx in valid_okx_ids and sym.id not in seen:
            selected.append(sym)
            seen.add(sym.id)
        if len(selected) >= target:
            break
    return selected
