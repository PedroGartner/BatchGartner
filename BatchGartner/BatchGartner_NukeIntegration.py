"""Nuke-side companion for Batch Gartner v1.5.

Sends Write nodes from the open script to the standalone Batch Gartner app over
localhost. Launching the app and waiting for it happens in a background thread,
so the Nuke interface never freezes.
"""

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import nuke

HOST = "127.0.0.1"
DEFAULT_PORT = 54321
NUKE_DIR = Path.home() / ".nuke"
CONFIG_PATH = NUKE_DIR / "batch_gartner_path.txt"  # location of BatchGartner.py
PYTHON_CACHE = NUKE_DIR / "batch_gartner_python.txt"  # external Python that has PySide6
APP_SETTINGS = Path.home() / ".batch_gartner" / "settings.json"  # Batch Gartner's own settings
LEGACY_APP_SETTINGS = Path.home() / ".nuke_batch_render_config.json"  # settings file of earlier releases
WRITE_CLASSES = ("Write", "Write2", "DeepWrite")

_launch_lock = threading.Lock()


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def _port():
    """Use the receiver port set in Batch Gartner > Preferences > Advanced."""
    for settings_file in (APP_SETTINGS, LEGACY_APP_SETTINGS):
        try:
            settings = json.loads(settings_file.read_text(encoding="utf-8"))
            return int(settings.get("receiver_port", DEFAULT_PORT))
        except Exception:
            continue
    return DEFAULT_PORT


def _main_thread_message(text):
    nuke.executeInMainThread(nuke.message, args=(text,))


def _script_path():
    path = nuke.root().name()
    if not path or path == "Root" or not path.lower().endswith(".nk"):
        nuke.message("Save the Nuke script before sending it to Batch Gartner.")
        return None
    try:
        modified = bool(nuke.root().modified())
    except Exception:
        modified = False
    if modified:
        if not nuke.ask(
            "The script has unsaved changes.\n\nSave it before sending to Batch Gartner?\n\n"
            "(Batch Gartner renders the saved .nk file.)"
        ):
            return None
        try:
            nuke.scriptSave()
        except Exception as exc:
            nuke.message("Could not save the script:\n%s" % exc)
            return None
    return nuke.root().name()


def _is_disabled(node):
    try:
        return bool(node["disable"].value())
    except Exception:
        return False


def _write_nodes(selected_only=True):
    if selected_only:
        nodes = nuke.selectedNodes()
    else:
        nodes = nuke.allNodes(recurseGroups=False)
    writes = [n for n in nodes if n.Class() in WRITE_CLASSES]
    disabled = [n for n in writes if _is_disabled(n)]
    writes = [n for n in writes if not _is_disabled(n)]
    if not writes:
        if disabled:
            nuke.message("The Write node(s) are disabled:\n\n" + "\n".join(n.fullName() for n in disabled))
        elif selected_only:
            nuke.message("Select at least one Write node first.")
        else:
            nuke.message("No Write nodes were found in this script.")
    return writes


def _output_for(node):
    try:
        return nuke.filename(node) or node["file"].value()
    except Exception:
        try:
            return node["file"].value()
        except Exception:
            return ""


def _nuke_executable():
    exe = os.path.abspath(os.sys.executable)
    return exe if "nuke" in Path(exe).name.lower() else ""


def _payload(nodes, current_frame_only=False):
    script = _script_path()
    if not script:
        return None
    if current_frame_only:
        first = last = int(nuke.frame())
    else:
        first = int(nuke.root()["first_frame"].value())
        last = int(nuke.root()["last_frame"].value())
    jobs = []
    for node in nodes:
        jobs.append(
            {
                "script_path": script,
                "write_node": node.fullName(),
                "output_path": _output_for(node),
                "first": first,
                "last": last,
                "nuke_exe": _nuke_executable(),
            }
        )
    return {"version": 1, "source": "Nuke", "jobs": jobs}


def _send_raw(payload, timeout=1.0):
    data = json.dumps(payload).encode("utf-8")
    with socket.create_connection((HOST, _port()), timeout=timeout) as sock:
        sock.sendall(data)
        sock.shutdown(socket.SHUT_WR)
        reply = sock.recv(1024).decode("utf-8", "replace")
    return reply.startswith("OK")


def _try_send(payload):
    try:
        return _send_raw(payload)
    except Exception:
        return False


# ----------------------------------------------------------------------------
# Locating and launching the standalone app
# ----------------------------------------------------------------------------
def _configured_app_path():
    env = os.environ.get("BATCH_GARTNER", "").strip()
    if env and Path(env).exists():
        return env
    try:
        p = CONFIG_PATH.read_text(encoding="utf-8").strip()
        if p and Path(p).exists():
            return p
    except Exception:
        pass
    return ""


def _choose_app_path():
    path = nuke.getFilename("Locate BatchGartner.py", "*.py")
    if path and Path(path).exists():
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(path, encoding="utf-8")
        except Exception:
            pass
        return path
    return ""


def _python_candidates():
    """External Python interpreters, never Nuke's embedded Python."""
    candidates = []

    def add(value):
        if not value:
            return
        value = os.path.abspath(os.path.expandvars(os.path.expanduser(str(value))))
        if os.path.isfile(value) and value not in candidates and "nuke" not in Path(value).name.lower():
            candidates.append(value)

    add(os.environ.get("BATCH_GARTNER_PYTHON", "").strip())
    try:
        add(PYTHON_CACHE.read_text(encoding="utf-8").strip())
    except Exception:
        pass
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            base = Path(local) / "Programs" / "Python"
            if base.exists():
                for exe in sorted(base.glob("Python*/python.exe"), reverse=True):
                    add(exe)
        for root in (os.environ.get("ProgramFiles", r"C:\Program Files"),):
            for exe in sorted(Path(root).glob("Python*/python.exe"), reverse=True):
                add(exe)
        names = ("python.exe", "python3.exe")
    else:
        for exe in ("/usr/local/bin/python3", "/opt/homebrew/bin/python3", "/usr/bin/python3"):
            add(exe)
        names = ("python3", "python")
    for name in names:
        exe = shutil.which(name)
        if exe and "windowsapps" not in exe.lower():  # skip the Microsoft Store stub
            add(exe)
    return candidates


def _clean_env():
    """Environment for the external Python without Nuke's own Python / Qt settings."""
    env = dict(os.environ)
    for key in (
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "QT_PLUGIN_PATH",
        "QT_QPA_PLATFORM_PLUGIN_PATH",
        "QML2_IMPORT_PATH",
        "PYSIDE_DESIGNER_PLUGINS",
    ):
        env.pop(key, None)
    nuke_dir = os.path.dirname(os.path.abspath(os.sys.executable)).lower()
    parts = [
        p
        for p in env.get("PATH", "").split(os.pathsep)
        if p and not os.path.abspath(p).lower().startswith(nuke_dir)
    ]
    env["PATH"] = os.pathsep.join(parts)
    return env


def _python_has_pyside6(python_exe, env):
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        result = subprocess.run(
            [python_exe, "-c", "import PySide6.QtWidgets"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            creationflags=flags,
            env=env,
        )
        return result.returncode == 0
    except Exception:
        return False


def _launch_app(app, env):
    """Runs in a background thread. Returns an error string, or '' on success."""
    candidates = _python_candidates()
    python_exe = next((exe for exe in candidates if _python_has_pyside6(exe, env)), None)
    if not python_exe:
        return (
            "Batch Gartner could not find an external Python installation with PySide6.\n\n"
            "Install it with:  python -m pip install PySide6 psutil\n"
            "or set the BATCH_GARTNER_PYTHON environment variable to that python executable."
        )
    try:
        PYTHON_CACHE.write_text(python_exe, encoding="utf-8")
    except Exception:
        pass
    kwargs = {"cwd": str(Path(app).parent), "env": env}
    if os.name == "nt":
        # CREATE_NO_WINDOW (not DETACHED_PROCESS): the app gets a hidden console that its own
        # helper processes share, so no cmd windows flash on screen.
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "CREATE_NO_WINDOW", 0x08000000
        )
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen([python_exe, app], **kwargs)
    except Exception as exc:
        return "Could not launch Batch Gartner with %s:\n%s" % (python_exe, exc)
    return ""


def _launch_and_deliver(app, payload, wait_seconds=45):
    """Background thread: start the app, wait for its listener, then deliver the payload."""
    if not _launch_lock.acquire(blocking=False):
        # Another launch is already waiting; just keep retrying our own payload.
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            if _try_send(payload):
                return
            time.sleep(0.5)
        _main_thread_message("Batch Gartner did not respond. Your jobs were not sent.")
        return
    try:
        error = _launch_app(app, _clean_env())
        if error:
            _main_thread_message(error)
            return
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            time.sleep(0.5)
            if _try_send(payload):
                return
        _main_thread_message(
            "Batch Gartner was launched but did not become ready within %d seconds, so the jobs were not sent.\n\n"
            "Run BatchGartner.py once from a terminal to see any startup error, and check that local port %d is free."
            % (wait_seconds, _port())
        )
    finally:
        _launch_lock.release()


def _send(payload):
    if not payload or not payload.get("jobs"):
        return
    if _try_send(payload):
        return
    app = _configured_app_path() or _choose_app_path()
    if not app:
        return
    threading.Thread(target=_launch_and_deliver, args=(app, payload), daemon=True).start()


# ----------------------------------------------------------------------------
# Batch Gartner -> Nuke: "Create Read in Nuke"
# A small localhost listener inside the Nuke GUI (started from menu.py, so it never
# runs inside render processes). Each open Nuke takes the first free port of
# (receiver port + 1 ... + 10). Batch Gartner asks every listener; the Nuke that has
# the job's script open creates the Read. If none has it open, the first Nuke does.
# ----------------------------------------------------------------------------
_read_listener = None


def _norm(path):
    try:
        return os.path.normcase(os.path.normpath(os.path.abspath(path or "")))
    except Exception:
        return path or ""


def _create_read(msg):
    same_script = _norm(nuke.root().name()) == _norm(msg.get("script", ""))
    if not msg.get("force") and not same_script:
        return "SKIP"
    file_path = msg.get("file") or ""
    write = (
        nuke.toNode(msg.get("write") or "")
        if same_script and msg.get("write") not in (None, "", "(all)")
        else None
    )
    if write is not None and not msg.get("override"):
        try:
            file_path = nuke.filename(write) or file_path  # evaluated path from the live Write
        except Exception:
            pass
    if not file_path:
        return "ERROR: no output path"
    first, last = msg.get("first"), msg.get("last")
    nuke.Undo.begin("Batch Gartner: Create Read")
    try:
        for n in nuke.selectedNodes():
            n.setSelected(False)
        read = nuke.nodes.Read()
        if first is not None and last is not None:
            read["file"].fromUserText("%s %d-%d" % (file_path, int(first), int(last)))
        else:
            read["file"].fromUserText(file_path)
        try:
            read["label"].setValue("Batch Gartner render")
        except Exception:
            pass
        if write is not None:
            read.setXYpos(write.xpos() + 150, write.ypos())
        read.setSelected(True)
    finally:
        nuke.Undo.end()
    return "OK %s" % read.name()


def _serve_reads(server):
    while True:
        try:
            conn, _ = server.accept()
        except Exception:
            time.sleep(0.2)
            continue
        try:
            conn.settimeout(2.0)
            chunks = []
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                chunks.append(data)
                if sum(len(c) for c in chunks) > 1_000_000:
                    raise ValueError("message too large")
            msg = json.loads(b"".join(chunks).decode("utf-8"))
            if msg.get("command") == "ping":
                reply = "OK " + _norm(nuke.root().name())
            elif msg.get("command") == "create_read":
                reply = nuke.executeInMainThreadWithResult(_create_read, (msg,))
            else:
                reply = "ERROR: unknown command"
        except Exception as exc:
            reply = "ERROR: %s" % exc
        try:
            conn.sendall(str(reply).encode("utf-8", "replace"))
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass


def start_read_listener():
    global _read_listener
    if _read_listener is not None:
        return
    base = _port() + 1
    for port in range(base, base + 10):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            server.bind((HOST, port))
            server.listen(4)
        except OSError:
            server.close()
            continue
        _read_listener = server
        threading.Thread(target=_serve_reads, args=(server,), daemon=True).start()
        return


# ----------------------------------------------------------------------------
# Menu commands
# ----------------------------------------------------------------------------
def send_selected_writes():
    nodes = _write_nodes(True)
    if nodes:
        _send(_payload(nodes, False))


def send_all_writes():
    nodes = _write_nodes(False)
    if nodes:
        _send(_payload(nodes, False))


def send_selected_current_frame():
    nodes = _write_nodes(True)
    if nodes:
        _send(_payload(nodes, True))


def open_batch_renderer():
    _send({"version": 1, "source": "Nuke", "command": "show", "jobs": [{}]})


def install_menu():
    menu = nuke.menu("Nuke").addMenu("Batch Gartner")
    menu.addCommand("Send Selected Write(s)", send_selected_writes, "Ctrl+Alt+B")
    menu.addCommand("Send All Writes", send_all_writes)
    menu.addCommand("Send Selected Write(s) - Current Frame", send_selected_current_frame)
    menu.addSeparator()
    menu.addCommand("Open Batch Gartner", open_batch_renderer)
    try:
        start_read_listener()
    except Exception as exc:
        print("Batch Gartner: Create Read listener not started:", exc)
