"""Small independent arithmetic/interval oracles; no legacy output as a golden truth."""
import copy
from datetime import date, datetime, timedelta, timezone
import uuid
import pytest
from pydantic import ValidationError
from packages.contracts.schemas import TypeDefinition, CustomField, SeriesDefinition, InstantInterval, RequirementSegment, validate_attributes
from packages.domain.time import aware, overlaps, union_hours, local_stamp
from packages.domain.validation import evaluate, matching_size
from packages.domain.recurrence import expand
from packages.domain.reports import safe_cell, fold_ics

@pytest.mark.parametrize("a,b,c,d,expected",[(0,4,4,8,False),(0,4,3,8,True),(0,4,0,4,True),(0,4,-3,0,False),(23,28,25,30,True),(0,1,0.5,0.75,True)])
def test_end_exclusive_overlap_oracles(a,b,c,d,expected):
    base=datetime(2026,10,4,tzinfo=timezone.utc)
    assert overlaps(*(base+timedelta(hours=t) for t in (a,b,c,d))) is expected

@pytest.mark.parametrize("intervals,expected",[([(0,4),(2,6)],6),([(0,4),(4,8)],8),([(0,4),(0,4)],4),([(0,4),(5,6)],5),([],0),([(4,4)],0),([(23,27),(26,30)],7)])
def test_elapsed_union_does_not_double_count(intervals,expected):
    base=datetime(2026,10,4,tzinfo=timezone.utc)
    assert union_hours([(base+timedelta(hours=a),base+timedelta(hours=b)) for a,b in intervals])==expected

@pytest.mark.parametrize("day,clock,zone",[(date(2026,3,8),"02:30","America/New_York"),(date(2026,11,1),"01:30","America/New_York")])
def test_ambiguous_or_nonexistent_dst_rejected(day,clock,zone):
    with pytest.raises(ValueError):local_stamp(day,clock,zone)


def test_midnight_2400_normalizes_and_riyadh_overnight_has_correct_date():
    assert local_stamp(date(2026,10,4),"24:00","Asia/Riyadh")==datetime(2026,10,4,21,tzinfo=timezone.utc)

@pytest.mark.parametrize("value",["=SUM(A1:A2)"," +cmd","-2+3","@import","\tformula","\rmalicious","\ninjection"])
def test_spreadsheet_formula_injection_escaped(value):
    assert safe_cell(value).startswith("'")


def test_ics_folding_is_by_utf8_octets_and_unfolds_exactly():
    original="SUMMARY:"+"باجن عام 🩺 "*30
    folded=fold_ics(original)
    assert all(len(line.encode())<=75 for line in folded.split("\r\n"))
    assert folded.replace("\r\n ","")==original

@pytest.mark.parametrize("start,end",[("2026-10-04T08:00:00","2026-10-04T12:00:00Z"),("2026-10-04T08:00:00Z","2026-10-04T08:00:00Z"),("2026-10-04T12:00:00Z","2026-10-04T08:00:00Z")])
def test_instants_require_offsets_and_positive_duration(start,end):
    with pytest.raises(ValidationError):InstantInterval(start=start,end=end)

@pytest.mark.parametrize("bad",[{"schema_version":2},{"allow_overlap":True},{"purpose":"on_call"},{"allow_shared_services":True},{"purpose":"protected"},{"maximum_duration_minutes":4000}])
def test_unsupported_behaviour_compositions_are_not_silent(bad):
    with pytest.raises(ValidationError):TypeDefinition.model_validate(bad)

@pytest.mark.parametrize("value",[True,float('nan'),float('inf'),-1,11,"4"])
def test_typed_number_is_not_boolean_or_nonfinite_or_text(value):
    typ=TypeDefinition(fields=[CustomField(key="capacity",label="Capacity",kind="number",minimum=0,maximum=10,required=True)])
    with pytest.raises(ValueError):validate_attributes(typ,{"capacity":value})


def test_conditional_custom_field_is_enforced_server_side():
    typ=TypeDefinition.model_validate({"fields":[{"key":"needs_detail","label":"Needs detail","kind":"boolean"},{"key":"detail","label":"Detail","kind":"text","required_when":{"field":"needs_detail","equals":True}}]})
    validate_attributes(typ,{"needs_detail":False})
    with pytest.raises(ValueError):validate_attributes(typ,{"needs_detail":True})
    validate_attributes(typ,{"needs_detail":True,"detail":"Reviewed"})
    with pytest.raises(ValueError):validate_attributes(typ,{"unrecognised":"silently discard?"})


def test_matching_distinct_capability_seats():
    assert matching_size(["lead","supervisor"],{"one":{"lead","supervisor"}})==1
    assert matching_size(["lead","supervisor"],{"one":{"lead","supervisor"},"two":{"lead"}})==2


def prepared(h,definition=None,people=None):
    if definition is not None:
        typ=h.configure("assignment_types",{"name":"Fresh type "+str(uuid.uuid4()),"starts_on":"2020-01-01","definition":definition})["record"]["id"]
    else:typ=None
    result=h.shift(h.workspace(),typ=typ,people=people)
    return h.snapshot(result["workspace"]),result


def test_time_varying_requirement_uses_each_boundary_not_day_headcount(h):
    pid=h.seed["people"][0]
    data,result=prepared(h,{"requirements":[{"from_minute":0,"to_minute":120,"minimum":1},{"from_minute":120,"minimum":2}]})
    gaps=[f for f in result["validation"]["findings"] if f["code"]=="COVERAGE_GAP"]
    assert len(gaps)==1
    assert gaps[0]["start"]=="2026-10-04T07:00:00+00:00" and gaps[0]["end"]=="2026-10-04T09:00:00+00:00"
    assert gaps[0]["detail"]=={"required":2,"available":1}


def test_capability_co_counting_vs_distinct_people(h):
    definition={"requirements":[{"capabilities":[{"capability_id":h.seed["capability"],"minimum":1},{"capability_id":h.seed["supervisor_capability"],"minimum":1}]}]}
    data,result=prepared(h,definition)
    assert result["validation"]["valid"]
    data["shifts"][0]["type_snapshot"]["definition"]["requirements"][0]["distinct_capability_seats"]=True
    assert "DISTINCT_PERSON_GAP" in {f["code"] for f in evaluate(data)["findings"]}


def test_supervision_cannot_be_bypassed_by_labelling_trainee_as_staff(h):
    learner=h.seed["people"][0]
    h.command("coordinator","membership.create",{"person_id":learner,"category_id":h.seed["trainee_category"],"starts_on":"2020-01-01"})
    data,result=prepared(h,{"supervision":{"trainee_category_ids":[h.seed["trainee_category"]],"capability_id":h.seed["supervisor_capability"],"trainees_per_supervisor":2}})
    assert "SUPERVISION_GAP" in {f["code"] for f in result["validation"]["findings"]}


def test_absent_supervisor_does_not_count_for_trainees(h):
    a,b=h.seed["people"][:2]
    h.command("coordinator","membership.create",{"person_id":a,"category_id":h.seed["trainee_category"],"starts_on":"2020-01-01"})
    people=[{"person_id":a,"start":"2026-10-04T08:00:00+03:00","end":"2026-10-04T12:00:00+03:00","participation":"trainee"},{"person_id":b,"start":"2026-10-04T08:00:00+03:00","end":"2026-10-04T12:00:00+03:00","participation":"supervisor"}]
    data,result=prepared(h,{"supervision":{"trainee_category_ids":[h.seed["trainee_category"]],"capability_id":h.seed["supervisor_capability"],"trainees_per_supervisor":2}},people)
    assert result["validation"]["valid"]
    data["availability"].append({"id":str(uuid.uuid4()),"person_id":b,"start":"2026-10-04T07:00:00Z","end":"2026-10-04T08:00:00Z","kind":"actual_absence"})
    gaps=[f for f in evaluate(data)["findings"] if f["code"]=="SUPERVISION_GAP"]
    assert len(gaps)==1 and gaps[0]["detail"]["qualified_supervisors"]==0


def test_renaming_random_catalogue_names_does_not_select_behaviour(h):
    data,result=prepared(h)
    first=evaluate(data)
    for kind in ("people","categories","types","services","locations","capabilities"):
        for row in data[kind].values():row["name"]=str(uuid.uuid4());row["display_name"]=str(uuid.uuid4())
    data["shifts"][0]["type_snapshot"]["name"]=str(uuid.uuid4())
    assert evaluate(data)==first


def test_capability_grant_for_one_service_does_not_cover_two_services(h):
    data,_=prepared(h,{"allow_shared_services":True,"shared_duty_reason":"Explicit common duty fixture","requirements":[{"capabilities":[{"capability_id":h.seed["capability"],"minimum":1}]}]})
    shift=data["shifts"][0];shift["service_ids"]=h.seed["services"]
    for grant in data["grants"]:grant["service_id"]=h.seed["services"][0]
    assert "CAPABILITY_GAP" in {f["code"] for f in evaluate(data)["findings"]}


def test_subject_hours_include_commitments_from_another_department(h):
    h.policy({"kind":"max_hours","hours":6,"window":"day"})
    data,_=prepared(h)
    other=copy.deepcopy(data["shifts"][0]);other["id"]=str(uuid.uuid4());other["start"]="2026-10-04T10:00:00Z";other["end"]="2026-10-04T14:00:00Z"
    other["participants"][0].update({"start":other["start"],"end":other["end"]})
    other_type=str(uuid.uuid4());data["types"][other_type]=copy.deepcopy(data["types"][other["assignment_type_id"]]);data["types"][other_type]["department_id"]=str(uuid.uuid4());other["assignment_type_id"]=other_type
    data["shifts"].append(other)
    findings=evaluate(data)["findings"]
    assert "MAX_HOURS" in {f["code"] for f in findings}


def test_recorded_external_duty_counts_toward_hours_and_rest(h):
    h.policy({"kind":"max_hours","hours":6,"window":"day"});h.policy({"kind":"min_rest","hours":8})
    data,_=prepared(h)
    data["availability"].append({"id":str(uuid.uuid4()),"department_id":h.seed["department_id"],"person_id":h.seed["people"][0],"start":"2026-10-04T00:00:00Z","end":"2026-10-04T04:00:00Z","kind":"external_commitment"})
    found={f["code"] for f in evaluate(data)["findings"]}
    assert {"MAX_HOURS","MIN_REST"}<=found
