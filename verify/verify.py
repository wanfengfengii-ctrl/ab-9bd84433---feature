"""One-shot verification service.

Aggregates, via its exit code:
  1. code tests   -- runs the unit-test suite;
  2. build        -- this container only runs if the image built;
  3. API smoke    -- creates a course, races concurrent competing fractions,
                     restarts the API container and verifies persisted state.

Exits 0 only if every check passes.
"""

import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

API_URL = os.environ.get("API_URL", "http://app:8000").rstrip("/")
APP_CONTAINER = os.environ.get("APP_CONTAINER", "rtqc-app")
DOCKER_SOCKET = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
RUN_UNIT_TESTS = os.environ.get("RUN_UNIT_TESTS", "1") == "1"

FAILURES = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail and not cond else ""), flush=True)
    if not cond:
        FAILURES.append(name)


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API_URL + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {}


def wait_healthy(timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(API_URL + "/health", timeout=3) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path):
        super().__init__("localhost")
        self.socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.socket_path)


def restart_app_container():
    try:
        conn = UnixHTTPConnection(DOCKER_SOCKET)
        conn.request("POST", f"/v1.41/containers/{APP_CONTAINER}/restart?t=3")
        resp = conn.getresponse()
        resp.read()
        return resp.status in (204, 304)
    except OSError as exc:
        print(f"restart failed: {exc}", flush=True)
        return False


def run_unit_tests():
    print("== stage 1/3: unit tests ==", flush=True)
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        capture_output=True, text=True,
    )
    tail = "\n".join(proc.stderr.strip().splitlines()[-5:])
    print(tail, flush=True)
    check("unit tests", proc.returncode == 0, tail)


def smoke_tests(course):
    print("== stage 2/3: API smoke (create / race / retry / limits) ==", flush=True)
    base = f"/api/courses/{course}"

    status, body = api("PUT", base, {"channels": {"A": "3.0", "B": "2.5"}})
    check("create course -> 201, revision 0",
          status == 201 and body.get("revision") == 0 and body.get("status") == "active",
          f"{status} {body}")

    status, body = api("PUT", base, {"channels": {"A": "3.0", "B": "2.5"}})
    check("same-content retry -> 200, still revision 0",
          status == 200 and body.get("revision") == 0, f"{status} {body}")

    status, _ = api("PUT", base, {"channels": {"A": "9.9"}})
    check("rewrite existing course -> 409", status == 409, f"{status}")

    status, _ = api("PUT", base, {"channels": {f"ch{i}": "1" for i in range(17)}})
    check("17 channels -> 422", status == 422, f"{status}")

    # Concurrent competing fractions: same expectedRevision, distinct ids.
    statuses, bodies = [], []
    barrier = threading.Barrier(8)

    def compete(i):
        barrier.wait()
        s, b = api("POST", base + "/deliveries",
                   {"deliveryId": f"race-{i}", "expectedRevision": 0,
                    "increments": {"A": "1.0"}})
        statuses.append(s)
        bodies.append(b)

    threads = [threading.Thread(target=compete, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("concurrent race: exactly one accepted",
          statuses.count(200) == 1 and statuses.count(409) == 7,
          f"statuses: {statuses}")
    winner = bodies[statuses.index(200)] if 200 in statuses else {}
    check("winner bumped revision to exactly 1",
          winner.get("revision") == 1 and winner.get("cumulative", {}).get("A") == "1",
          str(winner))

    win_id = winner.get("deliveryId", "race-0")
    status, body = api("POST", base + "/deliveries",
                       {"deliveryId": win_id, "expectedRevision": 0,
                        "increments": {"A": "1.0"}})
    check("retry of accepted delivery replays first result",
          status == 200 and body == winner, f"{status} {body}")

    status, _ = api("POST", base + "/deliveries",
                    {"deliveryId": win_id, "expectedRevision": 1,
                     "increments": {"A": "0.5"}})
    check("same deliveryId, different content -> 409", status == 409, f"{status}")

    status, body = api("POST", base + "/deliveries",
                       {"deliveryId": "stale-1", "expectedRevision": 0,
                        "increments": {"B": "1.0"}})
    check("stale revision -> 409", status == 409 and body.get("error") == "stale_revision",
          f"{status} {body}")

    status, body = api("POST", base + "/deliveries",
                       {"deliveryId": "over-1", "expectedRevision": 1,
                        "increments": {"A": "5.0"}})
    check("overflow -> 409 prescription_exceeded",
          status == 409 and body.get("error") == "prescription_exceeded", f"{status} {body}")

    status, body = api("GET", base)
    check("rejections wrote nothing (revision 1, A=1, B=0)",
          status == 200 and body.get("revision") == 1
          and body["channels"]["A"]["cumulative"] == "1"
          and body["channels"]["B"]["cumulative"] == "0", f"{status} {body}")

    status, body = api("POST", base + "/deliveries",
                       {"deliveryId": "fill-1", "expectedRevision": 1,
                        "increments": {"A": "2.0", "B": "2.5"}})
    check("fill to prescription -> complete at revision 2",
          status == 200 and body.get("status") == "complete" and body.get("revision") == 2,
          f"{status} {body}")
    fill_body = body

    status, body = api("POST", base + "/deliveries",
                       {"deliveryId": "late-1", "expectedRevision": 2,
                        "increments": {"A": "0.1"}})
    check("post-complete delivery -> 409 course_complete",
          status == 409 and body.get("error") == "course_complete", f"{status} {body}")

    status, body = api("POST", base + "/deliveries",
                       {"deliveryId": "fill-1", "expectedRevision": 1,
                        "increments": {"A": "2.0", "B": "2.5"}})
    check("post-complete replay of original -> 200 same body",
          status == 200 and body == fill_body, f"{status} {body}")
    return fill_body


def persistence_tests(course, fill_body):
    print("== stage 3/3: restart and persistence ==", flush=True)
    if not restart_app_container():
        check("restart app container via docker socket", False)
        return
    check("restart app container via docker socket", True)
    if not wait_healthy():
        check("API healthy after restart", False)
        return
    check("API healthy after restart", True)

    status, body = api("GET", f"/api/courses/{course}")
    check("state persisted across restart (complete, revision 2)",
          status == 200 and body.get("status") == "complete" and body.get("revision") == 2
          and body["channels"]["A"]["cumulative"] == "3"
          and body["channels"]["B"]["cumulative"] == "2.5", f"{status} {body}")

    status, body = api("POST", f"/api/courses/{course}/deliveries",
                       {"deliveryId": "fill-1", "expectedRevision": 1,
                        "increments": {"A": "2.0", "B": "2.5"}})
    check("delivery replay survives restart (no double count)",
          status == 200 and body == fill_body, f"{status} {body}")

    status, body = api("POST", f"/api/courses/{course}/deliveries",
                       {"deliveryId": "late-2", "expectedRevision": 2,
                        "increments": {"A": "0.1"}})
    check("completed course still rejects new deliveries after restart",
          status == 409 and body.get("error") == "course_complete", f"{status} {body}")

    followup = course + "-b"
    status, _ = api("PUT", f"/api/courses/{followup}",
                    {"channels": {"only": "0.5"}})
    status2, body = api("POST", f"/api/courses/{followup}/deliveries",
                        {"deliveryId": "d1", "expectedRevision": 0,
                         "increments": {"only": "0.5"}})
    check("API fully functional after restart (new course completes)",
          status == 201 and status2 == 200 and body.get("status") == "complete",
          f"{status} {status2} {body}")


def main():
    print(f"verify: target={API_URL} app_container={APP_CONTAINER}", flush=True)
    print("== build: image built and verify container is running ==", flush=True)
    if RUN_UNIT_TESTS:
        run_unit_tests()
    else:
        print("== stage 1/3: unit tests (skipped, RUN_UNIT_TESTS=0) ==", flush=True)

    check("API healthy", wait_healthy())
    course = f"verify-{int(time.time())}"
    fill_body = {}
    if not FAILURES:
        fill_body = smoke_tests(course)
    if not FAILURES:
        persistence_tests(course, fill_body)
    else:
        print("skipping remaining stages because of earlier failures", flush=True)

    total = "OK" if not FAILURES else f"{len(FAILURES)} FAILED: {FAILURES}"
    print(f"verify summary: {total}", flush=True)
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
