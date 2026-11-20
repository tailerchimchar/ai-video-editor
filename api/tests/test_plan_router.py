from test_edit_plan import compilation  # noqa: F401  (fixture)


def test_plan_version_endpoint(client, compilation):  # noqa: F811
    comp_id, _folder = compilation
    r = client.get(f"/api/v1/compilations/{comp_id}/plan/version")
    assert r.status_code == 200
    assert r.json() == {"version": "1"}


def test_plan_version_endpoint_404_for_unknown(client):
    r = client.get("/api/v1/compilations/nope/plan/version")
    assert r.status_code == 404
