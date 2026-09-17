import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock


SCRIPTS_DIR = (
    Path(__file__).resolve().parents[2]
    / "skills"
    / "project-handoff"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS_DIR))

import handoff_hook


PENDING_ID = "123e4567-e89b-42d3-a456-426614174000"


class FakeStore:
    def __init__(self):
        self.compaction_counts = {}
        self.compaction_sources = {}

    def record_compaction(self, session_id, source):
        count = self.compaction_counts.get(session_id, 0) + 1
        self.compaction_counts[session_id] = count
        sources = self.compaction_sources.setdefault(session_id, {})
        sources[source] = sources.get(source, 0) + 1
        return count


class FakeService:
    def __init__(self, records=None):
        self.records = dict(records or {})
        self.arm_calls = []
        self.respond_calls = []
        self.status_calls = []
        self.store = FakeStore()

    def arm(self, pending_id, session_id, timeout_seconds=300):
        self.arm_calls.append((pending_id, session_id, timeout_seconds))
        if pending_id != PENDING_ID:
            return None
        record = {
            "pending_id": pending_id,
            "session_id": session_id,
            "state": "armed",
        }
        self.records[session_id] = record
        return record

    def respond(self, session_id):
        self.respond_calls.append(session_id)
        record = self.records.get(session_id)
        if record is None or record.get("state") != "armed":
            return None
        responded = dict(record, state="responded")
        self.records[session_id] = responded
        return responded

    def status(self, session_id):
        self.status_calls.append(session_id)
        record = self.records.get(session_id)
        if record is None:
            count = self.store.compaction_counts.get(session_id, 0)
            if count:
                return {
                    "session_id": session_id,
                    "compaction_count": count,
                    "compaction_sources": dict(
                        self.store.compaction_sources[session_id]
                    ),
                }
            return None
        combined = dict(record)
        if session_id in self.store.compaction_counts:
            combined["compaction_count"] = self.store.compaction_counts[session_id]
            combined["compaction_sources"] = dict(
                self.store.compaction_sources[session_id]
            )
        return combined


class RecordingSpawner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **options):
        self.calls.append((list(argv), dict(options)))
        return object()


def stop_event(message):
    return {
        "hook_event_name": "Stop",
        "session_id": "thr-old",
        "last_assistant_message": message,
    }


def prompt_event(prompt="continue with the next check"):
    return {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "thr-old",
        "prompt": prompt,
    }


def post_compact_event(trigger):
    return {
        "hook_event_name": "PostCompact",
        "session_id": "thr-old",
        "trigger": trigger,
    }


def session_start_event(source="compact"):
    return {
        "hook_event_name": "SessionStart",
        "session_id": "thr-old",
        "source": source,
    }


class HandoffHookStopAndPromptTests(unittest.TestCase):
    def test_stop_arms_marker_and_spawns_wait_worker(self):
        service = FakeService()
        spawner = RecordingSpawner()
        marker = f"<!-- project-handoff:pending={PENDING_ID} -->"

        with mock.patch.object(
            handoff_hook,
            "HANDOFFCTL_PATH",
            Path("/installed/project-handoff/scripts/handoffctl.py"),
        ):
            result = handoff_hook.handle_event(
                stop_event(f"Shall I hand this off?\n\n{marker}"),
                service,
                spawner,
            )

        self.assertIsNone(result)
        self.assertEqual(
            service.arm_calls,
            [(PENDING_ID, "thr-old", 300)],
        )
        self.assertEqual(
            spawner.calls,
            [
                (
                    [
                        sys.executable,
                        "/installed/project-handoff/scripts/handoffctl.py",
                        "wait",
                        "--pending-id",
                        PENDING_ID,
                    ],
                    {
                        "stdin": subprocess.DEVNULL,
                        "stdout": subprocess.DEVNULL,
                        "stderr": subprocess.DEVNULL,
                        "start_new_session": True,
                        "close_fds": True,
                    },
                )
            ],
        )

    def test_stop_requires_one_exact_canonical_marker(self):
        valid = f"<!-- project-handoff:pending={PENDING_ID} -->"
        invalid_messages = (
            f"<!--project-handoff:pending={PENDING_ID} -->",
            f"<!-- project-handoff:pending={PENDING_ID}-->",
            f"<!-- project-handoff:pending={PENDING_ID.upper()} -->",
            "<!-- project-handoff:pending=not-a-uuid -->",
            f"{valid}\n{valid}",
        )

        for message in invalid_messages:
            with self.subTest(message=message):
                service = FakeService()
                spawner = RecordingSpawner()

                result = handoff_hook.handle_event(
                    stop_event(message),
                    service,
                    spawner,
                )

                self.assertIsNone(result)
                self.assertEqual(service.arm_calls, [])
                self.assertEqual(spawner.calls, [])

    def test_stop_does_not_spawn_when_draft_cannot_be_armed(self):
        class UnarmedService(FakeService):
            def arm(self, pending_id, session_id, timeout_seconds=300):
                self.arm_calls.append(
                    (pending_id, session_id, timeout_seconds)
                )
                return None

        service = UnarmedService()
        spawner = RecordingSpawner()
        marker = f"<!-- project-handoff:pending={PENDING_ID} -->"

        result = handoff_hook.handle_event(
            stop_event(marker),
            service,
            spawner,
        )

        self.assertIsNone(result)
        self.assertEqual(service.arm_calls, [(PENDING_ID, "thr-old", 300)])
        self.assertEqual(spawner.calls, [])

    def test_any_user_prompt_marks_armed_handoff_responded(self):
        prompts = (
            "Yes, hand it off.",
            "No, keep working here.",
            "Before that, what tests are left?",
        )

        for prompt in prompts:
            with self.subTest(prompt=prompt):
                service = FakeService(
                    {
                        "thr-old": {
                            "pending_id": PENDING_ID,
                            "session_id": "thr-old",
                            "state": "armed",
                        }
                    }
                )

                result = handoff_hook.handle_event(
                    prompt_event(prompt),
                    service,
                    RecordingSpawner(),
                )

                self.assertEqual(
                    service.records["thr-old"]["state"],
                    "responded",
                )
                context = result["hookSpecificOutput"]["additionalContext"]
                self.assertEqual(
                    result["hookSpecificOutput"]["hookEventName"],
                    "UserPromptSubmit",
                )
                self.assertIn("countdown was cancelled", context)
                self.assertIn(
                    f"handoffctl confirm --pending-id {PENDING_ID}",
                    context,
                )
                self.assertIn(
                    f"handoffctl cancel --pending-id {PENDING_ID}",
                    context,
                )
                self.assertIn("otherwise continue here", context)
                self.assertLess(len(context.split()), 120)

    def test_user_prompt_blocks_superseded_session(self):
        service = FakeService(
            {
                "thr-old": {
                    "pending_id": PENDING_ID,
                    "session_id": "thr-old",
                    "state": "transferred",
                    "new_thread_id": "thr-new",
                }
            }
        )

        result = handoff_hook.handle_event(
            prompt_event(),
            service,
            RecordingSpawner(),
        )

        self.assertEqual(
            result,
            {
                "decision": "block",
                "reason": (
                    "This conversation was handed off to thread thr-new; "
                    "open that thread instead of continuing duplicate work."
                ),
            },
        )
        self.assertEqual(service.respond_calls, [])

    def test_late_prompts_do_not_reopen_responded_or_cancelled_handoffs(self):
        for state in ("responded", "cancelled"):
            with self.subTest(state=state):
                service = FakeService(
                    {
                        "thr-old": {
                            "pending_id": PENDING_ID,
                            "session_id": "thr-old",
                            "state": state,
                        }
                    }
                )

                result = handoff_hook.handle_event(
                    prompt_event(),
                    service,
                    RecordingSpawner(),
                )

                self.assertIsNone(result)
                self.assertEqual(service.records["thr-old"]["state"], state)

    def test_late_prompt_reports_failed_transfer_recovery(self):
        recovery = "Open /new and paste the saved handoff resume prompt."
        service = FakeService(
            {
                "thr-old": {
                    "pending_id": PENDING_ID,
                    "session_id": "thr-old",
                    "state": "failed",
                    "recovery_prompt": recovery,
                }
            }
        )

        result = handoff_hook.handle_event(
            prompt_event(),
            service,
            RecordingSpawner(),
        )

        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(
            result["hookSpecificOutput"]["hookEventName"],
            "UserPromptSubmit",
        )
        self.assertIn("handoff transfer failed", context)
        self.assertIn(recovery, context)
        self.assertLess(len(context.split()), 120)


class HandoffHookCompactionTests(unittest.TestCase):
    def record_compactions(self, service, *triggers):
        outputs = []
        for trigger in triggers:
            outputs.append(
                handoff_hook.handle_event(
                    post_compact_event(trigger),
                    service,
                    RecordingSpawner(),
                )
            )
        return outputs

    def context_from(self, output):
        self.assertEqual(
            output["hookSpecificOutput"]["hookEventName"],
            "SessionStart",
        )
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertLess(len(context.split()), 120)
        return context

    def test_post_compact_records_manual_and_auto_counts(self):
        service = FakeService()

        outputs = self.record_compactions(service, "manual", "auto", "auto")

        self.assertEqual(
            outputs,
            [
                {
                    "systemMessage": (
                        "Recorded project handoff compaction 1 (manual)."
                    )
                },
                {
                    "systemMessage": (
                        "Recorded project handoff compaction 2 (auto)."
                    )
                },
                {
                    "systemMessage": (
                        "Recorded project handoff compaction 3 (auto)."
                    )
                },
            ],
        )
        self.assertEqual(service.store.compaction_counts["thr-old"], 3)
        self.assertEqual(
            service.store.compaction_sources["thr-old"],
            {"manual": 1, "auto": 2},
        )

    def test_session_start_first_compaction_only_records(self):
        service = FakeService()
        self.record_compactions(service, "manual")

        result = handoff_hook.handle_event(
            session_start_event(),
            service,
            RecordingSpawner(),
        )

        self.assertIsNone(result)
        self.assertEqual(service.store.compaction_counts["thr-old"], 1)
        self.assertEqual(
            service.store.compaction_sources["thr-old"],
            {"manual": 1},
        )

    def test_session_start_second_compaction_injects_stable_boundary_reminder(self):
        service = FakeService()
        self.record_compactions(service, "auto", "manual")

        result = handoff_hook.handle_event(
            session_start_event(),
            service,
            RecordingSpawner(),
        )

        context = self.context_from(result)
        self.assertIn("compacted 2 times", context)
        self.assertIn("next stable boundary", context)
        self.assertIn("request a project handoff", context)
        self.assertEqual(service.store.compaction_counts["thr-old"], 2)
        self.assertEqual(
            service.store.compaction_sources["thr-old"],
            {"auto": 1, "manual": 1},
        )

    def test_session_start_third_compaction_injects_strong_handoff_instruction(self):
        for count in (3, 4):
            with self.subTest(count=count):
                service = FakeService()
                self.record_compactions(service, *("auto",) * count)

                result = handoff_hook.handle_event(
                    session_start_event(),
                    service,
                    RecordingSpawner(),
                )

                context = self.context_from(result)
                self.assertIn(f"compacted {count} times", context)
                self.assertIn("strongly request a project handoff", context)
                self.assertIn("current non-interruptible step", context)
                self.assertEqual(
                    service.store.compaction_counts["thr-old"],
                    count,
                )

    def test_non_compact_session_start_does_not_emit_threshold_context(self):
        service = FakeService()
        self.record_compactions(service, "auto", "auto", "auto")

        for source in ("startup", "resume", "clear"):
            with self.subTest(source=source):
                result = handoff_hook.handle_event(
                    session_start_event(source),
                    service,
                    RecordingSpawner(),
                )

                self.assertIsNone(result)
        self.assertEqual(service.store.compaction_counts["thr-old"], 3)

    def test_session_start_reports_failed_transfer_recovery(self):
        recovery = (
            "Open /new and paste the saved resume prompt from "
            "/private/handoff.md."
        )
        service = FakeService(
            {
                "thr-old": {
                    "pending_id": PENDING_ID,
                    "session_id": "thr-old",
                    "state": "failed",
                    "recovery_prompt": recovery,
                }
            }
        )

        for source in ("startup", "resume", "clear", "compact"):
            with self.subTest(source=source):
                result = handoff_hook.handle_event(
                    session_start_event(source),
                    service,
                    RecordingSpawner(),
                )

                context = self.context_from(result)
                self.assertIn("handoff transfer failed", context)
                self.assertIn(recovery, context)

    def test_failed_transfer_context_stays_below_word_limit(self):
        service = FakeService(
            {
                "thr-old": {
                    "pending_id": PENDING_ID,
                    "session_id": "thr-old",
                    "state": "failed",
                    "recovery_prompt": "resume " * 150,
                }
            }
        )

        result = handoff_hook.handle_event(
            session_start_event("resume"),
            service,
            RecordingSpawner(),
        )

        context = self.context_from(result)
        self.assertIn("handoff transfer failed", context)
        self.assertIn(
            "handoffctl status --session-id thr-old",
            context,
        )

    def test_failed_recovery_and_compaction_context_stays_below_word_limit(self):
        service = FakeService(
            {
                "thr-old": {
                    "pending_id": PENDING_ID,
                    "session_id": "thr-old",
                    "state": "failed",
                    "recovery_prompt": "resume " * 100,
                }
            }
        )
        self.record_compactions(service, "auto", "auto", "auto")

        result = handoff_hook.handle_event(
            session_start_event("compact"),
            service,
            RecordingSpawner(),
        )

        context = self.context_from(result)
        self.assertIn("handoff transfer failed", context)
        self.assertIn("strongly request a project handoff", context)
        self.assertIn(
            "handoffctl status --session-id thr-old",
            context,
        )


class HandoffHookSafetyAndMainTests(unittest.TestCase):
    def test_malformed_and_unknown_events_have_no_effect(self):
        malformed = (
            None,
            [],
            {},
            {"hook_event_name": 42},
            {"hook_event_name": "Unknown", "session_id": "thr-old"},
            {
                "hook_event_name": "Stop",
                "session_id": None,
                "last_assistant_message": (
                    f"<!-- project-handoff:pending={PENDING_ID} -->"
                ),
            },
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "thr-old",
                "prompt": None,
            },
            {
                "hook_event_name": "PostCompact",
                "session_id": "thr-old",
                "trigger": "scheduled",
            },
            {
                "hook_event_name": "SessionStart",
                "session_id": "thr-old",
                "source": "fork",
            },
        )
        service = FakeService()
        spawner = RecordingSpawner()

        results = [
            handoff_hook.handle_event(payload, service, spawner)
            for payload in malformed
        ]

        self.assertEqual(results, [None] * len(malformed))
        self.assertEqual(service.arm_calls, [])
        self.assertEqual(service.respond_calls, [])
        self.assertEqual(service.status_calls, [])
        self.assertEqual(service.store.compaction_counts, {})
        self.assertEqual(spawner.calls, [])

    def test_main_writes_one_json_object_for_output(self):
        service = FakeService()
        stdin = io.StringIO(json.dumps(post_compact_event("manual")))
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = handoff_hook.main(
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            service=service,
            spawn_worker=RecordingSpawner(),
        )

        lines = stdout.getvalue().splitlines()
        self.assertEqual(code, 0)
        self.assertEqual(len(lines), 1)
        self.assertEqual(
            json.loads(lines[0]),
            {
                "systemMessage": (
                    "Recorded project handoff compaction 1 (manual)."
                )
            },
        )
        self.assertEqual(stderr.getvalue(), "")

    def test_main_emits_nothing_for_malformed_or_unknown_input(self):
        inputs = (
            "not json",
            "[]",
            json.dumps(
                {"hook_event_name": "Unknown", "session_id": "thr-old"}
            ),
        )

        for hook_input in inputs:
            with self.subTest(hook_input=hook_input):
                service = FakeService()
                stdout = io.StringIO()
                stderr = io.StringIO()

                code = handoff_hook.main(
                    stdin=io.StringIO(hook_input),
                    stdout=stdout,
                    stderr=stderr,
                    service=service,
                    spawn_worker=RecordingSpawner(),
                )

                self.assertEqual(code, 0)
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(stderr.getvalue(), "")
                self.assertEqual(service.arm_calls, [])
                self.assertEqual(service.respond_calls, [])
                self.assertEqual(service.store.compaction_counts, {})

    def test_main_contains_unexpected_failures_without_leaking_text(self):
        class FailingService(FakeService):
            def status(self, session_id):
                raise RuntimeError("Bearer private-token-value")

        stdout = io.StringIO()
        stderr = io.StringIO()

        code = handoff_hook.main(
            stdin=io.StringIO(json.dumps(session_start_event("startup"))),
            stdout=stdout,
            stderr=stderr,
            service=FailingService(),
            spawn_worker=RecordingSpawner(),
        )

        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_main_contains_service_construction_failure(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        with mock.patch.object(
            handoff_hook,
            "_build_service",
            side_effect=RuntimeError("Bearer private-token-value"),
        ):
            code = handoff_hook.main(
                stdin=io.StringIO(json.dumps(session_start_event("startup"))),
                stdout=stdout,
                stderr=stderr,
                spawn_worker=RecordingSpawner(),
            )

        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
