from pathlib import Path

import yaml

WF = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def load(name):
    data = yaml.safe_load((WF / name).read_text())
    data["on"] = data.pop(True, data.get("on"))   # YAML parses bare `on` as True
    return data


def test_both_workflows_parse_and_are_hardened():
    for name in ("refresh.yml", "update.yml"):
        wf = load(name)
        assert "pull_request" not in wf["on"] and "pull_request_target" not in wf["on"]
        assert wf["permissions"] == {"contents": "write"}
        assert wf["concurrency"] == {"group": "repo-data-writers", "cancel-in-progress": False}
        assert {"schedule", "workflow_dispatch", "repository_dispatch"} <= set(wf["on"])
    text = (WF / "refresh.yml").read_text() + (WF / "update.yml").read_text()
    assert "--force" not in text and "push -f" not in text


def test_schedules_and_inputs():
    assert load("refresh.yml")["on"]["schedule"][0]["cron"].split()[2] == "*/3"
    upd = load("update.yml")
    assert upd["on"]["schedule"][0]["cron"].split()[2:] == ["*", "*", "*"]
    inputs = upd["on"]["workflow_dispatch"]["inputs"]
    assert inputs["fetch_premium"]["default"] is False and inputs["fetch_premium"]["type"] == "boolean"
    assert inputs["download_all"]["default"] is True and inputs["download_all"]["type"] == "boolean"
    env = upd["jobs"]["update"]["steps"][-2]["env"]
    assert "workflow_dispatch" in env["FETCH_PREMIUM"] and "workflow_dispatch" in env["COOKIE"]
    assert "download_all == false" in env["MAX_VALID_PAGES"]


def test_refresh_does_not_get_cookie_or_age_key():
    env = (WF / "refresh.yml").read_text()
    assert "COOKIE" not in env and "AGE_RECIPIENT" not in env


def test_commit_step_runs_even_after_cancel_or_failure():
    for name in ("refresh.yml", "update.yml"):
        steps = load(name)["jobs"][name.split(".")[0]]["steps"]
        assert steps[-1]["if"] == "${{ always() }}", name
