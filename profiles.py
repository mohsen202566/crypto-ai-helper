from __future__ import annotations
from dataclasses import dataclass,field
from typing import Any
@dataclass
class SymbolProfile:
    symbol_id:str; version:str='v1.0'; parameters:dict[str,Any]=field(default_factory=dict); confidence:str='LOW'; samples:int=0
class Profiles:
    def __init__(self,storage): self.storage=storage
    def get(self,symbol_id):
        raw=self.storage.get_profile(symbol_id) or {}; return SymbolProfile(symbol_id,raw.get('version','v1.0'),raw.get('parameters',{}),raw.get('confidence','LOW'),int(raw.get('samples',0)))
    def save(self,p): self.storage.save_profile(p.symbol_id,p.version,p.parameters,p.confidence,p.samples)
