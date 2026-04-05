#!/usr/bin/env python3
"""
OmniAI Local Model Server — manages llama-server.

Standalone CLI tool and importable module.

Usage:
    python omniai_server.py start [--force]
    python omniai_server.py stop
    python omniai_server.py status
    python omniai_server.py list
    python omniai_server.py load <model>
    python omniai_server.py config [key] [value]
"""

import os
import sys
import json
import glob
import time
import socket
import signal
import atexit
import subprocess
import argparse
import threading
import requests

try:
    from huggingface_hub import hf_hub_download
except ImportError:
    hf_hub_download = None


# ── Path Helpers ──────────────────────────────────────────────────────────────

def get_app_root():
    """Get the path to the directory containing the executable or script."""
    if getattr(sys, 'frozen', False):
        base_path = os.path.dirname(sys.executable)
        if ".app/Contents/MacOS" in base_path:
            base_path = os.path.dirname(os.path.dirname(os.path.dirname(base_path)))
        return base_path
    else:
        return os.path.dirname(os.path.abspath(__file__))

def get_resource_root():
    """Get the path to the temporary directory where PyInstaller unpacks resources."""
    if getattr(sys, 'frozen', False):
        return sys._MEIPASS
    else:
        return get_app_root()


APP_ROOT = get_app_root()
RESOURCE_ROOT = get_resource_root()


# ── Config ────────────────────────────────────────────────────────────────────

CONFIG_PATH = os.path.join(APP_ROOT, "omniai_server.json")

DEFAULT_CONFIG = {
    "active_model_path": "",
    "active_projector_path": "",
    "context_length": 8192,
    "host": "127.0.0.1",
    "port": 8033,
    "flash_attn": True,
    "batch_size": 4096,
    "ubatch_size": 512,
    "threads": 8,
    "cache_type_k": "q8_0",
    "cache_type_v": "q8_0",
    "models_dir": "models",
}

def load_config():
    """Load config from JSON file, creating with defaults if missing."""
    if not os.path.exists(CONFIG_PATH):
        save_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, 'r') as f:
            config = json.load(f)
        # Merge with defaults for any missing keys
        merged = dict(DEFAULT_CONFIG)
        merged.update(config)
        return merged
    except Exception as e:
        print(f"Warning: Failed to load config: {e}")
        return dict(DEFAULT_CONFIG)

def save_config(config):
    """Write config to JSON file."""
    try:
        with open(CONFIG_PATH, 'w') as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        print(f"Warning: Failed to save config: {e}")

def get_models_dir(config=None):
    """Resolve models_dir from config (relative or absolute)."""
    if config is None:
        config = load_config()
    d = config.get("models_dir", "models")
    if not os.path.isabs(d):
        d = os.path.join(APP_ROOT, d)
    return d


MODELS_DIR = get_models_dir()
os.makedirs(MODELS_DIR, exist_ok=True)


# ── Server State ──────────────────────────────────────────────────────────────

_server_process = None
_active_model_path = None
_active_projector_path = None
_context_length = 8192


# ── Core Functions ────────────────────────────────────────────────────────────

def kill_port(port=8033):
    """Forcefully kill any process listening on the given port."""
    try:
        if os.name == 'posix':
            result = subprocess.check_output(
                ["lsof", "-t", "-i", f":{port}"], stderr=subprocess.DEVNULL
            )
            pids = result.decode().strip().split('\n')
            for pid in pids:
                if pid:
                    print(f"Killing process {pid} on port {port}...")
                    os.system(f"kill -9 {pid}")
        elif os.name == 'nt':
            subprocess.check_output(
                ["netstat", "-ano", "|", "findstr", f":{port}"], shell=True
            )
    except subprocess.CalledProcessError:
        pass  # Port not in use
    except Exception as e:
        print(f"Warning: Error killing port {port}: {e}")


def load_model_config():
    """Load model config from JSON into module state."""
    global _active_model_path, _active_projector_path, _context_length
    config = load_config()
    model_path = config.get("active_model_path", "")
    projector_path = config.get("active_projector_path", "")
    ctx_len = config.get("context_length", 8192)

    if model_path:
        _active_model_path = model_path
        _active_projector_path = projector_path or None
        print(f"Loaded model config: {os.path.basename(_active_model_path)}")
    if ctx_len:
        _context_length = int(ctx_len)


def save_model_config():
    """Save current module state to JSON config."""
    config = load_config()
    config["active_model_path"] = _active_model_path or ""
    config["active_projector_path"] = _active_projector_path or ""
    config["context_length"] = _context_length
    save_config(config)
    print("Saved model config.")


def get_server_status():
    """Check if the server is running and what model is loaded."""
    config = load_config()
    port = config.get("port", 8033)
    host = config.get("host", "127.0.0.1")
    result = {"running": False, "model": None, "port": port}

    try:
        resp = requests.get(f"http://{host}:{port}/health", timeout=2)
        if resp.status_code == 200:
            result["running"] = True
    except:
        return result

    try:
        resp = requests.get(f"http://{host}:{port}/v1/models", timeout=2)
        if resp.status_code == 200:
            data = resp.json()
            models = data.get("data", [])
            if models:
                result["model"] = models[0].get("id", "unknown")
    except:
        pass

    return result


def start_server(force_restart=False):
    """Start the llama-server process."""
    global _server_process, _active_model_path, _active_projector_path

    config = load_config()
    host = config.get("host", "127.0.0.1")
    port = config.get("port", 8033)

    # Load config if model not set
    if not _active_model_path:
        load_model_config()

    # Check if port is already in use
    if not force_restart:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((host, port)) == 0:
                print(f"Server already running on {host}:{port}")
                return
    else:
        kill_port(port)
        time.sleep(1)

    # Find binary
    binary_name = "llama-server"
    if os.name == 'nt':
        binary_name += ".exe"

    bundled_binary = os.path.join(RESOURCE_ROOT, binary_name)
    if os.path.exists(bundled_binary):
        binary_path = bundled_binary
        print(f"Using bundled llama-server: {binary_path}")
        try:
            os.chmod(binary_path, 0o755)
        except:
            pass
    else:
        binary_path = "/opt/homebrew/bin/llama-server"
        if not os.path.exists(binary_path):
            binary_path = binary_name  # Hope it's on PATH
        print(f"Using llama-server: {binary_path}")

    # Resolve model path with fallbacks
    final_model_path = _active_model_path
    final_projector_path = _active_projector_path

    DEFAULT_QWEN_MODEL = "/Users/ifichukudenwaesei/Library/Caches/llama.cpp/Qwen_Qwen3-VL-8B-Instruct-GGUF_Qwen3VL-8B-Instruct-Q4_K_M.gguf"
    DEFAULT_QWEN_PROJECTOR = "/Users/ifichukudenwaesei/Library/Caches/llama.cpp/Qwen_Qwen3-VL-8B-Instruct-GGUF_mmproj-Qwen3VL-8B-Instruct-F16.gguf"

    if not final_model_path or not os.path.exists(final_model_path):
        if os.path.exists(DEFAULT_QWEN_MODEL):
            final_model_path = DEFAULT_QWEN_MODEL
            final_projector_path = DEFAULT_QWEN_PROJECTOR
        else:
            local_ggufs = glob.glob(os.path.join(MODELS_DIR, "*.gguf"))
            if local_ggufs:
                final_model_path = local_ggufs[0]
                proj_guess = final_model_path.replace(".gguf", ".mmproj.gguf")
                final_projector_path = proj_guess if os.path.exists(proj_guess) else None
                print(f"Found local model: {os.path.basename(final_model_path)}")
            else:
                print("ERROR: No model found! Cannot start server.")
                return

    # Auto-attach Qwen3-VL projector
    if final_model_path and (not final_projector_path or not os.path.exists(final_projector_path)):
        basename = os.path.basename(final_model_path)
        if "Qwen3" in basename and "VL" in basename.upper():
            if os.path.exists(DEFAULT_QWEN_PROJECTOR):
                final_projector_path = DEFAULT_QWEN_PROJECTOR
                print(f"Auto-attached Qwen3-VL vision encoder")

    # Build command
    flash_attn = config.get("flash_attn", True)
    batch_size = config.get("batch_size", 4096)
    ubatch_size = config.get("ubatch_size", 512)
    threads = config.get("threads", 8)
    cache_type_k = config.get("cache_type_k", "q8_0")
    cache_type_v = config.get("cache_type_v", "q8_0")

    cmd = [
        binary_path,
        "-m", final_model_path,
        "--host", host,
        "--port", str(port),
        "--jinja",
        "-c", str(_context_length),
        "--n-gpu-layers", "99",
        "-t", str(threads),
        "-b", str(batch_size),
        "-ub", str(ubatch_size),
        "--cache-type-k", cache_type_k,
        "--cache-type-v", cache_type_v,
        "--no-webui",
    ]

    if flash_attn:
        cmd.extend(["--flash-attn", "on"])

    if final_projector_path and os.path.exists(final_projector_path):
        cmd.extend(["--mmproj", final_projector_path])

    print(f"Starting llama-server: {os.path.basename(final_model_path)} "
          f"(ctx: {_context_length}, batch: {batch_size}, flash_attn: {flash_attn}, "
          f"threads: {threads}, kv_cache: {cache_type_k})...")

    try:
        _server_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        time.sleep(1)
        if _server_process.poll() is not None:
            stdout, stderr = _server_process.communicate()
            print(f"Server died immediately!\nSTDOUT: {stdout}\nSTDERR: {stderr}")
            return

        print("Waiting for server to be ready...")
        for _ in range(30):
            try:
                requests.get(f"http://{host}:{port}/health", timeout=1)
                print("Server is ready!")
                return
            except:
                time.sleep(1)

        print("Server started but health check timed out (may still be loading).")

    except Exception as e:
        print(f"Failed to start llama-server: {e}")


def stop_server():
    """Stop the llama-server process."""
    global _server_process
    if _server_process:
        print("Stopping llama-server...")
        _server_process.terminate()
        try:
            _server_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _server_process.kill()
        _server_process = None
        print("Server stopped.")
    else:
        # Try killing by port in case it was started externally
        config = load_config()
        kill_port(config.get("port", 8033))


def get_local_models():
    """List all .gguf files in the models directory."""
    models = ["Default: Qwen3-VL-8B"]
    if os.path.exists(MODELS_DIR):
        for f in os.listdir(MODELS_DIR):
            if f.endswith(".gguf"):
                models.append(f)
    return models


def download_model(repo_id, filename):
    """Download a model from Hugging Face Hub."""
    if not hf_hub_download:
        print("ERROR: huggingface_hub not installed.")
        return
    try:
        print(f"Downloading: {repo_id}/{filename}")
        hf_hub_download(
            repo_id=repo_id,
            filename=filename,
            local_dir=MODELS_DIR,
            local_dir_use_symlinks=False
        )
        print(f"Download complete: {filename}")
    except Exception as e:
        print(f"Download failed: {e}")


def restart_with_model(model_filename):
    """Switch to a different model and restart the server."""
    global _active_model_path, _active_projector_path

    DEFAULT_MODEL_PATH = "/Users/ifichukudenwaesei/Library/Caches/llama.cpp/Qwen_Qwen3-VL-8B-Instruct-GGUF_Qwen3VL-8B-Instruct-Q4_K_M.gguf"
    DEFAULT_PROJECTOR_PATH = "/Users/ifichukudenwaesei/Library/Caches/llama.cpp/Qwen_Qwen3-VL-8B-Instruct-GGUF_mmproj-Qwen3VL-8B-Instruct-F16.gguf"

    if model_filename == "Default: Qwen3-VL-8B":
        new_model_path = DEFAULT_MODEL_PATH
        new_projector_path = DEFAULT_PROJECTOR_PATH
    else:
        new_model_path = os.path.join(MODELS_DIR, model_filename)
        new_projector_path = None
        if not os.path.exists(new_model_path):
            return False
        proj_guess = new_model_path.replace(".gguf", ".mmproj.gguf")
        if os.path.exists(proj_guess):
            new_projector_path = proj_guess
        elif "Qwen3" in model_filename and "VL" in model_filename.upper():
            if os.path.exists(DEFAULT_PROJECTOR_PATH):
                new_projector_path = DEFAULT_PROJECTOR_PATH

    _active_model_path = new_model_path
    _active_projector_path = new_projector_path

    save_model_config()
    start_server(force_restart=True)
    return True


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="omniai-server",
        description="OmniAI Local Model Server -- manages llama-server"
    )
    subparsers = parser.add_subparsers(dest="command")

    start_p = subparsers.add_parser("start", help="Start the server")
    start_p.add_argument("--force", action="store_true", help="Force restart")

    subparsers.add_parser("stop", help="Stop the server")
    subparsers.add_parser("status", help="Show server status")
    subparsers.add_parser("list", help="List available local models")

    load_p = subparsers.add_parser("load", help="Load a model (restarts server)")
    load_p.add_argument("model", help="Model filename or 'default'")

    config_p = subparsers.add_parser("config", help="View or set config")
    config_p.add_argument("key", nargs="?", help="Config key to get/set")
    config_p.add_argument("value", nargs="?", help="Value to set")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.command == "start":
        start_server(force_restart=args.force)
        # Keep running until interrupted
        atexit.register(stop_server)
        signal.signal(signal.SIGTERM, lambda n, f: stop_server())
        print("Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            stop_server()

    elif args.command == "stop":
        status = get_server_status()
        if status["running"]:
            kill_port(status["port"])
            print("Server stopped.")
        else:
            print("Server is not running.")

    elif args.command == "status":
        status = get_server_status()
        if status["running"]:
            print(f"Running | model: {status['model'] or 'unknown'} | port: {status['port']}")
        else:
            print(f"Stopped | port: {status['port']}")

    elif args.command == "list":
        models = get_local_models()
        for m in models:
            print(f"  {m}")

    elif args.command == "load":
        model = args.model
        if model.lower() == "default":
            model = "Default: Qwen3-VL-8B"
        success = restart_with_model(model)
        if not success:
            print(f"ERROR: Model '{model}' not found.")
            sys.exit(1)

    elif args.command == "config":
        config = load_config()
        if args.key and args.value:
            # Type coercion for known types
            val = args.value
            if args.key in ("context_length", "port", "batch_size", "ubatch_size", "threads"):
                val = int(val)
            elif args.key in ("flash_attn",):
                val = val.lower() in ("true", "1", "yes")
            config[args.key] = val
            save_config(config)
            print(f"Set {args.key} = {val}")
        elif args.key:
            print(f"{args.key} = {config.get(args.key, '(not set)')}")
        else:
            for k, v in config.items():
                print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
