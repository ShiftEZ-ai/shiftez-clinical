"""Real migrated database fixtures. Expected results are asserted independently."""
from contextlib import closing, contextmanager, ExitStack
import copy
import json
import os
import re
import sqlite3
import shutil
import subprocess
import uuid
from pathlib import Path
import pytest
pytest_plugins = ["tests.quarantine"]
os.environ.setdefault("ROTA_ENV", "test")  # unset means production (fail safe); tests opt out explicitly
# One test calendar: fixtures use fixed dates (most in 2026-10-04 .. 2026-10-11), so "now" is pinned just before them
# for the app (packages/domain/time.now), the tests and every browser page (test_browser.py), and time flows from
# there. The suite therefore passes on any real date. ROTA_TEST_NOW moves it (e.g. to see what a later date would do).
from packages.domain.time import pin_clock
TEST_NOW = os.getenv("ROTA_TEST_NOW", "2026-10-03T06:00:00+00:00")
pin_clock(TEST_NOW)
from fastapi.testclient import TestClient
from sqlalchemy import select
from apps.api.main import create_app
from apps.api.settings import Settings
from apps.api.services.planning import MANUAL_PLACEMENT_BLOCKERS
from apps.api.db import make_engine, transaction
from apps.api import models as m

from argon2 import PasswordHasher
import scripts.manage
from scripts.manage import migrate, demo_data
from tests.browser_seed import capture_state, restore_state

# Seeded test accounts get the cheapest Argon2 parameters: verification reads the parameters from the stored
# hash, so every test sign-in stops paying ~0.1 s of deliberate slowness. Production hashing is untouched.
scripts.manage.PASSWORDS = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)

TEST_PASSWORD = 'obviously-fake-test-password-123!'
ROLES = ("admin", "coordinator", "approver", "credentialer", "staff", "operator", "auditor")


@pytest.fixture
def synthetic_initial_seed(tmp_path, monkeypatch):
    """Supply invented configuration through the loader's existing directory boundary."""
    from scripts import initial_seed
    directory = tmp_path / "synthetic-seed"
    directory.mkdir()
    shutil.copyfile(Path(__file__).parent / "fixtures/seeds/synthetic-v1.json", directory / "production-v1.json")
    monkeypatch.setattr(initial_seed, "SEEDS", directory)
    return directory


@pytest.fixture
def public_oidc_dns(monkeypatch):
    """Synthetic provider hosts resolve publicly; OIDC network traffic remains mocked by each fixture."""
    from apps.api import oidc
    resolve = oidc.socket.getaddrinfo

    def addresses(host, port, *args, **kwargs):
        if isinstance(host, str) and host.endswith(".test"):
            return [(oidc.socket.AF_INET, oidc.socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
        return resolve(host, port, *args, **kwargs)

    monkeypatch.setattr(oidc.socket, "getaddrinfo", addresses)


@pytest.fixture(scope="session")
def client_modules(tmp_path_factory):
    """CI shares a source-bound build; consumers only read it in fresh Node processes."""
    from scripts.compiled_client import prepare, shared_modules
    root = Path(__file__).resolve().parents[1]
    if os.environ.get('FMUC_CLIENT_MODULES'):
        return shared_modules(root)
    return prepare(tmp_path_factory.mktemp('compiled-client'), root)


@pytest.fixture(scope="session")
def database_template(tmp_path_factory):
    path = tmp_path_factory.mktemp("template") / "base.db"
    url = "sqlite:///" + str(path)
    migrate(url)
    engine = make_engine(url)
    with transaction(engine, write=True) as db:
        seed = demo_data(db, {role: TEST_PASSWORD for role in ROLES})
    engine.dispose()
    return path, seed

class Harness:
    def __init__(self, url, seed):
        self.settings = Settings(environment="test", database_url=url,
                                 sqlite_synchronous=os.getenv("ROTA_SQLITE_SYNCHRONOUS") or ("OFF" if os.getenv("ROTA_ALLOW_DISPOSABLE_TEST_DB") == "yes" else "FULL"))
        self.app = create_app(self.settings)
        self.seed = copy.deepcopy(seed)
        self.clients = {}

    def client(self, role):
        if role not in self.clients:
            client = TestClient(self.app, base_url=self.settings.public_origin)
            client.headers["origin"] = self.settings.public_origin
            response = client.post("/auth/login", json={"username": role, "password": TEST_PASSWORD})
            assert response.status_code == 200, response.text
            client.headers["x-csrf-token"] = client.cookies["rota_csrf"]
            self.clients[role] = client
        return self.clients[role]

    def process_jobs(self, **kwargs):
        """Run queued work through the next generation job with its frozen engine."""
        from dataclasses import replace
        from apps.worker.main import process_one
        if 'settings' in kwargs:
            return process_one(self.app.state.engine, **kwargs)
        with transaction(self.app.state.engine) as db:
            queued = list(db.scalars(select(m.Job).where(m.Job.status == 'queued')
                                    .order_by(m.Job.created_at, m.Job.id)))
            target_id = next((job.id for job in queued if job.kind == 'generation'), None)
        while True:
            with transaction(self.app.state.engine) as db:
                queued = list(db.scalars(select(m.Job).where(m.Job.status == 'queued')
                                        .order_by(m.Job.created_at, m.Job.id)))
                engines = {job.input_snapshot.get('generation_engine', 'legacy')
                           for job in queued if job.kind == 'generation'}
                if len(engines) > 1:
                    raise AssertionError('process_jobs found queued generation jobs with different engines; process one engine at a time')
                next_job = queued[0] if queued else None
                if next_job is not None and next_job.kind == 'generation':
                    kwargs['settings'] = replace(self.settings,
                                                  generation_engine=next_job.input_snapshot.get('generation_engine', 'legacy'))
                else:
                    kwargs.pop('settings', None)
            processed = process_one(self.app.state.engine, **kwargs)
            if target_id is None:
                return processed
            with transaction(self.app.state.engine) as db:
                target = db.get(m.Job, target_id)
                if target is None or target.status not in {'queued', 'running'}:
                    return processed
            if not processed:
                return processed

    def command(self, role, kind, payload=None, target=None, revision=None, status=200, key=None, expected_fingerprint=None):
        response = self.client(role).post("/api/commands", headers={"idempotency-key": key or str(uuid.uuid4())},
            json={"kind": kind, "payload": payload or {}, "target_id": target, "expected_revision": revision,
                  **({"expected_fingerprint": expected_fingerprint} if expected_fingerprint is not None else {})})
        assert response.status_code in (status if isinstance(status, tuple) else (status,)), response.text
        reply = response.json()
        # Command replies carry the rota check as a summary only (serialization.compact_reply); tests that assert on
        # the findings read the full check the server stored on the rota, which is the same evaluation.
        workspace = reply.get("workspace")
        summary = reply.get("validation") or (workspace or {}).get("validation")
        if response.status_code == 200 and isinstance(workspace, dict) and workspace.get("id") and isinstance(summary, dict) and "findings" not in summary:
            read = self.client("coordinator").get("/api/workspaces/" + workspace["id"])
            stored = (read.json()["workspace"].get("validation") if read.status_code == 200 else None) or {}
            if stored.get('scope') == 'command_changes':
                # A detail-only current score is not part of the retained partial command check.
                stored = {key: value for key, value in stored.items() if key != 'score'}
            if stored.get("evaluated_at") == summary.get("evaluated_at"):
                reply = {**reply, "workspace": {**workspace, "validation": stored},
                         **({"validation": stored} if "validation" in reply else {})}
        return reply

    def get(self, role, path, status=200):
        response = self.client(role).get(path)
        assert response.status_code == status, response.text
        return response.json()

    def committed(self, role, key):
        response = self.client(role).get('/api/commands/' + key)
        if response.status_code == 404:
            assert response.json()['error']['code'] == 'RECEIPT_NOT_FOUND', response.text
            return None
        assert response.status_code == 200, response.text
        return response.json()['result']

    def route_json(self, role, path, transform):
        """Prefetch a simulated JSON answer in process; the route callback only fulfils it."""
        body = transform(self.get(role, path))
        return lambda route: route.fulfill(json=body)

    def workspace(self, name="Independent acceptance fixture", start="2026-10-04", end="2026-10-11", base=None):
        return self.command("coordinator", "workspace.create", {"name": name, "starts_on": start, "ends_on": end, "base_publication_id": base})["workspace"]

    def detail(self, workspace):
        return self.get("coordinator", "/api/workspaces/" + (workspace if isinstance(workspace,str) else workspace["id"]))

    def shift(self, w, start="2026-10-04T08:00:00+03:00", end="2026-10-04T12:00:00+03:00", people=None, service=0, location=0, typ=None, attributes=None, status=200, role="coordinator", *, seed_restricted=False):
        """Strict shift.create unless seed_restricted opts into retaining a person a manual reason cannot place:
        restriction, non-admissible unavailability or away rotation.

        A restricted seed is audited as bulk.apply with a bulk reason and lands as a bulk change set:
        history or Undo immediately afterwards sees a bulk entry, not a single shift.create.
        """
        people = [self.seed["people"][0]] if people is None else people
        participants = [p if isinstance(p,dict) else {"person_id": p, "start": start, "end": end} for p in people]
        reason = "Synthetic assignment for independent validation"
        payload = {"workspace_id": w["id"], "assignment_type_id": typ or self.seed["type"],
            "location_id": self.seed["locations"][location], "service_ids": [self.seed["services"][service]], "start": start, "end": end,
            "participants": participants, "attributes": attributes or {},
            "override_reason": reason}
        reply = self.command(role, "shift.create", payload, revision=w["revision"],
            status=(200, 422) if seed_restricted and status == 200 else status)
        if not (seed_restricted and status == 200 and "error" in reply):
            return reply
        error = reply["error"]
        details = error.get("details") if isinstance(error, dict) else None
        findings = details.get("findings", []) if isinstance(details, dict) else []
        restricted = (isinstance(error, dict) and error.get("code") == "EDIT_INVALID" and bool(findings)
                      and isinstance(findings, list) and all(isinstance(f, dict) and f.get("code") in MANUAL_PLACEMENT_BLOCKERS for f in findings))
        assert restricted, json.dumps(reply)
        # RUL-E04 bulk seeds retain these findings; a manual reason cannot admit the blocker.
        payload.pop("override_reason")
        self.command(role, "bulk.apply", {"items": [{"item_kind": "shift.create", "target_id": w["id"],
            "expected_revision": w["revision"], "payload": payload}], "reason": reason})
        return {"workspace": self.detail(w)["workspace"]}

    def lifecycle(self, w, action, role="coordinator", status=200):
        return self.command(role, "workspace."+action, {"reason": "Independent test review decision"}, w["id"], w["revision"], status=status)

    def refresh_draft_baseline(self, draft, role="coordinator"):
        """Refresh a draft's immutable inputs after a fixture changes department sources."""
        preview = self.command(role, "workspace.baseline.refresh.preview", {}, draft["id"], draft["revision"])
        applied = self.command(role, "workspace.baseline.refresh.apply", {
            "expected_baseline_hash": preview["baseline"]["snapshot_hash"],
            "decisions": {source: "use_department" for source in preview["conflicts"]},
            "reason": "Refresh the draft baseline for reviewed fixture inputs",
        }, draft["id"], draft["revision"])
        return applied["workspace"]

    def publish(self, w):
        w = self.lifecycle(w,"submit")["workspace"]
        w = self.lifecycle(w,"approve","approver")["workspace"]
        return self.lifecycle(w,"publish")

    def configure(self, kind, value, target=None, revision=None, role="admin", status=200):
        return self.command(role, "catalogue.update" if target else "catalogue.create", {"catalogue":kind,"value":value,"reason":"Synthetic configuration for independent acceptance"}, target, revision, status=status)

    def policy(self, rule, scope=None, severity="mandatory", exception=False, workspace_id=None, checked=()):
        """An active synthetic rule. `checked`: the current rotas it reaches, each given the feasibility check a CP-SAT
        deployment requires before activation (policies.require_context_receipts), run by the test worker."""
        proposal = self.command("coordinator","policy.create", {"name":"Synthetic rule " + str(uuid.uuid4())[:8],"owner":"Test policy owner","source":"Constructed fixture, not approved hospital policy","starts_on":"2020-01-01",
            "definition":{"rule":rule,"scope":scope or {},"severity":severity,"exception_allowed":exception,"workspace_id":workspace_id}})["record"]
        checks = []
        if checked:
            from apps.worker.main import process_one
            for w in checked:
                w = self.detail(w)["workspace"]
                checks.append(self.command("approver", "job.create", {"workspace_id": w["id"], "kind": "feasibility",
                    "rule_changes": [{"policy_id": proposal["id"], "expected_revision": proposal["revision"]}]}, revision=w["revision"])["job"]["id"])
                assert process_one(self.app.state.engine)
        return self.command("approver","policy.activate",{"reason":"Independent synthetic review",**({"check_job_ids":checks} if checks else {})},proposal["id"],proposal["revision"])["record"]

    def stored_advisory_safety(self, rule, scope=None):
        """Retain a historical severity that current authoring refuses."""
        policy = self.policy(rule, scope)
        with transaction(self.app.state.engine, write=True) as db:
            row = db.get(m.Policy, policy['id'])
            row.definition = {**row.definition, 'severity': 'advisory'}
            db.get(m.Organisation, self.seed['organisation_id']).input_revision += 1
            policy['definition'] = row.definition
        return policy

    def cover_target(self, amount, scope=None, *, settings=None, strength='medium'):
        from packages.domain.rule_catalogue_migration import who_where

        scope = scope or {}
        who, where = who_where(scope)
        current = next(e['rule'] for e in self.get('coordinator', '/api/rules/catalogue')['entries']
                       if e['id'] == 'people_per_session')
        lines = list(current['definition']['rule']['lines']) if current else []
        lines.append({'id': str(uuid.uuid4()), 'who': who, 'where': where,
                      **{k: scope[k] for k in ('weekdays', 'start_time', 'end_time') if k in scope},
                      'numbers': {'target': {'value': amount, 'level': strength}},
                      'settings': {'minimum_cover': settings} if settings else {}})
        proposal = self.command('coordinator', 'rule.propose', {
            'catalogue_id': 'people_per_session', 'lines': lines, 'reason': 'Synthetic cover target',
            'expected_policy_id': current['id'] if current else None,
            'expected_revision': current['revision'] if current else None,
        })['record']
        return self.command('approver', 'policy.activate', {'reason': 'Independent synthetic review'},
                            proposal['id'], proposal['revision'])['record']

    def snapshot(self,w):
        from apps.api.snapshots import domain_snapshot
        with transaction(self.app.state.engine) as db:
            return domain_snapshot(db, db.get(m.Workspace,w["id"]))

def sqlite_harness(tmp_path, database_template):
    template,seed=database_template
    path=tmp_path/"test.db"
    with closing(sqlite3.connect(template)) as source, closing(sqlite3.connect(path)) as destination:
        source.backup(destination)
    harness=Harness("sqlite:///"+str(path),seed)
    yield harness
    for client in harness.clients.values():
        client.close()
    harness.app.state.engine.dispose()


@pytest.fixture
def h(tmp_path, database_template, request):
    if getattr(request, 'param', 'sqlite') == 'postgresql':
        yield request.getfixturevalue('pg')
        return
    for harness in sqlite_harness(tmp_path, database_template):
        if request.node.get_closest_marker('browser'):
            scenario_cache = request.getfixturevalue('scenario_cache')
            harness.cached_scenario = lambda build, *args, **kw: scenario_cache(harness, build, args, kw)
        yield harness


def pytest_generate_tests(metafunc):
    if 'score_check_enabled' in metafunc.fixturenames:
        metafunc.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items):
    """Mark PostgreSQL dependencies before pytest applies either marker spelling."""
    for item in items:
        callspec = getattr(item, 'callspec', None)
        parameters = getattr(callspec, 'params', {})
        if (any(item.get_closest_marker(name) is not None for name in ('postgres', 'postgresql'))
                or {'pg', 'postgres_worker_url', 'postgres_template'} & set(item.fixturenames)
                or any(parameters.get(name) == 'postgresql' for name in ('h', 'harness'))
                or ('postgresql' in getattr(callspec, 'id', '').split('-')
                    and any(isinstance(value, str) and value in {'pg', 'postgres', 'postgresql'}
                            for value in parameters.values()))):
            for name in ('postgres', 'postgresql'):
                if item.get_closest_marker(name) is None:
                    item.add_marker(getattr(pytest.mark, name))


@pytest.fixture
def score_check_enabled(h):
    """Feature tests opt in explicitly; ordinary harnesses retain the shipped off state."""
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed['department_id']).score_check_enabled = True


@pytest.fixture
def cp_sat_engine(request):
    """Use the CP-SAT scheduler for score-check tests that queue generation jobs."""
    from dataclasses import replace
    callspec = getattr(request.node, 'callspec', None)
    harness_name = getattr(callspec, 'params', {}).get('backend', 'h')
    if harness_name not in {'h', 'pg'}:
        harness_name = 'h'
    h = request.getfixturevalue(harness_name)
    h.settings = replace(h.settings, generation_engine='cp_sat')
    h.app = create_app(h.settings, engine=h.app.state.engine)
    return h


@pytest.fixture(scope="session")
def golden_case(tmp_path_factory, database_template):
    """One API-seeded isolated database and one execution per reviewed scenario."""
    from scripts.golden_scenarios import compile_scenario, load_pack
    from tests.golden_checks import check_scenario

    from functools import cache
    specs = {spec['id']: spec for spec in load_pack()}

    @cache
    def run(scenario_id):
        spec = specs[scenario_id]
        fixture = sqlite_harness(tmp_path_factory.mktemp('golden-' + spec['id']), database_template)
        harness = next(fixture)
        try:
            from dataclasses import replace
            harness.app.state.engine.dispose()
            harness.settings = replace(harness.settings, generation_engine='cp_sat')
            harness.app = create_app(harness.settings)
            from apps.worker.main import process_one
            compiled = compile_scenario(harness.command, spec, read=harness.get, committed=harness.committed,
                work=lambda: process_one(harness.app.state.engine, solver_workers=1))
            from tests.test_staff_wishes import department_settings
            me = harness.get('admin', '/api/me')
            harness.command('admin', 'department.settings', {
                **department_settings(harness), 'generation_defaults': compiled['generation_options']},
                me['department']['id'], me['organisation']['revision'])
            draft = harness.refresh_draft_baseline(harness.detail(compiled['workspace'])['workspace'])
            data = harness.snapshot(draft)
            return check_scenario(data, compiled['ids'], spec)
        finally:
            next(fixture, None)
    return run


@pytest.fixture(scope="session")
def golden_receipts(golden_case):
    from scripts.golden_scenarios import load_pack

    return {spec['id']: golden_case(spec['id']) for spec in load_pack()}


@pytest.fixture(scope='session')
def scenario_cache(tmp_path_factory, database_template):
    """One immutable configured seed per browser scenario/worker; each test still owns its DB and contexts."""
    cached = {}
    def signature(path):
        with closing(sqlite3.connect(path)) as db:
            return tuple(db.iterdump())
    pristine = signature(database_template[0])
    def restore(harness, build, args=(), kw=None):
        kw = kw or {}
        url = harness.settings.database_url
        # Never overwrite a composed fixture, an earlier login, or direct SQL setup.
        if (not url.startswith('sqlite:///') or harness.clients or getattr(harness, 'browser_auth', None)
                or signature(url.removeprefix('sqlite:///')) != pristine):
            return build(harness, *args, **kw)
        key = build, args, tuple(sorted(kw.items()))
        if key not in cached:
            result = build(harness, *args, **kw)
            path = tmp_path_factory.mktemp('browser-scenario') / 'seed.db'
            with closing(sqlite3.connect(url.removeprefix('sqlite:///'))) as source, closing(sqlite3.connect(path)) as destination:
                source.backup(destination)
            auth = {role: dict(client.cookies) for role, client in harness.clients.items()}
            cached[key] = path, copy.deepcopy(result), capture_state(harness), auth
        else:
            path, _, _, _ = cached[key]
            harness.app.state.engine.dispose()
            with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(url.removeprefix('sqlite:///'))) as destination:
                source.backup(destination)
        _, result, state, auth = cached[key]
        restore_state(harness, state)
        harness.browser_auth = copy.deepcopy(auth)
        return copy.deepcopy(result)
    return restore


@pytest.fixture(scope="module")
def scale_harness(tmp_path_factory, database_template, request):
    """Own an isolated database per shared scale measurement, with normal harness cleanup."""
    def create(database="h"):
        if database == "pg":
            fixture = postgres_harness(request.getfixturevalue("postgres_worker_url"), request.getfixturevalue("postgres_template"))
        elif database == "h":
            fixture = sqlite_harness(tmp_path_factory.mktemp("scale"), database_template)
        else:
            raise ValueError("Unknown scale database: " + database)
        harness = next(fixture)
        request.addfinalizer(lambda: next(fixture, None))
        return harness
    return create


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    """Each phase's report on the test item (rep_setup, rep_call): with PW_TRACE_DIR, browser traces are kept only on failure."""
    report = yield
    setattr(item, "rep_" + report.when, report)
    return report


# --disable-dev-shm-usage: container runners give /dev/shm Docker's 64 MiB default, too small for Chromium's shared memory.
CHROMIUM_ARGS=["--no-sandbox","--disable-dev-shm-usage"]


class BrowserSession:
    """The worker's Playwright driver and Chromium, started again when either has died, so one crash fails its own test
    instead of every later test on the worker (maintenance browser 2/6: one dead driver connection failed 177 tests, maintenance)."""
    def __init__(self,start,launch):
        self._start,self._launch,self.playwright,self.browser=start,launch,None,None
        self.last_test=None  # the test that last took the browser: named when the next one finds it dead

    def alive(self):
        # shortcut: Playwright has no public probe for a dead driver connection (playwright 1.64.1 sync API: its dispatcher
        # greenlet ends and every call raises "Playwright connection closed"); re-check on a Playwright upgrade.
        return (self.browser is not None and self.browser.is_connected()
                and not getattr(getattr(self.browser,"_dispatcher_fiber",None),"dead",False))

    def ensure(self):
        if self.alive():return self.browser
        if self.browser is not None:
            import warnings
            warnings.warn("Playwright and Chromium relaunched: the previous test left a dead browser or driver connection; "
                          "its own failure is the original crash"+(f" (first victim: {self.last_test})" if self.last_test else ""))
            self.close()
        self.playwright=self._start()
        try:self.browser=self._launch(self.playwright)
        except BaseException:
            self.close()
            raise
        return self.browser

    def close(self):
        browser,playwright,self.browser,self.playwright=self.browser,self.playwright,None,None
        for stop in (browser and browser.close,playwright and playwright.stop):
            if stop is None:continue
            try:stop()
            except Exception:pass  # already dead; stopping the driver closes its Chromium too


@pytest.fixture(scope="session")
def chromium():
    """Launched once per test session (per xdist worker): a cold Chromium start costs about a second per test."""
    from playwright.sync_api import sync_playwright
    # Without CHROMIUM_EXECUTABLE: Playwright's pinned full Chromium (channel "chromium", new headless) on every host, not
    # whatever `chromium` a runner image has on PATH, nor the default headless shell, which reports Notification.permission
    # "denied" whatever the context grants.
    executable=os.getenv("CHROMIUM_EXECUTABLE") or None
    def launch(p):
        try:return p.chromium.launch(executable_path=executable,channel=None if executable else "chromium",headless=True,args=CHROMIUM_ARGS)
        except Exception as exc:pytest.fail("A real Chromium is required for browser qualification: "+str(exc))
    session=BrowserSession(lambda:sync_playwright().start(),launch)
    session.ensure()
    yield session
    session.close()


def disposable_postgres_url():
    """Validate destructive test authorization before provisioning any database or schema."""
    url=os.getenv('TEST_POSTGRES_URL')
    if os.getenv('ROTA_ENV')=='production':pytest.fail('Disposable PostgreSQL fixtures create and drop databases and schemas; never run them with ROTA_ENV=production')
    if not url or os.getenv('ROTA_ALLOW_DISPOSABLE_TEST_DB')!='yes':
        pytest.fail('PostgreSQL qualification requires TEST_POSTGRES_URL and ROTA_ALLOW_DISPOSABLE_TEST_DB=yes for a disposable database; no SQLite fallback')
    if not url.startswith('postgresql+psycopg://'):pytest.fail('Use the pinned psycopg PostgreSQL driver')
    return url


@contextmanager
def postgres_database(url, name, template=None):
    """Create and drop only this fixture's random database, including on setup failure."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    disposable_postgres_url()
    if not re.fullmatch(r'rota_(?:worker|template|test)_[0-9a-f]{32}',name):
        raise ValueError('Refusing a database not owned by the disposable fixture')
    if template is not None and not re.fullmatch(r'rota_template_[0-9a-f]{32}',template):
        raise ValueError('Refusing a template not owned by the disposable fixture')
    admin=create_engine(url,isolation_level='AUTOCOMMIT',hide_parameters=True)
    created=False
    try:
        with admin.connect() as db:db.execute(text('CREATE DATABASE '+name+(' TEMPLATE '+template if template else '')))
        created=True
        yield make_url(url).set(database=name).render_as_string(hide_password=False)
    finally:
        try:
            if created:
                with admin.connect() as db:db.execute(text('DROP DATABASE '+name+' WITH (FORCE)'))
        finally:
            admin.dispose()


@pytest.fixture(scope="session")
def postgres_worker_url():
    """One empty owned database per worker for tests exercising migration predecessors."""
    with postgres_database(disposable_postgres_url(),'rota_worker_'+uuid.uuid4().hex) as url:
        yield url


@contextmanager
def postgres_template(postgres_worker_url):
    """Build an owned migrated seed; close every connection before cloning."""
    from sqlalchemy import text
    from sqlalchemy.engine import make_url
    with postgres_database(postgres_worker_url,'rota_template_'+uuid.uuid4().hex) as url:
        schema='rota_test_'+uuid.uuid4().hex
        engine=make_engine(url)
        try:
            with engine.begin() as db:db.execute(text('CREATE SCHEMA '+schema))
        finally:
            engine.dispose()
        scoped_url=make_url(url).update_query_dict({'options':'-csearch_path='+schema}).render_as_string(hide_password=False)
        migrate(scoped_url)
        engine=make_engine(scoped_url)
        try:
            with transaction(engine,write=True) as db:seed=demo_data(db,{role:TEST_PASSWORD for role in ROLES})
        finally:
            engine.dispose()
        yield scoped_url,seed


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node):
    """The xdist controller owns one seed until all workers have stopped."""
    if not os.getenv('TEST_POSTGRES_URL') or os.getenv('ROTA_ALLOW_DISPOSABLE_TEST_DB')!='yes' or os.getenv('ROTA_ENV')=='production':
        return
    config=node.config
    if not hasattr(config,'_rota_postgres_template'):
        stack=ExitStack()
        config.add_cleanup(stack.close)
        config._rota_postgres_template=stack.enter_context(postgres_template(disposable_postgres_url()))
    node.workerinput['rota_postgres_template']=config._rota_postgres_template


@pytest.fixture(scope="session",name="postgres_template")
def worker_postgres_template(postgres_worker_url,request):
    """Clone the controller seed per worker, retaining private templates and test databases."""
    from sqlalchemy.engine import make_url
    disposable_postgres_url()
    shared=getattr(request.config,'workerinput',{}).get('rota_postgres_template')
    if shared is None:
        with postgres_template(postgres_worker_url) as template:
            yield template
    else:
        template_url,seed=shared
        template=make_url(template_url)
        with postgres_database(postgres_worker_url,'rota_template_'+uuid.uuid4().hex,template.database) as url:
            scoped_url=make_url(url).update_query_dict(template.query).render_as_string(hide_password=False)
            yield scoped_url,copy.deepcopy(seed)


def postgres_harness(postgres_worker_url, postgres_template):
    """Clone the migrated seed into a fresh database; locks and rows are private to each test."""
    from sqlalchemy.engine import make_url
    template_url,seed=postgres_template
    template=make_url(template_url)
    with postgres_database(postgres_worker_url,'rota_test_'+uuid.uuid4().hex,template.database) as url:
        scoped_url=make_url(url).update_query_dict(template.query).render_as_string(hide_password=False)
        harness=Harness(scoped_url,seed)
        try:
            yield harness
        finally:
            try:
                for client in harness.clients.values():client.close()
            finally:
                harness.app.state.engine.dispose()


@pytest.fixture
def pg(postgres_worker_url, postgres_template):
    """Authorised disposable PostgreSQL harness shared by API and live-browser gates."""
    yield from postgres_harness(postgres_worker_url, postgres_template)


# The job running the suite (own runner or cloud pool) must not leak its identity into tests that compute a runner
# class or admission; tests that need these set them explicitly with monkeypatch.
RUNNER_IDENTITY_ENV = ('CI_CLOUD_POOL_JOB', 'CI_CLOUD_POOL_ENABLED', 'CI_CLOUD_QUEUE_ON_IDLE0', 'CI_RUNNER_LABELS', 'RUNNER_LABELS', 'RUNNER_NAME')


@pytest.fixture(autouse=True)
def release_fastapi_endpoint_caches():
    """FastAPI keeps up to 4096 endpoint callables in module-level LRU caches; each test app's closures would
    keep ~15 finished apps (engines, statement caches) alive per worker, so later tests pay for their GC scans."""
    yield
    from fastapi.dependencies import models as dependency_models
    for name in ('_is_gen_callable_cached', '_is_async_gen_callable_cached', '_is_coroutine_callable_cached'):
        getattr(dependency_models, name).cache_clear()


@pytest.fixture(autouse=True)
def isolated_runner_identity(monkeypatch):
    for key in RUNNER_IDENTITY_ENV:
        monkeypatch.delenv(key, raising=False)
