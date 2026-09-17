import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = (
    Path(__file__).resolve().parents[2]
    / "skills"
    / "project-handoff"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS_DIR))

import handoffctl
from handoff_service import HandoffService
from state_store import StateStore
from tests.unit.test_handoff_service import FakeClock, RecordingClient, VALID_HANDOFF


class HandoffCtlTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.clock = FakeClock()
        self.client = RecordingClient()
        self.service = HandoffService(
            store=StateStore(self.root / "state", now=self.clock.now),
            app_server_client=self.client,
            private_handoff_dir=self.root / "private",
            sleeper=self.clock.sleep,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def invoke(self, *argv, service=None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        selected_service = self.service if service is None else service
        with mock.patch.object(
            handoffctl,
            "_build_service",
            return_value=selected_service,
        ):
            exit_code = handoffctl.main(list(argv), io.StringIO(), stdout, stderr)
        stdout_lines = stdout.getvalue().splitlines()
        stderr_lines = stderr.getvalue().splitlines()
        self.assertLessEqual(len(stdout_lines) + len(stderr_lines), 1)
        output = stdout_lines or stderr_lines
        payload = json.loads(output[0]) if output else None
        return exit_code, payload, stdout.getvalue(), stderr.getvalue()

    def prepare(self, name="draft.md"):
        draft = self.root / name
        draft.write_text(VALID_HANDOFF)
        code, payload, _stdout, _stderr = self.invoke(
            "prepare",
            "--cwd",
            str(self.root),
            "--from-file",
            str(draft),
            "--target",
            "docs/AI-HANDOFF.md",
        )
        self.assertEqual(code, 0)
        return payload["pending_id"]

    def test_prepare_outputs_pending_id_as_json(self):
        pending_id = self.prepare()

        self.assertEqual(
            self.service.store._read_record(
                self.service.store._pending_path(pending_id)
            )["state"],
            "draft",
        )

    def test_arm_and_status_output_state_as_json(self):
        pending_id = self.prepare()

        code, armed, stdout, stderr = self.invoke(
            "arm",
            "--pending-id",
            pending_id,
            "--session-id",
            "thr-old",
            "--timeout-seconds",
            "300",
        )
        status_code, status, _status_stdout, _status_stderr = self.invoke(
            "status",
            "--session-id",
            "thr-old",
        )

        self.assertEqual((code, status_code), (0, 0))
        self.assertEqual(armed["state"], "armed")
        self.assertEqual(status["pending_id"], pending_id)
        self.assertEqual(status["state"], "armed")
        self.assertTrue(stdout.endswith("\n"))
        self.assertEqual(stderr, "")

    def test_respond_outputs_responded_record(self):
        pending_id = self.prepare()
        self.invoke(
            "arm",
            "--pending-id",
            pending_id,
            "--session-id",
            "thr-old",
            "--timeout-seconds",
            "300",
        )

        code, payload, _stdout, _stderr = self.invoke(
            "respond",
            "--session-id",
            "thr-old",
        )

        self.assertEqual(code, 0)
        self.assertEqual(payload["state"], "responded")

    def test_confirm_outputs_transferred_record(self):
        pending_id = self.prepare()
        self.invoke(
            "arm",
            "--pending-id",
            pending_id,
            "--session-id",
            "thr-old",
            "--timeout-seconds",
            "300",
        )

        code, payload, _stdout, _stderr = self.invoke(
            "confirm",
            "--pending-id",
            pending_id,
        )

        self.assertEqual(code, 0)
        self.assertEqual(payload["state"], "transferred")
        self.assertEqual(payload["new_thread_id"], "thr-new")
        self.assertEqual(len(self.client.calls), 1)

    def test_cancel_outputs_cancelled_record(self):
        pending_id = self.prepare()

        code, payload, _stdout, _stderr = self.invoke(
            "cancel",
            "--pending-id",
            pending_id,
        )

        self.assertEqual(code, 0)
        self.assertEqual(payload["state"], "cancelled")

    def test_wait_outputs_transferred_record_without_real_sleep(self):
        pending_id = self.prepare()
        self.invoke(
            "arm",
            "--pending-id",
            pending_id,
            "--session-id",
            "thr-old",
            "--timeout-seconds",
            "300",
        )

        code, payload, _stdout, _stderr = self.invoke(
            "wait",
            "--pending-id",
            pending_id,
        )

        self.assertEqual(code, 0)
        self.assertEqual(payload["state"], "transferred")
        self.assertEqual(self.clock.sleeps, [300.0])

    def test_invalid_pending_id_returns_one_clean_json_error(self):
        code, payload, stdout, stderr = self.invoke(
            "confirm",
            "--pending-id",
            "not-a-uuid",
        )

        self.assertNotEqual(code, 0)
        self.assertEqual(payload, {"error": "invalid pending id"})
        self.assertEqual(stdout, "")
        self.assertNotIn("Traceback", stderr)

    def test_malformed_draft_returns_one_clean_json_error(self):
        draft = self.root / "malformed.md"
        draft.write_text("# Completed Work\nOnly one section.\n")

        code, payload, stdout, stderr = self.invoke(
            "prepare",
            "--cwd",
            str(self.root),
            "--from-file",
            str(draft),
            "--target",
            "docs/AI-HANDOFF.md",
        )

        self.assertNotEqual(code, 0)
        self.assertEqual(payload, {"error": "handoff is missing required sections"})
        self.assertEqual(stdout, "")
        self.assertNotIn("Traceback", stderr)

    def test_unexpected_failure_does_not_expose_exception_secret(self):
        class SecretFailingService:
            def status(self, session_id):
                raise RuntimeError("Bearer private-token-value")

        code, payload, stdout, stderr = self.invoke(
            "status",
            "--session-id",
            "thr-old",
            service=SecretFailingService(),
        )

        self.assertNotEqual(code, 0)
        self.assertEqual(payload, {"error": "command failed"})
        self.assertEqual(stdout, "")
        self.assertNotIn("private-token-value", stderr)
        self.assertNotIn("Traceback", stderr)


if __name__ == "__main__":
    unittest.main()
