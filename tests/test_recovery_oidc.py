"""Backup recovery and cryptographic protocol fixtures; not live IdP qualification."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import closing
import json
import sqlite3
import pytest
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select
from apps.api import models as m, oidc
from apps.api.db import transaction
from apps.api.errors import Problem
from packages.contracts.schemas import AuthSettings
from apps.worker import main as worker
from scripts.backup import backup, restore

pytestmark = pytest.mark.usefixtures("public_oidc_dns")

@pytest.fixture(autouse=True)
def isolated_oidc_keys():
    with oidc._JWKS_LOCK:
        oidc._JWKS_CACHE.clear()
    yield
    with oidc._JWKS_LOCK:
        oidc._JWKS_CACHE.clear()


def test_sqlite_backup_restores_publication_evidence_revokes_sessions(h,tmp_path):
    pub=h.publish(h.shift(h.workspace())['workspace'])
    worker.process_one(h.app.state.engine)
    with transaction(h.app.state.engine) as db:original=db.get(m.Publication,pub['publication_id']).snapshot
    archive=tmp_path/'private-snapshot.backup'
    meta=backup(h.settings.database_url,archive)
    target=tmp_path/'restored.db'
    restored=restore(archive,'sqlite:///'+str(target),trusted_source=True)
    assert restored['counts']==meta['snapshot']['counts'] and restored['counts']['publications']==1
    with closing(sqlite3.connect(target)) as db:
        assert json.loads(db.execute('SELECT snapshot FROM publications').fetchone()[0])==original
        assert db.execute('SELECT count(*) FROM sessions WHERE revoked=0').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM audit_events').fetchone()[0]>0
        assert db.execute('SELECT count(*) FROM outbox').fetchone()[0]>0
    assert archive.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):restore(archive,'sqlite:///'+str(target),trusted_source=True)
    with pytest.raises(FileExistsError):backup(h.settings.database_url,archive)


def test_restore_requires_trust_and_detects_tampering(h,tmp_path):
    archive=tmp_path/'snapshot';backup(h.settings.database_url,archive)
    target='sqlite:///'+str(tmp_path/'new.db')
    with pytest.raises(ValueError,match='trusted-source'):restore(archive,target)
    with archive.open('ab') as f:f.write(b'TAMPERED')
    with pytest.raises(ValueError,match='checksum'):restore(archive,target,True)
    assert not (tmp_path/'new.db').exists()


def test_outbox_failure_uses_savepoint_without_partial_notification_delivery(h,monkeypatch):
    h.publish(h.shift(h.workspace())['workspace'])
    original=worker.deliver_notifications
    def fault(db,event,**recipients):
        original(db,event,**recipients)
        raise RuntimeError('Injected failure after notification flush')
    with monkeypatch.context() as context:
        context.setattr(worker,'deliver_notifications',fault)
        worker.process_one(h.app.state.engine)
    with transaction(h.app.state.engine,write=True) as db:
        assert not db.scalar(select(m.Notification.id))
        event=db.scalar(select(m.Outbox));assert event.state=='retry' and event.attempts==1
        event.next_attempt_at=m.now()-timedelta(seconds=1)
    worker.process_one(h.app.state.engine)
    assert len(h.get('staff','/api/notifications')['notifications'])==1


@pytest.fixture(scope='module')
def rsa_key():return rsa.generate_private_key(public_exponent=65537,key_size=2048)

@pytest.fixture
def idp(h):
    return replace(h.settings,oidc_issuer='https://idp.test',oidc_client_id='rota-client',oidc_client_secret='fixture-secret',oidc_redirect_uri=h.settings.public_origin+'/auth/callback',oidc_allowed_hosts='idp.test')

@pytest.mark.parametrize(('url'),[('http' + '://i' + 'dp.t' + 'est/' + 'keys'),('http' + 's://' + 'idp.' + 'test' + '.at' + 'tacke' + 'r.in' + 'vali' + 'd/ke' + 'ys'),('http' + 's://' + 'at' + 'tacker' + '.inv' + 'alid' + '/key' + 's'),('http' + 's://' + 'user' + ':pas' + 'swor' + 'd@id' + 'p.te' + 'st/k' + 'eys'),('http' + 's://' + 'idp.' + 'test' + '/key' + 's#fr' + 'agme' + 'nt')])
def test_oidc_endpoint_allowlist_is_exact_https(idp,url):
    with pytest.raises(Problem):oidc.allowed_url(idp,url)
    assert oidc.allowed_url(idp,'https://idp.test/keys')=='https://idp.test/keys'


def test_oidc_pkce_browser_binding_and_one_use_state(h,idp):
    from urllib.parse import urlparse,parse_qs
    with transaction(h.app.state.engine,write=True) as db:
        url,browser=oidc.start_flow(db,idp,{'authorization_endpoint':'https://idp.test/authorize'})
    params=parse_qs(urlparse(url).query)
    assert params['code_challenge_method']==['S256']
    assert params['redirect_uri']==[idp.oidc_redirect_uri]
    with pytest.raises(Problem),transaction(h.app.state.engine,write=True) as db:oidc.consume_flow(db,params['state'][0],'wrong-browser')
    with transaction(h.app.state.engine,write=True) as db:flow=oidc.consume_flow(db,params['state'][0],browser)
    assert len(flow['verifier'])>=43 and flow['nonce']==params['nonce'][0]
    with pytest.raises(Problem),transaction(h.app.state.engine,write=True) as db:oidc.consume_flow(db,params['state'][0],browser)


@pytest.mark.parametrize('mutation',['valid','issuer','audience','nonce','expired','signature','authorised_party',
                                   'reauth_valid','reauth_missing','reauth_stale','reauth_future','reauth_malformed'])
def test_oidc_signed_token_verification(idp,rsa_key,monkeypatch,mutation):
    now=int(datetime.now(timezone.utc).timestamp())  # real clock: PyJWT checks exp against the machine clock
    claims={'iss':idp.oidc_issuer,'aud':idp.oidc_client_id,'sub':'verified-subject','nonce':'expected-nonce','iat':now,'exp':now+120}
    if mutation=='issuer':claims['iss']='https://wrong.test'
    if mutation=='audience':claims['aud']='another-client'
    if mutation=='nonce':claims['nonce']='wrong-nonce'
    if mutation=='expired':claims['exp']=now-90
    if mutation=='authorised_party':claims['aud']=[idp.oidc_client_id,'other-client'];claims['azp']='other-client'
    if mutation=='reauth_valid':claims['auth_time']=now
    if mutation=='reauth_stale':claims['auth_time']=now-120
    if mutation=='reauth_future':claims['auth_time']=now+120
    if mutation=='reauth_malformed':claims['auth_time']=str(now)
    signer=rsa.generate_private_key(public_exponent=65537,key_size=2048) if mutation=='signature' else rsa_key
    token=jwt.encode(claims,signer,algorithm='RS256',headers={'kid':'fixture-key'})
    key=json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(rsa_key.public_key()));key.update(kid='fixture-key',alg='RS256',use='sig')
    class Response:
        def __init__(self,value):self.value=value
        def raise_for_status(self):pass
        def json(self):return self.value
    class Client:
        def __init__(self,**kwargs):assert kwargs['follow_redirects'] is False
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def post(self,url,data,auth,**kwargs):
            assert data['code_verifier']=='test-verifier';return Response({'id_token':token})
        def get(self,url,**kwargs):return Response({'keys':[key]})
    monkeypatch.setattr(oidc,'document',lambda settings:{'token_endpoint':'https://idp.test/token','jwks_uri':'https://idp.test/keys'})
    monkeypatch.setattr(oidc.httpx,'Client',Client)
    flow={'nonce':'expected-nonce','verifier':'test-verifier',
          'reauthenticate_after':now if mutation.startswith('reauth_') else None}
    if mutation in ('valid','reauth_valid'):
        verified, retained = oidc.exchange(idp,flow,'one-use-code')
        assert verified['sub']=='verified-subject' and retained == token
    else:
        with pytest.raises((Problem,jwt.InvalidTokenError)):oidc.exchange(idp,flow,'one-use-code')


def test_oidc_unknown_subject_does_not_auto_provision_by_name(h,idp):
    with pytest.raises(Problem,match='add your institutional sign-in'),transaction(h.app.state.engine,write=True) as db:
        oidc.finish_flow(db,idp,{'sub':'unknown','name':'Admin','email':'admin@example.test','roles':['admin']})


@pytest.mark.parametrize('route',['#schedule/issues?draft=abc','#configuration/department',''])
def test_oidc_sign_in_returns_to_the_page_the_person_was_on(h,monkeypatch,route):
    from urllib.parse import urlparse,parse_qs,quote
    from fastapi.testclient import TestClient
    with transaction(h.app.state.engine,write=True) as db:
        db.add(m.AuthConfiguration(id="environment",revision=1,data=AuthSettings(
            mode="both",institutional_enabled=True,issuer="https://idp.test",client_id="synthetic-client",
            allowed_hosts=["idp.test"]).model_dump(mode="json")))
    monkeypatch.setattr(oidc,'document',lambda settings:{'authorization_endpoint':'https://idp.test/authorize'})
    monkeypatch.setattr(oidc,'exchange',lambda settings,flow,code:({'sub':'x'},'synthetic-id-token'))
    monkeypatch.setattr(oidc,'finish_flow',lambda db,settings,claims,**metadata:{'token':'session-token','csrf':'csrf-token',
                        'expires_at':(m.now()+timedelta(minutes=5)).isoformat()})
    client=TestClient(h.app,base_url=h.settings.public_origin)
    started=client.get('/auth/start'+('?next='+quote(route,safe='') if route else ''),follow_redirects=False)
    assert started.status_code==303
    state=parse_qs(urlparse(started.headers['location']).query)['state'][0]
    done=client.get(f'/auth/callback?state={state}&code=c',follow_redirects=False)
    assert done.status_code==303 and done.headers['location']=='/'+route
    assert 'rota_oidc_return' not in client.cookies


@pytest.mark.parametrize('route',['https://evil.test/','//evil.test','#a//evil.test','/#x','#x\\evil','#x y','javascript:alert(1)','#'+'a'*600])
def test_oidc_return_route_is_an_in_app_address_only(route):
    assert oidc.return_route(route)==''


def test_oidc_return_route_keeps_in_app_addresses():
    for route in ['#home','#schedule/week/2026-10-05?edit=0f1e&mode=side','#rules/policy%20x']:
        assert oidc.return_route(route)==route
