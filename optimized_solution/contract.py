"""Read the run and, when the submission target is the API, post the answer."""

from __future__ import annotations

import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

USER_AGENT = "optimized-solution/1"
LOCAL_HINT = 'Load the scouting logins first: atira scout --env | Out-String | Invoke-Expression'


def http(url: str, token: str | None = None, payload: dict | None = None, timeout: float = 30) -> tuple[int, str]:
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if token:
        headers["x-api-key"] = token
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
    try:
        with urlopen(Request(url, data=data, headers=headers), timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except HTTPError as error:
        return error.code, error.read().decode("utf-8", errors="replace")


def load_run(env=os.environ) -> tuple[dict, str | None]:
    token = env.get("CHALLENGE_RUN_TOKEN")
    if not token:
        raw = env.get("CHALLENGE_SCOUTING")
        if not raw:
            raise SystemExit(f"No run token and no scouting logins. {LOCAL_HINT}")
        try:
            return json.loads(raw), None
        except ValueError:
            raise SystemExit(f"CHALLENGE_SCOUTING is not JSON. {LOCAL_HINT}")
    url = env["CHALLENGE_API_URL"].rstrip("/") + "/v1/runs/current"
    status, body = 0, ""
    for attempt in range(5):
        try:
            status, body = http(url, token)
        except (URLError, OSError) as error:
            status, body = 0, str(error)
        if status == 200:
            return json.loads(body), token
        if status in {401, 403, 404}:
            break
        time.sleep(2 * (attempt + 1))
    raise SystemExit(f"Cannot read the run: HTTP {status} {body[:300]}")


def seconds_left(run: dict, now: float | None = None) -> float:
    expires_at = run.get("expires_at")
    if not expires_at:
        return float("inf")
    return expires_at / 1000 - (time.time() if now is None else now)


def submit_answer(run: dict, token: str | None, answer: dict) -> tuple[int, str]:
    submission = run.get("submission") or {}
    if submission.get("kind") != "api":
        raise ValueError("this run accepts the answer on the website")
    if not token:
        raise ValueError("local scouting has no run token; the website acknowledges a bid but does not score it")
    return http(run.get("answer_url") or submission["url"], token, answer, timeout=60)
