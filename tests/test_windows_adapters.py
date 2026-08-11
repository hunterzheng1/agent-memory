from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.subprocess_env import isolated_subprocess_env


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = REPO_ROOT / "scripts"
INSTALLER = SCRIPT_ROOT / "install_runtime.py"
WINDOWS_POWERSHELL = shutil.which("powershell.exe")
POWERSHELL_7 = shutil.which("pwsh.exe")


def make_windows_junction(link: Path, target: Path) -> None:
    created = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=30,
        check=False,
    )
    if created.returncode != 0:
        raise OSError(created.stdout + created.stderr)


def run_ps(
    executable: str,
    script: Path,
    *arguments: str,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            executable,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *arguments,
        ],
        cwd=REPO_ROOT,
        env=env if env is not None else isolated_subprocess_env(),
        input=input_text,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        check=False,
    )


@unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL, "Windows PowerShell 5.1 required")
class WindowsPowerShellAdapterTests(unittest.TestCase):
    def install_runtime(self, root: Path) -> Path:
        result = subprocess.run(
            [sys.executable, str(INSTALLER), "--config-root", str(root), "--json"],
            cwd=REPO_ROOT,
            env=isolated_subprocess_env(),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return root / "scripts"

    def test_ps51_and_ps7_parse_every_adapter(self) -> None:
        executables = [WINDOWS_POWERSHELL]
        if POWERSHELL_7:
            executables.append(POWERSHELL_7)
        adapter_paths = [
            SCRIPT_ROOT / "install-windows.ps1",
            SCRIPT_ROOT / "stop-hook.ps1",
            SCRIPT_ROOT / "install-codex-hook.ps1",
            SCRIPT_ROOT / "audit-task.ps1",
        ]
        quoted = ",".join("'{}'".format(str(path).replace("'", "''")) for path in adapter_paths)
        command = (
            "$all=@();@(" + quoted + ")|%{$t=$null;$e=$null;"
            "[System.Management.Automation.Language.Parser]::ParseFile($_,[ref]$t,[ref]$e)|Out-Null;"
            "$all+=@($e)};if($all.Count){$all|%{Write-Error $_};exit 9}"
        )
        for executable in executables:
            parsed = subprocess.run(
                [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
                cwd=REPO_ROOT,
                env=isolated_subprocess_env(),
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(parsed.returncode, 0, parsed.stdout + parsed.stderr)

    def test_installed_runtime_adapter_smoke_ps51(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve()
            runtime = root / "运行 runtime with spaces"
            scripts = self.install_runtime(runtime)
            probe = scripts / "agent_memory_stop_hook.py"
            probe.write_text(
                "import json, os, sys\n"
                "print(json.dumps({'args': sys.argv[1:], 'stdin': sys.stdin.read()}, ensure_ascii=False))\n"
                "raise SystemExit(int(os.environ.get('PROBE_EXIT', '0')))\n",
                encoding="utf-8",
            )
            env = isolated_subprocess_env({"PROBE_EXIT": "0"})
            payload = json.dumps({"session_id": "会话-42", "message": "路径 空格"}, ensure_ascii=False)
            result = run_ps(
                WINDOWS_POWERSHELL,
                scripts / "stop-hook.ps1",
                "-Actor",
                "codebuddy",
                "-Protocol",
                "claude",
                "-Event",
                "session-end",
                "-NonBlocking",
                "-AutoCloseout",
                "-Timeout",
                "17",
                "-Python",
                sys.executable,
                env=env,
                input_text=payload,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            observed = json.loads(result.stdout.strip())
            self.assertEqual(observed["stdin"], payload)
            self.assertEqual(
                observed["args"],
                [
                    "--actor",
                    "codebuddy",
                    "--protocol",
                    "claude",
                    "--event",
                    "session-end",
                    "--non-blocking",
                    "--timeout",
                    "17",
                    "--auto-closeout",
                ],
            )

            failed = run_ps(
                WINDOWS_POWERSHELL,
                scripts / "stop-hook.ps1",
                "-Actor",
                "codex",
                "-Protocol",
                "codex",
                "-Python",
                sys.executable,
                env=isolated_subprocess_env({"PROBE_EXIT": "7"}),
                input_text="{}",
            )
            self.assertEqual(failed.returncode, 7, failed.stdout + failed.stderr)

    def test_real_stop_hook_rejects_actor_protocol_mismatch(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            runtime = Path(raw_root).resolve() / "runtime"
            scripts = self.install_runtime(runtime)
            result = run_ps(
                WINDOWS_POWERSHELL,
                scripts / "stop-hook.ps1",
                "-Actor",
                "codebuddy",
                "-Protocol",
                "codex",
                "-Event",
                "session-end",
                "-NonBlocking",
                "-Python",
                sys.executable,
                env=isolated_subprocess_env(),
                input_text='{"session_id":"test"}',
            )
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("conflicts with --actor", result.stderr)

    def test_codex_hook_install_is_idempotent_and_preserves_existing_json(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve()
            runtime = root / "runtime 路径"
            scripts = self.install_runtime(runtime)
            hooks_path = root / "用户 profile" / "hooks.json"
            hooks_path.parent.mkdir(parents=True)
            original = {
                "custom": {"label": "保留"},
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": "existing-command"}]}
                    ],
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "existing-stop"}]}
                    ],
                },
            }
            hooks_path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")

            arguments = ("-RuntimeRoot", str(runtime), "-HooksPath", str(hooks_path), "-AutoCloseout")
            first = run_ps(WINDOWS_POWERSHELL, scripts / "install-codex-hook.ps1", *arguments)
            second = run_ps(WINDOWS_POWERSHELL, scripts / "install-codex-hook.ps1", *arguments)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)

            installed = json.loads(hooks_path.read_text(encoding="utf-8-sig"))
            self.assertEqual(installed["custom"], {"label": "保留"})
            self.assertEqual(installed["hooks"]["SessionStart"], original["hooks"]["SessionStart"])
            commands = [
                hook["command"]
                for group in installed["hooks"]["Stop"]
                for hook in group.get("hooks", [])
            ]
            self.assertIn("existing-stop", commands)
            managed = [command for command in commands if "stop-hook.ps1" in command.lower()]
            self.assertEqual(len(managed), 1)
            self.assertIn("-Actor codex", managed[0])
            self.assertIn("-Protocol codex", managed[0])
            self.assertIn("-Event stop-hook", managed[0])

    def test_codex_hook_updates_mode_and_converges_duplicate_managed_entries(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve()
            runtime = root / "runtime"
            scripts = self.install_runtime(runtime)
            hooks_path = root / "profile" / "hooks.json"
            hooks_path.parent.mkdir()
            hooks_path.write_text(
                json.dumps(
                    {
                        "custom": {"preserve": True},
                        "hooks": {
                            "Stop": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": "unrelated-stop",
                                            "timeout": 7,
                                        }
                                    ]
                                }
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )
            base_arguments = (
                "-RuntimeRoot",
                str(runtime),
                "-HooksPath",
                str(hooks_path),
            )

            normal = run_ps(
                WINDOWS_POWERSHELL,
                scripts / "install-codex-hook.ps1",
                *base_arguments,
            )
            self.assertEqual(normal.returncode, 0, normal.stdout + normal.stderr)
            auto = run_ps(
                WINDOWS_POWERSHELL,
                scripts / "install-codex-hook.ps1",
                *base_arguments,
                "-AutoCloseout",
            )
            self.assertEqual(auto.returncode, 0, auto.stdout + auto.stderr)
            auto_payload = json.loads(hooks_path.read_text(encoding="utf-8-sig"))
            managed_groups = [
                group
                for group in auto_payload["hooks"]["Stop"]
                if any(
                    "stop-hook.ps1" in hook.get("command", "").lower()
                    for hook in group["hooks"]
                )
            ]
            self.assertEqual(len(managed_groups), 1)
            managed_hook = next(
                hook
                for hook in managed_groups[0]["hooks"]
                if "stop-hook.ps1" in hook["command"].lower()
            )
            self.assertIn("-AutoCloseout", managed_hook["command"])
            self.assertEqual(managed_hook["timeout"], 320)

            auto_payload["hooks"]["Stop"].append(json.loads(json.dumps(managed_groups[0])))
            hooks_path.write_text(json.dumps(auto_payload), encoding="utf-8")
            back_to_normal = run_ps(
                WINDOWS_POWERSHELL,
                scripts / "install-codex-hook.ps1",
                *base_arguments,
            )
            self.assertEqual(
                back_to_normal.returncode,
                0,
                back_to_normal.stdout + back_to_normal.stderr,
            )
            final_payload = json.loads(hooks_path.read_text(encoding="utf-8-sig"))
            managed_hooks = [
                hook
                for group in final_payload["hooks"]["Stop"]
                for hook in group["hooks"]
                if "stop-hook.ps1" in hook.get("command", "").lower()
            ]
            self.assertEqual(len(managed_hooks), 1)
            self.assertNotIn("-AutoCloseout", managed_hooks[0]["command"])
            self.assertEqual(managed_hooks[0]["timeout"], 20)
            self.assertEqual(final_payload["custom"], {"preserve": True})
            unrelated = [
                hook
                for group in final_payload["hooks"]["Stop"]
                for hook in group["hooks"]
                if hook.get("command") == "unrelated-stop"
            ]
            self.assertEqual(len(unrelated), 1)

    def test_codex_hook_recognizes_single_quoted_managed_wrapper(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve()
            runtime = root / "runtime path"
            scripts = self.install_runtime(runtime)
            hooks_path = root / "profile" / "hooks.json"
            hooks_path.parent.mkdir()
            wrapper = scripts / "stop-hook.ps1"
            single_quoted = (
                "powershell.exe -NoProfile -File "
                f"'{wrapper}' -Actor codex -Protocol codex -Event stop-hook"
            )
            hooks_path.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "Stop": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": single_quoted,
                                            "timeout": 20,
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )

            result = run_ps(
                WINDOWS_POWERSHELL,
                scripts / "install-codex-hook.ps1",
                "-RuntimeRoot",
                str(runtime),
                "-HooksPath",
                str(hooks_path),
                "-AutoCloseout",
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(hooks_path.read_text(encoding="utf-8-sig"))
            managed = [
                hook
                for group in payload["hooks"]["Stop"]
                for hook in group["hooks"]
                if "stop-hook.ps1" in hook.get("command", "").lower()
            ]
            self.assertEqual(len(managed), 1)
            self.assertIn("-AutoCloseout", managed[0]["command"])
            self.assertEqual(managed[0]["timeout"], 320)

    def test_codex_hook_rejects_invalid_nested_schema_without_rewrite(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve()
            runtime = root / "runtime"
            scripts = self.install_runtime(runtime)
            hooks_path = root / "profile" / "hooks.json"
            hooks_path.parent.mkdir()
            before = b'{"hooks":{"Stop":[{"hooks":"not-an-array"}]}}\n'
            hooks_path.write_bytes(before)

            result = run_ps(
                WINDOWS_POWERSHELL,
                scripts / "install-codex-hook.ps1",
                "-RuntimeRoot",
                str(runtime),
                "-HooksPath",
                str(hooks_path),
            )

            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("schema", result.stderr.lower())
            self.assertEqual(hooks_path.read_bytes(), before)

    def test_codex_hook_detects_concurrent_edit_before_replace(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve()
            runtime = root / "runtime"
            scripts = self.install_runtime(runtime)
            hooks_path = root / "profile" / "hooks.json"
            hooks_path.parent.mkdir()
            hooks_path.write_text('{"custom":"initial"}\n', encoding="utf-8")
            barrier = root / "agent-memory-hook-test-race"
            barrier.mkdir()
            environment = isolated_subprocess_env(
                {"_AGENT_MEMORY_TEST_HOOK_BARRIER": str(barrier)}
            )
            command = [
                WINDOWS_POWERSHELL,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(scripts / "install-codex-hook.ps1"),
                "-RuntimeRoot",
                str(runtime),
                "-HooksPath",
                str(hooks_path),
            ]
            process = subprocess.Popen(
                command,
                cwd=REPO_ROOT,
                env=environment,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            ready = barrier / "ready"
            deadline = time.monotonic() + 10
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if not ready.exists():
                stdout, stderr = process.communicate(timeout=10)
                self.fail(f"hook test barrier was not reached: {stdout}{stderr}")
            concurrent = b'{"custom":"concurrent-editor"}\n'
            hooks_path.write_bytes(concurrent)
            (barrier / "continue").write_text("continue\n", encoding="utf-8")
            stdout, stderr = process.communicate(timeout=20)

            self.assertNotEqual(process.returncode, 0, stdout + stderr)
            self.assertIn("concurrent", stderr.lower())
            self.assertEqual(hooks_path.read_bytes(), concurrent)

    def test_codex_hook_restores_actual_object_changed_after_cas(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve()
            runtime = root / "runtime"
            scripts = self.install_runtime(runtime)
            hooks_path = root / "profile" / "hooks.json"
            hooks_path.parent.mkdir()
            hooks_path.write_text('{"custom":"initial"}\n', encoding="utf-8")
            barrier = root / "agent-memory-hook-test-after-cas"
            barrier.mkdir()
            environment = isolated_subprocess_env(
                {"_AGENT_MEMORY_TEST_HOOK_AFTER_CAS_BARRIER": str(barrier)}
            )
            command = [
                WINDOWS_POWERSHELL,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(scripts / "install-codex-hook.ps1"),
                "-RuntimeRoot",
                str(runtime),
                "-HooksPath",
                str(hooks_path),
            ]
            process = subprocess.Popen(
                command,
                cwd=REPO_ROOT,
                env=environment,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            ready = barrier / "ready"
            deadline = time.monotonic() + 10
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if not ready.exists():
                stdout, stderr = process.communicate(timeout=10)
                self.fail(f"after-CAS hook barrier was not reached: {stdout}{stderr}")
            concurrent = b'{"custom":"after-cas-editor"}\n'
            hooks_path.write_bytes(concurrent)
            (barrier / "continue").write_text("continue\n", encoding="utf-8")
            stdout, stderr = process.communicate(timeout=20)

            self.assertNotEqual(process.returncode, 0, stdout + stderr)
            self.assertIn("actual hooks object changed", stderr.lower())
            self.assertEqual(hooks_path.read_bytes(), concurrent)

    def test_codex_hook_default_path_uses_temporary_userprofile(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve()
            runtime = root / "runtime"
            scripts = self.install_runtime(runtime)
            profile = root / "temporary profile"
            profile.mkdir()

            result = run_ps(
                WINDOWS_POWERSHELL,
                scripts / "install-codex-hook.ps1",
                "-RuntimeRoot",
                str(runtime),
                env=isolated_subprocess_env(
                    {"USERPROFILE": str(profile), "HOME": str(profile)}
                ),
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            hooks_path = profile / ".codex" / "hooks.json"
            self.assertTrue(hooks_path.is_file())
            payload = json.loads(hooks_path.read_text(encoding="utf-8-sig"))
            self.assertEqual(len(payload["hooks"]["Stop"]), 1)

    def test_codex_hook_rejects_parent_junction_and_file_symlink(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve()
            runtime = root / "runtime"
            scripts = self.install_runtime(runtime)

            outside_directory = root / "outside-directory"
            outside_directory.mkdir()
            sentinel = outside_directory / "sentinel.txt"
            sentinel.write_text("unchanged\n", encoding="utf-8")
            hooks_parent = root / "profile-link"
            try:
                make_windows_junction(hooks_parent, outside_directory)
            except OSError as exc:
                self.skipTest(f"junctions unavailable: {exc}")
            try:
                parent_result = run_ps(
                    WINDOWS_POWERSHELL,
                    scripts / "install-codex-hook.ps1",
                    "-RuntimeRoot",
                    str(runtime),
                    "-HooksPath",
                    str(hooks_parent / "hooks.json"),
                )
                self.assertNotEqual(
                    parent_result.returncode,
                    0,
                    parent_result.stdout + parent_result.stderr,
                )
                self.assertIn("reparse", parent_result.stderr.lower())
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged\n")
                self.assertFalse((outside_directory / "hooks.json").exists())
            finally:
                os.rmdir(hooks_parent)

            safe_parent = root / "safe-profile"
            safe_parent.mkdir()
            external_file = root / "outside-hooks.json"
            external_file.write_text('{"sentinel":"unchanged"}\n', encoding="utf-8")
            hooks_file = safe_parent / "hooks.json"
            try:
                hooks_file.symlink_to(external_file)
            except OSError as exc:
                self.skipTest(f"file symlinks unavailable: {exc}")
            file_result = run_ps(
                WINDOWS_POWERSHELL,
                scripts / "install-codex-hook.ps1",
                "-RuntimeRoot",
                str(runtime),
                "-HooksPath",
                str(hooks_file),
            )
            self.assertNotEqual(
                file_result.returncode,
                0,
                file_result.stdout + file_result.stderr,
            )
            self.assertIn("reparse", file_result.stderr.lower())
            self.assertEqual(
                external_file.read_text(encoding="utf-8"),
                '{"sentinel":"unchanged"}\n',
            )

    def test_audit_task_plan_only_has_no_scheduled_task_side_effect(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            runtime = Path(raw_root).resolve() / "runtime with spaces"
            scripts = self.install_runtime(runtime)
            result = run_ps(
                WINDOWS_POWERSHELL,
                scripts / "audit-task.ps1",
                "install",
                "-RuntimeRoot",
                str(runtime),
                "-Python",
                sys.executable,
                "-TaskName",
                "AgentMemoryVaultAudit-Test-DoNotCreate",
                "-PlanOnly",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            plan = json.loads(result.stdout)
            self.assertEqual(plan["action"], "install")
            self.assertEqual(plan["task_name"], "AgentMemoryVaultAudit-Test-DoNotCreate")
            self.assertEqual(plan["python"], sys.executable)
            self.assertIn("agent_memory_audit_autorun.py", plan["arguments"])

            uninstall = run_ps(
                WINDOWS_POWERSHELL,
                scripts / "audit-task.ps1",
                "uninstall",
                "-RuntimeRoot",
                str(runtime),
                "-Python",
                sys.executable,
                "-TaskName",
                "AgentMemoryVaultAudit-Test-DoNotCreate",
                "-PlanOnly",
            )
            self.assertEqual(uninstall.returncode, 0, uninstall.stdout + uninstall.stderr)
            uninstall_plan = json.loads(uninstall.stdout)
            self.assertEqual(uninstall_plan["action"], "uninstall")
            self.assertFalse(uninstall_plan["side_effects"])

    def test_audit_task_rejects_unsafe_task_names(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            runtime = Path(raw_root).resolve() / "runtime"
            scripts = self.install_runtime(runtime)
            invalid_names = (
                "unsafe*wildcard",
                "unsafe?wildcard",
                "unsafe[range]",
                "unsafe/path",
                "unsafe\\path",
                "unsafe\ncontrol",
                "x" * 129,
            )
            for task_name in invalid_names:
                with self.subTest(task_name=repr(task_name)):
                    result = run_ps(
                        WINDOWS_POWERSHELL,
                        scripts / "audit-task.ps1",
                        "status",
                        "-TaskName",
                        task_name,
                        "-PlanOnly",
                        env=isolated_subprocess_env({"PATH": ""}),
                    )
                    self.assertNotEqual(
                        result.returncode,
                        0,
                        result.stdout + result.stderr,
                    )
                    self.assertIn("TaskName", result.stderr)

    def test_audit_noninstall_plan_does_not_resolve_python_and_has_fixed_path(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            runtime = Path(raw_root).resolve() / "runtime"
            scripts = self.install_runtime(runtime)
            for action in ("status", "run", "uninstall"):
                with self.subTest(action=action):
                    result = run_ps(
                        WINDOWS_POWERSHELL,
                        scripts / "audit-task.ps1",
                        action,
                        "-TaskName",
                        "AgentMemoryVaultAudit-Test",
                        "-PlanOnly",
                        env=isolated_subprocess_env({"PATH": ""}),
                    )
                    self.assertEqual(
                        result.returncode,
                        0,
                        result.stdout + result.stderr,
                    )
                    payload = json.loads(result.stdout)
                    self.assertEqual(payload["task_path"], "\\AgentMemory\\")
                    self.assertIn(payload["python"], (None, ""))

    def test_audit_noninstall_actions_work_without_python_using_exact_mock_task(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve()
            runtime = root / "runtime"
            scripts = self.install_runtime(runtime)
            probe = root / "audit-task-mock.ps1"
            audit_script_literal = str(scripts / "audit-task.ps1").replace("'", "''")
            probe.write_text(
                "\n".join(
                    [
                        "$ErrorActionPreference = 'Stop'",
                        "function Get-ScheduledTask {",
                        "  [CmdletBinding()] param([string]$TaskPath, [string]$TaskName)",
                        "  if ($TaskPath -ne '\\AgentMemory\\') { throw 'wrong task path' }",
                        "  [pscustomobject]@{ TaskPath=$TaskPath; TaskName=$TaskName; State='Ready' }",
                        "}",
                        "function Get-ScheduledTaskInfo {",
                        "  [CmdletBinding()] param([string]$TaskPath, [string]$TaskName)",
                        "  if ($TaskPath -ne '\\AgentMemory\\') { throw 'wrong info path' }",
                        "  [pscustomobject]@{ LastTaskResult=0; NextRunTime='soon' }",
                        "}",
                        "function Start-ScheduledTask {",
                        "  [CmdletBinding()] param([string]$TaskPath, [string]$TaskName)",
                        "  if ($TaskPath -ne '\\AgentMemory\\') { throw 'wrong start path' }",
                        "}",
                        "function Unregister-ScheduledTask {",
                        "  [CmdletBinding()] param([string]$TaskPath, [string]$TaskName, [switch]$Confirm)",
                        "  if ($TaskPath -ne '\\AgentMemory\\') { throw 'wrong unregister path' }",
                        "}",
                        f"& '{audit_script_literal}' $env:TEST_AUDIT_ACTION -TaskName 'AgentMemoryVaultAudit-Test'",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            for action in ("status", "run", "uninstall"):
                with self.subTest(action=action):
                    result = run_ps(
                        WINDOWS_POWERSHELL,
                        probe,
                        env=isolated_subprocess_env(
                            {"PATH": "", "TEST_AUDIT_ACTION": action}
                        ),
                    )
                    self.assertEqual(
                        result.returncode,
                        0,
                        result.stdout + result.stderr,
                    )

    def test_audit_task_rejects_ambiguous_exact_lookup(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve()
            runtime = root / "runtime"
            scripts = self.install_runtime(runtime)
            probe = root / "audit-task-ambiguous.ps1"
            audit_script_literal = str(scripts / "audit-task.ps1").replace("'", "''")
            python_literal = str(sys.executable).replace("'", "''")
            probe.write_text(
                "\n".join(
                    [
                        "$ErrorActionPreference = 'Stop'",
                        "function Get-ScheduledTask {",
                        "  [CmdletBinding()] param([string]$TaskPath, [string]$TaskName)",
                        "  1..2 | ForEach-Object {",
                        "    [pscustomobject]@{ TaskPath=$TaskPath; TaskName=$TaskName; State='Ready' }",
                        "  }",
                        "}",
                        f"& '{audit_script_literal}' status -TaskName 'AgentMemoryVaultAudit-Test' -Python '{python_literal}'",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            result = run_ps(WINDOWS_POWERSHELL, probe)

            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("multiple", result.stderr.lower())

    def test_windows_installer_fresh_install_and_upgrade_preserve_config(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve()
            runtime = root / "配置 runtime with spaces"
            vault = root / "记忆 vault with spaces"
            safe_home = root / "fake user"
            safe_home.mkdir()
            env = isolated_subprocess_env(
                {
                    "USERPROFILE": str(safe_home),
                    "HOME": str(safe_home),
                    "LOCALAPPDATA": str(root / "local app data"),
                }
            )
            arguments = (
                "-MemoryRoot",
                str(vault),
                "-ConfigRoot",
                str(runtime),
                "-UserId",
                'user "quoted" 测试',
                "-AgentId",
                "shared",
                "-AppId",
                "app\\segment",
            )
            first = run_ps(
                WINDOWS_POWERSHELL,
                SCRIPT_ROOT / "install-windows.ps1",
                *arguments,
                env=env,
                timeout=300,
            )
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertIn("windows_acl_unverified", (first.stdout + first.stderr).lower())
            config = runtime / "config" / "agent-memory.toml"
            self.assertTrue(config.is_file())
            self.assertFalse(config.read_bytes().startswith(b"\xef\xbb\xbf"))
            self.assertTrue((runtime / ".venv" / "Scripts" / "python.exe").is_file())
            self.assertTrue((runtime / "config" / "runtime-manifest.json").is_file())
            self.assertTrue((runtime / "state.sqlite").is_file())
            self.assertTrue((vault / ".git" / "HEAD").is_file())
            self.assertFalse((safe_home / ".codex" / "hooks.json").exists())

            loaded = subprocess.run(
                [
                    str(runtime / ".venv" / "Scripts" / "python.exe"),
                    "-c",
                    (
                        "import json,sys;"
                        f"sys.path.insert(0,{str(runtime / 'scripts')!r});"
                        "import agent_memory_env;"
                        "print(json.dumps(agent_memory_env.load_config(),ensure_ascii=False))"
                    ),
                ],
                cwd=root,
                env=isolated_subprocess_env({"AGENT_MEMORY_CONFIG_FILE": str(config)}),
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(loaded.returncode, 0, loaded.stdout + loaded.stderr)
            parsed_config = json.loads(loaded.stdout)
            self.assertEqual(Path(parsed_config["memory_root"]), vault)
            self.assertEqual(parsed_config["user_id"], 'user "quoted" 测试')
            self.assertEqual(parsed_config["app_id"], "app\\segment")

            marker = "\n# preserve-upgrade-marker\n"
            config.write_text(config.read_text(encoding="utf-8") + marker, encoding="utf-8")
            before = config.read_bytes()
            other_vault = root / "must not create this vault"
            second_args = list(arguments)
            second_args[1] = str(other_vault)
            second = run_ps(
                WINDOWS_POWERSHELL,
                runtime / "scripts" / "install-windows.ps1",
                *second_args,
                env=env,
                timeout=300,
            )
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertIn("existing_config_preserved", second.stdout)
            self.assertEqual(config.read_bytes(), before)
            self.assertFalse(other_vault.exists())

            invalid_config = before + b"\ninvalid = [\n"
            config.write_bytes(invalid_config)
            invalid_upgrade = run_ps(
                WINDOWS_POWERSHELL,
                runtime / "scripts" / "install-windows.ps1",
                *second_args,
                env=env,
                timeout=300,
            )
            self.assertNotEqual(
                invalid_upgrade.returncode,
                0,
                invalid_upgrade.stdout + invalid_upgrade.stderr,
            )
            self.assertIn("config_validation=error", invalid_upgrade.stderr)
            self.assertEqual(config.read_bytes(), invalid_config)
            self.assertFalse(other_vault.exists())
            config.write_bytes(before)

            third = run_ps(
                WINDOWS_POWERSHELL,
                runtime / "scripts" / "install-windows.ps1",
                *second_args,
                "-OverwriteConfig",
                env=env,
                timeout=300,
            )
            self.assertEqual(third.returncode, 0, third.stdout + third.stderr)
            self.assertIn("config_overwritten explicit=", third.stdout)
            self.assertTrue((other_vault / ".git" / "HEAD").is_file())
            self.assertNotEqual(config.read_bytes(), before)

    def test_windows_installer_explicit_hook_path_preserves_json_idempotently(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve()
            runtime = root / "runtime with hooks"
            vault = root / "vault with hooks"
            safe_home = root / "fake user"
            safe_home.mkdir()
            hooks_path = root / "explicit hooks" / "hooks.json"
            hooks_path.parent.mkdir()
            original = {
                "custom": {"label": "preserve-explicit"},
                "hooks": {
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "existing-stop"}]}
                    ]
                },
            }
            hooks_path.write_text(json.dumps(original), encoding="utf-8")
            env = isolated_subprocess_env(
                {
                    "USERPROFILE": str(safe_home),
                    "HOME": str(safe_home),
                    "LOCALAPPDATA": str(root / "local app data"),
                }
            )
            arguments = (
                "-MemoryRoot",
                str(vault),
                "-ConfigRoot",
                str(runtime),
                "-InstallCodexHook",
                "-CodexHooksPath",
                str(hooks_path),
            )

            first = run_ps(
                WINDOWS_POWERSHELL,
                SCRIPT_ROOT / "install-windows.ps1",
                *arguments,
                env=env,
                timeout=300,
            )
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            second = run_ps(
                WINDOWS_POWERSHELL,
                runtime / "scripts" / "install-windows.ps1",
                *arguments,
                env=env,
                timeout=300,
            )
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)

            installed = json.loads(hooks_path.read_text(encoding="utf-8-sig"))
            self.assertEqual(installed["custom"], original["custom"])
            commands = [
                hook["command"]
                for group in installed["hooks"]["Stop"]
                for hook in group.get("hooks", [])
            ]
            self.assertIn("existing-stop", commands)
            managed = [command for command in commands if "stop-hook.ps1" in command.lower()]
            self.assertEqual(len(managed), 1)
            self.assertFalse((safe_home / ".codex" / "hooks.json").exists())

    def test_windows_installer_default_hook_parent_reparse_fails_before_barrier(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve()
            runtime = root / "runtime"
            vault = root / "must-not-exist-vault"
            safe_home = root / "fake user"
            safe_home.mkdir()
            external = root / "outside hooks"
            external.mkdir()
            sentinel = external / "sentinel.txt"
            sentinel.write_text("unchanged\n", encoding="utf-8")
            hooks_parent = safe_home / ".codex"
            try:
                make_windows_junction(hooks_parent, external)
            except OSError as exc:
                self.skipTest(f"junctions unavailable: {exc}")
            barrier = root / "agent-memory-installer-test-default-hook-boundary"
            barrier.mkdir()
            env = isolated_subprocess_env(
                {
                    "USERPROFILE": str(safe_home),
                    "HOME": str(safe_home),
                    "LOCALAPPDATA": str(root / "local app data"),
                    "AGENT_MEMORY_INSTALLER_TEST_BARRIER": str(barrier),
                }
            )
            command = [
                WINDOWS_POWERSHELL,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(SCRIPT_ROOT / "install-windows.ps1"),
                "-MemoryRoot",
                str(vault),
                "-ConfigRoot",
                str(runtime),
                "-InstallCodexHook",
            ]
            process = subprocess.Popen(
                command,
                cwd=REPO_ROOT,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            ready = barrier / "ready"
            deadline = time.monotonic() + 15
            try:
                while (
                    not ready.is_file()
                    and process.poll() is None
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.05)
                if ready.is_file():
                    process.terminate()
                    stdout, stderr = process.communicate(timeout=30)
                    self.fail(
                        "unsafe default hooks path reached the installer barrier\n"
                        + stdout
                        + stderr
                    )
                stdout, stderr = process.communicate(timeout=30)

                self.assertNotEqual(process.returncode, 0, stdout + stderr)
                self.assertIn("installer_preflight=error", stderr)
                self.assertIn("reparse", stderr.lower())
                self.assertFalse((runtime / ".venv").exists())
                self.assertFalse((runtime / "scripts").exists())
                self.assertFalse(vault.exists())
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged\n")
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.communicate(timeout=30)
                os.rmdir(hooks_parent)

    def test_windows_installer_default_hook_path_uses_temporary_userprofile(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve()
            runtime = root / "default hook runtime"
            vault = root / "default hook vault"
            safe_home = root / "temporary user profile"
            safe_home.mkdir()
            hooks_path = safe_home / ".codex" / "hooks.json"
            env = isolated_subprocess_env(
                {
                    "USERPROFILE": str(safe_home),
                    "HOME": str(safe_home),
                    "LOCALAPPDATA": str(root / "local app data"),
                }
            )
            arguments = (
                "-MemoryRoot",
                str(vault),
                "-ConfigRoot",
                str(runtime),
                "-InstallCodexHook",
            )

            first = run_ps(
                WINDOWS_POWERSHELL,
                SCRIPT_ROOT / "install-windows.ps1",
                *arguments,
                env=env,
                timeout=300,
            )
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            second = run_ps(
                WINDOWS_POWERSHELL,
                runtime / "scripts" / "install-windows.ps1",
                *arguments,
                env=env,
                timeout=300,
            )
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)

            installed = json.loads(hooks_path.read_text(encoding="utf-8-sig"))
            commands = [
                hook["command"]
                for group in installed["hooks"]["Stop"]
                for hook in group.get("hooks", [])
            ]
            managed = [command for command in commands if "stop-hook.ps1" in command.lower()]
            self.assertEqual(len(managed), 1)

    def test_windows_installer_hook_file_symlink_fails_before_side_effects(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve()
            runtime = root / "runtime"
            vault = root / "must-not-exist-vault"
            hooks_path = root / "hooks" / "hooks.json"
            hooks_path.parent.mkdir()
            external = root / "outside-hooks.json"
            external.write_text('{"sentinel":"unchanged"}\n', encoding="utf-8")
            try:
                hooks_path.symlink_to(external)
            except OSError as exc:
                self.skipTest(f"file symlinks unavailable: {exc}")

            result = run_ps(
                WINDOWS_POWERSHELL,
                SCRIPT_ROOT / "install-windows.ps1",
                "-MemoryRoot",
                str(vault),
                "-ConfigRoot",
                str(runtime),
                "-InstallCodexHook",
                "-CodexHooksPath",
                str(hooks_path),
                env=isolated_subprocess_env(),
                timeout=60,
            )

            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("installer_preflight=error", result.stderr)
            self.assertIn("reparse", result.stderr.lower())
            self.assertFalse((runtime / ".venv").exists())
            self.assertFalse((runtime / "scripts").exists())
            self.assertFalse(vault.exists())
            self.assertEqual(
                external.read_text(encoding="utf-8"),
                '{"sentinel":"unchanged"}\n',
            )

    def test_windows_installer_preflight_rejects_config_directory_before_side_effects(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve()
            runtime = root / "runtime"
            config_path = runtime / "config" / "agent-memory.toml"
            config_path.mkdir(parents=True)
            vault = root / "must-not-exist-vault"
            result = run_ps(
                WINDOWS_POWERSHELL,
                SCRIPT_ROOT / "install-windows.ps1",
                "-MemoryRoot",
                str(vault),
                "-ConfigRoot",
                str(runtime),
                env=isolated_subprocess_env(),
                timeout=60,
            )

            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("installer_preflight=error", result.stderr)
            self.assertFalse(vault.exists())
            self.assertFalse((runtime / ".venv").exists())
            self.assertFalse((runtime / "scripts").exists())
            self.assertFalse((runtime / "config" / "runtime-manifest.json").exists())
            self.assertTrue(config_path.is_dir())

    def test_windows_installer_preflight_rejects_config_file_symlink_before_side_effects(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve()
            runtime = root / "runtime"
            config_path = runtime / "config" / "agent-memory.toml"
            config_path.parent.mkdir(parents=True)
            outside = root / "outside-config.toml"
            outside.write_text("sentinel = true\n", encoding="utf-8")
            try:
                config_path.symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"file symlinks unavailable: {exc}")
            vault = root / "must-not-exist-vault"
            result = run_ps(
                WINDOWS_POWERSHELL,
                SCRIPT_ROOT / "install-windows.ps1",
                "-MemoryRoot",
                str(vault),
                "-ConfigRoot",
                str(runtime),
                env=isolated_subprocess_env(),
                timeout=60,
            )

            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("installer_preflight=error", result.stderr)
            self.assertIn("reparse", result.stderr.lower())
            self.assertFalse(vault.exists())
            self.assertFalse((runtime / ".venv").exists())
            self.assertFalse((runtime / "scripts").exists())
            self.assertEqual(outside.read_text(encoding="utf-8"), "sentinel = true\n")

    def test_windows_installer_preflight_rejects_config_parent_junction_before_side_effects(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve()
            runtime = root / "runtime"
            runtime.mkdir()
            outside = root / "outside-config-parent"
            outside.mkdir()
            sentinel = outside / "sentinel.txt"
            sentinel.write_text("unchanged\n", encoding="utf-8")
            junction = runtime / "config"
            try:
                make_windows_junction(junction, outside)
            except OSError as exc:
                self.skipTest(f"junctions unavailable: {exc}")
            vault = root / "must-not-exist-vault"
            try:
                result = run_ps(
                    WINDOWS_POWERSHELL,
                    SCRIPT_ROOT / "install-windows.ps1",
                    "-MemoryRoot",
                    str(vault),
                    "-ConfigRoot",
                    str(runtime),
                    env=isolated_subprocess_env(),
                    timeout=60,
                )

                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("installer_preflight=error", result.stderr)
                self.assertIn("reparse", result.stderr.lower())
                self.assertFalse(vault.exists())
                self.assertFalse((runtime / ".venv").exists())
                self.assertFalse((runtime / "scripts").exists())
                self.assertEqual(
                    sorted(path.name for path in outside.iterdir()),
                    ["sentinel.txt"],
                )
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged\n")
            finally:
                os.rmdir(junction)

    def test_windows_installer_rechecks_reserved_config_parent_before_venv(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve()
            runtime = root / "runtime"
            vault = root / "must-not-exist-vault"
            external = root / "external-config-parent"
            external.mkdir()
            sentinel = external / "sentinel.txt"
            sentinel.write_text("unchanged\n", encoding="utf-8")
            barrier = root / "agent-memory-installer-test-barrier"
            barrier.mkdir()
            env = isolated_subprocess_env(
                {"AGENT_MEMORY_INSTALLER_TEST_BARRIER": str(barrier)}
            )
            command = [
                WINDOWS_POWERSHELL,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(SCRIPT_ROOT / "install-windows.ps1"),
                "-MemoryRoot",
                str(vault),
                "-ConfigRoot",
                str(runtime),
            ]
            process = subprocess.Popen(
                command,
                cwd=REPO_ROOT,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            ready = barrier / "ready"
            deadline = time.monotonic() + 15
            while not ready.is_file() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if not ready.is_file():
                process.terminate()
                stdout, stderr = process.communicate(timeout=30)
                self.fail("installer test barrier was not reached\n" + stdout + stderr)

            config_dir = runtime / "config"
            self.assertTrue(config_dir.is_dir())
            os.rmdir(config_dir)
            try:
                make_windows_junction(config_dir, external)
            except OSError:
                process.terminate()
                process.communicate(timeout=30)
                raise
            try:
                (barrier / "continue").write_text("continue\n", encoding="utf-8")
                stdout, stderr = process.communicate(timeout=60)

                self.assertNotEqual(process.returncode, 0, stdout + stderr)
                self.assertIn("installer_preflight=error", stderr)
                self.assertIn("reparse", stderr.lower())
                self.assertFalse((runtime / ".venv").exists())
                self.assertFalse((runtime / "scripts").exists())
                self.assertFalse(vault.exists())
                self.assertEqual(
                    sorted(path.name for path in external.iterdir()),
                    ["sentinel.txt"],
                )
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged\n")
            finally:
                os.rmdir(config_dir)

    def test_windows_installer_rechecks_config_content_hash_before_venv(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
            root = Path(raw_root).resolve()
            runtime = root / "runtime"
            config = runtime / "config" / "agent-memory.toml"
            config.parent.mkdir(parents=True)
            before = "memory_root = 'vault-before'\n"
            after = "memory_root = 'vault-after-'\n"
            self.assertEqual(len(before), len(after))
            config.write_text(before, encoding="utf-8")
            vault = root / "must-not-exist-vault"
            barrier = root / "agent-memory-installer-test-config-hash"
            barrier.mkdir()
            env = isolated_subprocess_env(
                {"AGENT_MEMORY_INSTALLER_TEST_BARRIER": str(barrier)}
            )
            command = [
                WINDOWS_POWERSHELL,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(SCRIPT_ROOT / "install-windows.ps1"),
                "-MemoryRoot",
                str(vault),
                "-ConfigRoot",
                str(runtime),
            ]
            process = subprocess.Popen(
                command,
                cwd=REPO_ROOT,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            ready = barrier / "ready"
            deadline = time.monotonic() + 15
            while not ready.is_file() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if not ready.is_file():
                process.terminate()
                stdout, stderr = process.communicate(timeout=30)
                self.fail("installer config hash barrier was not reached\n" + stdout + stderr)

            config.write_text(after, encoding="utf-8")
            (barrier / "continue").write_text("continue\n", encoding="utf-8")
            stdout, stderr = process.communicate(timeout=60)

            self.assertNotEqual(process.returncode, 0, stdout + stderr)
            self.assertIn("installer_preflight=error", stderr)
            self.assertIn("path_changed", stderr)
            self.assertFalse((runtime / ".venv").exists())
            self.assertFalse((runtime / "scripts").exists())
            self.assertFalse(vault.exists())
            self.assertEqual(config.read_text(encoding="utf-8"), after)


if __name__ == "__main__":
    unittest.main()
