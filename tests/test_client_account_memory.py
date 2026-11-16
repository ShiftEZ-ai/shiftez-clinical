"""LIVE-50: what this device remembers for one account (rota.* keys) is forgotten when another account signs in and
when the account is removed from the device; the "Continue as" entry itself stays."""
import json
import subprocess



def run(client_modules, steps, out="[...store.keys()].sort()"):
    script = ("const store=new Map(),api={getItem:k=>store.has(k)?store.get(k):null,setItem:(k,v)=>store.set(k,String(v)),removeItem:k=>store.delete(k)};"
              "Object.defineProperty(globalThis,'localStorage',{configurable:true,value:new Proxy(api,{ownKeys:()=>[...store.keys()],"
              "getOwnPropertyDescriptor:(t,k)=>store.has(k)?{enumerable:true,configurable:true,value:store.get(k)}:undefined})});"
              f"const m=await import({json.dumps((client_modules/'account_memory.js').as_uri())});"
              f"{steps};console.log(JSON.stringify({out}));")
    result = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, check=True, timeout=15)
    return json.loads(result.stdout)


def test_another_account_signing_in_forgets_the_first_accounts_views(client_modules):
    seeded = "m.rememberAccount('a','A','a');localStorage.setItem('rota.board.view','{}');localStorage.setItem('rota.table.a:people','{}')"
    assert run(client_modules, seeded + ";m.rememberAccount('a','A','a')") == ['rota.account', 'rota.board.view', 'rota.table.a:people']
    assert run(client_modules, seeded + ";m.rememberAccount('b','B','b')") == ['rota.account']
    assert run(client_modules, seeded + ";m.forgetAccount()") == []
    assert run(client_modules, seeded + ";localStorage.setItem('other.app','1');m.forgetDeviceMemory()") == ['other.app', 'rota.account']



OPERATOR = "m.rememberAccount('op-id','Operator (test)','op1');localStorage.setItem('rota.board.view','{}')"


def test_a_finished_invitation_replaces_the_account_remembered_here(client_modules):
    """maintenance: sign-in offers the account that just set its password, never the one used here before; that account's
    device memory goes too. Only a name and a username are kept."""
    steps = OPERATOR + ";m.accountSetUp('Invited Person (test)','invited.person')"
    assert run(client_modules, steps) == ['rota.account']
    assert run(client_modules, steps, 'm.rememberedAccount()') == {'identity': '', 'name': 'Invited Person (test)', 'username': 'invited.person'}
    stored = run(client_modules, steps, "JSON.parse(localStorage.getItem('rota.account'))")
    assert set(stored) == {'identity', 'name', 'username', 'at'}  # never a password or session
    # Signing in as the new account (its username typed on the sign-in page) records its identity.
    signed_in = steps + ";m.signingInAs('invited.person');m.rememberAccount('new-id','Invited Person (test)',null)"
    assert run(client_modules, signed_in, 'm.rememberedAccount()') == {'identity': 'new-id', 'name': 'Invited Person (test)', 'username': 'invited.person'}


def test_a_password_reset_of_the_same_account_keeps_it(client_modules):
    steps = OPERATOR + ";m.accountSetUp('Operator (test)','op1')"
    assert run(client_modules, steps) == ['rota.account', 'rota.board.view']
    assert run(client_modules, steps, 'm.rememberedAccount()') == {'identity': 'op-id', 'name': 'Operator (test)', 'username': 'op1'}
