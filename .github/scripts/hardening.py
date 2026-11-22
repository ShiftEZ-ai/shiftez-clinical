"""Stop-loss switches, loaded only from protected trusted-source config."""
import hashlib
import json
from pathlib import Path
KEYS={'canaries','node_census','content_classifier','plan_lapse_backstop','window_expiry'}
HERE=Path(__file__).resolve().parent
CONFIG=HERE.parents[1]/'ci/hardening.json' if HERE.name=='scripts' and HERE.parent.name=='.github' else HERE/'hardening.json'

def load(path=None,pin=None):
    raw=Path(path or CONFIG).read_bytes()
    if pin is not None and hashlib.sha256(raw).hexdigest()!=pin:raise ValueError('Hardening config pin mismatch')
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError('Duplicate hardening field')
            result[key]=value
        return result
    data=json.loads(raw,object_pairs_hook=unique)
    if (not isinstance(data,dict) or set(data)!=KEYS|{'schema'} or type(data['schema']) is not int or data['schema']!=1
            or any(type(data[k]) is not bool for k in KEYS)):
        raise ValueError('Invalid protected hardening config')
    return data
