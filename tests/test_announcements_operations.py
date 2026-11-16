"""Announcements reach exactly their audience; the worker reports it is alive, clears backlogs in one pass,
and staff find their rotas through the publication index; many people can be given a rank in one step."""
from datetime import timedelta
import pytest
from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.schemas import ROLE_NAMES
from tests.outbox_support import process_one_after_undo as process_one
from .governance_support import identity


def announce(h, audience, starts_on="2020-01-01", role="coordinator", notify=True, active=True, name="Clinic closure"):
    return h.command(role, "catalogue.create", {"catalogue": "department_content", "reason": "Tell the team",
        "value": {"name": name, "description": "North clinic closed on Thursday", "starts_on": starts_on, "active": active,
                  "kind": "announcement", "audience": audience, "importance": "important", "notify": notify}})["record"]


def inbox(h, who):
    return [n for n in h.get(who, "/api/notifications")["notifications"] if n["kind"] == "announcement"]


def test_announcement_reaches_only_its_rank_and_named_people(h):
    staff_person = h.get("staff", "/api/me")["person_id"]
    other = next(p for p in h.seed["people"] if p != staff_person)
    identity(h, "colleague", ["staff"], other)
    rank = h.configure("categories", {"name": "Night registrar", "starts_on": "2020-01-01"})["record"]
    h.command("coordinator", "membership.create", {"person_id": staff_person, "category_id": rank["id"], "starts_on": "2020-01-01"})
    row = announce(h, {"category_ids": [rank["id"]]})
    process_one(h.app.state.engine)
    notes = inbox(h, "staff")
    assert notes and notes[0]["title"] == "Important: Clinic closure"
    assert not inbox(h, "colleague")
    # The overview shows it only to the audience, important notices first.
    assert [i["id"] for i in h.get("staff", "/api/content")["items"]][0] == row["id"]
    assert row["id"] not in [i["id"] for i in h.get("colleague", "/api/content")["items"]]
    # Roles match the roles actually held: an administrator is not swept into a "staff" message.
    announce(h, {"roles": ["staff"]}, name="Staff only")
    process_one(h.app.state.engine)
    assert any(n["title"].endswith("Staff only") for n in inbox(h, "colleague"))
    assert not any(n["title"].endswith("Staff only") for n in inbox(h, "admin"))


def test_future_and_switched_off_announcements_are_not_sent_early(h):
    announce(h, {"everyone": True}, starts_on="2099-01-01", name="Next year")
    announce(h, {"everyone": True}, active=False, name="Draft notice")
    process_one(h.app.state.engine)
    assert not inbox(h, "staff")
    with transaction(h.app.state.engine) as db:
        waiting = db.query(m.Outbox).filter(m.Outbox.kind == "announcement", m.Outbox.state == "queued").all()
    assert len(waiting) == 1 and waiting[0].next_attempt_at.year == 2099
    h.command("staff", "catalogue.create", {"catalogue": "department_content", "reason": "x", "value": {"name": "n", "starts_on": "2020-01-01"}}, status=403)
    h.command("coordinator", "catalogue.create", {"catalogue": "department_content", "reason": "x",
              "value": {"name": "n", "starts_on": "2020-01-01", "audience": {"everyone": False}}}, status=422)


def test_worker_heartbeat_and_one_pass_clears_the_backlog(h):
    assert h.get("operator", "/api/operations")["metrics"]["worker_liveness"] == "never_seen"
    for name in ("One", "Two", "Three"):
        announce(h, {"everyone": True}, name=name)
    process_one(h.app.state.engine)
    assert {n["title"] for n in inbox(h, "staff")} == {"Important: One", "Important: Two", "Important: Three"}
    metrics = h.get("operator", "/api/operations")["metrics"]
    assert metrics["worker_liveness"] == "running" and metrics["workers_running"] == 1
    with transaction(h.app.state.engine, write=True) as db:
        for row in db.query(m.WorkerHeartbeat):
            row.last_seen_at = m.now() - timedelta(minutes=5)
    assert h.get("operator", "/api/operations")["metrics"]["worker_liveness"] == "not_responding"


def test_staff_see_only_publications_they_are_on(h):
    me = h.get("staff", "/api/me")["person_id"]
    other = next(p for p in h.seed["people"] if p != me)
    mine = h.publish(h.shift(h.workspace("Mine"), people=[me])["workspace"])
    h.publish(h.shift(h.workspace("Not mine", "2026-10-11", "2026-10-18"), start="2026-10-11T08:00:00+03:00", end="2026-10-11T12:00:00+03:00", people=[other])["workspace"])
    listed = h.get("staff", "/api/publications")["publications"]
    assert [p["id"] for p in listed] == [mine["publication_id"]] and listed[0]["name"] == "Mine"
    assert len(h.get("coordinator", "/api/publications")["publications"]) == 2


def test_give_many_people_a_rank_in_one_step(h):
    rank = h.configure("categories", {"name": "Registrar", "starts_on": "2020-01-01"})["record"]
    first, second = h.seed["people"][:2]
    h.command("coordinator", "membership.create", {"person_id": first, "category_id": rank["id"], "starts_on": "2026-01-01"})
    result = h.command("coordinator", "membership.bulk_create", {"person_ids": [first, second], "category_id": rank["id"],
                       "starts_on": "2026-10-01", "reason": "New registrars"})
    assert result["added"] == 1 and result["already_had_it"] == 1
    senior = h.configure("categories", {"name": "Senior registrar", "starts_on": "2020-01-01"})["record"]
    moved = h.command("coordinator", "membership.bulk_create", {"person_ids": [second], "category_id": senior["id"],
                      "starts_on": "2026-12-01", "end_other_ranks": True, "reason": "Promotion"})
    assert moved["added"] == 1 and moved["other_ranks_ended"] >= 1
    h.command("staff", "membership.bulk_create", {"person_ids": [second], "category_id": senior["id"], "starts_on": "2026-12-01", "reason": "x"}, status=403)


def test_live_stream_carries_the_unread_count(h):
    announce(h, {"everyone": True})
    process_one(h.app.state.engine)
    client = h.client("staff")
    with client.stream("GET", "/api/events?once=true") as response:
        body = "".join(response.iter_text())
    assert '"unread": 1' in body


@pytest.mark.postgres
def test_a_saved_change_is_announced_to_other_server_processes_postgres(pg):
    # Another API process listening on PostgreSQL hears the department change the moment the save commits.
    import psycopg
    import json
    from sqlalchemy.engine import make_url
    conninfo = make_url(pg.settings.database_url).set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(conninfo, autocommit=True) as conn:
        conn.execute("LISTEN rota_changes")
        pg.workspace("Heard elsewhere")
        # Read only this test department's notification from the worker database.
        heard = any(json.loads(note.payload).get("department_id") == pg.seed["department_id"] for note in conn.notifies(timeout=5))
    assert heard


def test_a_changed_or_switched_off_notice_is_not_sent_from_an_older_save(h):
    row = announce(h, {"everyone": True}, name="First wording")
    # Edited before the worker ran (not asking to send again): the older send is stale and skipped.
    h.command("coordinator", "catalogue.update", {"catalogue": "department_content", "reason": "Fix wording",
              "value": {"name": "Second wording", "starts_on": "2020-01-01", "kind": "announcement"}}, row["id"], row["revision"])
    process_one(h.app.state.engine)
    assert not inbox(h, "staff")


def test_long_notices_fit_the_inbox_and_restricted_notices_stay_private(h):
    h.command("coordinator", "catalogue.create", {"catalogue": "department_content", "reason": "Long one",
              "value": {"name": "Long", "description": "x" * 1500, "starts_on": "2020-01-01", "notify": True}})
    process_one(h.app.state.engine)
    note = inbox(h, "staff")[0]
    assert len(note["message"]) <= 500 and note["message"].endswith("(more on Home)")
    announce(h, {"roles": ["coordinator"]}, name="Coordinators only", notify=False)
    assert h.get("approver", "/api/catalogues")["department_content"] == []
    assert any(r["name"] == "Coordinators only" for r in h.get("coordinator", "/api/catalogues")["department_content"])


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_announcement_history_requires_management_even_for_a_previously_known_id(request, database):
    from datetime import date

    h = request.getfixturevalue(database)
    notice = announce(h, {'everyone': True}, notify=False)
    assert notice['id'] in {row['id'] for row in h.get('approver', '/api/content')['items']}
    h.command('coordinator', 'catalogue.update', {'catalogue': 'department_content',
        'reason': 'Synthetic restricted revision', 'value': {'name': notice['name'],
        'description': 'Synthetic administrator-only revision', 'starts_on': '2020-01-01',
        'kind': 'announcement', 'audience': {'roles': ['admin']}, 'importance': 'important',
        'active': True, 'notify': False}}, notice['id'], notice['revision'])
    path = '/api/catalogues/department_content/' + notice['id'] + '/history'
    assert notice['id'] not in {row['id'] for row in h.get('approver', '/api/content')['items']}
    role_sets = [(role,) for role in ROLE_NAMES] + [('staff', role) for role in ROLE_NAMES if role != 'staff']
    for roles in role_sets:
        if len(roles) == 1:
            reader = roles[0]
            if reader not in h.seed['identities']:
                identity(h, reader, roles)
        else:
            reader = 'history_' + '_'.join(roles)
            identity(h, reader, roles)
        if {'admin', 'coordinator'} & set(roles):
            assert 'Synthetic administrator-only revision' in str(h.get(reader, path)['versions'])
        else:
            assert h.get(reader, path, status=403)['error']['code'] == 'FORBIDDEN'
    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=db.get(m.Department, h.seed['department_id']).organisation_id,
                                  name='Synthetic other department')
        db.add(department)
        db.flush()
        foreign = m.DepartmentContent(department_id=department.id, name='Synthetic foreign notice',
                                      starts_on=date(2020, 1, 1))
        db.add(foreign)
        db.flush()
        foreign_id = foreign.id
    h.get('coordinator', '/api/catalogues/department_content/' + foreign_id + '/history', status=404)


def test_moving_rank_needs_an_open_ended_new_rank(h):
    rank = h.configure("categories", {"name": "Fellow", "starts_on": "2020-01-01"})["record"]
    refused = h.command("coordinator", "membership.bulk_create", {"person_ids": [h.seed["people"][0]], "category_id": rank["id"],
                        "starts_on": "2026-10-01", "ends_on": "2026-12-01", "end_other_ranks": True, "reason": "Temporary"}, status=422)
    assert refused["error"]["code"] == "RANK_MOVE_NEEDS_OPEN_END"


def test_conditions_with_and_or_groups_and_none_of(h):
    """"Where rank is any of Night registrar and (tag is none of locum or role is approver)": the tree decides who is told."""
    staff_person = h.get("staff", "/api/me")["person_id"]
    other = next(p for p in h.seed["people"] if p != staff_person)
    identity(h, "colleague", ["staff"], other)
    rank = h.configure("categories", {"name": "Night registrar", "starts_on": "2020-01-01"})["record"]
    for person in (staff_person, other):
        h.command("coordinator", "membership.create", {"person_id": person, "category_id": rank["id"], "starts_on": "2020-01-01"})
    h.command("admin", "catalogue.update", {"catalogue": "people", "reason": "Tag a locum",
        "value": {**{k: v for k, v in next(p for p in h.get("admin", "/api/catalogues")["people"] if p["id"] == other).items()
                     if k in ("display_name", "active", "colour", "short_code", "title")}, "tags": ["Locum"]}},
        other, next(p for p in h.get("admin", "/api/catalogues")["people"] if p["id"] == other)["revision"])
    where = {"match": "all", "conditions": [
        {"field": "category_ids", "op": "any", "values": [rank["id"]]},
        {"match": "any", "conditions": [{"field": "tags", "op": "none", "values": ["locum"]},
                                        {"field": "roles", "op": "any", "values": ["approver"]}]}]}
    announce(h, {"everyone": False, "where": where}, name="Registrars, not locums")
    process_one(h.app.state.engine)
    assert any(n["title"].endswith("Registrars, not locums") for n in inbox(h, "staff"))
    assert not any(n["title"].endswith("Registrars, not locums") for n in inbox(h, "colleague"))
    # Groups nest one level only, and a line needs at least one value.
    deep = {"match": "all", "conditions": [{"match": "any", "conditions": [{"match": "all", "conditions": [
        {"field": "roles", "values": ["staff"]}]}]}]}
    for bad in (deep, {"match": "all", "conditions": [{"field": "roles", "values": []}]}):
        h.command("coordinator", "catalogue.create", {"catalogue": "department_content", "reason": "r",
            "value": {"name": "n", "starts_on": "2020-01-01", "audience": {"everyone": False, "where": bad}}}, status=422)
