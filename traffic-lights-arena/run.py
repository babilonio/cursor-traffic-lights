from __future__ import annotations

import argparse
import importlib.util
import json
import os
import threading
import time
import traceback
import webbrowser
from dataclasses import asdict
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from traffic_arena.engine import fixed_time_controller, run_scenario
from traffic_arena.score_profiles import score_profile
from traffic_arena.scenarios import PUBLIC_SCENARIOS
from traffic_arena.scoring import scenario_score

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / ".arena"
CONTROLLER = ROOT / "controller.py"


def load_controller():
    spec = importlib.util.spec_from_file_location("team_controller", CONTROLLER)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load controller.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "control", None)):
        raise TypeError("controller.py must define control(state)")
    return module.control


def simulate(scenario_index: int) -> None:
    scenario = PUBLIC_SCENARIOS[scenario_index]
    try:
        controller = load_controller()
        baseline = run_scenario(scenario, fixed_time_controller, record_replay=False)
        result = run_scenario(scenario, controller, record_replay=True)
        profile = score_profile(scenario.id)
        score = scenario_score(result.metrics.cost, baseline.metrics.cost, profile.target_cost)
        payload = result.replay
        assert payload is not None
        payload["score"] = score
        OUTPUT.mkdir(exist_ok=True)
        temporary = OUTPUT / "replay.tmp"
        temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        temporary.replace(OUTPUT / "replay.json")
        write_status(
            {
                "ok": True,
                "revision": time.time_ns(),
                "score": score,
                "scenario": scenario.name,
                "metrics": asdict(result.metrics),
            }
        )
        print(f"[{time.strftime('%H:%M:%S')}] {scenario.name}: {score:,} points")
    except Exception as exc:
        OUTPUT.mkdir(exist_ok=True)
        error = {"ok": False, "error": str(exc), "traceback": traceback.format_exc()[-4000:]}
        write_status({**error, "revision": time.time_ns()})
        print(f"[{time.strftime('%H:%M:%S')}] Error: {exc}")


def write_status(payload: dict) -> None:
    OUTPUT.mkdir(exist_ok=True)
    temporary = OUTPUT / "status.tmp"
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(OUTPUT / "status.json")


class NoCacheHandler(SimpleHTTPRequestHandler):
    def allowed_request(self) -> bool:
        host = (self.headers.get("Host") or "").split(":", 1)[0].lower()
        if host not in {"127.0.0.1", "localhost"}:
            self.send_error(403)
            return False

        requested_path = unquote(urlsplit(self.path).path)
        requested = (ROOT / requested_path.lstrip("/")).resolve()
        viewer_root = (ROOT / "viewer").resolve()
        public_runtime_files = {
            (OUTPUT / "status.json").resolve(),
            (OUTPUT / "replay.json").resolve(),
        }
        if (
            requested != viewer_root
            and viewer_root not in requested.parents
            and requested not in public_runtime_files
        ):
            self.send_error(404)
            return False
        return True

    def do_GET(self) -> None:
        if self.allowed_request():
            super().do_GET()

    def do_HEAD(self) -> None:
        if self.allowed_request():
            super().do_HEAD()

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def log_message(self, _format: str, *_args) -> None:
        return


def create_server(port: int) -> ThreadingHTTPServer:
    handler = lambda *args, **kwargs: NoCacheHandler(*args, directory=str(ROOT), **kwargs)
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Traffic Lights Arena locally")
    parser.add_argument("--scenario", choices=[scenario.id for scenario in PUBLIC_SCENARIOS], default=PUBLIC_SCENARIOS[0].id)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    scenario_index = next(index for index, scenario in enumerate(PUBLIC_SCENARIOS) if scenario.id == args.scenario)

    try:
        httpd = create_server(args.port)
    except OSError as exc:
        raise SystemExit(f"Could not start viewer on port {args.port}: {exc}") from exc
    server = threading.Thread(target=httpd.serve_forever, daemon=True)
    server.start()
    simulate(scenario_index)
    url = f"http://127.0.0.1:{args.port}/viewer/"
    print(f"Viewer: {url}")
    print("Watching controller.py. Save the file to run again. Press Ctrl+C to stop.")
    if not args.no_browser:
        webbrowser.open(url)

    last_modified = CONTROLLER.stat().st_mtime_ns
    try:
        while True:
            time.sleep(0.35)
            modified = CONTROLLER.stat().st_mtime_ns
            if modified != last_modified:
                last_modified = modified
                time.sleep(0.15)
                simulate(scenario_index)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    main()
