import os
import json
import time
import uuid
import signal
import select
import shutil
import subprocess
from dataclasses import dataclass


class WorkspaceRuntimeError(RuntimeError):
    pass


@dataclass
class RuntimeLimits:
    max_runtime_seconds: int = 30
    max_output_bytes: int = 65536
    max_processes_per_workspace: int = 8


class WorkspaceRuntime:
    """Workspace runtime abstraction for isolated per-conversation work."""

    def __init__(
        self,
        root_dir,
        backend="local",
        docker_image="ghcr.io/open-webui/python:3.11-bookworm",
        network_enabled=False,
        limits=None,
    ):
        self.root_dir = os.path.realpath(root_dir)
        self.backend = (backend or "local").lower()
        self.docker_image = docker_image
        self.network_enabled = bool(network_enabled)
        self.limits = limits or RuntimeLimits()
        self._interactive = {}
        os.makedirs(self.root_dir, exist_ok=True)

    def ensure_workspace(self, workspace_id):
        root = self.workspace_root(workspace_id)
        os.makedirs(root, exist_ok=True)
        if self.backend == "docker":
            self._ensure_docker_container(workspace_id)
        return root

    def workspace_root(self, workspace_id):
        return os.path.join(self.root_dir, str(workspace_id))

    def list_files(self, workspace_id, path=""):
        root = self.ensure_workspace(workspace_id)
        target = self._resolve_path(root, path or ".")
        if not os.path.exists(target):
            raise WorkspaceRuntimeError(f"Path not found: {path or '.'}")
        results = []
        if os.path.isfile(target):
            st = os.stat(target)
            return [{
                "name": os.path.basename(target),
                "path": os.path.relpath(target, root),
                "type": "file",
                "size": st.st_size,
            }]
        for entry in sorted(os.listdir(target), key=str.lower):
            if entry.startswith("."):
                continue
            full_path = os.path.join(target, entry)
            st = os.stat(full_path)
            results.append({
                "name": entry,
                "path": os.path.relpath(full_path, root),
                "type": "dir" if os.path.isdir(full_path) else "file",
                "size": st.st_size,
            })
        return results

    def read_file(self, workspace_id, path):
        root = self.ensure_workspace(workspace_id)
        target = self._resolve_path(root, path)
        if not os.path.isfile(target):
            raise WorkspaceRuntimeError(f"File not found: {path}")
        with open(target, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()

    def write_file(self, workspace_id, path, content):
        root = self.ensure_workspace(workspace_id)
        target = self._resolve_path(root, path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(content)
        return {
            "path": os.path.relpath(target, root),
            "size": os.path.getsize(target),
        }

    def run_command(self, workspace_id, command, cwd=""):
        self._check_command(command)
        root = self.ensure_workspace(workspace_id)
        if self.backend == "docker":
            return self._run_command_docker(workspace_id, command, cwd)
        return self._run_command_local(root, command, cwd)

    def start_process(self, workspace_id, command, cwd=""):
        if self.backend != "local":
            raise WorkspaceRuntimeError("Interactive processes are only supported with the local backend.")
        self._check_command(command)
        root = self.ensure_workspace(workspace_id)
        cwd_path = self._resolve_path(root, cwd or ".")
        active_for_workspace = [
            p for p in self._interactive.values()
            if p["workspace_id"] == workspace_id and p["proc"].poll() is None
        ]
        if len(active_for_workspace) >= self.limits.max_processes_per_workspace:
            raise WorkspaceRuntimeError("Workspace process limit reached.")
        proc = subprocess.Popen(
            ["bash", "-lc", command],
            cwd=cwd_path,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            preexec_fn=os.setsid,
        )
        process_id = str(uuid.uuid4())
        self._interactive[process_id] = {
            "workspace_id": workspace_id,
            "root": root,
            "cwd": cwd_path,
            "command": command,
            "proc": proc,
            "created_at": time.time(),
            "last_active": time.time(),
        }
        return {
            "process_id": process_id,
            "status": "running",
        }

    def read_process_output(self, process_id):
        proc_info = self._interactive.get(process_id)
        if not proc_info:
            raise WorkspaceRuntimeError("Process not found or already expired.")
        proc = proc_info["proc"]
        proc_info["last_active"] = time.time()
        output = []
        try:
            while True:
                ready, _, _ = select.select([proc.stdout], [], [], 0.05)
                if not ready:
                    break
                chunk = os.read(proc.stdout.fileno(), 4096).decode("utf-8", errors="replace")
                if not chunk:
                    break
                output.append(chunk)
                if sum(len(c.encode("utf-8")) for c in output) >= self.limits.max_output_bytes:
                    break
        except Exception:
            pass
        running = proc.poll() is None
        if not running:
            self._interactive.pop(process_id, None)
        return {
            "process_id": process_id,
            "output": "".join(output),
            "running": running,
            "exit_code": proc.returncode,
        }

    def send_process_input(self, process_id, input_text):
        proc_info = self._interactive.get(process_id)
        if not proc_info:
            raise WorkspaceRuntimeError("Process not found or already expired.")
        proc = proc_info["proc"]
        if proc.poll() is not None:
            raise WorkspaceRuntimeError("Process has already exited.")
        proc.stdin.write(input_text)
        proc.stdin.flush()
        proc_info["last_active"] = time.time()
        return {"success": True}

    def stop_process(self, process_id):
        proc_info = self._interactive.pop(process_id, None)
        if not proc_info:
            return {"success": True, "note": "Process already stopped."}
        proc = proc_info["proc"]
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.wait(timeout=2)
        except Exception:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        return {"success": True}

    def _run_command_local(self, root, command, cwd=""):
        cwd_path = self._resolve_path(root, cwd or ".")
        try:
            result = subprocess.run(
                ["bash", "-lc", command],
                cwd=cwd_path,
                capture_output=True,
                text=True,
                timeout=self.limits.max_runtime_seconds,
            )
        except subprocess.TimeoutExpired:
            return {
                "status": "timeout",
                "exit_code": None,
                "output": f"Command timed out after {self.limits.max_runtime_seconds} seconds.",
                "backend": "local",
            }
        output = (result.stdout or "") + (result.stderr or "")
        output = output[: self.limits.max_output_bytes]
        return {
            "status": "completed",
            "exit_code": result.returncode,
            "output": output,
            "backend": "local",
        }

    def _run_command_docker(self, workspace_id, command, cwd=""):
        container_name = self._ensure_docker_container(workspace_id)
        workdir = "/workspace"
        if cwd:
            clean_cwd = cwd.strip().lstrip("./")
            workdir = f"/workspace/{clean_cwd}"
        cmd = [
            "docker", "exec", "-w", workdir, container_name, "bash", "-lc", command,
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.limits.max_runtime_seconds,
            )
        except FileNotFoundError as exc:
            raise WorkspaceRuntimeError("Docker backend requested but docker is not installed.") from exc
        except subprocess.TimeoutExpired:
            return {
                "status": "timeout",
                "exit_code": None,
                "output": f"Command timed out after {self.limits.max_runtime_seconds} seconds.",
                "backend": "docker",
            }
        output = (result.stdout or "") + (result.stderr or "")
        output = output[: self.limits.max_output_bytes]
        return {
            "status": "completed",
            "exit_code": result.returncode,
            "output": output,
            "backend": "docker",
        }

    def _ensure_docker_container(self, workspace_id):
        root = self.workspace_root(workspace_id)
        os.makedirs(root, exist_ok=True)
        name = f"omniai-ws-{workspace_id}".replace("_", "-").lower()
        try:
            lookup = subprocess.run(
                ["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except FileNotFoundError as exc:
            raise WorkspaceRuntimeError("Docker backend requested but docker is not installed.") from exc
        existing = lookup.stdout.strip().splitlines()
        if name in existing:
            subprocess.run(["docker", "start", name], capture_output=True, text=True, timeout=15)
            return name
        cmd = [
            "docker", "run", "-d",
            "--name", name,
            "-v", f"{root}:/workspace",
            "-w", "/workspace",
        ]
        if not self.network_enabled:
            cmd.extend(["--network", "none"])
        cmd.extend([self.docker_image, "sleep", "infinity"])
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if result.returncode != 0:
            raise WorkspaceRuntimeError(result.stderr.strip() or "Failed to start workspace container.")
        return name

    def _check_command(self, command):
        if not command or not command.strip():
            raise WorkspaceRuntimeError("Command is required.")
        blocked = [
            "rm -rf /",
            "mkfs",
            "dd if=",
            ":(){ :|:& };:",
            "> /dev/sd",
        ]
        if any(token in command for token in blocked):
            raise WorkspaceRuntimeError("Command blocked for safety.")
        if not self.network_enabled:
            network_blocked = [
                "curl ",
                "wget ",
                "pip install",
                "python -m pip install",
                "npm install",
                "npm i ",
                "pnpm add",
                "yarn add",
                "git clone",
            ]
            lowered = command.lower()
            if any(token in lowered for token in network_blocked):
                raise WorkspaceRuntimeError("Outbound-network package and fetch commands are disabled for this workspace.")

    def _resolve_path(self, root, path):
        candidate = os.path.realpath(os.path.join(root, path or "."))
        if not (candidate == root or candidate.startswith(root + os.sep)):
            raise WorkspaceRuntimeError("Invalid workspace path.")
        return candidate
