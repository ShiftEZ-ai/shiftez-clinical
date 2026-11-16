def test_policy_preview_includes_coverage_consequences_without_changing_activation(h):
    workspace = h.shift(h.workspace())["workspace"]
    proposal = h.command("coordinator", "policy.create", {
        "name": "Restriction with coverage consequence", "owner": "Fictional owner",
        "source": "Regression for complete preview totals", "starts_on": "2020-01-01",
        "definition": {"scope": {"person_id": h.seed["people"][0]},
                       "rule": {"kind": "restriction"}, "severity": "mandatory"}
    })["record"]
    impact = h.get("approver", "/api/policies/" + proposal["id"] + "/impact")["impact"][0]
    assert impact["workspace_id"] == workspace["id"]
    assert len(impact["findings"]) == 1
    assert impact["evaluation"]["error_count"] == 1
    assert not impact["evaluation"]["valid"]
    [restriction] = impact["evaluation"]["findings"]
    assert restriction["code"] == "RESTRICTION" and restriction["rule_id"] == proposal["id"]
    [gap] = restriction["detail"]["derived_findings"]
    assert gap["code"] == "COVERAGE_GAP" and gap["rule_id"] != proposal["id"]
    assert gap["shift_id"] == restriction["shift_id"]
    assert gap["detail"]["required"] == 1 and gap["detail"]["available"] == 0
    # Preview does not mutate the draft or activate policy. Final validation agrees.
    assert h.detail(workspace)["workspace"]["validation"]["error_count"] == 0
    h.command("approver", "policy.activate", {"reason": "Independent review"},
              proposal["id"], proposal["revision"])
    result = h.lifecycle(workspace, "validate")["workspace"]["validation"]
    assert result["error_count"] == impact["evaluation"]["error_count"]
    assert result["findings"] == impact["evaluation"]["findings"]
