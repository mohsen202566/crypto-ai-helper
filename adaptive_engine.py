from __future__ import annotations
class AdaptiveEngine:
    def __init__(self,storage,profiles): self.storage=storage; self.profiles=profiles
    def create_candidate(self,symbol_id,learning):
        if learning.get('action')!='CREATE_HYPOTHESIS': return None
        p=self.profiles.get(symbol_id); err=learning.get('top_error')
        change={}
        if err=='ENTRY_TOO_LATE': change={'trigger_threshold_delta':-1}
        elif err=='DIRECTION_ERROR': change={'direction_threshold_delta':1}
        elif err=='NO_FOLLOW_THROUGH': change={'strength_threshold_delta':1}
        else:return None
        cid=f'{symbol_id}-{p.version}-candidate'; self.storage.save_model_change(cid,symbol_id,p.version,change,'SHADOW'); return {'candidate_id':cid,'change':change,'status':'SHADOW'}
