import os
import stat
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


class FileRecordingClient:
    def __init__(self, path):
        self.path = path

    def launch(self, cwd: str, prompt: str) -> LaunchResult:
        descriptor = os.open(
            self.path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            os.write(descriptor, b"launch\n")
        finally:
            os.close(descriptor)
        return LaunchResult(thread_id="thr-new", turn_id="turn-new")


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

    def test_project_publish_replaces_within_verified_parent_descriptor(self):
        service, _client, _clock = self.make_service()
        pending_id, target = self.prepare_armed(service)
        real_replace = os.replace
        project_replaces = []

        def record_replace(source, destination, *args, **kwargs):
            if "dst_dir_fd" in kwargs:
                project_replaces.append((source, destination, dict(kwargs)))
            return real_replace(source, destination, *args, **kwargs)

        with mock.patch("handoff_service.os.replace", side_effect=record_replace):
            service.confirm(pending_id)

        self.assertEqual(len(project_replaces), 1)
        source, destination, options = project_replaces[0]
        self.assertNotIn(os.sep, source)
        self.assertEqual(destination, "AI-HANDOFF.md")
        self.assertEqual(options["src_dir_fd"], options["dst_dir_fd"])
        self.assertEqual(target.read_text(), VALID_HANDOFF)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(target.parent.stat().st_mode & 0o022, 0)

    def test_temp_creation_cleans_up_if_permission_update_fails(self):
        parent = self.root / "parent"
        parent.mkdir()
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        real_open = os.open
        opened_descriptors = []

        def record_open(*args, **kwargs):
            descriptor = real_open(*args, **kwargs)
            opened_descriptors.append(descriptor)
            return descriptor

        try:
            with mock.patch(
                "handoff_service.os.open",
                side_effect=record_open,
            ), mock.patch(
                "handoff_service.os.fchmod",
                side_effect=OSError("permission update failed"),
            ):
                with self.assertRaisesRegex(OSError, "permission update failed"):
                    HandoffService._create_temp_at(parent_fd)

            leaked_fd = opened_descriptors[0]
            try:
                os.fstat(leaked_fd)
            except OSError:
                descriptor_is_open = False
            else:
                descriptor_is_open = True
            remaining_entries = list(parent.iterdir())
            if descriptor_is_open:
                os.close(leaked_fd)
            for entry in remaining_entries:
                entry.unlink()

            self.assertFalse(descriptor_is_open)
            self.assertEqual(remaining_entries, [])
        finally:
            os.close(parent_fd)

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX fork")
    def test_confirm_is_single_winner_across_processes(self):
        service, _client, clock = self.make_service()
        pending_id, target = self.prepare_armed(service)
        marker = self.root / "launches.log"
        read_fd, write_fd = os.pipe()
        children = []

        for _index in range(2):
            process_id = os.fork()
            if process_id == 0:
                try:
                    os.close(write_fd)
                    os.read(read_fd, 1)
                    child_service = HandoffService(
                        store=StateStore(
                            self.root / "state",
                            now=clock.now,
                        ),
                        app_server_client=FileRecordingClient(marker),
                        private_handoff_dir=self.root / "private",
                        sleeper=clock.sleep,
                    )
                    child_service.confirm(pending_id)
                except BaseException:
                    os._exit(1)
                os._exit(0)
            children.append(process_id)

        os.close(read_fd)
        os.write(write_fd, b"xx")
        os.close(write_fd)
        statuses = [os.waitpid(process_id, 0)[1] for process_id in children]

        self.assertEqual(statuses, [0, 0])
        self.assertEqual(marker.read_text().splitlines(), ["launch"])
        self.assertEqual(service.status("thr-old")["state"], "transferred")
        self.assertEqual(target.read_text(), VALID_HANDOFF)

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

    def test_wait_rechecks_after_early_wake_and_backward_clock_shift(self):
        service, client, clock = self.make_service()
        pending_id, target = self.prepare_armed(service)
        wake_times = iter((150.0, 50.0, 400.0))

        def irregular_sleep(seconds):
            clock.sleeps.append(seconds)
            clock.value = next(wake_times)

        service.sleeper = irregular_sleep

        result = service.wait_and_expire(pending_id)

        self.assertEqual(clock.sleeps, [300.0, 250.0, 350.0])
        self.assertEqual(result["state"], "transferred")
        self.assertEqual(target.read_text(), VALID_HANDOFF)
        self.assertEqual(len(client.calls), 1)

    def test_wait_rechecks_when_locked_expiry_claim_sees_clock_move_back(self):
        timeline = iter((100.0, 100.0, 400.0, 350.0, 350.0, 400.0, 400.0))
        last_time = 100.0

        def now():
            nonlocal last_time
            try:
                last_time = next(timeline)
            except StopIteration:
                pass
            return last_time

        sleeps = []
        client = RecordingClient()
        service = HandoffService(
            store=StateStore(self.root / "state", now=now),
            app_server_client=client,
            private_handoff_dir=self.root / "private",
            sleeper=sleeps.append,
        )
        target = self.root / "docs" / "AI-HANDOFF.md"
        pending_id = service.prepare(self.root, VALID_HANDOFF, target)
        service.arm(pending_id, "thr-old", 300)

        result = service.wait_and_expire(pending_id)

        self.assertEqual(sleeps, [0.05, 50.0])
        self.assertEqual(result["state"], "transferred")
        self.assertEqual(target.read_text(), VALID_HANDOFF)
        self.assertEqual(len(client.calls), 1)

    def test_wait_backs_off_between_repeated_declined_expiry_claims(self):
        timeline = iter(
            (
                100.0,
                100.0,
                400.0,
                350.0,
                400.0,
                350.0,
                400.0,
                350.0,
                400.0,
                400.0,
                400.0,
            )
        )
        last_time = 100.0

        def now():
            nonlocal last_time
            try:
                last_time = next(timeline)
            except StopIteration:
                pass
            return last_time

        sleeps = []
        service = HandoffService(
            store=StateStore(self.root / "state", now=now),
            app_server_client=RecordingClient(),
            private_handoff_dir=self.root / "private",
            sleeper=sleeps.append,
        )
        pending_id = service.prepare(
            self.root,
            VALID_HANDOFF,
            self.root / "docs" / "AI-HANDOFF.md",
        )
        service.arm(pending_id, "thr-old", 300)

        result = service.wait_and_expire(pending_id)

        self.assertEqual(sleeps, [0.05, 0.05, 0.05])
        self.assertEqual(result["state"], "transferred")

    def test_wait_exits_without_backoff_when_another_actor_wins_claim(self):
        service, client, clock = self.make_service()
        pending_id, target = self.prepare_armed(service)
        clock.value = 400.0
        original_claim_expired = service.store.claim_expired

        def confirm_then_decline_expiry(claimed_pending_id):
            service.confirm(claimed_pending_id)
            return original_claim_expired(claimed_pending_id)

        with mock.patch.object(
            service.store,
            "claim_expired",
            side_effect=confirm_then_decline_expiry,
        ):
            result = service.wait_and_expire(pending_id)

        self.assertIsNone(result)
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(service.status("thr-old")["state"], "transferred")
        self.assertEqual(target.read_text(), VALID_HANDOFF)
        self.assertEqual(len(client.calls), 1)

    def test_wait_stops_after_response_or_cancel_during_early_wake(self):
        for action in ("respond", "cancel"):
            with self.subTest(action=action):
                case_root = self.root / action
                clock = FakeClock()
                client = RecordingClient()
                service = HandoffService(
                    store=StateStore(case_root / "state", now=clock.now),
                    app_server_client=client,
                    private_handoff_dir=case_root / "private",
                    sleeper=None,
                )
                target = case_root / "docs" / "AI-HANDOFF.md"
                pending_id = service.prepare(case_root, VALID_HANDOFF, target)
                service.arm(pending_id, "thr-old", 300)

                def interrupt(seconds):
                    clock.sleeps.append(seconds)
                    clock.value = 150.0
                    if action == "respond":
                        service.respond("thr-old")
                    else:
                        service.cancel(pending_id)

                service.sleeper = interrupt

                result = service.wait_and_expire(pending_id)

                self.assertIsNone(result)
                self.assertEqual(clock.sleeps, [300.0])
                expected_state = {
                    "respond": "responded",
                    "cancel": "cancelled",
                }[action]
                self.assertEqual(
                    service.status("thr-old")["state"],
                    expected_state,
                )
                self.assertFalse(target.exists())
                self.assertEqual(client.calls, [])

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

        def deny_project_write(source, destination, *args, **kwargs):
            is_project_publish = Path(destination) == target or (
                destination == target.name and "dst_dir_fd" in kwargs
            )
            if is_project_publish:
                raise PermissionError("project is read-only")
            return real_replace(source, destination, *args, **kwargs)

        with mock.patch("handoff_service.os.replace", side_effect=deny_project_write):
            result = service.confirm(pending_id)

        private_copy = self.root / "private" / f"{pending_id}.md"
        self.assertEqual(result["state"], "transferred")
        self.assertFalse(target.exists())
        self.assertEqual(private_copy.read_text(), VALID_HANDOFF)
        self.assertIn(str(private_copy), client.calls[0][1])
        self.assertEqual(
            stat.S_IMODE(service.private_handoff_dir.stat().st_mode),
            0o700,
        )
        self.assertEqual(stat.S_IMODE(private_copy.stat().st_mode), 0o600)

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

    def test_absolute_target_outside_cwd_is_rejected(self):
        service, client, _clock = self.make_service()

        with tempfile.TemporaryDirectory() as outside_name:
            outside = Path(outside_name) / "AI-HANDOFF.md"
            with self.assertRaisesRegex(ValueError, "beneath cwd"):
                service.prepare(self.root, VALID_HANDOFF, outside)

            self.assertFalse(outside.exists())
        self.assertEqual(client.calls, [])

    def test_existing_outside_parent_symlink_uses_private_fallback(self):
        service, client, _clock = self.make_service()
        with tempfile.TemporaryDirectory() as outside_name:
            outside = Path(outside_name)
            (self.root / "docs").symlink_to(outside, target_is_directory=True)
            pending_id = service.prepare(
                self.root,
                VALID_HANDOFF,
                "docs/AI-HANDOFF.md",
            )
            service.arm(pending_id, "thr-old", 300)

            result = service.confirm(pending_id)

            self.assertFalse((outside / "AI-HANDOFF.md").exists())
            self.assertEqual(result["state"], "transferred")
            self.assertEqual(
                (self.root / "private" / f"{pending_id}.md").read_text(),
                VALID_HANDOFF,
            )
            self.assertIn(
                str(self.root / "private" / f"{pending_id}.md"),
                client.calls[0][1],
            )
        self.assertEqual(len(client.calls), 1)

    def test_in_tree_project_parent_symlink_uses_private_fallback(self):
        service, client, _clock = self.make_service()
        actual_parent = self.root / "actual-docs"
        actual_parent.mkdir()
        (self.root / "docs").symlink_to(
            actual_parent,
            target_is_directory=True,
        )
        pending_id = service.prepare(
            self.root,
            VALID_HANDOFF,
            "docs/AI-HANDOFF.md",
        )
        service.arm(pending_id, "thr-old", 300)

        result = service.confirm(pending_id)

        private_copy = self.root / "private" / f"{pending_id}.md"
        self.assertEqual(result["state"], "transferred")
        self.assertFalse((actual_parent / "AI-HANDOFF.md").exists())
        self.assertEqual(private_copy.read_text(), VALID_HANDOFF)
        self.assertIn(str(private_copy), client.calls[0][1])

    def test_parent_symlink_substitution_cannot_redirect_publication(self):
        service, client, _clock = self.make_service()
        target = self.root / "docs" / "AI-HANDOFF.md"
        pending_id = service.prepare(self.root, VALID_HANDOFF, target)
        service.arm(pending_id, "thr-old", 300)
        target.parent.mkdir()
        displaced = self.root / "verified-docs"
        real_replace = os.replace

        with tempfile.TemporaryDirectory() as outside_name:
            outside = Path(outside_name)
            substituted = False

            def substitute_parent(source, destination, *args, **kwargs):
                nonlocal substituted
                is_project_publish = Path(destination) == target or (
                    destination == target.name and "dst_dir_fd" in kwargs
                )
                if not substituted and is_project_publish:
                    substituted = True
                    target.parent.rename(displaced)
                    if "src_dir_fd" not in kwargs:
                        real_replace(
                            displaced / Path(source).name,
                            outside / Path(source).name,
                        )
                    target.parent.symlink_to(outside, target_is_directory=True)
                return real_replace(source, destination, *args, **kwargs)

            with mock.patch(
                "handoff_service.os.replace",
                side_effect=substitute_parent,
            ):
                result = service.confirm(pending_id)

            private_copy = self.root / "private" / f"{pending_id}.md"
            self.assertEqual(result["state"], "transferred")
            self.assertFalse((outside / target.name).exists())
            self.assertEqual(private_copy.read_text(), VALID_HANDOFF)
            self.assertIn(str(private_copy), client.calls[0][1])

    def test_parent_moved_outside_is_rolled_back_before_private_fallback(self):
        service, client, _clock = self.make_service()
        target = self.root / "docs" / "AI-HANDOFF.md"
        pending_id = service.prepare(self.root, VALID_HANDOFF, target)
        service.arm(pending_id, "thr-old", 300)
        target.parent.mkdir()
        real_replace = os.replace

        with tempfile.TemporaryDirectory() as outside_name:
            displaced = Path(outside_name) / "displaced-docs"
            moved = False

            def move_parent_outside(source, destination, *args, **kwargs):
                nonlocal moved
                is_project_publish = (
                    destination == target.name and "dst_dir_fd" in kwargs
                )
                if not moved and is_project_publish:
                    moved = True
                    target.parent.rename(displaced)
                return real_replace(source, destination, *args, **kwargs)

            with mock.patch(
                "handoff_service.os.replace",
                side_effect=move_parent_outside,
            ):
                result = service.confirm(pending_id)

            private_copy = self.root / "private" / f"{pending_id}.md"
            self.assertEqual(result["state"], "transferred")
            self.assertEqual((displaced / target.name).read_bytes(), b"")
            self.assertEqual(private_copy.read_text(), VALID_HANDOFF)
            self.assertIn(str(private_copy), client.calls[0][1])

    def test_exact_inode_sanitization_failure_marks_failed_and_does_not_launch(self):
        service, client, _clock = self.make_service()
        target = self.root / "docs" / "AI-HANDOFF.md"
        pending_id = service.prepare(self.root, VALID_HANDOFF, target)
        service.arm(pending_id, "thr-old", 300)
        target.parent.mkdir()
        real_replace = os.replace

        with tempfile.TemporaryDirectory() as outside_name:
            displaced = Path(outside_name) / "displaced-docs"
            moved = False

            def move_parent_outside(source, destination, *args, **kwargs):
                nonlocal moved
                is_project_publish = (
                    destination == target.name and "dst_dir_fd" in kwargs
                )
                if not moved and is_project_publish:
                    moved = True
                    target.parent.rename(displaced)
                return real_replace(source, destination, *args, **kwargs)

            with mock.patch(
                "handoff_service.os.replace",
                side_effect=move_parent_outside,
            ), mock.patch(
                "handoff_service.os.ftruncate",
                side_effect=OSError("sanitize denied"),
            ):
                with self.assertRaisesRegex(OSError, "roll back unsafe"):
                    service.confirm(pending_id)

            private_copy = self.root / "private" / f"{pending_id}.md"
            self.assertEqual(service.status("thr-old")["state"], "failed")
            self.assertEqual(private_copy.read_text(), VALID_HANDOFF)
            self.assertTrue((displaced / target.name).exists())
            self.assertEqual(client.calls, [])

    def test_displaced_rollback_never_deletes_swapped_unrelated_file(self):
        service, client, _clock = self.make_service()
        target = self.root / "docs" / "AI-HANDOFF.md"
        pending_id = service.prepare(self.root, VALID_HANDOFF, target)
        service.arm(pending_id, "thr-old", 300)
        target.parent.mkdir()
        real_replace = os.replace
        real_stat = os.stat
        real_ftruncate = os.ftruncate

        with tempfile.TemporaryDirectory() as outside_name:
            displaced = Path(outside_name) / "displaced-docs"
            moved = False
            swapped = False
            unrelated_content = b"unrelated-user-content"

            def move_parent_outside(source, destination, *args, **kwargs):
                nonlocal moved
                is_project_publish = (
                    destination == target.name and "dst_dir_fd" in kwargs
                )
                if not moved and is_project_publish:
                    moved = True
                    target.parent.rename(displaced)
                    (displaced / "unrelated.txt").write_bytes(unrelated_content)
                return real_replace(source, destination, *args, **kwargs)

            def swap_destination():
                nonlocal swapped
                if swapped:
                    return
                swapped = True
                real_replace(
                    displaced / target.name,
                    displaced / "published-stash.md",
                )
                real_replace(
                    displaced / "unrelated.txt",
                    displaced / target.name,
                )

            def race_stat(name, *args, **kwargs):
                metadata = real_stat(name, *args, **kwargs)
                if (
                    moved
                    and not swapped
                    and name == target.name
                    and "dir_fd" in kwargs
                ):
                    swap_destination()
                return metadata

            def race_ftruncate(descriptor, length):
                if moved and not swapped:
                    swap_destination()
                return real_ftruncate(descriptor, length)

            with mock.patch(
                "handoff_service.os.replace",
                side_effect=move_parent_outside,
            ), mock.patch(
                "handoff_service.os.stat",
                side_effect=race_stat,
            ), mock.patch(
                "handoff_service.os.ftruncate",
                side_effect=race_ftruncate,
            ):
                result = service.confirm(pending_id)

            private_copy = self.root / "private" / f"{pending_id}.md"
            self.assertEqual(result["state"], "transferred")
            self.assertEqual(
                (displaced / target.name).read_bytes(),
                unrelated_content,
            )
            self.assertEqual(
                (displaced / "published-stash.md").read_bytes(),
                b"",
            )
            self.assertEqual(private_copy.read_text(), VALID_HANDOFF)
            self.assertIn(str(private_copy), client.calls[0][1])

    def test_post_replace_fsync_failure_rolls_back_displaced_destination(self):
        service, client, _clock = self.make_service()
        target = self.root / "docs" / "AI-HANDOFF.md"
        pending_id = service.prepare(self.root, VALID_HANDOFF, target)
        service.arm(pending_id, "thr-old", 300)
        target.parent.mkdir()
        real_replace = os.replace
        real_fsync = os.fsync

        with tempfile.TemporaryDirectory() as outside_name:
            displaced = Path(outside_name) / "displaced-docs"
            moved = False
            fsync_failed = False

            def move_parent_outside(source, destination, *args, **kwargs):
                nonlocal moved
                is_project_publish = (
                    destination == target.name and "dst_dir_fd" in kwargs
                )
                if not moved and is_project_publish:
                    moved = True
                    target.parent.rename(displaced)
                return real_replace(source, destination, *args, **kwargs)

            def fail_displaced_parent_fsync(descriptor):
                nonlocal fsync_failed
                if moved and not fsync_failed and displaced.exists():
                    opened = os.fstat(descriptor)
                    moved_parent = displaced.stat()
                    if (opened.st_dev, opened.st_ino) == (
                        moved_parent.st_dev,
                        moved_parent.st_ino,
                    ):
                        fsync_failed = True
                        raise OSError("directory fsync failed")
                return real_fsync(descriptor)

            with mock.patch(
                "handoff_service.os.replace",
                side_effect=move_parent_outside,
            ), mock.patch(
                "handoff_service.os.fsync",
                side_effect=fail_displaced_parent_fsync,
            ):
                result = service.confirm(pending_id)

            private_copy = self.root / "private" / f"{pending_id}.md"
            self.assertEqual(result["state"], "transferred")
            self.assertEqual((displaced / target.name).read_bytes(), b"")
            self.assertEqual(private_copy.read_text(), VALID_HANDOFF)
            self.assertIn(str(private_copy), client.calls[0][1])

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

    def test_prepare_ignores_required_headings_inside_markdown_code(self):
        service, client, _clock = self.make_service()
        disguised_headings = {
            "backtick fence": "```text\n# Completed Work\n```",
            "tilde fence": "~~~\n# Completed Work\n~~~",
            "four-space code": "    # Completed Work",
            "tab-indented code": "\t# Completed Work",
        }

        for label, disguised in disguised_headings.items():
            with self.subTest(label=label):
                malformed = VALID_HANDOFF.replace("# Completed Work", disguised)
                with self.assertRaisesRegex(
                    ValueError,
                    "missing required sections",
                ):
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
