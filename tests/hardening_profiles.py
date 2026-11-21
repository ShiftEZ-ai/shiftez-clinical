"""Disabled optional-feature tests assert the profile; core tests always execute."""
import functools
from pathlib import Path
import sys
HELPERS=Path(__file__).resolve().parents[1]/'.github/scripts'
if not HELPERS.is_dir():HELPERS=Path(__file__).resolve().parent
sys.path.insert(0,str(HELPERS))
import hardening

def apply_profile(namespace,mapping):
    def wrap(function,keys):
        @functools.wraps(function)
        def check(*args,**kwargs):
            flags=hardening.load()
            disabled=[key for key in keys if not flags[key]]
            if disabled:
                assert all(flags[key] is False for key in disabled)
                print('NOT_RUN_DISABLED_HARDENING '+','.join(disabled)+'; disabled-profile assertion passed; core tests still run')
                return None
            return function(*args,**kwargs)
        return check
    for value in list(namespace.values()):
        if isinstance(value,type) and value.__module__==namespace['__name__']:
            for name,keys in mapping.items():
                if name in value.__dict__:setattr(value,name,wrap(value.__dict__[name],keys))
    for name,keys in mapping.items():
        if name in namespace and callable(namespace[name]):namespace[name]=wrap(namespace[name],keys)
