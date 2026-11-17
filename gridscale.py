#!/usr/bin/env python3
"""GridScale - zero-code-change, carbon- and cost-aware compute scheduler.

GridScale wraps *any* shell command (Python, Node.js, Bash, Docker, ...) with
``subprocess.run`` and decides **when** and **where** it should run:

* Temporal shifting  - hold the job until grid carbon intensity drops below a
  threshold, protected by a hard SLA deadline (``--max-delay``).
* Spatial routing    - ``--multi-region`` compares live regional grid carbon
  intensity and dispatches immediately to the cleanest region by injecting
  ``GRIDSCALE_TARGET_REGION`` into the child process environment.

Every run appends a receipt to ``esg_audit_log.json``.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from rich import box
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

__version__ = "1.0.0"

console = Console()

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
LOG_FILE = "esg_audit_log.json"
TARGET_REGION_ENV = "GRIDSCALE_TARGET_REGION"
SPOT_DISCOUNT = 0.58  # 58% spot-instance discount applied to every dispatch
HTTP_TIMEOUT = 8  # seconds
DEMO_STEP_SECONDS = 1.5  # real seconds per simulated 2-hour step
DEMO_STEP_MINUTES = 120  # simulated minutes represented by one demo step
DEFAULT_POLL_SECONDS = 300  # live-mode polling interval (override via env)

UK_INTENSITY_URL = "https://api.carbonintensity.org.uk/intensity"
UK_REGIONAL_URL = "https://api.carbonintensity.org.uk/regional"
EMAPS_URL = "https://api.electricitymap.org/v3/carbon-intensity/latest"
USER_AGENT = f"GridScale/{__version__} (+https://github.com/gridscale/gridscale)"

UK_NATIONAL_LABEL = "eu-west-2 (UK National Grid)"
UK_NATIONAL_FALLBACK = 182

# UK regional shortname -> (cloud zone label, offline fallback gCO2/kWh)
UK_REGION_MAP: Dict[str, Tuple[str, int]] = {
    "South Scotland": ("eu-north-1a (Glasgow/Scotland)", 38),
    "North East England": ("eu-west-2b (Newcastle)", 121),
    "North West England": ("eu-west-2d (Manchester)", 164),
    "South Wales": ("eu-west-2c (Cardiff)", 207),
    "London": ("eu-west-2a (London)", 236),
}
UK_HOME_REGION = "London"  # baseline region for spatial-shift accounting

# Electricity Maps zone -> (cloud zone label, calibrated fallback gCO2/kWh)
EMAPS_REGIONS: Dict[str, Tuple[str, int]] = {
    "SE-SE3": ("eu-north-1 (Stockholm)", 28),
    "US-CAL-CISO": ("us-west-1 (N. California)", 135),
    "DE": ("eu-central-1 (Frankfurt)", 265),
    "IN-SO": ("ap-south-2 (South India)", 485),
    "IN-WE": ("ap-south-1 (Mumbai/West India)", 610),
}
EMAPS_UNKNOWN_FALLBACK = 400  # used for zones outside the calibrated table

# (clock label, phase name, gCO2/kWh) - a fast-forwarded 12-hour grid cycle
DEMO_SCHEDULE: List[Tuple[str, str, int]] = [
    ("18:00", "Evening Peak", 285),
    ("20:00", "Evening Demand", 240),
    ("22:00", "Wind Ramp-Up", 195),
    ("00:00", "Wind Surge", 115),
    ("02:00", "Peak Clean", 78),
]


# --------------------------------------------------------------------------- #
# Carbon index classification
# --------------------------------------------------------------------------- #
def classify_intensity(gco2: float) -> Tuple[str, str]:
    """Map a gCO2/kWh value to (UK-style index label, rich style)."""
    if gco2 < 40:
        return "very low", "bold green"
    if gco2 < 110:
        return "low", "green"
    if gco2 < 190:
        return "moderate", "yellow"
    if gco2 < 270:
        return "high", "orange3"
    return "very high", "bold red"


def styled_intensity(gco2: float) -> str:
    """Rich-markup string: colored value plus its index label."""
    label, style = classify_intensity(gco2)
    return f"[{style}]{int(gco2)} gCO2/kWh ({label})[/]"


# --------------------------------------------------------------------------- #
# Telemetry providers (live -> calibrated/offline fallback)
# --------------------------------------------------------------------------- #
def http_get(url: str, params: Optional[Dict[str, Any]] = None,
             headers: Optional[Dict[str, str]] = None,
             timeout: float = HTTP_TIMEOUT) -> requests.Response:
    """Issue a GET request with standard User-Agent and JSON headers."""
    merged = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if headers:
        merged.update(headers)
    return requests.get(url, params=params, headers=merged, timeout=timeout)


def fetch_uk_intensity() -> Tuple[int, str]:
    """National UK intensity. Returns (gCO2/kWh, source description)."""
    try:
        resp = http_get(UK_INTENSITY_URL)
        resp.raise_for_status()
        item = resp.json()["data"][0]
        intensity = item["intensity"]
        value = intensity.get("actual")
        if value is None:
            value = intensity.get("forecast")
        return int(value), f"UK National Grid ESO (live, period to {item.get('to', 'unknown')})"
    except Exception:
        return UK_NATIONAL_FALLBACK, "Offline fallback (UK national profile)"


def _uk_fallback_regions(source: str) -> List[Dict[str, Any]]:
    return [
        {"zone": name, "cloud": cloud, "intensity": val, "source": source}
        for name, (cloud, val) in UK_REGION_MAP.items()
    ]


def fetch_uk_regions() -> List[Dict[str, Any]]:
    """Five mapped UK regions; any missing region falls back individually."""
    try:
        resp = http_get(UK_REGIONAL_URL)
        resp.raise_for_status()
        live: Dict[str, int] = {}
        item = resp.json()["data"][0]
        stamp = item.get("to", "unknown")
        for region in item["regions"]:
            name = region.get("shortname")
            if name in UK_REGION_MAP:
                inten = region["intensity"]
                val = inten.get("forecast")
                if val is None:
                    val = inten.get("actual")
                if val is not None:
                    live[name] = int(val)
        rows = []
        for name, (cloud, fallback) in UK_REGION_MAP.items():
            if name in live:
                rows.append(
                    {"zone": name, "cloud": cloud, "intensity": live[name],
                     "source": f"UK National Grid ESO (live, period to {stamp})"}
                )
            else:
                rows.append(
                    {"zone": name, "cloud": cloud, "intensity": fallback,
                     "source": "Offline fallback (UK regional profile)"}
                )
        return rows
    except Exception:
        return _uk_fallback_regions("Offline fallback (UK regional profile)")


def emaps_cloud_label(zone: str) -> str:
    return EMAPS_REGIONS.get(zone, (zone, 0))[0]


def fetch_emaps_intensity(zone: str) -> Tuple[int, str]:
    """Electricity Maps latest intensity for ``zone`` with calibrated fallback."""
    fallback = EMAPS_REGIONS.get(zone, ("", EMAPS_UNKNOWN_FALLBACK))[1]
    token = os.environ.get("ELECTRICITY_MAPS_TOKEN")
    if not token:
        return fallback, "Calibrated profile (ELECTRICITY_MAPS_TOKEN not set)"
    try:
        resp = http_get(
            EMAPS_URL,
            params={"zone": zone},
            headers={"auth-token": token},
        )
        resp.raise_for_status()
        return int(round(resp.json()["carbonIntensity"])), "Electricity Maps (live)"
    except Exception:
        return fallback, "Calibrated profile (Electricity Maps unreachable)"


def fetch_emaps_regions() -> List[Dict[str, Any]]:
    rows = []
    for zone, (cloud, _) in EMAPS_REGIONS.items():
        val, source = fetch_emaps_intensity(zone)
        rows.append({"zone": zone, "cloud": cloud, "intensity": val, "source": source})
    return rows


def fetch_regions(provider: str) -> List[Dict[str, Any]]:
    return fetch_uk_regions() if provider == "uk" else fetch_emaps_regions()


def fetch_current(provider: str, zone: str) -> Tuple[int, str, str]:
    """Single-region reading -> (gCO2/kWh, cloud region label, source)."""
    if provider == "uk":
        val, source = fetch_uk_intensity()
        return val, UK_NATIONAL_LABEL, source
    val, source = fetch_emaps_intensity(zone)
    return val, emaps_cloud_label(zone), source


# --------------------------------------------------------------------------- #
# Scheduling logic
# --------------------------------------------------------------------------- #
def decide(current: float, threshold: float, elapsed_min: float,
           max_delay: float) -> Tuple[str, str]:
    """Return ("run"|"wait", reason). The SLA deadline always wins."""
    if current <= threshold:
        return "run", f"Carbon threshold met ({int(current)} <= {int(threshold)} gCO2/kWh)"
    if elapsed_min >= max_delay:
        return "run", f"SLA deadline override (max delay of {int(max_delay)} min reached)"
    return "wait", "Grid still above carbon threshold"


def sort_cleanest_first(regions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(regions, key=lambda r: r["intensity"])


def pick_baseline(regions: List[Dict[str, Any]], provider: str, zone: str) -> Dict[str, Any]:
    """Home region the job would otherwise run in (for savings accounting)."""
    home = UK_HOME_REGION if provider == "uk" else zone
    for r in regions:
        if r["zone"] == home:
            return r
    return max(regions, key=lambda r: r["intensity"])  # unknown home: dirtiest


def calc_savings(initial_gco2: float, final_gco2: float, est_kwh: float,
                 base_cost: float, spot_discount: float = SPOT_DISCOUNT) -> Tuple[float, float]:
    """Return (co2_saved_g, cost_saved_usd).

    CO2 is never negative. The (modelled) spot discount is credited only when the
    job actually ran on a cleaner grid than at submission; otherwise nothing moved
    and no saving is claimed.
    """
    co2_saved_g = max(0.0, (initial_gco2 - final_gco2) * est_kwh)
    cost_saved = base_cost * spot_discount if final_gco2 < initial_gco2 else 0.0
    return co2_saved_g, cost_saved


def resolve_poll(args: argparse.Namespace, default: float = DEFAULT_POLL_SECONDS) -> float:
    """Seconds between live checks: --poll, else GRIDSCALE_POLL_SECONDS, else default."""
    if getattr(args, "poll", None) is not None:
        return max(1.0, float(args.poll))
    return max(1.0, float(os.environ.get("GRIDSCALE_POLL_SECONDS", default)))


def run_temporal(args: argparse.Namespace) -> Dict[str, Any]:
    """Hold the workload until the grid is clean enough or the SLA expires."""
    if args.demo:
        return _temporal_demo(args)
    return _temporal_live(args)


def _temporal_demo(args: argparse.Namespace) -> Dict[str, Any]:
    _, region, _ = fetch_current_label_only(args)
    as_json = getattr(args, "json", False)
    if not as_json:
        console.print(Panel(
            "Fast-forwarding a 12-hour grid cycle (1 step = 2 simulated hours)",
            title="Demo Mode", border_style="cyan"))
    initial = DEMO_SCHEDULE[0][2]
    for step, (clock, phase, gco2) in enumerate(DEMO_SCHEDULE):
        elapsed = step * DEMO_STEP_MINUTES
        if not as_json:
            console.print(f"  {clock}  {escape(phase):<16} {styled_intensity(gco2)}"
                          f"   (simulated delay: {elapsed} min)")
        action, reason = decide(gco2, args.threshold, elapsed, args.max_delay)
        if action == "run":
            return {"initial": initial, "final": gco2, "delay": elapsed, "reason": reason,
                    "region": region, "source": "Simulated 12-hour grid cycle",
                    "strategy": "temporal-demo"}
        if step == len(DEMO_SCHEDULE) - 1:  # cycle exhausted: safety net
            return {"initial": initial, "final": gco2, "delay": elapsed,
                    "reason": "SLA deadline override (demo cycle ended)", "region": region,
                    "source": "Simulated 12-hour grid cycle", "strategy": "temporal-demo"}
        if not as_json:
            with console.status(f"Holding workload - waiting for cleaner grid "
                                f"(threshold {args.threshold} gCO2/kWh)..."):
                time.sleep(DEMO_STEP_SECONDS)
        else:
            time.sleep(DEMO_STEP_SECONDS)
    raise RuntimeError("unreachable")  # pragma: no cover


def fetch_current_label_only(args: argparse.Namespace) -> Tuple[None, str, None]:
    """Cloud-region label without making a network call (used by demo mode)."""
    if args.provider == "uk":
        return None, UK_NATIONAL_LABEL, None
    return None, emaps_cloud_label(args.zone), None


def _temporal_live(args: argparse.Namespace) -> Dict[str, Any]:
    poll = resolve_poll(args)
    start = time.monotonic()
    initial: Optional[int] = None
    as_json = getattr(args, "json", False)
    while True:
        value, region, source = fetch_current(args.provider, args.zone)
        if initial is None:
            initial = value
        elapsed = (time.monotonic() - start) / 60.0
        if not as_json:
            console.print(f"  grid now: {styled_intensity(value)}  "
                          f"[dim]{escape(source)} | waited {elapsed:.1f} min[/]")
        action, reason = decide(value, args.threshold, elapsed, args.max_delay)
        if action == "run":
            return {"initial": initial, "final": value, "delay": round(elapsed, 1),
                    "reason": reason, "region": region, "source": source,
                    "strategy": "temporal-live"}
        remaining_s = max(1.0, (args.max_delay - elapsed) * 60.0)
        if not as_json:
            with console.status(f"Holding workload - rechecking in {int(min(poll, remaining_s))}s "
                                f"(SLA in {remaining_s / 60:.0f} min)..."):
                time.sleep(min(poll, remaining_s))
        else:
            time.sleep(min(poll, remaining_s))


def render_region_table(regions: List[Dict[str, Any]]) -> Table:
    """Colour-coded comparison table; ``regions`` must already be sorted."""
    table = Table(title="Regional Grid Carbon Intensity (cleanest first)",
                  box=box.ROUNDED, header_style="bold cyan")
    table.add_column("Rank", justify="right")
    table.add_column("Region")
    table.add_column("Zone")
    table.add_column("gCO2/kWh", justify="right")
    table.add_column("Index")
    table.add_column("Source", style="dim")
    for rank, r in enumerate(regions, start=1):
        label, style = classify_intensity(r["intensity"])
        marker = " *" if rank == 1 else ""
        table.add_row(str(rank), f"[{style}]{escape(r['cloud'])}{marker}[/]", escape(r["zone"]),
                      f"[{style}]{r['intensity']}[/]", f"[{style}]{label}[/]",
                      escape(r["source"]))
    return table


def run_spatial(args: argparse.Namespace) -> Dict[str, Any]:
    """Compare regions, show a table, and select the cleanest immediately."""
    as_json = getattr(args, "json", False)
    if not as_json:
        with console.status("Querying regional grid telemetry..."):
            regions = sort_cleanest_first(fetch_regions(args.provider))
    else:
        regions = sort_cleanest_first(fetch_regions(args.provider))
    baseline = pick_baseline(regions, args.provider, args.zone)
    best = regions[0]

    if not as_json:
        console.print(render_region_table(regions))
        console.print(f"Baseline (home) region: [bold]{escape(baseline['cloud'])}[/] at "
                      f"{styled_intensity(baseline['intensity'])}")
        console.print(f"Dispatching immediately to cleanest region: "
                      f"[bold green]{escape(best['cloud'])}[/]")
    return {"initial": baseline["intensity"], "final": best["intensity"], "delay": 0,
            "reason": f"Multi-region routing: cleanest of {len(regions)} regions",
            "region": best["cloud"], "source": best["source"], "strategy": "spatial"}


def run_watch(args: argparse.Namespace, max_cycles: Optional[int] = None) -> int:
    """Live monitor: poll the grid forever (Ctrl+C to stop). No command is run."""
    interval = resolve_poll(args, default=30)
    console.print(Rule("[bold green]GridScale Live Monitor[/]"))
    console.print(f"Provider: {args.provider}   refresh every {interval:g}s   Ctrl+C to stop")
    cycles = 0
    try:
        while True:
            stamp = datetime.now().astimezone().strftime("%H:%M:%S %Z")
            if args.multi_region:
                regions = sort_cleanest_first(fetch_regions(args.provider))
                console.print(f"[dim]{stamp}[/]")
                console.print(render_region_table(regions))
            else:
                value, region, source = fetch_current(args.provider, args.zone)
                verdict = ("at/below threshold - would run now" if value <= args.threshold
                           else "above threshold - would wait")
                console.print(f"{stamp}  {escape(region)}  {styled_intensity(value)}  "
                              f"({verdict}; threshold {args.threshold})  [dim]{escape(source)}[/]")
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        console.print("\nMonitor stopped.")
    return 0


# --------------------------------------------------------------------------- #
# Audit ledger
# --------------------------------------------------------------------------- #
def resolve_log_file(args: Optional[argparse.Namespace] = None) -> Optional[str]:
    """Determine log file path: None if --no-log, else --log-file, else GRIDSCALE_LOG_FILE, else LOG_FILE."""
    if args and getattr(args, "no_log", False):
        return None
    if args and getattr(args, "log_file", None):
        return args.log_file
    return os.environ.get("GRIDSCALE_LOG_FILE", LOG_FILE)


def load_log(log_path_str: Optional[str] = None) -> List[Dict[str, Any]]:
    target = log_path_str if log_path_str is not None else LOG_FILE
    if not target or not os.path.exists(target):
        return []
    try:
        with open(target, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def append_receipt(receipt: Dict[str, Any], log_path_str: Optional[str] = None) -> None:
    target = log_path_str if log_path_str is not None else LOG_FILE
    if not target:
        return
    entries = load_log(target)
    entries.append(receipt)
    log_path = os.path.abspath(target)
    log_dir = os.path.dirname(log_path) or "."
    temp_file: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=log_dir, delete=False, encoding="utf-8") as fh:
            temp_file = fh.name
            json.dump(entries, fh, indent=2)
        os.replace(temp_file, log_path)
    except Exception:
        if temp_file and os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except OSError:
                pass
        raise


def reset_log(log_path_str: Optional[str] = None) -> bool:
    """Delete the ledger. Returns True if a file was removed."""
    target = log_path_str if log_path_str is not None else LOG_FILE
    if target and os.path.exists(target):
        os.remove(target)
        return True
    return False


def summarize_log(entries: List[Dict[str, Any]]) -> Dict[str, float]:
    return {
        "total_jobs": len(entries),
        "jobs_shifted": sum(1 for e in entries if e.get("shifted")),
        "co2_avoided_kg": sum(e.get("co2_saved_g", 0.0) for e in entries) / 1000.0,
        "cost_saved_usd": sum(e.get("cost_saved_usd", 0.0) for e in entries),
    }


def print_report(log_path_str: Optional[str] = None, as_json: bool = False) -> int:
    target = log_path_str if log_path_str is not None else LOG_FILE
    entries = load_log(target)
    if not entries:
        if as_json:
            print(json.dumps({
                "total_jobs": 0,
                "jobs_shifted": 0,
                "co2_avoided_kg": 0.0,
                "cost_saved_usd": 0.0,
            }, indent=2))
            return 0
        console.print(Panel(f"No audit data found ({target or 'none'}). Run a wrapped job first.",
                            title="GridScale ESG Report", border_style="yellow"))
        return 0
    s = summarize_log(entries)
    if as_json:
        print(json.dumps(s, indent=2))
        return 0
    table = Table(title="GridScale Cumulative ESG Report", box=box.ROUNDED,
                  header_style="bold green")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Total jobs executed", str(int(s["total_jobs"])))
    table.add_row("Total jobs shifted", str(int(s["jobs_shifted"])))
    table.add_row("Scope 2 CO2e avoided", f"{s['co2_avoided_kg']:.3f} kg")
    table.add_row("Spot savings", f"${s['cost_saved_usd']:.2f}")
    console.print(table)
    return 0


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #
def build_env(region: str) -> Dict[str, str]:
    env = os.environ.copy()
    env[TARGET_REGION_ENV] = region
    # Security: do not leak GridScale's API token to child processes
    env.pop("ELECTRICITY_MAPS_TOKEN", None)
    return env


def strip_separator(cmd: List[str]) -> List[str]:
    return cmd[1:] if cmd and cmd[0] == "--" else list(cmd)


def mask_command(cmd: List[str]) -> str:
    """Return a sanitized string representation of ``cmd`` with sensitive arguments redacted."""
    sensitive_keys = {
        "password", "passwd", "pass", "token", "auth", "secret",
        "api-key", "apikey", "api_key", "access-token", "access_token",
        "client-secret", "client_secret", "private-key", "private_key",
    }
    sanitized: List[str] = []
    skip_next = False

    for i, token in enumerate(cmd):
        if skip_next:
            sanitized.append("[REDACTED]")
            skip_next = False
            continue

        # Handle key=value or --key=value
        if "=" in token:
            key, val = token.split("=", 1)
            norm_key = key.lstrip("-").lower().replace("_", "-")
            if norm_key in sensitive_keys or any(s in norm_key for s in ("password", "secret", "token", "apikey", "api-key")):
                sanitized.append(f"{key}=[REDACTED]")
                continue

        # Handle --key value
        if token.startswith("-"):
            norm_key = token.lstrip("-").lower().replace("_", "-")
            if (norm_key in sensitive_keys or any(s in norm_key for s in ("password", "secret", "token", "apikey", "api-key"))) and i + 1 < len(cmd):
                sanitized.append(token)
                skip_next = True
                continue

        # Redact embedded credentials in URLs: http(s)://user:pass@host
        redacted = re.sub(r"(://[^:]+:)([^@]+)(@)", r"\1[REDACTED]\3", token)
        # Redact Bearer tokens in headers or strings
        redacted = re.sub(r"(?i)(bearer\s+)[a-zA-Z0-9_\-\.]+", r"\1[REDACTED]", redacted)
        sanitized.append(redacted)

    return " ".join(sanitized)


def execute_and_audit(cmd: List[str], plan: Dict[str, Any], args: argparse.Namespace) -> int:
    region = plan["region"]
    sanitized_cmd = mask_command(cmd)
    if not getattr(args, "json", False):
        console.print(Rule("Executing wrapped command"))
        console.print(f"[bold]$ {escape(sanitized_cmd)}[/]  "
                      f"[dim]({TARGET_REGION_ENV}={escape(region)})[/]")
    try:
        result = subprocess.run(cmd, env=build_env(region))
        exit_code = result.returncode
    except FileNotFoundError:
        console.print(f"[bold red]Command not found:[/] {escape(cmd[0])}")
        return 127
    except KeyboardInterrupt:
        console.print("\n[yellow]Execution interrupted by user (SIGINT).[/]")
        return 130

    co2_g, cost_saved = calc_savings(plan["initial"], plan["final"], args.kwh, args.cost)
    shifted = plan["final"] < plan["initial"]  # only a real improvement counts
    receipt = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "command": sanitized_cmd,
        "strategy": plan["strategy"],
        "provider": args.provider,
        "execution_region": region,
        "trigger_reason": plan["reason"],
        "data_source": plan["source"],
        "initial_gco2_kwh": plan["initial"],
        "final_gco2_kwh": plan["final"],
        "delay_minutes": plan["delay"],
        "est_kwh": args.kwh,
        "baseline_cost_usd": args.cost,
        "spot_discount": SPOT_DISCOUNT,
        "cost_basis": "modelled",
        "co2_saved_g": round(co2_g, 3),
        "cost_saved_usd": round(cost_saved, 2),
        "shifted": shifted,
        "exit_code": exit_code,
    }
    log_target = resolve_log_file(args)
    if log_target:
        append_receipt(receipt, log_target)

    if getattr(args, "json", False):
        print(json.dumps(receipt, indent=2))
        return exit_code

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column()
    status = "[green]0 (success)[/]" if exit_code == 0 else f"[red]{exit_code} (failed)[/]"
    grid.add_row("Exit Status", status)
    grid.add_row("Execution Region", escape(region))
    grid.add_row("Trigger Reason", escape(plan["reason"]))
    grid.add_row("Carbon Shift", f"{plan['initial']} -> {plan['final']} gCO2/kWh")
    grid.add_row("Scope 2 CO2 Avoided", f"{co2_g / 1000:.3f} kg CO2e ({co2_g:.0f} g)")
    if cost_saved > 0:
        spot_text = (f"${cost_saved:.2f} of ${args.cost:.2f} "
                     f"({round(SPOT_DISCOUNT * 100)}% spot discount, modelled)")
    else:
        spot_text = "$0.00 (no cleaner grid reached, so no spot discount credited)"
    grid.add_row("Spot Cost Saved", spot_text)
    console.print(Panel(grid, title="GridScale Execution & ESG Audit Receipt",
                        border_style="green", padding=(1, 2)))
    return exit_code


def run_dry_run(cmd: List[str], plan: Dict[str, Any], args: argparse.Namespace) -> int:
    co2_g, cost_saved = calc_savings(plan["initial"], plan["final"], args.kwh, args.cost)
    sanitized = mask_command(cmd)
    if getattr(args, "json", False):
        summary = {
            "dry_run": True,
            "command": sanitized,
            "strategy": plan["strategy"],
            "region": plan["region"],
            "trigger_reason": plan["reason"],
            "initial_gco2_kwh": plan["initial"],
            "final_gco2_kwh": plan["final"],
            "delay_minutes": plan["delay"],
            "est_kwh": args.kwh,
            "est_co2_saved_g": round(co2_g, 3),
            "est_cost_saved_usd": round(cost_saved, 2),
        }
        print(json.dumps(summary, indent=2))
        return 0

    table = Table(title="GridScale Dry-Run Simulation (No command executed)",
                  box=box.ROUNDED, header_style="bold cyan")
    table.add_column("Property", style="bold")
    table.add_column("Value")
    table.add_row("Command to execute", escape(sanitized))
    table.add_row("Target Region", escape(plan["region"]))
    table.add_row("Strategy", escape(plan["strategy"]))
    table.add_row("Trigger Reason", escape(plan["reason"]))
    table.add_row("Carbon Shift", f"{plan['initial']} -> {plan['final']} gCO2/kWh")
    table.add_row("Estimated Avoided CO2", f"{co2_g / 1000:.3f} kg CO2e ({co2_g:.0f} g)")
    table.add_row("Estimated Spot Savings", f"${cost_saved:.2f}")
    console.print(Panel(table, border_style="cyan"))
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gridscale",
        description="Carbon- and cost-aware scheduler wrapping any command. "
                    "Usage: gridscale [options] -- <command ...>")
    p.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--provider", choices=["uk", "emaps"], default="uk",
                   help="telemetry provider: UK National Grid or Electricity Maps (default: uk)")
    p.add_argument("--zone", type=str, default="IN-SO",
                   help="Electricity Maps zone, e.g. IN-SO, IN-WE, US-CAL-CISO, US-TEX-ERCO, "
                        "DE, SE-SE3 (default: IN-SO)")
    p.add_argument("--max-delay", type=int, default=240,
                   help="max SLA delay in minutes before forcing execution (default: 240)")
    p.add_argument("--threshold", type=int, default=150,
                   help="target gCO2/kWh threshold (default: 150)")
    p.add_argument("--kwh", type=float, default=18.5,
                   help="estimated workload energy in kWh (default: 18.5)")
    p.add_argument("--cost", type=float, default=24.00,
                   help="baseline on-demand cost in USD (default: 24.00)")
    p.add_argument("--demo", action="store_true",
                   help="fast-forward a simulated 12-hour grid cycle")
    p.add_argument("--multi-region", action="store_true",
                   help="route immediately to the cleanest of 5 regions")
    p.add_argument("--poll", type=float, default=None,
                   help="seconds between live grid checks (default: 300; 30 for --watch)")
    p.add_argument("--watch", action="store_true",
                   help="live grid monitor (no command needed); add --multi-region for all regions")
    p.add_argument("--report", action="store_true",
                   help="print cumulative ESG summary from the audit ledger")
    p.add_argument("--reset", action="store_true",
                   help="delete the audit ledger and exit")
    p.add_argument("--dry-run", action="store_true",
                   help="evaluate grid and simulate decision without running command")
    p.add_argument("--json", action="store_true",
                   help="output receipt, report, or dry-run summary as JSON")
    p.add_argument("--log-file", type=str, default=None,
                   help="custom path for ESG audit log (default: esg_audit_log.json or GRIDSCALE_LOG_FILE)")
    p.add_argument("--no-log", action="store_true",
                   help="do not append execution receipt to audit ledger")
    p.add_argument("cmd", nargs=argparse.REMAINDER,
                   help="command to wrap, after '--'")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    log_target = resolve_log_file(args)

    if args.reset:
        removed = reset_log(log_target)
        target_name = log_target or LOG_FILE
        console.print(f"[green]Audit ledger {'deleted' if removed else 'already clean'} "
                      f"({target_name}).[/]")
        return 0
    if args.report:
        return print_report(log_target, as_json=args.json)

    if args.watch:
        return run_watch(args)

    cmd = strip_separator(args.cmd)
    if not cmd:
        parser.error("no command given. Usage: gridscale [options] -- <command ...>")

    try:
        if not args.json:
            console.print(Rule("[bold green]GridScale[/] - carbon & cost-aware scheduler"))
            console.print(f"Local time: {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}")
        plan = run_spatial(args) if args.multi_region else run_temporal(args)
        if args.dry_run:
            return run_dry_run(cmd, plan, args)
        return execute_and_audit(cmd, plan, args)
    except KeyboardInterrupt:
        console.print("\n[yellow]GridScale execution aborted by user.[/]")
        return 130


if __name__ == "__main__":
    sys.exit(main())