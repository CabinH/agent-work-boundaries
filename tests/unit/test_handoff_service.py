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

from app_server_client import LaunchResult
from handoff_service import HandoffService
from state_store import StateStore


VALID_HANDOFF = """# Completed Work
Implemented the parser.
# Agreed Rules and Decisions
Keep the public API stable.
# Verification Status
Unit tests passed.
# Next Step
Add the integration fixture.
"""


class RecordingClient:
    def __init__(self):
        self.calls = []

    def launch(self, cwd: str, prompt: str) -> LaunchResult:
        self.calls.append((cwd, prompt))
        return LaunchResult(thread_id="thr-new", turn_id="turn-new")


class FailingClient(RecordingClient):
    def launch(self, cwd: str, prompt: str) -> LaunchResult:
        self.calls.append((cwd, prompt))
        raise RuntimeError("launch failed: " + "x" * 2_000)


class FakeClock:
    def __init__(self, value=100.0):
        self.value = value
        self.sleeps = []
        self.on_sleep = None

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        if self.on_sleep is not None:
            self.on_sleep()
        self.value += seconds


class HandoffServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_service(self, client=None, clock=None):
        client = client or RecordingClient()
        clock = clock or FakeClock()
        service = HandoffService(
            store=StateStore(self.root / "state", now=clock.now),
            app_server_client=client,
            private_handoff_dir=self.root / "private",
            sleeper=clock.sleep,
        )
        return service, client, clock

    def prepare_armed(self, service, target=None):
        target = target or self.root / "docs" / "AI-HANDOFF.md"
        pending_id = service.prepare(self.root, VALID_HANDOFF, target)
        service.arm(pending_id, session_id="thr-old", timeout_seconds=300)
        return pending_id, target

    def test_confirm_publishes_then_launches_once(self):
        client = RecordingClient()
        service = HandoffService(
            store=StateStore(self.root / "state", now=lambda: 100.0),
            app_server_client=client,
            private_handoff_dir=self.root / "private",
        )
        target = self.root / "docs" / "AI-HANDOFF.md"
        pending_id = service.prepare(self.root, VALID_HANDOFF, target)
        service.arm(pending_id, session_id="thr-old", timeout_seconds=300)

        result = service.confirm(pending_id)

        self.assertEqual(target.read_text(), VALID_HANDOFF)
        self.assertEqual(result["new_thread_id"], "thr-new")
        self.assertEqual(len(client.calls), 1)

    def test_wait_exits_without_transfer_after_response(self):
        service, client, clock = self.make_service()
        pending_id, target = self.prepare_armed(service)
        responded = service.respond("thr-old")

        result = service.wait_and_expire(pending_id)

        self.assertIsNone(result)
        self.assertEqual(responded["state"], "responded")
        self.assertEqual(service.status("thr-old")["state"], "responded")
        self.assertEqual(clock.sleeps, [])
        self.assertFalse(target.exists())
        self.assertEqual(client.calls, [])

    def test_wait_transfers_after_deadline(self):
        service, client, clock = self.make_service()
        pending_id, target = self.prepare_armed(service)

        result = service.wait_and_expire(pending_id)

        self.assertEqual(clock.sleeps, [300.0])
        self.assertEqual(result["state"], "transferred")
        self.assertEqual(result["claim_reason"], "expired")
        self.assertEqual(target.read_text(), VALID_HANDOFF)
        self.assertEqual(len(client.calls), 1)

    def test_confirm_and_timeout_create_only_one_thread(self):
        service, client, clock = self.make_service()
        pending_id, target = self.prepare_armed(service)
        clock.on_sleep = lambda: service.confirm(pending_id)

        result = service.wait_and_expire(pending_id)

        self.assertIsNone(result)
        self.assertEqual(service.status("thr-old")["state"], "transferred")
        self.assertEqual(target.read_text(), VALID_HANDOFF)
        self.assertEqual(len(client.calls), 1)

    def test_launch_failure_marks_failed_and_keeps_document(self):
        service, client, _clock = self.make_service(client=FailingClient())
        pending_id, target = self.prepare_armed(service)

        with self.assertRaisesRegex(RuntimeError, "launch failed"):
            service.confirm(pending_id)

        status = service.status("thr-old")
        self.assertEqual(status["state"], "failed")
        self.assertLessEqual(len(status["error_summary"]), 512)
        self.assertEqual(status["recovery_prompt"], client.calls[0][1])
        self.assertEqual(target.read_text(), VALID_HANDOFF)

    def test_worker_exception_marks_failed_for_recovery(self):
        service, _client, _clock = self.make_service()
        pending_id, target = self.prepare_armed(service)

        with mock.patch.object(
            service,
            "_resume_prompt",
            side_effect=RuntimeError("worker failed unexpectedly"),
        ):
            with self.assertRaisesRegex(RuntimeError, "worker failed"):
                service.confirm(pending_id)

        status = service.status("thr-old")
        self.assertEqual(status["state"], "failed")
        self.assertIn("worker failed unexpectedly", status["error_summary"])
        self.assertIn(str(target), status["recovery_prompt"])
        self.assertEqual(target.read_text(), VALID_HANDOFF)

    def test_unwritable_project_falls_back_to_private_state_copy(self):
        service, client, _clock = self.make_service()
        pending_id, target = self.prepare_armed(service)
        real_replace = __import__("os").replace

        def deny_project_write(source, destination):
            if Path(destination) == target:
                raise PermissionError("project is read-only")
            return real_replace(source, destination)

        with mock.patch("handoff_service.os.replace", side_effect=deny_project_write):
            result = service.confirm(pending_id)

        private_copy = self.root / "private" / f"{pending_id}.md"
        self.assertEqual(result["state"], "transferred")
        self.assertFalse(target.exists())
        self.assertEqual(private_copy.read_text(), VALID_HANDOFF)
        self.assertIn(str(private_copy), client.calls[0][1])

    def test_target_path_rejects_directory_traversal(self):
        service, client, _clock = self.make_service()
        escaped_target = self.root.parent / f"escaped-{self.root.name}.md"
        traversal = Path("..") / escaped_target.name

        with self.assertRaisesRegex(ValueError, "beneath cwd"):
            service.prepare(self.root, VALID_HANDOFF, traversal)

        opaque_record = service.store.prepare(
            str(self.root),
            VALID_HANDOFF,
            str(traversal),
        )
        pending_id = str(opaque_record["pending_id"])
        service.arm(pending_id, "thr-old", 300)

        with self.assertRaisesRegex(ValueError, "beneath cwd"):
            service.confirm(pending_id)

        self.assertFalse(escaped_target.exists())
        self.assertEqual(service.status("thr-old")["state"], "failed")
        self.assertEqual(
            (self.root / "private" / f"{pending_id}.md").read_text(),
            VALID_HANDOFF,
        )
        self.assertEqual(client.calls, [])

    def test_prepare_rejects_section_names_without_required_headings(self):
        service, client, _clock = self.make_service()
        malformed = VALID_HANDOFF.replace("# Completed Work", "## Completed Work")

        with self.assertRaisesRegex(ValueError, "missing required sections"):
            service.prepare(
                self.root,
                malformed,
                self.root / "docs" / "AI-HANDOFF.md",
            )

        self.assertEqual(client.calls, [])

    def test_resume_prompt_names_exact_next_step_source(self):
        service, client, _clock = self.make_service()
        pending_id, target = self.prepare_armed(service)

        service.confirm(pending_id)

        self.assertEqual(
            client.calls,
            [
                (
                    str(self.root),
                    "This thread continues work handed off from thr-old.\n"
                    f"Read project instructions and {target}. Verify durable "
                    "source-of-truth files before trusting the summary.\n"
                    "Continue from the single “Next Step” in the handoff. Do not "
                    "redo completed work. Report any contradiction before changing "
                    "files.",
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
