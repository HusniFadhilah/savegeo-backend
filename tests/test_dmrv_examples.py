from tests.test_dmrv_projects import call, env as dmrv_env  # noqa: F401 (pytest fixture)


def test_arr_accounting_example_is_authenticated_but_not_project_bound(dmrv_env):  # noqa: F811
    client, _ = dmrv_env
    response = call(client, "GET", "/dmrv/examples/arr-accounting", viewer="user:1")

    assert response.status_code == 200
    body = response.json()
    assert body["qualification"] == "illustrative_only"
    assert body["methodology_profile"] is None
    assert body["verification_status"] == "not_verified"
    assert body["issuance_status"] == "not_issued"
    assert body["area_ha"] == "100"
    lines = {line["key"]: line for line in body["lines"]}
    assert lines["additional_stock_change"]["value"] == "1500"
    assert lines["gross_additional_removal"]["value"] == "5500"
    assert lines["project_emissions"]["value"] is None
    assert lines["leakage"]["value"] is None
    assert lines["net_mitigation"]["value"] is None
    assert set(body["blockers"]) == {"project_emissions_tco2e", "leakage_tco2e"}
