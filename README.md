# Agent Work Boundaries

This bundle installs the `project-handoff` and `task-router` Skills and merges
four project-handoff handlers into the user-level Codex Hooks configuration.
The installer preserves unrelated Skills and Hook handlers, uses staged
replacement, and emits one JSON report for every successful operation.

## Install and activate

Preview the exact targets and backup plan. A dry run validates existing files
but does not create the Codex home:

```sh
./install.sh --dry-run
```

Install or upgrade the bundle:

```sh
./install.sh
```

The JSON report lists `changed_paths`, `backed_up_files`, and `backup_root`.
Hooks are installed but remain inactive until their current hash is trusted.
Finish activation with this required operator step:

```text
Open Codex and run /hooks, review the four Agent Work Boundaries handlers, and trust their current hash.
```

The four handlers are `Stop`, `UserPromptSubmit`, `PostCompact`, and
`SessionStart`. Until they are trusted, automatic five-minute transfer and
compaction tracking are inactive. Explicit manual confirmation through
`handoffctl.py confirm` remains available.

## Status

Inspect a pending, completed, or failed handoff with:

```sh
python3 ~/.codex/skills/project-handoff/scripts/handoffctl.py status --session-id <current-session-id>
```

Replace `<current-session-id>` with the real ID of the current Codex session;
the angle-bracket token is user-supplied command syntax, not an implementation
field. On transfer, App Server creates and starts the new thread, but the
current Codex UI may not focus it automatically. Use the reported thread ID to
open it when necessary.

## Uninstall and restore

Preview removal with `./install.sh --uninstall --dry-run`, then remove only the
two managed Skills and the four managed Hook handlers:

```sh
./install.sh --uninstall
```

Install, upgrade, and uninstall protect replaced files under the timestamped
backup root reported as `backup_root`:

```text
~/.codex/backups/agent-work-boundaries/YYYYMMDDTHHMMSSZ
```

If two operations start in the same second, the later directory has a suffix
such as `-01`. A first install into an empty Codex home has no prior files to
back up, so `backup_root` is `null`.

For manual restoration, first uninstall the current managed bundle, select the
specific timestamped directory from an earlier JSON report, and copy back only
the entries you intend to restore:

```sh
./install.sh --uninstall
AWB_BACKUP="$HOME/.codex/backups/agent-work-boundaries/YYYYMMDDTHHMMSSZ"
cp -a "$AWB_BACKUP/skills/project-handoff" "$HOME/.codex/skills/"
cp -a "$AWB_BACKUP/skills/task-router" "$HOME/.codex/skills/"
cp "$AWB_BACKUP/hooks.json" "$HOME/.codex/hooks.json"
```

Some backups may contain only the paths that existed before that operation.
Inspect the selected directory before copying. Restoring `hooks.json` restores
the complete saved file, including unrelated handlers as they existed at that
timestamp; run `/hooks` again afterward because its trusted hash may have
changed.
