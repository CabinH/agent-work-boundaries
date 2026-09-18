"""Cross-component regressions for snapshot, recovery and session isolation."""

import io
import json
from pathlib import Path
import select
import subprocess
import sys
import unittest
import warnings
from unittest import mock

from tests.unit import test_handoff_service as service_tests
from tests.unit import test_handoffctl as cli_tests

VALID_HANDOFF = service_tests.VALID_HANDOFF
import handoff_hook


class ReviewRegressions(unittest.TestCase):
    setUp = service_tests.HandoffServiceTests.setUp
    tearDown = service_tests.HandoffServiceTests.tearDown
    make_service = service_tests.HandoffServiceTests.make_service
    prepare_armed = service_tests.HandoffServiceTests.prepare_armed

    def test_later_handoff_cannot_change_first_continuation(self):
        service, client, _ = self.make_service()
        first, target = self.prepare_armed(service)
        service.confirm(first)
        second_text = VALID_HANDOFF.replace("integration fixture", "different task")
        second = service.prepare(self.root, second_text, target)
        service.arm(second, "other-session")
        service.confirm(second)
        self.assertEqual(target.read_text(), second_text)
        snapshot = service._private_target(first)
        self.assertEqual(snapshot.read_text(), VALID_HANDOFF)
        self.assertIn(str(snapshot), client.calls[0][1])
        self.assertNotIn(str(target), client.calls[0][1])

    def test_killed_worker_is_recoverable_without_sending(self):
        service, client, _ = self.make_service()
        pending_id, _ = self.prepare_armed(service)
        script = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from handoff_service import HandoffService
from state_store import StateStore
class PausedClient:
    def start_thread(self, cwd, before_send):
        print('claimed', flush=True)
        sys.stdin.read()
service = HandoffService(StateStore(Path(sys.argv[2]), now=lambda: 100),
                         PausedClient(), Path(sys.argv[3]))
service.confirm(sys.argv[4])
"""
        import handoff_service
        process = subprocess.Popen(
            [sys.executable, "-c", script,
             str(Path(handoff_service.__file__).parent),
             str(service.store.root), str(service.private_handoff_dir), pending_id],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        try:
            ready, _, _ = select.select([process.stdout], [], [], 5)
            self.assertTrue(ready, "worker did not reach pre-send phase")
            self.assertEqual(process.stdout.readline().strip(), "claimed")
            self.assertIsNone(service.recover(pending_id))
            self.assertEqual(service.status("thr-old")["state"], "transferring")
        finally:
            process.kill()
            process.communicate(timeout=5)
        restarted, _, _ = self.make_service(client=client)
        recovered = restarted.recover(pending_id)
        self.assertEqual(recovered["state"], "failed")
        self.assertEqual(client.calls, [])
        self.assertEqual(restarted.confirm(pending_id)["state"], "transferred")
        self.assertEqual(len(client.calls), 1)

    def test_recover_never_reclaims_possibly_sent_or_legacy_workers(self):
        for state in ("transferring", "thread_starting", "turn_starting", "indeterminate"):
            with self.subTest(state=state):
                service, client, _ = self.make_service()
                pending_id = service.prepare(self.root, VALID_HANDOFF, "docs/AI-HANDOFF.md")
                service.arm(pending_id, state)
                service.store.claim_confirm(pending_id)
                record = service._pending_record(pending_id)
                record["state"] = state
                record["worker_guarded"] = state != "transferring"
                service.store._write_record(service.store._pending_path(pending_id), record)
                self.assertIsNone(service.recover(pending_id))
                self.assertEqual(service._pending_record(pending_id)["state"], state)
                self.assertEqual(client.calls, [])

    def test_corruption_blocks_owner_but_not_unrelated_prompt(self):
        service, _, _ = self.make_service()
        pending_id, _ = self.prepare_armed(service)
        # Even a lost session cache must not erase durable ownership.
        service.store._session_path("thr-old").unlink()
        service.store._pending_path(pending_id).write_text("{broken")
        for session, should_block in (("thr-old", True), ("unrelated", False)):
            output = io.StringIO()
            with warnings.catch_warnings(record=True):
                handoff_hook.main(
                    stdin=io.StringIO(json.dumps({
                        "hook_event_name": "UserPromptSubmit", "session_id": session,
                        "prompt": "continue",
                    })), stdout=output, stderr=io.StringIO(), service=service,
                )
            result = json.loads(output.getvalue()) if output.getvalue() else {}
            self.assertEqual(result.get("decision") == "block", should_block)

    def test_legacy_corrupt_record_uses_cached_ownership(self):
        service, _, _ = self.make_service()
        pending_id, _ = self.prepare_armed(service)
        service.store._membership_path("thr-old").unlink()
        service.store._pending_path(pending_id).write_text("{broken")
        with self.assertRaises(ValueError):
            service.status("thr-old")
        with warnings.catch_warnings(record=True) as diagnostics:
            self.assertIsNone(service.status("unrelated"))
        self.assertTrue(diagnostics)

    def test_active_and_transferred_records_cannot_be_superseded(self):
        for state in ("transferring", "thread_starting", "thread_created",
                      "turn_starting", "indeterminate", "transferred"):
            with self.subTest(state=state):
                service, _, _ = self.make_service()
                first = service.prepare(self.root, VALID_HANDOFF, "docs/AI-HANDOFF.md")
                service.arm(first, state)
                record = service._pending_record(first)
                record["state"] = state
                service.store._write_record(service.store._pending_path(first), record)
                second = service.prepare(self.root, VALID_HANDOFF, "docs/AI-HANDOFF.md")
                self.assertIsNone(service.arm(second, state))
                self.assertEqual(service.status(state)["pending_id"], first)

    def test_legacy_superseded_worker_cannot_send(self):
        service, client, _ = self.make_service()
        first, _ = self.prepare_armed(service)
        second, _ = self.prepare_armed(service)
        old = service._pending_record(first)
        old["state"] = "transferring"
        service.store._write_record(service.store._pending_path(first), old)
        with self.assertRaisesRegex(RuntimeError, "phase could not be claimed"):
            service._transfer_claimed(old)
        self.assertEqual(client.thread_calls, [])
        self.assertEqual(service.status("thr-old")["pending_id"], second)


class RecoverCliRegression(unittest.TestCase):
    setUp = cli_tests.HandoffCtlTests.setUp
    tearDown = cli_tests.HandoffCtlTests.tearDown
    invoke = cli_tests.HandoffCtlTests.invoke
    prepare = cli_tests.HandoffCtlTests.prepare

    def test_recover_outputs_failed_without_external_send(self):
        pending_id = self.prepare()
        self.service.arm(pending_id, "thr-old")
        with mock.patch.object(self.service, "_transfer_claimed", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                self.service.confirm(pending_id)
        code, result, _, _ = self.invoke("recover", "--pending-id", pending_id)
        self.assertEqual(code, 0)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(self.client.calls, [])
