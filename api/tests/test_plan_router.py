from test_edit_plan import _http_plan

from ai_video_editor.edit_plan import EditPlan, plan_hash


def test_plan_version_endpoint(client, compilation):
    comp_id, _folder = compilation
    r = client.get(f"/api/v1/compilations/{comp_id}/plan/version")
    assert r.status_code == 200
    assert r.json() == {"version": "1", "plan_hash": None, "plan_complete": False}


def test_plan_version_returns_hash_after_plan_runs(client, compilation):
    comp_id, _folder = compilation
    plan = _http_plan()
    assert client.post(f"/api/v1/compilations/{comp_id}/plan", json=plan).status_code == 200
    body = client.get(f"/api/v1/compilations/{comp_id}/plan/version").json()
    assert body["plan_hash"] == plan_hash(EditPlan.model_validate(plan))
    assert body["version"] != "1"
    assert body["plan_complete"] is True


def test_plan_complete_false_mid_run(client, compilation):
    from ai_video_editor.edit_plan import STEPS, PlanProgress, _save_progress

    comp_id, folder = compilation
    plan = _http_plan()
    _save_progress(
        folder,
        PlanProgress(
            plan_hash=plan_hash(EditPlan.model_validate(plan)),
            completed_steps=len(STEPS) - 1,
            snapshot_version="1",
        ),
    )
    body = client.get(f"/api/v1/compilations/{comp_id}/plan/version").json()
    assert body["plan_hash"] and body["plan_complete"] is False


def test_plan_version_endpoint_404_for_unknown(client):
    r = client.get("/api/v1/compilations/nope/plan/version")
    assert r.status_code == 404
