import hashlib
import json
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest


SCRIPTS_DIR = (
    Path(__file__).resolve().parents[2]
    / "skills"
    / "project-handoff"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS_DIR))

from state_store import StateStore


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def read_pending(self, pending_id):
        path = self.root / "pending" / f"{pending_id}.json"
        return json.loads(path.read_text())

    def read_session(self, session_id):
        session_name = hashlib.sha256(session_id.encode()).hexdigest()
        path = self.root / "sessions" / f"{session_name}.json"
        return json.loads(path.read_text())

    def run_race(self, *operations):
        barrier = threading.Barrier(len(operations) + 1)
        results = [None] * len(operations)
        errors = []

        def worker(index, operation):
            try:
                barrier.wait()
                results[index] = operation()
            except BaseException as error:
                errors.append(error)

        threads = [
            threading.Thread(target=worker, args=(index, operation))
            for index, operation in enumerate(operations)
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        return results

    def test_prepare_then_arm_binds_session_and_deadline(self):
        store = StateStore(self.root, now=lambda: 100.0)
        record = store.prepare(
            cwd="/workspace/repo",
            handoff_text="# Current Focus\nShip it\n",
            target_path="/workspace/repo/docs/AI-HANDOFF.md",
        )

        armed = store.arm(record["pending_id"], "thr-old", timeout_seconds=300)

        self.assertEqual(armed["state"], "armed")
        self.assertEqual(armed["session_id"], "thr-old")
        self.assertEqual(armed["deadline_at"], 400.0)
        pending_path = self.root / "pending" / f'{record["pending_id"]}.json'
        self.assertEqual(json.loads(pending_path.read_text()), armed)
        session_name = hashlib.sha256(b"thr-old").hexdigest()
        session_path = self.root / "sessions" / f"{session_name}.json"
        self.assertEqual(json.loads(session_path.read_text()), armed)
        self.assertEqual(stat.S_IMODE(pending_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(session_path.stat().st_mode), 0o600)
        for directory in ("pending", "sessions", "locks"):
            self.assertEqual(
                stat.S_IMODE((self.root / directory).stat().st_mode),
                0o700,
            )

    def test_arm_rejects_every_timeout_except_five_minutes(self):
        store = StateStore(self.root, now=lambda: 100.0)

        for timeout_seconds in (0, -1, 1, 299, 301):
            with self.subTest(timeout_seconds=timeout_seconds):
                record = store.prepare(
                    "/repo",
                    "handoff",
                    "/repo/AI-HANDOFF.md",
                )
                session_id = f"thr-{timeout_seconds}"

                with self.assertRaisesRegex(
                    ValueError,
                    "timeout_seconds must be exactly 300",
                ):
                    store.arm(
                        record["pending_id"],
                        session_id,
                        timeout_seconds=timeout_seconds,
                    )

                self.assertEqual(self.read_pending(record["pending_id"]), record)
                self.assertIsNone(store.get_session_status(session_id))

    def test_any_response_wins_against_expiry(self):
        clock = [100.0]
        store = StateStore(self.root, now=lambda: clock[0])
        record = store.prepare("/repo", "handoff", "/repo/AI-HANDOFF.md")
        store.arm(record["pending_id"], "thr-old", timeout_seconds=300)
        clock[0] = 399.0

        results = self.run_race(
            lambda: store.respond("thr-old"),
            lambda: store.claim_expired(record["pending_id"]),
        )

        self.assertEqual(sum(result is not None for result in results), 1)
        responded = next(result for result in results if result is not None)
        self.assertEqual(responded["state"], "responded")
        self.assertEqual(self.read_pending(record["pending_id"]), responded)
        self.assertEqual(self.read_session("thr-old"), responded)

    def test_session_lookup_recovers_after_interrupted_cache_write(self):
        store = StateStore(self.root, now=lambda: 100.0)
        record = store.prepare("/repo", "handoff", "/repo/AI-HANDOFF.md")
        armed = store.arm(record["pending_id"], "thr-old", timeout_seconds=300)
        session_name = hashlib.sha256(b"thr-old").hexdigest()
        (self.root / "sessions" / f"{session_name}.json").unlink()

        recovered = store.get_session_status("thr-old")
        responded = store.respond("thr-old")

        self.assertEqual(recovered, armed)
        self.assertIsNotNone(responded)
        self.assertEqual(responded["state"], "responded")
        self.assertEqual(responded["pending_id"], armed["pending_id"])
        self.assertEqual(self.read_pending(record["pending_id"]), responded)
        self.assertEqual(self.read_session("thr-old"), responded)

    def test_session_lookup_recovers_stale_cache_after_interrupted_update(self):
        store = StateStore(self.root, now=lambda: 100.0)
        record = store.prepare("/repo", "handoff", "/repo/AI-HANDOFF.md")
        armed = store.arm(record["pending_id"], "thr-old", timeout_seconds=300)
        claimed = store.claim_confirm(record["pending_id"])
        session_name = hashlib.sha256(b"thr-old").hexdigest()
        session_path = self.root / "sessions" / f"{session_name}.json"
        session_path.write_text(json.dumps(armed))

        recovered = store.get_session_status("thr-old")

        self.assertEqual(recovered, claimed)
        self.assertEqual(self.read_pending(record["pending_id"]), claimed)
        self.assertEqual(self.read_session("thr-old"), claimed)

    def test_expiry_wins_against_late_response(self):
        clock = [100.0]
        store = StateStore(self.root, now=lambda: clock[0])
        record = store.prepare("/repo", "handoff", "/repo/AI-HANDOFF.md")
        store.arm(record["pending_id"], "thr-old", timeout_seconds=300)
        clock[0] = 401.0

        results = self.run_race(
            lambda: store.respond("thr-old"),
            lambda: store.claim_expired(record["pending_id"]),
        )

        self.assertEqual(sum(result is not None for result in results), 1)
        claimed = next(result for result in results if result is not None)
        self.assertEqual(claimed["state"], "transferring")
        self.assertEqual(claimed["claim_reason"], "expired")
        self.assertEqual(self.read_pending(record["pending_id"]), claimed)
        self.assertEqual(self.read_session("thr-old"), claimed)

    def test_confirm_can_claim_responded_record_once(self):
        store = StateStore(self.root, now=lambda: 100.0)
        record = store.prepare("/repo", "handoff", "/repo/AI-HANDOFF.md")
        store.arm(record["pending_id"], "thr-old", timeout_seconds=300)
        store.respond("thr-old")

        results = self.run_race(
            lambda: store.claim_confirm(record["pending_id"]),
            lambda: store.claim_confirm(record["pending_id"]),
        )

        self.assertEqual(sum(result is not None for result in results), 1)
        claimed = next(result for result in results if result is not None)
        self.assertEqual(claimed["state"], "transferring")
        self.assertEqual(claimed["claim_reason"], "confirmed")
        self.assertEqual(self.read_pending(record["pending_id"]), claimed)
        self.assertEqual(self.read_session("thr-old"), claimed)

    def test_cancel_is_idempotent(self):
        store = StateStore(self.root, now=lambda: 100.0)
        record = store.prepare("/repo", "handoff", "/repo/AI-HANDOFF.md")
        store.arm(record["pending_id"], "thr-old", timeout_seconds=300)
        store.respond("thr-old")

        cancelled = store.cancel(record["pending_id"])
        cancelled_again = store.cancel(record["pending_id"])

        self.assertEqual(cancelled_again, cancelled)
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(self.read_pending(record["pending_id"]), cancelled)
        self.assertEqual(self.read_session("thr-old"), cancelled)

    def test_transferred_session_points_to_new_thread(self):
        store = StateStore(self.root, now=lambda: 100.0)
        record = store.prepare("/repo", "handoff", "/repo/AI-HANDOFF.md")
        store.arm(record["pending_id"], "thr-old", timeout_seconds=300)
        store.claim_confirm(record["pending_id"])

        transferred = store.mark_transferred(record["pending_id"], "thr-new")

        self.assertEqual(transferred["state"], "transferred")
        self.assertEqual(transferred["new_thread_id"], "thr-new")
        self.assertEqual(store.get_session_status("thr-old"), transferred)
        self.assertEqual(self.read_pending(record["pending_id"]), transferred)
        self.assertEqual(self.read_session("thr-old"), transferred)

    def test_failed_record_keeps_recovery_prompt(self):
        store = StateStore(self.root, now=lambda: 100.0)
        record = store.prepare("/repo", "handoff", "/repo/AI-HANDOFF.md")
        store.arm(record["pending_id"], "thr-old", timeout_seconds=300)
        store.claim_confirm(record["pending_id"])

        failed = store.mark_failed(
            record["pending_id"],
            error_summary="app server unavailable",
            recovery_prompt="Open /new and paste this complete recovery prompt.",
        )

        self.assertEqual(failed["state"], "failed")
        self.assertEqual(
            failed["recovery_prompt"],
            "Open /new and paste this complete recovery prompt.",
        )
        self.assertEqual(self.read_pending(record["pending_id"]), failed)
        retried = store.claim_confirm(record["pending_id"])
        self.assertEqual(retried["state"], "transferring")
        self.assertEqual(retried["recovery_prompt"], failed["recovery_prompt"])
        self.assertEqual(self.read_pending(record["pending_id"]), retried)
        self.assertEqual(self.read_session("thr-old"), retried)

    def test_invalid_pending_id_is_rejected(self):
        store = StateStore(self.root, now=lambda: 100.0)

        with self.assertRaisesRegex(ValueError, "invalid pending id"):
            store.claim_confirm("../../outside")

        self.assertEqual(list((self.root / "pending").iterdir()), [])
        self.assertEqual(list((self.root / "locks").iterdir()), [])

    def test_compaction_counts_are_per_session(self):
        store = StateStore(self.root, now=lambda: 100.0)

        first = store.record_compaction("thr-a", "manual")
        second = store.record_compaction("thr-a", "auto")
        other = store.record_compaction("thr-b", "auto")

        self.assertEqual((first, second, other), (1, 2, 1))
        self.assertEqual(store.get_compaction_count("thr-a"), 2)
        self.assertEqual(store.get_compaction_count("thr-b"), 1)
        self.assertEqual(store.get_compaction_count("thr-missing"), 0)
        self.assertEqual(
            self.read_session("thr-a"),
            {
                "session_id": "thr-a",
                "compaction_count": 2,
                "compaction_sources": {"auto": 1, "manual": 1},
            },
        )
        self.assertEqual(
            self.read_session("thr-b"),
            {
                "session_id": "thr-b",
                "compaction_count": 1,
                "compaction_sources": {"auto": 1},
            },
        )
