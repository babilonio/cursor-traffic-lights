from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / ".arena" / "team.json"
CONTROLLER = ROOT / "controller.py"
DEFAULT_API = os.getenv("TRAFFIC_ARENA_URL", "http://localhost:3000")
POLL_TIMEOUT_SECONDS = 15 * 60
TOKEN_PATTERN = re.compile(r"^MLG-(?:DEMO-DEMO-DEMO-DEMO|[A-Z2-9]{4})$")


def save_token(token: str, base_url: str) -> None:
    CONFIG.parent.mkdir(exist_ok=True)
    temporary = CONFIG.with_suffix(".tmp")
    temporary.write_text(json.dumps({"token": token, "baseUrl": base_url}), encoding="utf-8")
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(CONFIG)


def load_config() -> dict[str, str]:
    if not CONFIG.exists():
        raise SystemExit("Run `python submit.py login YOUR_CODE` first.")
    try:
        config = json.loads(CONFIG.read_text(encoding="utf-8"))
        if not isinstance(config.get("token"), str) or not isinstance(config.get("baseUrl"), str):
            raise ValueError
        return config
    except (json.JSONDecodeError, ValueError) as exc:
        raise SystemExit("Saved login is invalid. Run `python submit.py login YOUR_CODE --url EVENT_URL` again.") from exc


def request_json(url: str, *, token: str, data: bytes | None = None, content_type: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if content_type:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            try:
                payload = json.loads(response.read())
                if not isinstance(payload, dict):
                    raise ValueError
                return payload
            except json.JSONDecodeError as exc:
                raise SystemExit("Server returned an invalid response.") from exc
            except ValueError as exc:
                raise SystemExit("Server returned an unexpected response.") from exc
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read() or b"{}").get("error", exc.reason)
        except json.JSONDecodeError:
            message = exc.reason
        raise SystemExit(f"Server error ({exc.code}): {message}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Could not reach {url}: {exc.reason}") from exc


def multipart_file(path: Path) -> tuple[bytes, str]:
    boundary = f"----traffic-arena-{secrets.token_hex(12)}"
    content_type = mimetypes.guess_type(path.name)[0] or "text/plain"
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="controller"; filename="{path.name}"\r\n'.encode(),
            f"Content-Type: {content_type}\r\n\r\n".encode(),
            path.read_bytes(),
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    return body, f"multipart/form-data; boundary={boundary}"


def submit() -> None:
    config = load_config()
    if not CONTROLLER.is_file():
        raise SystemExit("controller.py was not found. Run this command from the TrafficLightsArena folder.")
    body, content_type = multipart_file(CONTROLLER)
    result = request_json(
        f"{config['baseUrl']}/api/v1/submissions",
        token=config["token"],
        data=body,
        content_type=content_type,
    )
    submission_id = result.get("id")
    if not isinstance(submission_id, str) or not submission_id:
        raise SystemExit("Server did not return a submission ID.")
    print(f"Queued submission {submission_id[:8]}…")
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        status = request_json(
            f"{config['baseUrl']}/api/v1/submissions/{submission_id}",
            token=config["token"],
        )
        current_status = status.get("status")
        if current_status == "completed":
            print("Evaluation completed.")
            replay_url = f"{config['baseUrl']}/es/replay?submission={submission_id}"
            print(f"Replay: {replay_url}")
            webbrowser.open(replay_url)
            return
        if current_status == "failed":
            raise SystemExit(status.get("errorMessage", "Evaluation failed."))
        if current_status == "invalidated":
            raise SystemExit("This submission was invalidated by an organizer.")
        if current_status not in {"queued", "running"}:
            raise SystemExit("Server returned an unknown submission status.")
        time.sleep(1.5)
    raise SystemExit("Timed out waiting for the evaluation. Your submission may still finish; check the team page.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Submit controller.py to Traffic Lights Arena")
    subcommands = parser.add_subparsers(dest="command")
    login = subcommands.add_parser("login")
    login.add_argument("token")
    login.add_argument("--url", default=DEFAULT_API)
    args = parser.parse_args()
    if args.command == "login":
        token = args.token.strip().upper()
        base_url = args.url.rstrip("/")
        if not TOKEN_PATTERN.fullmatch(token):
            raise SystemExit("Team code must look like MLG-XXXX.")
        parsed_url = urlsplit(base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname or parsed_url.query or parsed_url.fragment:
            raise SystemExit("Event URL must be a valid http:// or https:// address without a query or fragment.")
        save_token(token, base_url)
        print("Team code saved. Run `python submit.py` when you are ready.")
        return
    submit()


if __name__ == "__main__":
    main()
