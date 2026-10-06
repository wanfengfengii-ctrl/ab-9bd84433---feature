"""Unit tests for the radiotherapy QC platform core logic."""

import os
import sys
import tempfile
import threading
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.server import (
    CourseStore,
    canonical_decimal,
    parse_positive_decimal,
    strict_loads,
    validate_correction_payload,
    validate_course_payload,
    validate_delivery_payload,
)


class DecimalParsingTests(unittest.TestCase):
    def test_valid_decimals(self):
        for text, expected in [("1", "1"), ("1.5", "1.5"), ("0.25", "0.25"),
                               ("10.50", "10.5"), ("100", "100"), ("0.000001", "0.000001")]:
            d = parse_positive_decimal(text)
            self.assertIsNotNone(d, text)
            self.assertEqual(canonical_decimal(d), expected)

    def test_invalid_decimals(self):
        for bad in ["0", "0.0", "-1", "1.5.2", "abc", "", " 1", "1 ", "01",
                    "1e3", ".5", "1.", None, 1.5, 2, True, "NaN", "Infinity"]:
            self.assertIsNone(parse_positive_decimal(bad), bad)

    def test_strict_loads_rejects_duplicate_keys(self):
        with self.assertRaises(ValueError):
            strict_loads(b'{"channels": {"A": "1", "A": "2"}}')
        self.assertEqual(strict_loads(b'{"a": 1}'), {"a": 1})


class PayloadValidationTests(unittest.TestCase):
    def test_course_channel_count_bounds(self):
        _, err = validate_course_payload({"channels": {}})
        self.assertIsNotNone(err)
        too_many = {f"ch{i}": "1" for i in range(17)}
        _, err = validate_course_payload({"channels": too_many})
        self.assertIsNotNone(err)
        ok, err = validate_course_payload({"channels": {f"ch{i}": "1" for i in range(16)}})
        self.assertIsNone(err)
        self.assertEqual(len(ok), 16)

    def test_course_rejects_bad_doses(self):
        _, err = validate_course_payload({"channels": {"A": "0"}})
        self.assertIsNotNone(err)
        _, err = validate_course_payload({"channels": {"A": "-1"}})
        self.assertIsNotNone(err)
        _, err = validate_course_payload({"channels": {"A": 1.5}})
        self.assertIsNotNone(err)

    def test_delivery_validation(self):
        parsed, err = validate_delivery_payload(
            {"deliveryId": "d1", "expectedRevision": 0, "increments": {"A": "1"}})
        self.assertIsNone(err)
        self.assertEqual(parsed[0], "d1")
        # missing increments
        _, err = validate_delivery_payload(
            {"deliveryId": "d1", "expectedRevision": 0, "increments": {}})
        self.assertIsNotNone(err)
        # bool is not an int revision
        _, err = validate_delivery_payload(
            {"deliveryId": "d1", "expectedRevision": True, "increments": {"A": "1"}})
        self.assertIsNotNone(err)
        # negative revision
        _, err = validate_delivery_payload(
            {"deliveryId": "d1", "expectedRevision": -1, "increments": {"A": "1"}})
        self.assertIsNotNone(err)

    def test_correction_validation(self):
        parsed, err = validate_correction_payload(
            {"correctionId": "c1", "deliveryId": "d1", "expectedRevision": 1,
             "increments": {"A": "1"}})
        self.assertIsNone(err)
        self.assertEqual(parsed[0], "c1")
        self.assertEqual(parsed[1], "d1")
        # empty correctionId
        _, err = validate_correction_payload(
            {"correctionId": "", "deliveryId": "d1", "expectedRevision": 1,
             "increments": {"A": "1"}})
        self.assertIsNotNone(err)
        # missing deliveryId
        _, err = validate_correction_payload(
            {"correctionId": "c1", "expectedRevision": 1, "increments": {"A": "1"}})
        self.assertIsNotNone(err)
        # bool is not an int revision
        _, err = validate_correction_payload(
            {"correctionId": "c1", "deliveryId": "d1", "expectedRevision": False,
             "increments": {"A": "1"}})
        self.assertIsNotNone(err)
        # empty increments
        _, err = validate_correction_payload(
            {"correctionId": "c1", "deliveryId": "d1", "expectedRevision": 1,
             "increments": {}})
        self.assertIsNotNone(err)
        # non-positive replacement dose
        _, err = validate_correction_payload(
            {"correctionId": "c1", "deliveryId": "d1", "expectedRevision": 1,
             "increments": {"A": "0"}})
        self.assertIsNotNone(err)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "test.db")
        self.store = CourseStore(self.db)
        status, body = self.store.create_course(
            "c1", {"A": Decimal("3"), "B": Decimal("2.5")})
        self.assertEqual(status, 201, body)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def deliver(self, delivery_id, expected, increments, course="c1"):
        return self.store.submit_delivery(
            course, delivery_id, expected,
            {k: Decimal(v) for k, v in increments.items()})

    def test_create_idempotent_and_conflict(self):
        status, body = self.store.create_course(
            "c1", {"A": Decimal("3.0"), "B": Decimal("2.50")})
        self.assertEqual(status, 200)
        self.assertEqual(body["revision"], 0)
        status, _ = self.store.create_course("c1", {"A": Decimal("3")})
        self.assertEqual(status, 409)
        status, _ = self.store.create_course(
            "c1", {"A": Decimal("3"), "B": Decimal("9")})
        self.assertEqual(status, 409)

    def test_successful_delivery_increments_revision_once(self):
        status, body = self.deliver("d1", 0, {"A": "1"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["revision"], 1)
        self.assertEqual(body["cumulative"], {"A": "1", "B": "0"})
        self.assertEqual(body["status"], "active")

    def test_replay_same_delivery_returns_first_result(self):
        _, first = self.deliver("d1", 0, {"A": "1"})
        status, replay = self.deliver("d1", 0, {"A": "1"})
        self.assertEqual(status, 200)
        self.assertEqual(replay, first)
        _, state = self.store.get_course("c1")
        self.assertEqual(state["revision"], 1)
        self.assertEqual(state["channels"]["A"]["cumulative"], "1")

    def test_same_delivery_id_different_content_conflicts(self):
        self.deliver("d1", 0, {"A": "1"})
        status, body = self.deliver("d1", 1, {"A": "1"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "delivery_conflict")
        status, body = self.deliver("d1", 0, {"A": "0.5"})
        self.assertEqual(status, 409)

    def test_stale_revision_rejected_without_writes(self):
        self.deliver("d1", 0, {"A": "1"})
        status, body = self.deliver("d2", 0, {"B": "1"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "stale_revision")
        _, state = self.store.get_course("c1")
        self.assertEqual(state["revision"], 1)
        self.assertEqual(state["channels"]["B"]["cumulative"], "0")

    def test_prescription_overflow_rejected_without_writes(self):
        status, body = self.deliver("d1", 0, {"A": "3.1"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "prescription_exceeded")
        self.assertEqual(body["channels"], ["A"])
        _, state = self.store.get_course("c1")
        self.assertEqual(state["revision"], 0)
        self.assertEqual(state["channels"]["A"]["cumulative"], "0")

    def test_multi_channel_submission_is_atomic(self):
        # B would overflow -> whole submission rejected, A untouched.
        status, _ = self.deliver("d1", 0, {"A": "1", "B": "99"})
        self.assertEqual(status, 409)
        _, state = self.store.get_course("c1")
        self.assertEqual(state["channels"]["A"]["cumulative"], "0")
        self.assertEqual(state["revision"], 0)

    def test_unknown_channel_rejected(self):
        status, body = self.deliver("d1", 0, {"ZZ": "1"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "unknown_channel")

    def test_completion_and_post_complete_rejection(self):
        self.deliver("d1", 0, {"A": "3", "B": "2"})
        status, body = self.deliver("d2", 1, {"B": "0.5"})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "complete")
        self.assertEqual(body["revision"], 2)
        # New deliveries rejected after completion...
        status, body = self.deliver("d3", 2, {"A": "0.1"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "course_complete")
        # ...but the original submission still replays.
        status, replay = self.deliver("d2", 1, {"B": "0.5"})
        self.assertEqual(status, 200)
        self.assertEqual(replay["status"], "complete")

    def test_exact_decimal_arithmetic(self):
        for i in range(3):
            status, body = self.deliver(f"d{i}", i, {"A": "1"})
            self.assertEqual(status, 200, body)
        self.assertEqual(body["cumulative"]["A"], "3")
        status, body = self.deliver("d3", 3, {"B": "2.5"})
        self.assertEqual(body["status"], "complete")

    def test_delivery_on_missing_course(self):
        status, body = self.deliver("d1", 0, {"A": "1"}, course="nope")
        self.assertEqual(status, 404)

    def test_concurrent_competing_deliveries_exactly_one_wins(self):
        results = []
        barrier = threading.Barrier(8)

        def compete(i):
            barrier.wait()
            results.append(self.deliver(f"race-{i}", 0, {"A": "1"})[0])

        threads = [threading.Thread(target=compete, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count(200), 1)
        self.assertEqual(results.count(409), 7)
        _, state = self.store.get_course("c1")
        self.assertEqual(state["revision"], 1)
        self.assertEqual(state["channels"]["A"]["cumulative"], "1")

    def test_concurrent_retries_never_double_count(self):
        def retry():
            return self.deliver("d1", 0, {"A": "1"})[0]

        with concurrent_pool(8) as pool:
            results = list(pool.map(lambda _: retry(), range(8)))
        self.assertTrue(all(s == 200 for s in results))
        _, state = self.store.get_course("c1")
        self.assertEqual(state["revision"], 1)
        self.assertEqual(state["channels"]["A"]["cumulative"], "1")

    def test_persistence_across_restart(self):
        self.deliver("d1", 0, {"A": "1.5"})
        self.store.close()
        self.store = CourseStore(self.db)  # simulate service restart
        status, state = self.store.get_course("c1")
        self.assertEqual(status, 200)
        self.assertEqual(state["revision"], 1)
        self.assertEqual(state["channels"]["A"]["cumulative"], "1.5")
        # Replay still works after restart, no double counting.
        status, body = self.deliver("d1", 0, {"A": "1.5"})
        self.assertEqual(status, 200)
        self.assertEqual(body["revision"], 1)
        _, state = self.store.get_course("c1")
        self.assertEqual(state["channels"]["A"]["cumulative"], "1.5")


class CorrectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "test.db")
        self.store = CourseStore(self.db)
        status, body = self.store.create_course(
            "c1", {"A": Decimal("3"), "B": Decimal("2.5")})
        self.assertEqual(status, 201, body)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def deliver(self, delivery_id, expected, increments, course="c1"):
        return self.store.submit_delivery(
            course, delivery_id, expected,
            {k: Decimal(v) for k, v in increments.items()})

    def correct(self, correction_id, delivery_id, expected, increments, course="c1"):
        return self.store.submit_correction(
            course, correction_id, delivery_id, expected,
            {k: Decimal(v) for k, v in increments.items()})

    def test_correction_undoes_original_and_applies_replacement(self):
        self.deliver("d1", 0, {"A": "1", "B": "1"})
        status, body = self.correct("corr-1", "d1", 1, {"A": "0.5"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["correctionId"], "corr-1")
        self.assertEqual(body["deliveryId"], "d1")
        self.assertEqual(body["revision"], 2)
        self.assertEqual(body["status"], "active")
        # A: 1 - 1 + 0.5 = 0.5; B: 1 - 1 + 0 = 0 (no replacement for B).
        self.assertEqual(body["cumulative"], {"A": "0.5", "B": "0"})
        _, state = self.store.get_course("c1")
        self.assertEqual(state["revision"], 2)
        self.assertEqual(state["channels"]["A"]["cumulative"], "0.5")
        self.assertEqual(state["channels"]["B"]["cumulative"], "0")

    def test_correction_undoes_only_target_delivery(self):
        self.deliver("d1", 0, {"A": "1"})
        self.deliver("d2", 1, {"A": "1", "B": "2"})
        status, body = self.correct("corr-1", "d1", 2, {"B": "0.5"})
        self.assertEqual(status, 200, body)
        # d1's A:1 undone, d2 untouched, replacement B:0.5 added.
        self.assertEqual(body["cumulative"], {"A": "1", "B": "2.5"})

    def test_correction_replay_returns_first_result(self):
        self.deliver("d1", 0, {"A": "1"})
        _, first = self.correct("corr-1", "d1", 1, {"A": "0.5"})
        status, replay = self.correct("corr-1", "d1", 1, {"A": "0.5"})
        self.assertEqual(status, 200)
        self.assertEqual(replay, first)
        _, state = self.store.get_course("c1")
        self.assertEqual(state["revision"], 2)
        self.assertEqual(state["channels"]["A"]["cumulative"], "0.5")

    def test_same_correction_id_different_content_conflicts(self):
        self.deliver("d1", 0, {"A": "1"})
        self.correct("corr-1", "d1", 1, {"A": "0.5"})
        status, body = self.correct("corr-1", "d1", 2, {"A": "0.5"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "correction_conflict")
        status, body = self.correct("corr-1", "d1", 1, {"A": "0.25"})
        self.assertEqual(status, 409)

    def test_delivery_corrected_at_most_once(self):
        self.deliver("d1", 0, {"A": "1"})
        self.correct("corr-1", "d1", 1, {"A": "0.5"})
        status, body = self.correct("corr-2", "d1", 2, {"A": "0.25"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "delivery_already_corrected")
        _, state = self.store.get_course("c1")
        self.assertEqual(state["revision"], 2)
        self.assertEqual(state["channels"]["A"]["cumulative"], "0.5")

    def test_correction_of_unknown_delivery(self):
        status, body = self.correct("corr-1", "nope", 0, {"A": "1"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "delivery_not_found")

    def test_correction_on_missing_course(self):
        status, body = self.correct("corr-1", "d1", 0, {"A": "1"}, course="nope")
        self.assertEqual(status, 404)

    def test_correction_stale_revision_rejected_without_writes(self):
        self.deliver("d1", 0, {"A": "1"})
        status, body = self.correct("corr-1", "d1", 0, {"A": "0.5"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "stale_revision")
        _, state = self.store.get_course("c1")
        self.assertEqual(state["revision"], 1)
        self.assertEqual(state["channels"]["A"]["cumulative"], "1")

    def test_correction_overflow_rejected_without_writes(self):
        self.deliver("d1", 0, {"A": "1"})
        status, body = self.correct("corr-1", "d1", 1, {"A": "3.5", "B": "1"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "prescription_exceeded")
        self.assertEqual(body["channels"], ["A"])
        _, state = self.store.get_course("c1")
        self.assertEqual(state["revision"], 1)
        self.assertEqual(state["channels"]["A"]["cumulative"], "1")
        self.assertEqual(state["channels"]["B"]["cumulative"], "0")

    def test_correction_unknown_channel_rejected(self):
        self.deliver("d1", 0, {"A": "1"})
        status, body = self.correct("corr-1", "d1", 1, {"ZZ": "1"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "unknown_channel")

    def test_correction_reactivates_complete_course_and_recompletion(self):
        self.deliver("d1", 0, {"A": "3", "B": "2.5"})  # complete at revision 1
        status, body = self.correct("corr-1", "d1", 1, {"A": "1", "B": "1"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["status"], "active")
        self.assertEqual(body["cumulative"], {"A": "1", "B": "1"})
        # Subsequent deliveries are accepted per the original rules...
        status, body = self.deliver("d2", 2, {"A": "2", "B": "1.5"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["status"], "complete")
        self.assertEqual(body["revision"], 3)
        # ...and completion locks the course again.
        status, body = self.deliver("d3", 3, {"A": "0.1"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "course_complete")

    def test_original_delivery_replay_returns_precorrection_response(self):
        _, first = self.deliver("d1", 0, {"A": "1"})
        self.correct("corr-1", "d1", 1, {"A": "0.5"})
        status, replay = self.deliver("d1", 0, {"A": "1"})
        self.assertEqual(status, 200)
        self.assertEqual(replay, first)
        self.assertEqual(replay["cumulative"]["A"], "1")
        _, state = self.store.get_course("c1")
        self.assertEqual(state["revision"], 2)

    def test_concurrent_corrections_same_target_exactly_one_wins(self):
        self.deliver("d1", 0, {"A": "1"})
        results = []
        barrier = threading.Barrier(8)

        def race(i):
            barrier.wait()
            results.append(self.correct(f"corr-{i}", "d1", 1, {"A": "0.5"})[0])

        threads = [threading.Thread(target=race, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count(200), 1)
        self.assertEqual(results.count(409), 7)
        _, state = self.store.get_course("c1")
        self.assertEqual(state["revision"], 2)
        self.assertEqual(state["channels"]["A"]["cumulative"], "0.5")

    def test_concurrent_identical_corrections_never_double_apply(self):
        self.deliver("d1", 0, {"A": "1"})

        def retry():
            return self.correct("corr-1", "d1", 1, {"A": "0.5"})[0]

        with concurrent_pool(8) as pool:
            results = list(pool.map(lambda _: retry(), range(8)))
        self.assertTrue(all(s == 200 for s in results))
        _, state = self.store.get_course("c1")
        self.assertEqual(state["revision"], 2)
        self.assertEqual(state["channels"]["A"]["cumulative"], "0.5")

    def test_concurrent_correction_and_delivery_exactly_one_wins(self):
        self.deliver("d1", 0, {"A": "1"})  # revision 1
        results = []
        barrier = threading.Barrier(2)

        def correct():
            barrier.wait()
            results.append(self.correct("corr-1", "d1", 1, {"A": "0.5"})[0])

        def deliver():
            barrier.wait()
            results.append(self.deliver("d2", 1, {"B": "1"})[0])

        threads = [threading.Thread(target=correct), threading.Thread(target=deliver)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count(200), 1)
        self.assertEqual(results.count(409), 1)
        _, state = self.store.get_course("c1")
        self.assertEqual(state["revision"], 2)

    def test_correction_persists_across_restart(self):
        self.deliver("d1", 0, {"A": "1"})
        _, corr_body = self.correct("corr-1", "d1", 1, {"A": "0.5"})
        self.store.close()
        self.store = CourseStore(self.db)  # simulate service restart
        _, state = self.store.get_course("c1")
        self.assertEqual(state["revision"], 2)
        self.assertEqual(state["channels"]["A"]["cumulative"], "0.5")
        # Correction replay survives the restart without double undo.
        status, body = self.correct("corr-1", "d1", 1, {"A": "0.5"})
        self.assertEqual(status, 200)
        self.assertEqual(body, corr_body)
        # ...and the once-only rule still holds.
        status, body = self.correct("corr-2", "d1", 2, {"A": "0.25"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "delivery_already_corrected")

    def test_migration_from_pre_correction_schema(self):
        import sqlite3

        legacy_db = os.path.join(self.tmp.name, "legacy.db")
        conn = sqlite3.connect(legacy_db)
        conn.executescript(
            """
            CREATE TABLE courses (
                course_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL,
                revision INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('active', 'complete')),
                created_at TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
            CREATE TABLE channels (
                course_id TEXT NOT NULL REFERENCES courses(course_id),
                channel TEXT NOT NULL, prescription TEXT NOT NULL,
                cumulative TEXT NOT NULL, PRIMARY KEY (course_id, channel)
            );
            CREATE TABLE deliveries (
                course_id TEXT NOT NULL REFERENCES courses(course_id),
                delivery_id TEXT NOT NULL, request_hash TEXT NOT NULL,
                response TEXT NOT NULL,
                created_at TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                PRIMARY KEY (course_id, delivery_id)
            );
            INSERT INTO courses(course_id, request_hash, revision, status)
                VALUES ('legacy', 'h', 1, 'active');
            INSERT INTO channels VALUES ('legacy', 'A', '3', '1');
            INSERT INTO deliveries(course_id, delivery_id, request_hash, response)
                VALUES ('legacy', 'd0', 'h', '{}');
            """
        )
        conn.close()
        store = CourseStore(legacy_db)
        try:
            # Column added; the pre-existing delivery cannot be undone safely.
            status, body = store.submit_correction(
                "legacy", "c1", "d0", 1, {"A": Decimal("0.5")})
            self.assertEqual(status, 409)
            self.assertEqual(body["error"], "delivery_not_correctable")
            # New deliveries record increments and are correctable.
            status, _ = store.submit_delivery("legacy", "d1", 1, {"A": Decimal("1")})
            self.assertEqual(status, 200)
            status, body = store.submit_correction(
                "legacy", "c2", "d1", 2, {"A": Decimal("0.5")})
            self.assertEqual(status, 200, body)
            _, state = store.get_course("legacy")
            self.assertEqual(state["channels"]["A"]["cumulative"], "1.5")
        finally:
            store.close()


class concurrent_pool:
    """Tiny thread-pool context manager (avoids concurrent.futures import noise)."""

    def __init__(self, n):
        self.n = n

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def map(self, fn, items):
        results = [None] * len(items)

        def worker(i, item):
            results[i] = fn(item)

        threads = [threading.Thread(target=worker, args=(i, it))
                   for i, it in enumerate(items)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return iter(results)


if __name__ == "__main__":
    unittest.main()
