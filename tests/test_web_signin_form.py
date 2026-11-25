"""QA-076: the sign-in steps are one native <form> with a submit button and a submit handler, so Enter, password
managers and assistive tech submit it natively. No keydown handler may stand in for that. (The CI browser test
test_browser_loading.py::test_enter_submits_each_sign_in_step presses Enter on each step.)"""
import re
from pathlib import Path

APP = (Path(__file__).resolve().parents[1] / 'apps/web/src/app.ts').read_text(encoding='utf-8')


def sign_in_source():
    start = APP.index('async function showLogin(')
    end = re.compile(r'^(?:async function|function) ', re.M).search(APP, start + 10)
    return APP[start:end.start()]


def test_each_step_is_a_native_form():
    source = sign_in_source()
    assert "submit=el('button',{type:'submit',class:'button primary'},'Continue')" in source
    assert "form.addEventListener('submit',async e=>{e.preventDefault();" in source
    # The refusal sits directly under the password, like other field errors (design verdict A-1).
    assert "form.append(whoRow,username.wrap,password.wrap,error,remember.wrap,submit)" in source
    # The hidden username for password managers stays in the form during the password step.
    assert "autocomplete:'username'" in source


def test_no_keydown_hack_submits_sign_in():
    source = sign_in_source()
    assert not re.search(r"addEventListener\('key(?:down|press|up)'", source)
    assert 'requestSubmit' not in source
