"""maintenance / maintenance from the real compiled client in Node (logic only): a Schedule view of only your own duties says
"You have no duties", never "Nothing published"; a request's history never shows a raw command verb."""
import json
import subprocess

SHIM = ("globalThis.document={cookie:'',addEventListener(){},documentElement:{dataset:{}}};"
        "globalThis.matchMedia=()=>({matches:false,addEventListener(){}});globalThis.window=globalThis;"
        "globalThis.addEventListener=()=>{};globalThis.localStorage={getItem:()=>null,setItem(){},removeItem(){}};"
        "globalThis.location={hash:'',pathname:'/',search:''};globalThis.history={pushState(){},replaceState(){}};")


def node(client_modules, module, expression):
    script = (SHIM + f"const m=await import({json.dumps((client_modules / module).as_uri())});"
              f"console.log(JSON.stringify({expression}));")
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, check=True, timeout=15)
    return json.loads(out.stdout)


def test_a_view_of_your_own_duties_says_you_have_none_not_nothing_published(client_modules):
    got = node(client_modules, 'schedule.js', "[m.scheduleEmptyWords('week',{filtered:false,hooks:{mine:true},published:true}),"
               "m.scheduleEmptyWords('week',{filtered:false,hooks:{},published:true}),"
               "m.scheduleEmptyWords('month',{filtered:true,hooks:{mine:true},published:true}),"
               "m.scheduleEmptyWords('week',{filtered:false,hooks:{mine:true,create:'Add session'}})]")
    assert [(w['kind'], w['title']) for w in got] == [
        ('mine', 'You have no duties this week'),      # published, but none of yours: never "Nothing published"
        ('none', 'Nothing published this week'),       # everyone's view with nothing published is still said so
        ('filtered', 'Hidden by filters'),             # your duties exist but the filters hide them
        ('mine', 'You have no duties this week')]      # My duties on over a draft: yours are what is empty


def test_history_words_never_show_a_raw_command_verb(client_modules):
    got = node(client_modules, 'requests.js', "['availability.record','availability.fictional_verb','person.workload.fictional','request.link_rota']"
               ".map(a=>m.eventWord({source:'availability',stage:'approved'},a))")
    assert got == ['Recorded', 'Changed', 'Changed', 'Rota linked']
