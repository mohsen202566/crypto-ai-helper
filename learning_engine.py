from __future__ import annotations
import config
class LearningEngine:
    def __init__(self,storage): self.storage=storage
    def run(self,symbol_id):
        rows=self.storage.list_experiences(symbol_id,500); n=len(rows)
        if n<config.LEARNING_MIN_SAMPLES:return {'action':'INSUFFICIENT_DATA','samples':n}
        wins=sum(1 for x in rows if x['outcome']=='TP'); net=sum(float(x.get('net_pnl',0)) for x in rows); causes={}
        non_errors={'CLEAN_WIN','HIGH_MAE_WIN','NO_RESOLUTION_WITHIN_TIME_LIMIT'}
        for x in rows:
            cause=x.get('primary_cause')
            if cause and cause not in non_errors:
                causes[cause]=causes.get(cause,0)+1
        top=max(causes,key=causes.get) if causes else None
        return {'action':'CREATE_HYPOTHESIS' if top else 'STORE_ONLY','samples':n,'win_rate':wins/n*100,'net_pnl':net,'top_error':top,'confidence':'HIGH' if n>=100 else 'MEDIUM'}
