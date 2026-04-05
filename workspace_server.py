from flask import Flask, jsonify, request

from workspace_runtime import WorkspaceRuntime, RuntimeLimits, WorkspaceRuntimeError

import os


app = Flask(__name__)
AUTH_TOKEN = os.environ.get("WORKSPACE_SERVICE_TOKEN", "").strip()

runtime = WorkspaceRuntime(
    root_dir=os.environ.get("OMNIAI_WORKSPACE_ROOT", os.path.join(os.getcwd(), "storage", "sandboxes")),
    backend=os.environ.get("OMNIAI_WORKSPACE_BACKEND", "local"),
    docker_image=os.environ.get("OMNIAI_WORKSPACE_IMAGE", "ghcr.io/open-webui/python:3.11-bookworm"),
    network_enabled=os.environ.get("OMNIAI_WORKSPACE_NETWORK", "false").lower() == "true",
    limits=RuntimeLimits(
        max_runtime_seconds=int(os.environ.get("OMNIAI_WORKSPACE_MAX_RUNTIME", "30")),
        max_output_bytes=int(os.environ.get("OMNIAI_WORKSPACE_MAX_OUTPUT_BYTES", "65536")),
        max_processes_per_workspace=int(os.environ.get("OMNIAI_WORKSPACE_MAX_PROCESSES", "8")),
    ),
)


@app.before_request
def require_token():
    if not AUTH_TOKEN:
        return None
    if request.headers.get("X-Workspace-Token", "") != AUTH_TOKEN:
        return jsonify({"error": "Unauthorized"}), 401
    return None


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "backend": runtime.backend})


@app.route("/workspaces/<workspace_id>", methods=["POST"])
def create_workspace(workspace_id):
    root = runtime.ensure_workspace(workspace_id)
    return jsonify({"success": True, "workspace_id": workspace_id, "root": root})


@app.route("/workspaces/<workspace_id>/files", methods=["GET"])
def list_files(workspace_id):
    path = request.args.get("path", "")
    try:
        return jsonify({"success": True, "files": runtime.list_files(workspace_id, path)})
    except WorkspaceRuntimeError as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/workspaces/<workspace_id>/read", methods=["POST"])
def read_file(workspace_id):
    data = request.get_json() or {}
    try:
        return jsonify({"success": True, "content": runtime.read_file(workspace_id, data.get("path", ""))})
    except WorkspaceRuntimeError as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/workspaces/<workspace_id>/write", methods=["POST"])
def write_file(workspace_id):
    data = request.get_json() or {}
    try:
        return jsonify({"success": True, "file": runtime.write_file(workspace_id, data.get("path", ""), data.get("content", ""))})
    except WorkspaceRuntimeError as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/workspaces/<workspace_id>/exec", methods=["POST"])
def run_command(workspace_id):
    data = request.get_json() or {}
    try:
        return jsonify({"success": True, "result": runtime.run_command(workspace_id, data.get("command", ""), data.get("working_dir", ""))})
    except WorkspaceRuntimeError as e:
        return jsonify({"success": False, "error": str(e)}), 400


if __name__ == "__main__":
    app.run(host=os.environ.get("WORKSPACE_SERVICE_HOST", "127.0.0.1"), port=int(os.environ.get("WORKSPACE_SERVICE_PORT", "8090")))
