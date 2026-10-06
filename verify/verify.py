"""One-shot verification service.

Aggregates, via its exit code:
  1. code tests   -- runs the unit-test suite;
  2. build        -- this container only runs if the image built;
  3. API smoke    -- creates a course, races concurrent competing fractions;
  4. corrections  -- replaces an accepted fraction, races concurrent
                     corrections, reactivates and re-completes the course;
  5. persistence  -- restarts the API container and verifies persisted
                     state plus delivery/correction replay.

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
    print("== stage 1/4: unit tests ==", flush=True)
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        capture_output=True, text=True,
    )
    tail = "\n".join(proc.stderr.strip().splitlines()[-5:])
    print(tail, flush=True)
    check("unit tests", proc.returncode == 0, tail)


def smoke_tests(course):
    print("== stage 2/4: API smoke (create / race / retry / limits) ==", flush=True)
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
    return win_id, winner, fill_body


def correction_tests(course, win_id, winner_body):
    print("== stage 3/4: corrections (replace / replay / race / reactivate) ==",
          flush=True)
    base = f"/api/courses/{course}"

    # The course is complete (revision 2, A=3, B=2.5). Correcting fill-1
    # undoes its {A: 2, B: 2.5} and substitutes {A: 1.5, B: 1.5}.
    status, body = api("POST", base + "/corrections",
                       {"correctionId": "corr-1", "deliveryId": "fill-1",
                        "expectedRevision": 2,
                        "increments": {"A": "1.5", "B": "1.5"}})
    check("correction accepted -> 200, complete course active again at revision 3",
          status == 200 and body.get("revision") == 3
          and body.get("status") == "active"
          and body.get("cumulative", {}).get("A") == "2.5"
          and body.get("cumulative", {}).get("B") == "1.5", f"{status} {body}")
    correction_body = body

    status, body = api("POST", base + "/corrections",
                       {"correctionId": "corr-1", "deliveryId": "fill-1",
                        "expectedRevision": 2,
                        "increments": {"A": "1.5", "B": "1.5"}})
    check("same correctionId + same content replays first result",
          status == 200 and body == correction_body, f"{status} {body}")

    status, _ = api("POST", base + "/corrections",
                    {"correctionId": "corr-1", "deliveryId": "fill-1",
                     "expectedRevision": 2,
                     "increments": {"A": "1.0", "B": "1.5"}})
    check("same correctionId, different content -> 409", status == 409, f"{status}")

    status, body = api("POST", base + "/corrections",
                       {"correctionId": "corr-2", "deliveryId": "fill-1",
                        "expectedRevision": 3, "increments": {"A": "1.0"}})
    check("second correction of same delivery -> 409",
          status == 409 and body.get("error") == "delivery_already_corrected",
          f"{status} {body}")

    status, body = api("POST", base + "/corrections",
                       {"correctionId": "corr-3", "deliveryId": "no-such",
                        "expectedRevision": 3, "increments": {"A": "1.0"}})
    check("correction of unknown delivery -> 409",
          status == 409 and body.get("error") == "delivery_not_found",
          f"{status} {body}")

    status, body = api("POST", base + "/corrections",
                       {"correctionId": "corr-4", "deliveryId": win_id,
                        "expectedRevision": 2, "increments": {"A": "0.5"}})
    check("stale revision correction -> 409",
          status == 409 and body.get("error") == "stale_revision", f"{status} {body}")

    status, body = api("POST", base + "/corrections",
                       {"correctionId": "corr-5", "deliveryId": win_id,
                        "expectedRevision": 3, "increments": {"A": "10"}})
    check("overflowing correction -> 409 prescription_exceeded",
          status == 409 and body.get("error") == "prescription_exceeded",
          f"{status} {body}")

    status, body = api("POST", base + "/corrections",
                       {"correctionId": "corr-6", "deliveryId": win_id,
                        "expectedRevision": 3, "increments": {"ZZ": "1"}})
    check("correction with unknown channel -> 409",
          status == 409 and body.get("error") == "unknown_channel", f"{status} {body}")

    status, body = api("GET", base)
    check("rejected corrections wrote nothing (revision 3, A=2.5, B=1.5)",
          status == 200 and body.get("revision") == 3
          and body["channels"]["A"]["cumulative"] == "2.5"
          and body["channels"]["B"]["cumulative"] == "1.5", f"{status} {body}")

    # Concurrent corrections of the same fraction: exactly one is accepted.
    statuses = []
    barrier = threading.Barrier(8)

    def race(i):
        barrier.wait()
        s, _ = api("POST", base + "/corrections",
                   {"correctionId": f"race-corr-{i}", "deliveryId": win_id,
                    "expectedRevision": 3, "increments": {"A": "0.5"}})
        statuses.append(s)

    threads = [threading.Thread(target=race, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("concurrent corrections: exactly one accepted",
          statuses.count(200) == 1 and statuses.count(409) == 7,
          f"statuses: {statuses}")

    status, body = api("GET", base)
    check("winning correction applied exactly once (revision 4, A=2, B=1.5)",
          status == 200 and body.get("revision") == 4
          and body["channels"]["A"]["cumulative"] == "2"
          and body["channels"]["B"]["cumulative"] == "1.5", f"{status} {body}")

    # The corrected delivery's own id still replays its pre-correction response.
    status, body = api("POST", base + "/deliveries",
                       {"deliveryId": win_id, "expectedRevision": 0,
                        "increments": {"A": "1.0"}})
    check("original deliveryId retry returns pre-correction response",
          status == 200 and body == winner_body, f"{status} {body}")

    # The reactivated course accepts new fractions and completes again.
    status, body = api("POST", base + "/deliveries",
                       {"deliveryId": "fill-2", "expectedRevision": 4,
                        "increments": {"A": "1.0", "B": "1.0"}})
    check("course completes again at revision 5",
          status == 200 and body.get("status") == "complete"
          and body.get("revision") == 5, f"{status} {body}")
    return correction_body


def persistence_tests(course, fill_body, correction_body):
    print("== stage 4/4: restart and persistence ==", flush=True)
    if not restart_app_container():
        check("restart app container via docker socket", False)
        return
    check("restart app container via docker socket", True)
    if not wait_healthy():
        check("API healthy after restart", False)
        return
    check("API healthy after restart", True)

    status, body = api("GET", f"/api/courses/{course}")
    check("state persisted across restart (complete, revision 5)",
          status == 200 and body.get("status") == "complete" and body.get("revision") == 5
          and body["channels"]["A"]["cumulative"] == "3"
          and body["channels"]["B"]["cumulative"] == "2.5", f"{status} {body}")

    status, body = api("POST", f"/api/courses/{course}/deliveries",
                       {"deliveryId": "fill-1", "expectedRevision": 1,
                        "increments": {"A": "2.0", "B": "2.5"}})
    check("delivery replay survives restart (pre-correction body, no double count)",
          status == 200 and body == fill_body, f"{status} {body}")

    status, body = api("POST", f"/api/courses/{course}/corrections",
                       {"correctionId": "corr-1", "deliveryId": "fill-1",
                        "expectedRevision": 2,
                        "increments": {"A": "1.5", "B": "1.5"}})
    check("correction replay survives restart (no double undo)",
          status == 200 and body == correction_body, f"{status} {body}")

    status, body = api("POST", f"/api/courses/{course}/corrections",
                       {"correctionId": "corr-9", "deliveryId": "fill-1",
                        "expectedRevision": 5, "increments": {"A": "1.0"}})
    check("already-corrected delivery still rejected after restart",
          status == 409 and body.get("error") == "delivery_already_corrected",
          f"{status} {body}")

    status, body = api("POST", f"/api/courses/{course}/deliveries",
                       {"deliveryId": "late-2", "expectedRevision": 5,
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
        print("== stage 1/4: unit tests (skipped, RUN_UNIT_TESTS=0) ==", flush=True)

    check("API healthy", wait_healthy())
    course = f"verify-{int(time.time())}"
    win_id, winner_body, fill_body, correction_body = "race-0", {}, {}, {}
    if not FAILURES:
        win_id, winner_body, fill_body = smoke_tests(course)
    if not FAILURES:
        correction_body = correction_tests(course, win_id, winner_body)
    if not FAILURES:
        persistence_tests(course, fill_body, correction_body)
    else:
        print("skipping remaining stages because of earlier failures", flush=True)

    total = "OK" if not FAILURES else f"{len(FAILURES)} FAILED: {FAILURES}"
    print(f"verify summary: {total}", flush=True)
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
