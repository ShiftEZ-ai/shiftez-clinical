"""The real client keeps catalogue notices distinct using the server's issue identity."""
from packages.domain.rota_summary import issue_key
from .test_client_proposal_since import run


def test_catalogue_finding_keys_match_the_server_without_changing_existing_keys(client_modules):
    findings = [{"code": "LOCATION_INACTIVE", "shift_id": "session", "detail": {"record_id": "ignored"}},
                *[{"code": "CATALOGUE_VALIDITY_CHANGED", "detail": {"catalogue": source, "record_id": ident}}
                  for source, ident in (("locations", "one"), ("locations", "two"), ("types", "one"))]]
    keys = run(client_modules, "console.log(JSON.stringify(i.map(m.issueKey)));", findings)
    assert keys == ["LOCATION_INACTIVE||session||", "CATALOGUE_VALIDITY_CHANGED|||||locations|one",
                    "CATALOGUE_VALIDITY_CHANGED|||||locations|two", "CATALOGUE_VALIDITY_CHANGED|||||types|one"]
    assert keys == [issue_key(finding) for finding in findings]
