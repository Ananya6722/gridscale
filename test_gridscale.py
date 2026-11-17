"""pytest suite for GridScale. No test touches the real network."""
import json
import os
import re
import subprocess
import sys

import pytest

import gridscale


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """Private ledger in a temp dir, no real sleeping, no real network."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gridscale, "LOG_FILE", str(tmp_path / "esg_audit_log.json"))
    monkeypatch.setattr(gridscale.time, "sleep", lambda s: None)
    monkeypatch.delenv("ELECTRICITY_MAPS_TOKEN", raising=False)

    def no_network(*a, **k):
        raise gridscale.requests.ConnectionError("network disabled in tests")

    monkeypatch.setattr(gridscale.requests, "get", no_network)


class FakeRun:
    """Stand-in for subprocess.run that records its call."""

    def __init__(self, returncode=0):
        self.calls = []
        self.returncode = returncode

    def __call__(self, cmd, env=None, **kwargs):
        self.calls.append({"cmd": cmd, "env": env})
        return subprocess.CompletedProcess(cmd, self.returncode)


def flat(text):
    """Collapse whitespace and box-drawing characters so wrapped panel lines compare."""
    return " ".join(re.sub(r"[\u2502\u250c\u2510\u2514\u2518\u2500]", " ", text).split())


def ledger():
    with open(gridscale.LOG_FILE) as fh:
        return json.load(fh)


# ---------------------------------------------------------------- classification
def test_classify_intensity_bands():
    assert gridscale.classify_intensity(0)[0] == "very low"
    assert gridscale.classify_intensity(39)[0] == "very low"
    assert gridscale.classify_intensity(40)[0] == "low"
    assert gridscale.classify_intensity(109)[0] == "low"
    assert gridscale.classify_intensity(110)[0] == "moderate"
    assert gridscale.classify_intensity(189)[0] == "moderate"
    assert gridscale.classify_intensity(190)[0] == "high"
    assert gridscale.classify_intensity(269)[0] == "high"
    assert gridscale.classify_intensity(270)[0] == "very high"
    assert gridscale.classify_intensity(610)[0] == "very high"


# ---------------------------------------------------------------- demo forecast
def test_demo_forecast_progression():
    values = [g for _, _, g in gridscale.DEMO_SCHEDULE]
    clocks = [c for c, _, _ in gridscale.DEMO_SCHEDULE]
    assert values == [285, 240, 195, 115, 78]
    assert clocks == ["18:00", "20:00", "22:00", "00:00", "02:00"]
    assert values == sorted(values, reverse=True)  # monotonically cleaner


def test_demo_waits_until_threshold_met(monkeypatch):
    run = FakeRun()
    monkeypatch.setattr(gridscale.subprocess, "run", run)
    code = gridscale.main(["--demo", "--threshold", "130", "--max-delay", "600", "--", "echo", "hi"])
    assert code == 0
    r = ledger()[0]
    assert r["initial_gco2_kwh"] == 285
    assert r["final_gco2_kwh"] == 115  # first step at/below 130
    assert r["delay_minutes"] == 360
    assert "threshold met" in r["trigger_reason"].lower()


def test_demo_sleeps_between_steps(monkeypatch):
    sleeps = []
    monkeypatch.setattr(gridscale.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(gridscale.subprocess, "run", FakeRun())
    gridscale.main(["--demo", "--threshold", "130", "--max-delay", "600", "--", "echo"])
    assert sleeps == [1.5, 1.5, 1.5]  # steps 18:00, 20:00, 22:00 wait; 00:00 runs


# ---------------------------------------------------------------- SLA override
def test_decide_threshold_beats_deadline():
    action, reason = gridscale.decide(100, 150, elapsed_min=0, max_delay=240)
    assert action == "run" and "threshold" in reason.lower()


def test_decide_waits_then_sla_forces():
    assert gridscale.decide(300, 150, 10, 240)[0] == "wait"
    action, reason = gridscale.decide(300, 150, 240, 240)
    assert action == "run" and "SLA" in reason


def test_sla_override_forces_dirty_run(monkeypatch):
    run = FakeRun()
    monkeypatch.setattr(gridscale.subprocess, "run", run)
    # threshold 50 never reached before the 120-min deadline (step 1 = 240 g)
    code = gridscale.main(["--demo", "--threshold", "50", "--max-delay", "120", "--", "echo"])
    assert code == 0 and len(run.calls) == 1
    r = ledger()[0]
    assert "SLA" in r["trigger_reason"]
    assert r["final_gco2_kwh"] == 240
    assert r["delay_minutes"] == 120


def test_default_sla_240_forces_at_195(monkeypatch):
    monkeypatch.setattr(gridscale.subprocess, "run", FakeRun())
    gridscale.main(["--demo", "--threshold", "130", "--", "echo"])
    r = ledger()[0]
    assert "SLA" in r["trigger_reason"] and r["final_gco2_kwh"] == 195


def test_immediate_run_when_already_clean(monkeypatch):
    monkeypatch.setattr(gridscale.subprocess, "run", FakeRun())
    gridscale.main(["--demo", "--threshold", "999", "--", "echo"])
    r = ledger()[0]
    assert r["delay_minutes"] == 0 and r["shifted"] is False
    assert r["co2_saved_g"] == 0


# ---------------------------------------------------------------- env injection
def test_env_injected_into_subprocess(monkeypatch):
    run = FakeRun()
    monkeypatch.setattr(gridscale.subprocess, "run", run)
    monkeypatch.setenv("KEEP_ME", "yes")
    gridscale.main(["--multi-region", "--", "python", "real_workload.py"])
    call = run.calls[0]
    assert call["cmd"] == ["python", "real_workload.py"]  # '--' stripped
    assert call["env"]["GRIDSCALE_TARGET_REGION"] == "eu-north-1a (Glasgow/Scotland)"
    assert call["env"]["KEEP_ME"] == "yes"  # parent env preserved
    assert "GRIDSCALE_TARGET_REGION" not in os.environ  # parent not mutated


def test_env_reaches_real_child_process(capfd):
    code = gridscale.main(
        ["--multi-region", "--provider", "emaps", "--", sys.executable, "-c",
         "import os; print('REGION=' + os.environ['GRIDSCALE_TARGET_REGION'])"])
    assert code == 0
    assert "REGION=eu-north-1 (Stockholm)" in capfd.readouterr().out


def test_child_exit_code_propagates(monkeypatch):
    monkeypatch.setattr(gridscale.subprocess, "run", FakeRun(returncode=3))
    assert gridscale.main(["--multi-region", "--", "false"]) == 3
    assert ledger()[0]["exit_code"] == 3


def test_missing_command_returns_127_and_no_receipt():
    assert gridscale.main(["--multi-region", "--", "definitely-not-a-real-binary-xyz"]) == 127
    assert not os.path.exists(gridscale.LOG_FILE)


def test_no_command_is_usage_error():
    with pytest.raises(SystemExit) as exc:
        gridscale.main(["--multi-region"])
    assert exc.value.code == 2


# ---------------------------------------------------------------- multi-region
def test_uk_regions_fall_back_offline_and_sort():
    rows = gridscale.sort_cleanest_first(gridscale.fetch_uk_regions())
    assert [r["intensity"] for r in rows] == sorted(r["intensity"] for r in rows)
    assert rows[0]["cloud"] == "eu-north-1a (Glasgow/Scotland)"
    assert len(rows) == 5 and "Offline" in rows[0]["source"]


def test_uk_regional_live_parsing(monkeypatch):
    payload = {"data": [{"regions": [
        {"shortname": "South Scotland", "intensity": {"forecast": 20, "index": "very low"}},
        {"shortname": "London", "intensity": {"forecast": 300, "index": "very high"}},
        {"shortname": "Wales", "intensity": {"forecast": 1, "index": "x"}},  # unmapped: ignored
    ]}]}

    class Resp:
        def raise_for_status(self): pass
        def json(self): return payload

    monkeypatch.setattr(gridscale.requests, "get", lambda *a, **k: Resp())
    by_zone = {r["zone"]: r for r in gridscale.fetch_uk_regions()}
    assert by_zone["South Scotland"]["intensity"] == 20
    assert "live" in by_zone["South Scotland"]["source"]
    assert by_zone["London"]["intensity"] == 300
    assert "Offline" in by_zone["South Wales"]["source"]  # missing -> per-region fallback


def test_uk_national_prefers_actual_over_forecast(monkeypatch):
    class Resp:
        def raise_for_status(self): pass
        def json(self): return {"data": [{"intensity": {"actual": 111, "forecast": 222}}]}

    monkeypatch.setattr(gridscale.requests, "get", lambda *a, **k: Resp())
    assert gridscale.fetch_uk_intensity()[0] == 111


def test_emaps_fallback_profiles_without_token():
    expected = {"SE-SE3": 28, "US-CAL-CISO": 135, "DE": 265, "IN-SO": 485, "IN-WE": 610}
    for zone, val in expected.items():
        got, source = gridscale.fetch_emaps_intensity(zone)
        assert got == val and "ELECTRICITY_MAPS_TOKEN" in source


def test_emaps_sends_auth_header_when_token_set(monkeypatch):
    seen = {}

    class Resp:
        def raise_for_status(self): pass
        def json(self): return {"carbonIntensity": 99.6}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.update(url=url, params=params, headers=headers)
        return Resp()

    monkeypatch.setenv("ELECTRICITY_MAPS_TOKEN", "tok123")
    monkeypatch.setattr(gridscale.requests, "get", fake_get)
    val, source = gridscale.fetch_emaps_intensity("DE")
    assert (val, source) == (100, "Electricity Maps (live)")
    assert seen["headers"]["auth-token"] == "tok123"
    assert "GridScale" in seen["headers"]["User-Agent"]
    assert seen["params"] == {"zone": "DE"}


def test_emaps_falls_back_when_request_fails(monkeypatch):
    monkeypatch.setenv("ELECTRICITY_MAPS_TOKEN", "tok123")  # token set, network down
    val, source = gridscale.fetch_emaps_intensity("IN-SO")
    assert val == 485 and "unreachable" in source


def test_multi_region_baseline_savings(monkeypatch):
    monkeypatch.setattr(gridscale.subprocess, "run", FakeRun())
    gridscale.main(["--provider", "emaps", "--zone", "IN-SO", "--multi-region",
                    "--kwh", "25", "--cost", "40", "--", "echo"])
    r = ledger()[0]
    assert (r["initial_gco2_kwh"], r["final_gco2_kwh"]) == (485, 28)
    assert r["co2_saved_g"] == pytest.approx((485 - 28) * 25)
    assert r["cost_saved_usd"] == pytest.approx(40 * 0.58)
    assert r["shifted"] is True


def test_calc_savings_never_negative():
    co2, cost = gridscale.calc_savings(100, 300, 10, 20)
    assert co2 == 0.0 and cost == 0.0  # grid got dirtier: nothing credited


# ---------------------------------------------------------------- audit ledger
def test_ledger_appends_and_persists(monkeypatch):
    monkeypatch.setattr(gridscale.subprocess, "run", FakeRun())
    for _ in range(3):
        gridscale.main(["--multi-region", "--", "echo"])
    entries = ledger()
    assert len(entries) == 3
    for key in ("timestamp", "command", "execution_region", "trigger_reason",
                "initial_gco2_kwh", "final_gco2_kwh", "co2_saved_g",
                "cost_saved_usd", "exit_code"):
        assert key in entries[0]


def test_reset_deletes_ledger_and_is_idempotent(monkeypatch):
    monkeypatch.setattr(gridscale.subprocess, "run", FakeRun())
    gridscale.main(["--multi-region", "--", "echo"])
    assert os.path.exists(gridscale.LOG_FILE)
    assert gridscale.main(["--reset"]) == 0
    assert not os.path.exists(gridscale.LOG_FILE)
    assert gridscale.main(["--reset"]) == 0  # nothing to delete: still clean exit


def test_corrupt_ledger_is_recovered():
    with open(gridscale.LOG_FILE, "w") as fh:
        fh.write("{not json")
    assert gridscale.load_log() == []
    gridscale.append_receipt({"co2_saved_g": 1.0})
    assert len(ledger()) == 1


def test_report_totals(capsys):
    gridscale.append_receipt({"shifted": True, "co2_saved_g": 2000.0, "cost_saved_usd": 10.0})
    gridscale.append_receipt({"shifted": False, "co2_saved_g": 0.0, "cost_saved_usd": 5.5})
    s = gridscale.summarize_log(gridscale.load_log())
    assert s == {"total_jobs": 2, "jobs_shifted": 1, "co2_avoided_kg": 2.0, "cost_saved_usd": 15.5}
    assert gridscale.main(["--report"]) == 0
    out = capsys.readouterr().out
    assert "2.000 kg" in out and "$15.50" in out


def test_report_without_ledger(capsys):
    assert gridscale.main(["--report"]) == 0
    assert "No audit data" in capsys.readouterr().out


# ---------------------------------------------------------------- CLI surface
def test_parser_defaults():
    a = gridscale.build_parser().parse_args([])
    assert (a.provider, a.zone, a.max_delay, a.threshold, a.kwh, a.cost) == \
        ("uk", "IN-SO", 240, 150, 18.5, 24.00)
    assert not (a.demo or a.multi_region or a.report or a.reset)


def test_receipt_panel_output(monkeypatch, capsys):
    monkeypatch.setattr(gridscale.subprocess, "run", FakeRun())
    gridscale.main(["--multi-region", "--", "echo"])
    out = flat(capsys.readouterr().out)
    for text in ("GridScale Execution & ESG Audit Receipt", "Exit Status", "Execution Region",
                 "Trigger Reason", "Carbon Shift", "Scope 2 CO2 Avoided", "Spot Cost Saved"):
        assert text in out


def test_real_workload_has_no_gridscale_dependency():
    src = open(os.path.join(os.path.dirname(gridscale.__file__), "real_workload.py")).read()
    # the only allowed token is the documented environment variable name
    assert "import gridscale" not in src and "from gridscale" not in src
    assert src.replace("GRIDSCALE_TARGET_REGION", "").lower().count("gridscale") == 0


def test_receipt_shows_exact_58_percent(monkeypatch, capsys):
    monkeypatch.setattr(gridscale.subprocess, "run", FakeRun())
    gridscale.main(["--multi-region", "--", "echo"])
    assert "58% spot discount" in flat(capsys.readouterr().out)


def test_uk_live_source_shows_api_period(monkeypatch):
    class Resp:
        def raise_for_status(self): pass
        def json(self): return {"data": [{"to": "2026-11-17T10:30Z",
                                          "intensity": {"actual": 100, "forecast": 120}}]}

    monkeypatch.setattr(gridscale.requests, "get", lambda *a, **k: Resp())
    val, source = gridscale.fetch_uk_intensity()
    assert val == 100 and "2026-11-17T10:30Z" in source


def test_banner_shows_local_time(monkeypatch, capsys):
    monkeypatch.setattr(gridscale.subprocess, "run", FakeRun())
    gridscale.main(["--multi-region", "--", "echo"])
    assert "Local time:" in capsys.readouterr().out


# ---------------------------------------------------------------- live mode
class FakeClock:
    """time.monotonic/sleep pair where sleeping advances the clock."""

    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s


def test_live_mode_runs_immediately_when_grid_clean(monkeypatch):
    monkeypatch.setattr(gridscale, "fetch_current", lambda p, z: (100, "r1", "stub (live)"))
    monkeypatch.setattr(gridscale.subprocess, "run", FakeRun())
    assert gridscale.main(["--threshold", "150", "--", "echo"]) == 0
    r = ledger()[0]
    assert r["strategy"] == "temporal-live" and r["delay_minutes"] == 0
    assert "threshold met" in r["trigger_reason"].lower()


def test_live_mode_polls_then_sla_forces_run(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(gridscale.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(gridscale.time, "sleep", clock.sleep)
    monkeypatch.setattr(gridscale, "fetch_current", lambda p, z: (300, "r1", "stub (live)"))
    run = FakeRun()
    monkeypatch.setattr(gridscale.subprocess, "run", run)
    code = gridscale.main(["--threshold", "50", "--max-delay", "2", "--poll", "30", "--", "echo"])
    assert code == 0 and len(run.calls) == 1
    assert clock.sleeps == [30.0, 30.0, 30.0, 30.0]  # polled every 30s until 2 min
    r = ledger()[0]
    assert "SLA" in r["trigger_reason"] and r["delay_minutes"] == 2.0


def test_live_mode_runs_when_grid_turns_clean(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(gridscale.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(gridscale.time, "sleep", clock.sleep)
    readings = iter([300, 250, 90])
    monkeypatch.setattr(gridscale, "fetch_current", lambda p, z: (next(readings), "r1", "stub"))
    monkeypatch.setattr(gridscale.subprocess, "run", FakeRun())
    gridscale.main(["--threshold", "100", "--max-delay", "60", "--poll", "10", "--", "echo"])
    r = ledger()[0]
    assert (r["initial_gco2_kwh"], r["final_gco2_kwh"]) == (300, 90)
    assert "threshold met" in r["trigger_reason"].lower() and r["shifted"] is True


def test_resolve_poll_priority(monkeypatch):
    a = gridscale.build_parser().parse_args([])
    assert gridscale.resolve_poll(a) == 300
    assert gridscale.resolve_poll(a, default=30) == 30
    monkeypatch.setenv("GRIDSCALE_POLL_SECONDS", "45")
    assert gridscale.resolve_poll(a) == 45
    a = gridscale.build_parser().parse_args(["--poll", "7"])
    assert gridscale.resolve_poll(a) == 7  # flag beats env


def test_watch_prints_timestamped_readings(monkeypatch, capsys):
    sleeps = []
    monkeypatch.setattr(gridscale.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(gridscale, "fetch_current", lambda p, z: (120, "eu-west-2 (UK)", "stub (live)"))
    args = gridscale.build_parser().parse_args(["--watch", "--threshold", "150"])
    assert gridscale.run_watch(args, max_cycles=3) == 0
    out = capsys.readouterr().out
    assert out.count("would run now") == 3 and sleeps == [30.0, 30.0]
    assert not os.path.exists(gridscale.LOG_FILE)  # monitoring never writes receipts


def test_watch_multi_region_shows_table(monkeypatch, capsys):
    args = gridscale.build_parser().parse_args(["--watch", "--multi-region", "--poll", "5"])
    gridscale.run_watch(args, max_cycles=1)
    assert "Regional Grid Carbon Intensity" in capsys.readouterr().out


def test_watch_flag_needs_no_command(capsys, monkeypatch):
    def stop(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(gridscale.time, "sleep", stop)
    assert gridscale.main(["--watch"]) == 0
    assert "Monitor stopped" in capsys.readouterr().out


def test_calc_savings_credits_spot_only_on_improvement():
    assert gridscale.calc_savings(200, 100, 10, 20) == (pytest.approx(1000.0), pytest.approx(11.6))
    assert gridscale.calc_savings(111, 111, 10, 20) == (0.0, 0.0)


def test_sla_override_without_improvement_claims_nothing(monkeypatch, capsys):
    """Mirrors a real live run: grid stuck at 111, SLA forces the job."""
    clock = FakeClock()
    monkeypatch.setattr(gridscale.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(gridscale.time, "sleep", clock.sleep)
    monkeypatch.setattr(gridscale, "fetch_current", lambda p, z: (111, "eu-west-2 (UK)", "stub"))
    monkeypatch.setattr(gridscale.subprocess, "run", FakeRun())
    gridscale.main(["--threshold", "100", "--max-delay", "2", "--poll", "30", "--cost", "24", "--", "echo"])
    r = ledger()[0]
    assert "SLA" in r["trigger_reason"]
    assert r["co2_saved_g"] == 0 and r["cost_saved_usd"] == 0 and r["shifted"] is False
    assert "no spot discount credited" in flat(capsys.readouterr().out)


def test_improved_run_labels_cost_as_modelled(monkeypatch, capsys):
    monkeypatch.setattr(gridscale.subprocess, "run", FakeRun())
    gridscale.main(["--multi-region", "--", "echo"])
    assert "modelled" in flat(capsys.readouterr().out) and ledger()[0]["cost_basis"] == "modelled"


# ---------------------------------------------------------------- security / redaction
def test_mask_command_redacts_credentials():
    # Flags with space-separated values
    cmd1 = ["python", "train.py", "--api-key", "secret123", "--epochs", "5"]
    assert gridscale.mask_command(cmd1) == "python train.py --api-key [REDACTED] --epochs 5"

    # Key=value flags and environment assignments
    cmd2 = ["python", "app.py", "--token=secret456", "--password=pwd789", "API_KEY=key000"]
    assert gridscale.mask_command(cmd2) == "python app.py --token=[REDACTED] --password=[REDACTED] API_KEY=[REDACTED]"

    # URL with embedded credentials and Bearer token header
    cmd3 = ["curl", "-H", "Authorization: Bearer my_jwt_token", "https://admin:mypass@example.com/api"]
    masked3 = gridscale.mask_command(cmd3)
    assert "Bearer [REDACTED]" in masked3
    assert "https://admin:[REDACTED]@example.com/api" in masked3
    assert "my_jwt_token" not in masked3
    assert "mypass" not in masked3


def test_command_redaction_in_audit_ledger_and_console(monkeypatch, capsys):
    run = FakeRun()
    monkeypatch.setattr(gridscale.subprocess, "run", run)
    cmd = ["python", "task.py", "--token", "super_secret_token", "--api-key=secret_key_123"]
    code = gridscale.main(["--multi-region", "--"] + cmd)
    assert code == 0

    # Child process received raw unaltered arguments
    assert run.calls[0]["cmd"] == cmd

    # Audit ledger contains sanitized command string
    r = ledger()[0]
    assert "super_secret_token" not in r["command"]
    assert "secret_key_123" not in r["command"]
    assert "--token [REDACTED]" in r["command"]
    assert "--api-key=[REDACTED]" in r["command"]

    # Console output displays sanitized command string
    out = capsys.readouterr().out
    assert "super_secret_token" not in out
    assert "secret_key_123" not in out
    assert "[REDACTED]" in out


def test_append_receipt_atomic_and_cleans_up_on_failure(monkeypatch, tmp_path):
    # Simulate a JSON dump serialization failure during receipt write
    def broken_dump(*args, **kwargs):
        raise TypeError("Simulated serialization failure")

    monkeypatch.setattr(gridscale.json, "dump", broken_dump)
    with pytest.raises(TypeError, match="Simulated serialization failure"):
        gridscale.append_receipt({"test": "data"})

    # Check that no stray temporary files were left in the directory
    temp_files = list(tmp_path.glob("*.tmp")) + [f for f in tmp_path.iterdir() if f.name.startswith("tmp")]
    assert len(temp_files) == 0


def test_child_env_scrubs_electricity_maps_token(monkeypatch):
    run = FakeRun()
    monkeypatch.setattr(gridscale.subprocess, "run", run)
    monkeypatch.setenv("ELECTRICITY_MAPS_TOKEN", "private_token_12345")
    monkeypatch.setenv("SAFE_VAR", "keep_this")

    code = gridscale.main(["--multi-region", "--", "echo"])
    assert code == 0
    child_env = run.calls[0]["env"]
    assert "ELECTRICITY_MAPS_TOKEN" not in child_env
    assert child_env["SAFE_VAR"] == "keep_this"
    assert "GRIDSCALE_TARGET_REGION" in child_env


def test_dry_run_does_not_execute_or_write_ledger(monkeypatch, capsys):
    run = FakeRun()
    monkeypatch.setattr(gridscale.subprocess, "run", run)

    code = gridscale.main(["--dry-run", "--multi-region", "--", "echo", "hello"])
    assert code == 0
    # Command was not executed
    assert len(run.calls) == 0
    # Audit log was not created
    assert not os.path.exists(gridscale.LOG_FILE)
    out = flat(capsys.readouterr().out)
    assert "Dry-Run Simulation" in out


def test_dry_run_json_output(monkeypatch, capsys):
    code = gridscale.main(["--dry-run", "--json", "--multi-region", "--", "echo", "test"])
    assert code == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["dry_run"] is True
    assert "region" in data
    assert "est_co2_saved_g" in data
    assert "echo test" in data["command"]


def test_custom_log_file_and_no_log(monkeypatch, tmp_path):
    run = FakeRun()
    monkeypatch.setattr(gridscale.subprocess, "run", run)

    custom_log = str(tmp_path / "custom_audit.json")
    code = gridscale.main(["--log-file", custom_log, "--multi-region", "--", "echo"])
    assert code == 0
    assert os.path.exists(custom_log)
    with open(custom_log) as fh:
        assert len(json.load(fh)) == 1

    # Now with --no-log: execution succeeds, but no new entry is written
    code2 = gridscale.main(["--no-log", "--log-file", custom_log, "--multi-region", "--", "echo"])
    assert code2 == 0
    with open(custom_log) as fh:
        assert len(json.load(fh)) == 1  # count remained 1


def test_report_json_output(capsys):
    gridscale.append_receipt({"shifted": True, "co2_saved_g": 1000.0, "cost_saved_usd": 12.0})
    code = gridscale.main(["--report", "--json"])
    assert code == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["total_jobs"] == 1
    assert data["co2_avoided_kg"] == 1.0
    assert data["cost_saved_usd"] == 12.0


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        gridscale.main(["--version"])
    assert exc.value.code == 0
    assert gridscale.__version__ in capsys.readouterr().out


def test_keyboard_interrupt_handled_cleanly(monkeypatch, capsys):
    def fake_interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(gridscale.subprocess, "run", fake_interrupt)
    code = gridscale.main(["--multi-region", "--", "echo"])
    assert code == 130
    assert "interrupted by user" in capsys.readouterr().out

