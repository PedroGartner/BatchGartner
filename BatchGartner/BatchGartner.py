#!/usr/bin/env python3
"""Batch Gartner v1.5.1 — local Nuke render queue manager."""

import datetime
import glob
import json
import os
import queue as pyqueue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ImportError:
    raise SystemExit("PySide6 is required. Install it with: python -m pip install PySide6")

try:
    import psutil
except ImportError:
    psutil = None

APP_VERSION = "1.5.1"

# Everything the app stores lives in one folder in the user's home directory.
DATA_DIR = Path.home() / ".batch_gartner"
SETTINGS_FILE = DATA_DIR / "settings.json"
RECOVERY_FILE = DATA_DIR / "recovery.json"
CLEAN_EXIT_FILE = DATA_DIR / "clean_exit"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
LOG_DIR = DATA_DIR / "logs"
# Settings file used by earlier releases; read once if no settings exist yet.
LEGACY_SETTINGS_FILE = Path.home() / ".nuke_batch_render_config.json"

# Where Nuke is usually installed, per platform.
NUKE_SEARCH_PATTERNS = {
    "windows": [r"C:\Program Files\Nuke*\Nuke*.exe"],
    "mac": ["/Applications/Nuke*/Nuke*.app/Contents/MacOS/Nuke*"],
    "linux": ["/usr/local/Nuke*/Nuke*", "/opt/Nuke*/Nuke*", "/opt/Foundry/Nuke*/Nuke*"],
}
NUKE_EXECUTABLE_NAME = re.compile(r"(?i)^nuke\d+(\.\d+)?(v\d+)?(\.exe)?$")


def find_nuke_installs():
    """Installed Nuke executables, newest version first. Crash reporters and uninstallers are ignored."""
    if sys.platform.startswith("win"):
        platform_key = "windows"
    elif sys.platform == "darwin":
        platform_key = "mac"
    else:
        platform_key = "linux"
    found = []
    for pattern in NUKE_SEARCH_PATTERNS[platform_key]:
        for candidate in glob.glob(pattern):
            path = Path(candidate)
            if NUKE_EXECUTABLE_NAME.match(path.name) and path.is_file() and candidate not in found:
                found.append(candidate)
    return sorted(found, key=lambda p: [int(n) for n in re.findall(r"\d+", p)], reverse=True)


# Helper run by Nuke (nuke -t) for snapshot renders and output overrides.
# argv: <script to open> <original script path or ""> <write node or "(all)"> <output override or ""> <frames or "">
# It opens the snapshot, then sets root.name back to the ORIGINAL script path, so
# [value root.name] expressions, relative paths and project settings behave exactly as in the GUI.
OVERRIDE_WRAPPER = r'''
import sys
import time
import nuke

WRITE_CLASSES = ("Write", "Write2", "DeepWrite")


def target_writes(name):
    if name and name not in ("(all)", "all"):
        node = nuke.toNode(name)
        if node is None:
            raise RuntimeError("Write node not found: %s" % name)
        return [node]
    nodes = [n for n in nuke.allNodes(recurseGroups=True)
             if n.Class() in WRITE_CLASSES and not (n.knob("disable") and n["disable"].value())]
    if not nodes:
        raise RuntimeError("No enabled Write nodes in the script")
    def order(n):
        k = n.knob("render_order")
        return k.value() if k else 1
    return sorted(nodes, key=order)


FRAME_ATTEMPTS = 4          # a frame is tried up to 4 times before the job fails


def render_frame(active, frame):
    """Render one frame, retrying if it fails. A locked output file (cloud sync such as
    MEGA / Dropbox / OneDrive, or a viewer holding the file) usually frees up in seconds."""
    for attempt in range(1, FRAME_ATTEMPTS + 1):
        try:
            if len(active) == 1:
                nuke.execute(active[0], frame, frame)
            else:
                nuke.executeMultiple(active, ((frame, frame, 1),))
            return
        except RuntimeError as exc:
            if attempt == FRAME_ATTEMPTS:
                raise
            wait = 3 * attempt
            print("WARNING: render of %d did not succeed (%s). Retrying in %d s (try %d of %d)."
                  % (frame, exc, wait, attempt + 1, FRAME_ATTEMPTS))
            sys.stdout.flush()
            time.sleep(wait)


def in_limit(node, frame):
    k = node.knob("use_limit")
    if k and k.value():
        return int(node["first"].value()) <= frame <= int(node["last"].value())
    return True


def main():
    args = sys.argv[1:] + [""] * 5
    script, original, write_name, output, spec = args[:5]
    nuke.scriptOpen(script)
    if original:
        nuke.root()["name"].setValue(original)
    writes = target_writes(write_name)
    if output:
        if len(writes) != 1:
            raise RuntimeError("An output override needs a single Write node")
        writes[0]["file"].setValue(output)
    spec = spec.strip()
    if spec:
        frames = list(nuke.FrameRanges(spec).toFrameList())
    else:
        root = nuke.root()
        frames = list(range(int(root["first_frame"].value()), int(root["last_frame"].value()) + 1))
    total = len(frames)
    for n, frame in enumerate(frames, 1):
        active = [w for w in writes if in_limit(w, frame)]
        if active:
            render_frame(active, frame)
        print("Frame %d (%d of %d)" % (frame, n, total))
        sys.stdout.flush()
    return 0


try:
    code = main()
except Exception as exc:
    import traceback
    traceback.print_exc()
    print("ERROR: %s" % exc)
    code = 1
sys.stdout.flush()
sys.exit(code)
'''


def _hidden():
    """Popen kwargs that stop Windows from opening a console window for a helper process."""
    if sys.platform.startswith("win"):
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000), "startupinfo": si}
    return {}


EXR_HELP = (
    "EXR preview needs one extra library for the Python that runs Batch Gartner:\n"
    "python -m pip install OpenEXR numpy"
)


def _exr_to_rgb_array(path, data):
    """Decode an EXR into a float32 array (h, w, 3). Tries OpenEXR (new and old API), OpenCV, OpenImageIO."""
    import numpy as np

    errors = []

    def to_rgb(a):
        a = np.asarray(a, dtype=np.float32)
        if a.ndim == 2:
            a = a[:, :, None]
        if a.shape[2] == 1:
            a = np.repeat(a, 3, axis=2)
        return a[:, :, :3]

    try:
        import OpenEXR

        if hasattr(OpenEXR, "File"):  # OpenEXR 3.3+
            with OpenEXR.File(path) as f:
                ch = f.channels()
                for key in ("RGBA", "RGB"):
                    if key in ch:
                        return to_rgb(ch[key].pixels)
                rgb = [k for c in "RGB" for k in ch if k == c or k.endswith("." + c)]
                if len(rgb) >= 3:
                    return np.stack([np.asarray(ch[k].pixels, dtype=np.float32) for k in rgb[:3]], -1)
                return to_rgb(next(iter(ch.values())).pixels)
        import Imath  # OpenEXR 1.x

        f = OpenEXR.InputFile(path)
        hdr = f.header()
        dw = hdr["dataWindow"]
        w, h = dw.max.x - dw.min.x + 1, dw.max.y - dw.min.y + 1
        names = [c for c in "RGB" if c in hdr["channels"]] or [sorted(hdr["channels"])[0]]
        pt = Imath.PixelType(Imath.PixelType.FLOAT)
        a = np.stack([np.frombuffer(f.channel(c, pt), dtype=np.float32).reshape(h, w) for c in names], -1)
        f.close()
        return to_rgb(a)
    except ImportError:
        pass
    except Exception as e:
        errors.append(f"OpenEXR: {e}")
    try:
        os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")
        import cv2

        a = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
        if a is not None:
            a = np.asarray(a, dtype=np.float32)
            if a.ndim == 3 and a.shape[2] >= 3:
                a = a[:, :, [2, 1, 0]]  # BGR -> RGB
            return to_rgb(a)
        errors.append("OpenCV could not decode the file")
    except ImportError:
        pass
    except Exception as e:
        errors.append(f"OpenCV: {e}")
    try:
        import OpenImageIO as oiio

        buf = oiio.ImageBuf(path)
        return to_rgb(buf.get_pixels(oiio.FLOAT))
    except ImportError:
        pass
    except Exception as e:
        errors.append(f"OpenImageIO: {e}")
    raise RuntimeError("; ".join(errors) if errors else EXR_HELP)


def _exr_preview_image(path, data, max_w, max_h):
    """EXR -> display QImage: downscaled, linear -> sRGB, clipped to 0-1."""
    import numpy as np

    a = _exr_to_rgb_array(path, data)
    h, w = a.shape[:2]
    step = max(1, int(np.ceil(max(w / max(1, max_w), h / max(1, max_h)))))
    a = np.nan_to_num(a[::step, ::step], nan=0.0, posinf=1.0, neginf=0.0)
    a = np.clip(a, 0.0, 1.0)
    a = np.where(a <= 0.0031308, a * 12.92, 1.055 * np.power(a, 1 / 2.4) - 0.055)
    rgb = np.ascontiguousarray((a * 255.0 + 0.5).astype(np.uint8))
    hh, ww = rgb.shape[:2]
    return QtGui.QImage(rgb.tobytes(), ww, hh, 3 * ww, QtGui.QImage.Format_RGB888).copy()


def _read_text(path):
    """Read a .nk / text file as UTF-8 (what Nuke writes), falling back to the system encoding."""
    data = Path(path).read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(sys.getdefaultencoding(), errors="replace")


def read_settings():
    """Load the settings. Falls back to an earlier release's file, then to defaults."""
    for source in (SETTINGS_FILE, LEGACY_SETTINGS_FILE):
        if not source.exists():
            continue
        try:
            return json.loads(source.read_text(encoding="utf-8"))
        except Exception:
            # Keep the unreadable file for inspection instead of silently overwriting it.
            try:
                shutil.copy2(source, source.with_suffix(".corrupt.json"))
            except Exception:
                pass
    installs = find_nuke_installs()
    return {"queue": [], "nuke_exe": installs[0] if installs else ""}


def write_settings(settings):
    """Atomic write: a crash mid-save can no longer corrupt the queue."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        os.replace(tmp, SETTINGS_FILE)
    except Exception:
        pass


def _nk_knob(block, knob):
    """Return a knob value from a node block, handling "quoted strings", {braced expressions} and bare words."""
    m = re.search(r"^[ \t]*" + re.escape(knob) + r"[ \t]+(.*)$", block, re.MULTILINE)
    if not m:
        return None
    rest = block[m.start(1) :]
    if rest.startswith('"'):
        out = []
        i = 1
        while i < len(rest):
            ch = rest[i]
            if ch == "\\" and i + 1 < len(rest):
                out.append(rest[i + 1])
                i += 2
                continue
            if ch == '"':
                break
            out.append(ch)
            i += 1
        return "".join(out)
    if rest.startswith("{"):
        depth = 0
        for i, ch in enumerate(rest):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return rest[1:i]
        return rest[1:]
    return m.group(1).split()[0] if m.group(1).split() else ""


WRITE_CLASSES = ("Write", "Write2", "DeepWrite")


def _nk_node_blocks(text):
    """Yield (node class, body text) for each node block: a 'Class {' line up to its closing '}' line."""
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        header = lines[index].rstrip()
        if header.endswith("{") and header[:1].isalpha():
            node_class = header[:-1].strip()
            body = []
            index += 1
            while index < len(lines) and not lines[index].startswith("}"):
                body.append(lines[index])
                index += 1
            yield node_class, "\n".join(body)
        index += 1


def _as_int(value, default):
    """Knob value as an int; a missing knob means Nuke's default, an unreadable one means unknown."""
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return None


def read_nk_script(path):
    """Frame range and Write nodes of a .nk file, read as text (Nuke is not needed)."""
    try:
        text = _read_text(path)
    except Exception:
        return None, None, []
    first = last = None
    writes = []
    for node_class, body in _nk_node_blocks(text):
        if node_class == "Root" and first is None and last is None:
            # Nuke does not save knobs left at their defaults (first frame 1, last frame 100).
            first = _as_int(_nk_knob(body, "first_frame"), 1)
            last = _as_int(_nk_knob(body, "last_frame"), 100)
        elif node_class in WRITE_CLASSES:
            writes.append(
                {
                    "name": _nk_knob(body, "name") or "(Write without name)",
                    "file": _nk_knob(body, "file") or "(no file path)",
                    "disabled": (_nk_knob(body, "disable") or "").lower() in ("true", "1"),
                }
            )
    return first, last, writes


FRAME_RANGE = re.compile(r"(-?\d+)\s*(?:-|:|to)\s*(-?\d+)", re.IGNORECASE)


def parse_frame_range(text):
    """'1001-1100', '1001:1100' or '1001 to 1100' -> (1001, 1100); anything else -> None."""
    match = FRAME_RANGE.fullmatch((text or "").strip())
    return tuple(int(v) for v in match.groups()) if match else None


SESSION_ID = uuid.uuid4().hex


class RenderJob:
    def __init__(
        self,
        path,
        first=None,
        last=None,
        write_node="(all)",
        write_nodes=None,
        enabled=True,
        output_override="",
        notes="",
        nuke_exe="",
        chunk_size=0,
        retry_limit=0,
        job_priority=50,
        suspended=False,
        dependency_path="",
        require_assets=False,
        frame_order="First → Last",
        task_timeout_min=0,
        render_power="Normal",
        ram_reserve_gb=2,
        cpu_threshold=90,
        smart_protection=True,
        schedule_at="",
    ):
        self.path, self.first, self.last = path, first, last
        self.write_node = write_node
        self.write_nodes = write_nodes or []
        self.enabled = enabled
        self.output_override = output_override
        self.notes = notes
        self.nuke_exe = nuke_exe
        self.chunk_size = int(chunk_size or 0)
        self.retry_limit = int(retry_limit or 0)
        self.job_priority = max(
            0, min(100, int(50 if job_priority in (None, "") else job_priority))
        )  # 0 is a valid priority
        self.suspended = bool(suspended)
        self.dependency_path = dependency_path or ""
        self.require_assets = bool(require_assets)
        self.frame_order = frame_order or "First → Last"
        self.task_timeout_min = max(0, int(task_timeout_min or 0))
        self.render_power = (
            render_power if render_power in ("Low", "Normal", "High", "Maximum", "Custom") else "Normal"
        )
        self.ram_reserve_gb = max(0.0, float(ram_reserve_gb or 0))
        self.cpu_threshold = max(10, min(100, int(cpu_threshold or 90)))
        self.smart_protection = bool(smart_protection)
        self.schedule_at = schedule_at or ""
        self.attempt_history = []
        self.zero_byte_frames = []
        self.peak_ram_gb = 0.0
        self.avg_cpu_samples = []
        self.heavy_frames = []
        self.task_states = {}
        self.last_error_type = ""
        self.status, self.progress, self.error = "Queued", 0.0, ""
        self.current_frame = None
        self.render_started_at = None
        self.last_frame_at = None
        self.frame_durations = []
        self.missing_frames = []
        self.verified = None
        self.completed_at = None
        self.session_id = SESSION_ID
        self.saved_status = "Queued"
        self.locked = False
        self.error = ""
        self.id = uuid.uuid4().hex
        self.snapshot_path = ""  # copy of the .nk taken when the job was queued
        self.snapshot_at = 0.0  # time.time() of that copy

    @property
    def name(self):
        return Path(self.path).name

    @property
    def frames_label(self):
        if self.first is None or self.last is None:
            return "(script range)"
        return f"{self.first}-{self.last}"

    @property
    def renders_all_writes(self):
        return self.write_node in ("(all)", "all", "", None)

    @property
    def source_output_path(self):
        """Output path of the Write node this job renders (from the script)."""
        paths = {w.get("name"): w.get("file", "") for w in self.write_nodes}
        if not self.renders_all_writes:
            return paths.get(self.write_node, "")
        if len(paths) == 1:
            return next(iter(paths.values()))
        return f"(multiple Writes: {len(paths)})" if paths else ""

    @property
    def output_path(self):
        return self.output_override or self.source_output_path

    def to_dict(self):
        return {
            "path": self.path,
            "first": self.first,
            "last": self.last,
            "write_node": self.write_node,
            "write_nodes": self.write_nodes,
            "enabled": self.enabled,
            "output_override": self.output_override,
            "notes": self.notes,
            "nuke_exe": self.nuke_exe,
            "chunk_size": self.chunk_size,
            "retry_limit": self.retry_limit,
            "job_priority": self.job_priority,
            "suspended": self.suspended,
            "dependency_path": self.dependency_path,
            "require_assets": self.require_assets,
            "frame_order": self.frame_order,
            "task_timeout_min": self.task_timeout_min,
            "render_power": self.render_power,
            "ram_reserve_gb": self.ram_reserve_gb,
            "cpu_threshold": self.cpu_threshold,
            "smart_protection": self.smart_protection,
            "schedule_at": self.schedule_at,
            "attempt_history": self.attempt_history,
            "zero_byte_frames": self.zero_byte_frames,
            "status": self.status,
            "progress": self.progress,
            "error": self.error,
            "current_frame": self.current_frame,
            "completed_at": self.completed_at,
            "session_id": self.session_id,
            "locked": self.locked,
            "task_states": self.task_states,
            "heavy_frames": self.heavy_frames,
            "peak_ram_gb": self.peak_ram_gb,
            "id": self.id,
            "missing_frames": self.missing_frames,
            "verified": self.verified,
            "last_error_type": self.last_error_type,
            "snapshot_path": self.snapshot_path,
            "snapshot_at": self.snapshot_at,
        }

    @staticmethod
    def from_dict(d):
        j = RenderJob(
            d["path"],
            d.get("first"),
            d.get("last"),
            d.get("write_node", "(all)"),
            d.get("write_nodes", d.get("write_options", [])),
            d.get("enabled", True),
            d.get("output_override", ""),
            d.get("notes", ""),
            d.get("nuke_exe", ""),
            d.get("chunk_size", 0),
            d.get("retry_limit", 0),
            d.get("job_priority", 50),
            d.get("suspended", False),
            d.get("dependency_path", ""),
            d.get("require_assets", False),
            d.get("frame_order", "First → Last"),
            d.get("task_timeout_min", 0),
            d.get("render_power", "Normal"),
            d.get("ram_reserve_gb", 6),
            d.get("cpu_threshold", 90),
            d.get("smart_protection", True),
            d.get("schedule_at", ""),
        )
        j.status = d.get("state", "Queued")
        if j.status == "Rendering":
            j.status = "Stopped"  # app closed while this job was active
        j.progress = float(d.get("progress", 0.0) or 0.0)
        j.error = d.get("error", "") or ""
        j.current_frame = d.get("current_frame")
        j.completed_at = d.get("completed_at")
        j.session_id = d.get("session_id", "previous") or "previous"
        j.locked = bool(d.get("locked", False))
        j.task_states = {
            k: ("QUEUED" if v == "RENDERING" else v) for k, v in dict(d.get("task_states", {}) or {}).items()
        }
        j.id = d.get("id") or j.id
        j.missing_frames = list(d.get("missing_frames", []) or [])
        j.verified = d.get("verified")
        j.last_error_type = d.get("last_error_type", "") or ""
        j.snapshot_path = d.get("snapshot_path", "") or ""
        j.snapshot_at = float(d.get("snapshot_at", 0.0) or 0.0)
        if j.status in ("Waiting", "Waiting for Assets", "Waiting (resources)"):
            j.status = "Queued"
        j.heavy_frames = list(d.get("heavy_frames", []) or [])
        j.peak_ram_gb = float(d.get("peak_ram_gb", 0.0) or 0.0)
        j.attempt_history = list(d.get("attempt_history", []) or [])
        j.zero_byte_frames = list(d.get("zero_byte_frames", []) or [])
        return j


class JobEditorDialog(QtWidgets.QDialog):
    def __init__(self, job, parent=None):
        super().__init__(parent)
        self.job = job
        self.setWindowTitle(f"{job.name} — frames and Write")
        self.setMinimumWidth(480)
        form = QtWidgets.QFormLayout(self)
        form.setContentsMargins(12, 12, 12, 12)
        form.setSpacing(8)
        self.range_edit = QtWidgets.QLineEdit(job.frames_label if job.first is not None else "")
        self.write_combo = QtWidgets.QComboBox()
        self.write_combo.addItems(["(all)"] + [w["name"] for w in job.write_nodes])
        ix = self.write_combo.findText(job.write_node)
        self.write_combo.setCurrentIndex(max(0, ix))
        self.output = QtWidgets.QLabel()
        self.output.setWordWrap(True)
        self.output.setObjectName("muted")
        form.addRow("Frame range", self.range_edit)
        form.addRow("Write node", self.write_combo)
        form.addRow("Output", self.output)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.validate_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.write_combo.currentTextChanged.connect(self.update_output)
        self.update_output()

    def update_output(self):
        sel = self.write_combo.currentText()
        if sel == "(all)":
            txt = (
                self.job.output_path
                if len(self.job.write_nodes) <= 1
                else f"{len(self.job.write_nodes)} Write nodes will render"
            )
        else:
            txt = next((w.get("file", "") for w in self.job.write_nodes if w["name"] == sel), "")
        self.output.setText(txt or "Script default")

    def validate_accept(self):
        text = self.range_edit.text().strip()
        if not text:
            self.accept()
            return
        frames = parse_frame_range(text)
        if frames is None:
            QtWidgets.QMessageBox.warning(self, "Frame range", "Enter a frame range such as 1001-1100.")
        elif frames[0] > frames[1]:
            QtWidgets.QMessageBox.warning(
                self, "Frame range", "The first frame has to come before the last frame."
            )
        else:
            self.accept()

    def values(self):
        frames = parse_frame_range(self.range_edit.text())
        first, last = frames if frames else (self.job.first, self.job.last)
        return first, last, self.write_combo.currentText()


# Status colours. Strong colours are used only for states that need attention.
STATUS_COLORS = {
    "Rendering": "#4f8a5b",
    "Failed": "#b04a45",
    "Stopped": "#b0703e",
    "Blocked": "#9a5f3a",
    "Waiting": "#9a8047",
    "Waiting for Assets": "#9a8047",
    "Waiting (resources)": "#9a8047",
    "Suspended": "#8c7946",
    "Tested": "#56708f",
    "Queued": "#6a6a6a",
    "Completed": "#4d6356",
    "Done": "#4d6356",
    "Disabled": "#444444",
}
# Former default colours: saved preferences that still match them use the current palette.
LEGACY_STATUS_COLORS = {
    "Rendering": "#477552",
    "Completed": "#527b8c",
    "Done": "#527b8c",
    "Queued": "#506f80",
    "Waiting": "#8b7747",
    "Suspended": "#8c7946",
    "Stopped": "#8d6543",
    "Blocked": "#806044",
    "Failed": "#87504d",
    "Disabled": "#555555",
}
ATTENTION_STATES = {"Rendering", "Failed", "Stopped", "Blocked", "Waiting (resources)"}
_TASK_STATE_NAMES = {
    "RENDERING": "Rendering",
    "COMPLETE": "Completed",
    "COMPLETED": "Completed",
    "QUEUED": "Queued",
    "FAILED": "Failed",
    "STOPPED": "Stopped",
    "WAITING": "Waiting",
    "PENDING": "Waiting",
    "SUSPENDED": "Suspended",
    "BLOCKED": "Blocked",
    "DONE": "Completed",
    "DISABLED": "Disabled",
}


def _state_name(raw):
    state = str(raw or "").split(" •", 1)[0].strip()
    return _TASK_STATE_NAMES.get(state, state)


def _draw_cell_background(delegate, painter, option, index):
    """Let Qt draw the normal cell background / selection, without text."""
    opt = QtWidgets.QStyleOptionViewItem(option)
    delegate.initStyleOption(opt, index)
    opt.text = ""
    style = opt.widget.style() if opt.widget else QtWidgets.QApplication.style()
    style.drawControl(QtWidgets.QStyle.CE_ItemViewItem, opt, painter, opt.widget)


class StatusDelegate(QtWidgets.QStyledItemDelegate):
    """Status as a coloured pill (Rendering / Failed / Stopped ...) or a small dot + grey text (Queued / Completed)."""

    def __init__(self, parent=None, colors=None):
        super().__init__(parent)
        self.colors = colors or {}

    def set_colors(self, colors):
        self.colors = colors or {}
        self.parent().viewport().update() if self.parent() else None

    STATE_COLORS = STATUS_COLORS

    def paint(self, painter, option, index):
        _draw_cell_background(self, painter, option, index)
        raw = str(index.data(QtCore.Qt.DisplayRole) or "")
        state = _state_name(raw)
        label = raw if raw.split(" •", 1)[0].strip() == state else state + raw[len(raw.split(" •", 1)[0]) :]
        color = QtGui.QColor(self.colors.get(state, self.STATE_COLORS.get(state, "#5a5a5a")))
        painter.save()
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        r = option.rect.adjusted(5, 0, -4, 0)
        font = QtGui.QFont(option.font)
        if state in ATTENTION_STATES:
            font.setBold(True)
            painter.setFont(font)
            fm = QtGui.QFontMetrics(font)
            w = min(r.width(), fm.horizontalAdvance(label) + 16)
            h = min(r.height() - 6, fm.height() + 4)
            pill = QtCore.QRectF(r.left(), r.center().y() - h / 2 + 0.5, w, h)
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(pill, h / 2, h / 2)
            painter.setPen(QtGui.QColor("#ffffff"))
            painter.drawText(
                pill.adjusted(8, 0, -6, 0),
                QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft,
                QtGui.QFontMetrics(font).elidedText(label, QtCore.Qt.ElideRight, int(w - 14)),
            )
        else:
            painter.setFont(font)
            d = 7
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(QtCore.QRectF(r.left() + 2, r.center().y() - d / 2 + 0.5, d, d))
            painter.setPen(
                QtGui.QColor("#8e8e8e" if state in ("Completed", "Queued", "Disabled") else "#c8c8c8")
            )
            painter.drawText(r.adjusted(d + 9, 0, 0, 0), QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft, label)
        painter.restore()


class ProgressDelegate(QtWidgets.QStyledItemDelegate):
    """Slim progress bar with the percentage on the right. Bright only while rendering."""

    def __init__(self, parent=None, colors=None):
        super().__init__(parent)
        self.colors = colors or {}

    def set_colors(self, colors):
        self.colors = colors or {}
        self.parent().viewport().update() if self.parent() else None

    STATE_COLORS = STATUS_COLORS

    def paint(self, painter, option, index):
        value = index.data(QtCore.Qt.UserRole)
        if value is None:
            return super().paint(painter, option, index)
        _draw_cell_background(self, painter, option, index)
        state = _state_name(index.data(QtCore.Qt.UserRole + 1) or "")
        color = QtGui.QColor(self.colors.get(state, self.STATE_COLORS.get(state, "#707070")))
        if state in ("Completed", "Queued"):
            color = QtGui.QColor("#5b6b62" if state == "Completed" else "#5a5a5a")
        pct = max(0.0, min(100.0, float(value)))
        r = option.rect.adjusted(6, 0, -6, 0)
        text_w = 38
        bar = QtCore.QRect(r.left(), r.center().y() - 3, max(10, r.width() - text_w - 6), 6)
        painter.save()
        painter.fillRect(bar, QtGui.QColor("#2a2a2a"))
        width = int(bar.width() * pct / 100.0)
        if width > 0:
            painter.fillRect(QtCore.QRect(bar.left(), bar.top(), width, bar.height()), color)
        painter.setPen(QtGui.QColor("#e6e6e6" if state in ATTENTION_STATES else "#8e8e8e"))
        painter.drawText(
            QtCore.QRect(r.right() - text_w, r.top(), text_w, r.height()),
            QtCore.Qt.AlignVCenter | QtCore.Qt.AlignRight,
            f"{int(pct)}%",
        )
        painter.restore()


class ScriptDelegate(QtWidgets.QStyledItemDelegate):
    """Script name followed by a dim line of details that tells identical jobs apart."""

    def paint(self, painter, option, index):
        _draw_cell_background(self, painter, option, index)
        name = str(index.data(QtCore.Qt.DisplayRole) or "")
        detail = str(index.data(QtCore.Qt.UserRole + 2) or "")
        fg = index.data(QtCore.Qt.ForegroundRole)
        name_color = (
            fg.color() if hasattr(fg, "color") else (QtGui.QColor(fg) if fg else QtGui.QColor("#d0d0d0"))
        )
        if option.state & QtWidgets.QStyle.State_Selected:
            name_color = QtGui.QColor("#ffffff")
        r = option.rect.adjusted(6, 0, -6, 0)
        fm = QtGui.QFontMetrics(option.font)
        painter.save()
        painter.setFont(option.font)
        name_w = min(fm.horizontalAdvance(name), r.width())
        painter.setPen(name_color)
        painter.drawText(
            QtCore.QRect(r.left(), r.top(), name_w, r.height()),
            QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft,
            fm.elidedText(name, QtCore.Qt.ElideMiddle, r.width()),
        )
        rest = r.width() - name_w - 12
        if detail and rest > 30:
            painter.setPen(QtGui.QColor("#6c6c6c"))
            painter.drawText(
                QtCore.QRect(r.left() + name_w + 12, r.top(), rest, r.height()),
                QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft,
                fm.elidedText(detail, QtCore.Qt.ElideRight, rest),
            )
        painter.restore()


class HistoryGraph(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.values = []
        self.setMinimumHeight(34)
        self.setMaximumHeight(42)

    def push(self, value):
        if value is None:
            return
        self.values = (self.values + [max(0.0, min(100.0, float(value)))])[-60:]
        self.update()

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, False)
        r = self.rect().adjusted(1, 1, -1, -1)
        p.fillRect(r, QtGui.QColor('#202020'))
        p.setPen(QtGui.QColor('#343434'))
        p.drawRect(r)
        if len(self.values) < 2:
            return
        pts = []
        w = max(1, r.width() - 2)
        h = max(1, r.height() - 2)
        n = max(59, len(self.values) - 1)
        for i, v in enumerate(self.values):
            x = r.left() + 1 + (w * i / n)
            y = r.bottom() - 1 - (h * v / 100.0)
            pts.append(QtCore.QPointF(x, y))
        p.setPen(QtGui.QPen(QtGui.QColor('#7d8f98'), 1))
        p.drawPolyline(QtGui.QPolygonF(pts))


class FrameStrip(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.job = None
        self.setMinimumHeight(38)
        self.setToolTip('Rendered progress and detected missing frames')

    def setJob(self, job):
        self.job = job
        self.update()

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        r = self.rect().adjusted(1, 13, -1, -9)
        p.fillRect(r, QtGui.QColor('#202020'))
        j = self.job
        if not j or j.first is None or j.last is None:
            return
        done = int(r.width() * max(0, min(100, j.progress)) / 100.0)
        if done:
            p.fillRect(QtCore.QRect(r.left(), r.top(), done, r.height()), QtGui.QColor('#65757d'))
        missing = set(getattr(j, 'missing_frames', []) or [])
        p.setPen(QtGui.QColor('#b5b5b5'))
        p.drawText(self.rect().adjusted(1, 0, -1, 0), QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft, str(j.first))
        p.drawText(self.rect().adjusted(1, 0, -1, 0), QtCore.Qt.AlignTop | QtCore.Qt.AlignRight, str(j.last))
        if missing:
            p.setPen(QtGui.QPen(QtGui.QColor('#b48766'), 1))
            for f in missing:
                pos = (f - j.first) / (j.last - j.first) if j.last != j.first else 0
                x = r.left() + int(max(0, min(1, pos)) * r.width())
                p.drawLine(x, r.top(), x, r.bottom())
        if j.current_frame is not None:
            pos = (j.current_frame - j.first) / (j.last - j.first) if j.last != j.first else 0
            x = r.left() + int(max(0, min(1, pos)) * r.width())
            p.setPen(QtGui.QPen(QtGui.QColor('#d0d0d0'), 1))
            p.drawLine(x, r.top() - 3, x, r.bottom() + 3)


class BatchRenderApp(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Batch Gartner")
        self.resize(1280, 760)
        self.setAcceptDrops(True)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.settings = read_settings()
        self._unclean_start = RECOVERY_FILE.exists() and not CLEAN_EXIT_FILE.exists()
        self.jobs = (
            [RenderJob.from_dict(d) for d in self._startup_queue()]
            if self.settings.get("restore_queue", True)
            else []
        )
        self.activity = []
        self.session_started_at = time.monotonic()
        try:
            CLEAN_EXIT_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        self.history = self.settings.get("history", [])
        self.pause_after_job = False
        self._migrate_resource_defaults()
        self._events = pyqueue.Queue()
        self._render_thread = None
        self._stop_event = threading.Event()
        self._queue_running = False
        self.current_process = None
        self.current_job = None
        self._create_widgets()
        self._apply_style()
        self._apply_interface_preferences()
        self._redraw_queue()
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._process_events)
        self.timer.start(120)
        self.monitor_timer = QtCore.QTimer(self)
        self.monitor_timer.timeout.connect(self._update_system_monitor)
        self.monitor_timer.start(max(250, int(self.settings.get("monitor_refresh_ms", 1000))))
        self.pause_after_job = False
        self.skip_current = False
        self.clipboard_settings = None
        self.queue_undo = []
        self.history = self.settings.get("history", [])
        self._gpu_query_running = False
        self._last_gpu = None
        self._update_system_monitor()
        self._ipc_stop = threading.Event()
        self._ipc_thread = threading.Thread(target=self._ipc_server_worker, daemon=True)
        self._ipc_thread.start()
        self._restore_ui_state()
        if hasattr(self, "inspector_pane"):
            if self.settings.get("show_inspector_startup", True):
                self.inspector_pane.show()
            else:
                self.inspector_pane.hide()
        QtCore.QTimer.singleShot(350, self._offer_recovery)
        QtCore.QTimer.singleShot(2000, self._cleanup_snapshots)
        QtCore.QTimer.singleShot(2000, lambda: self._write_override_wrapper(None))

    def _migrate_resource_defaults(self):
        """Move jobs from the former strict resource presets (6-10 GB kept free) to the current ones.
        Values that were entered by hand are kept."""
        if self.settings.get("resource_presets_updated"):
            return
        old = {"Low": (10, 70), "Normal": (6, 85), "High": (4, 95), "Maximum": (2, 100)}
        new = {"Low": (4, 70), "Normal": (2, 90), "High": (1, 97), "Maximum": (0, 100)}
        for j in self.jobs:
            if (j.ram_reserve_gb, j.cpu_threshold) == old.get(j.render_power) or (
                j.ram_reserve_gb == 6 and j.cpu_threshold == 90
            ):
                j.ram_reserve_gb, j.cpu_threshold = new.get(j.render_power, (2, 90))
        if self.settings.get("default_ram_reserve_gb", 6) == 6:
            self.settings["default_ram_reserve_gb"] = 2
        self.settings["resource_presets_updated"] = True

    def _startup_queue(self):
        queue = self.settings.get("queue", [])
        if self._unclean_start and RECOVERY_FILE.exists():
            try:
                snap = json.loads(RECOVERY_FILE.read_text(encoding="utf-8"))
                if isinstance(snap.get("queue"), list):
                    return snap["queue"]
            except Exception:
                pass
        return queue

    def _ipc_server_worker(self):
        """Receive jobs from the companion Nuke menu script over localhost only."""
        host, port = "127.0.0.1", int(self.settings.get("receiver_port", 54321))
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if sys.platform.startswith("win") and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            # Windows SO_REUSEADDR would let a second instance bind the same port.
            server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind((host, port))
            server.listen(8)
            server.settimeout(0.5)
            self._events.put(("console", f">> Nuke integration listening on {host}:{port}"))
        except OSError as exc:
            self._events.put(("console", f"!! Nuke integration unavailable on port {port}: {exc}"))
            try:
                server.close()
            except Exception:
                pass
            return
        try:
            while not self._ipc_stop.is_set():
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self._ipc_stop.is_set():
                        break
                    time.sleep(0.2)
                    continue
                try:
                    self._handle_ipc_connection(conn)
                except Exception as exc:
                    self._events.put(("console", f"!! Nuke integration: ignored a bad connection ({exc})"))
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass
        finally:
            try:
                server.close()
            except Exception:
                pass

    def _handle_ipc_connection(self, conn):
        conn.settimeout(2.0)
        chunks = []
        total = 0
        while True:
            data = conn.recv(65536)
            if not data:
                break
            total += len(data)
            if total > 2_000_000:
                conn.sendall(b"ERROR: message too large")
                return
            chunks.append(data)
        try:
            payload = json.loads(b"".join(chunks).decode("utf-8"))
        except Exception as exc:
            conn.sendall(("ERROR: " + str(exc)).encode("utf-8", "replace"))
            return
        if isinstance(payload, dict) and payload.get("command") == "show":
            self._events.put(("show_window",))
        else:
            self._events.put(("external_jobs", payload))
        conn.sendall(b"OK")

    def _add_external_jobs(self, payload):
        jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
        added = 0
        for data in jobs:
            path = str(data.get("script_path", "")).strip()
            if not path or not Path(path).exists():
                self._write_console(f">> Nuke integration skipped missing/unsaved script: {path or '(none)'}")
                continue
            first, last, writes = read_nk_script(path)
            first = data.get("first", first)
            last = data.get("last", last)
            node = str(data.get("write_node") or "(all)")
            # Prefer the live output value sent by Nuke while preserving parsed Write metadata.
            live_output = str(data.get("output_path") or "")
            found = False
            for w in writes:
                if w.get("name") == node:
                    found = True
                    if live_output:
                        w["file"] = live_output
                    break
            if node not in ("(all)", "all", "") and not found:
                writes.append({"name": node, "file": live_output})
            job = RenderJob(path, first, last, node, writes, nuke_exe=str(data.get("nuke_exe") or ""))
            job.render_power = self.settings.get("default_render_power", "Normal")
            job.notes = "Sent from Nuke"
            self.jobs.append(job)
            added += 1
        if added:
            self._ask_power_for_jobs(self.jobs[-added:])
        if added:
            self._redraw_queue()
            self._save_state()
            self.raise_()
            self.activateWindow()
            self.statusBar().showMessage(f"Received {added} job{'s' if added != 1 else ''} from Nuke", 5000)
            self._write_console(f">> Received {added} Write job{'s' if added != 1 else ''} from Nuke.")

    def _create_widgets(self):
        self._build_menus()
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        outer = QtWidgets.QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Nuke executable path (the bar is hidden; the version is picked in the toolbar).
        pathbar = QtWidgets.QFrame()
        pathbar.setObjectName("topStrip")
        pl = QtWidgets.QHBoxLayout(pathbar)
        pl.setContentsMargins(7, 4, 7, 4)
        pl.setSpacing(5)
        pl.addWidget(QtWidgets.QLabel("Nuke"))
        self.nuke_path = QtWidgets.QLineEdit(self.settings.get("nuke_exe", ""))
        pl.addWidget(self.nuke_path, 1)
        self.nuke_combo = QtWidgets.QComboBox()
        self.nuke_combo.setMinimumWidth(145)
        self.nuke_combo.addItem("Detected versions...")
        for exe in self._installed_nuke_versions():
            self.nuke_combo.addItem(Path(exe).stem, exe)
        self.nuke_combo.currentIndexChanged.connect(self._choose_detected_nuke)
        pl.addWidget(self.nuke_combo)
        browse = QtWidgets.QToolButton()
        browse.setText("...")
        browse.setToolTip("Browse for Nuke executable")
        browse.clicked.connect(self._choose_nuke_executable)
        pl.addWidget(browse)
        pl.addSpacing(8)
        pl.addWidget(QtWidgets.QLabel("Priority"))
        self.priority_combo = QtWidgets.QComboBox()
        self.priority_combo.addItems(["Low", "Normal", "High"])
        self.priority_combo.setCurrentText(self.settings.get("priority", "Normal"))
        self.priority_combo.setFixedWidth(78)
        pl.addWidget(self.priority_combo)
        outer.addWidget(pathbar)

        # Toolbar.
        toolbar = QtWidgets.QFrame()
        toolbar.setObjectName("toolStrip")
        tl = QtWidgets.QHBoxLayout(toolbar)
        tl.setContentsMargins(5, 3, 5, 3)
        tl.setSpacing(2)
        add_btn = QtWidgets.QPushButton("+ ADD")
        add_btn.setToolTip("Add Nuke scripts")
        add_btn.clicked.connect(self._add_script_files)
        tl.addWidget(add_btn)
        tl.addSpacing(6)
        self.start_button = QtWidgets.QPushButton("START")
        self.start_button.setObjectName("primaryButton")
        self.start_button.setToolTip("Start the render queue")
        self.start_button.clicked.connect(self._start_render)
        tl.addWidget(self.start_button)
        self.render_sel_btn = QtWidgets.QPushButton("RENDER SELECTED")
        self.render_sel_btn.setToolTip("Render only the selected job")
        self.render_sel_btn.clicked.connect(self._render_selected)
        tl.addWidget(self.render_sel_btn)
        self.pause_btn = QtWidgets.QPushButton("PAUSE")
        self.pause_btn.setObjectName("pauseButton")
        self.pause_btn.setCheckable(True)
        self.pause_btn.setToolTip("Pause after the current job (click again to cancel)")
        self.pause_btn.toggled.connect(self._on_pause_toggled)
        tl.addWidget(self.pause_btn)
        self.stop_button = QtWidgets.QPushButton("STOP")
        self.stop_button.clicked.connect(self._request_stop)
        self.stop_button.setEnabled(False)
        tl.addWidget(self.stop_button)
        tl.addStretch(1)
        power_title = QtWidgets.QLabel("Power")
        power_title.setObjectName("toolbarLabel")
        tl.addWidget(power_title)
        self.global_power_combo = QtWidgets.QComboBox()
        self.global_power_combo.addItems(["Low", "Normal", "High", "Maximum"])
        self.global_power_combo.setCurrentText(self.settings.get("default_render_power", "Normal"))
        self.global_power_combo.setObjectName("toolbarCombo")
        self.global_power_combo.setFixedWidth(105)
        self.global_power_combo.currentTextChanged.connect(self._set_default_power)
        tl.addWidget(self.global_power_combo)
        self.work_mode_toolbar = QtWidgets.QCheckBox("Render While I Work")
        self.work_mode_toolbar.setObjectName("toolbarCheck")
        self.work_mode_toolbar.setChecked(bool(self.settings.get("render_while_work", False)))
        self.work_mode_toolbar.toggled.connect(self._set_work_mode_toolbar)
        tl.addWidget(self.work_mode_toolbar)
        self._ui_sync_timer = QtCore.QTimer(self)
        self._ui_sync_timer.timeout.connect(self._sync_toolbar_state)
        self._ui_sync_timer.start(400)
        outer.addWidget(toolbar)

        # Main vertical splitter: work area above, console below.
        self.main_splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self.main_splitter.setChildrenCollapsible(False)
        outer.addWidget(self.main_splitter, 1)
        work = QtWidgets.QWidget()
        wl = QtWidgets.QVBoxLayout(work)
        wl.setContentsMargins(5, 5, 5, 2)
        wl.setSpacing(3)
        self.work_splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.work_splitter.setChildrenCollapsible(False)
        wl.addWidget(self.work_splitter, 1)
        self.work_splitter.splitterMoved.connect(
            lambda *_: QtCore.QTimer.singleShot(0, self._fill_queue_table_width)
        )

        # Queue pane.
        left = QtWidgets.QFrame()
        left.setObjectName("pane")
        ll = QtWidgets.QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(0)
        qhead = QtWidgets.QFrame()
        qhead.setObjectName("paneHeader")
        qhl = QtWidgets.QHBoxLayout(qhead)
        qhl.setContentsMargins(7, 3, 5, 3)
        qhl.setSpacing(5)
        qt = QtWidgets.QLabel("JOBS")
        qt.setObjectName("paneTitle")
        qhl.addWidget(qt)
        self.queue_header_summary = QtWidgets.QLabel()
        self.queue_header_summary.setObjectName("headerSummary")
        qhl.addWidget(self.queue_header_summary)
        qhl.addStretch(1)
        self.status_filter = QtWidgets.QComboBox()
        self.status_filter.addItems(["Active", "All", "Waiting", "Completed", "Stopped", "Failed"])
        self.status_filter.setCurrentText("All")
        self.status_filter.setFixedWidth(100)
        self.status_filter.currentTextChanged.connect(lambda _: self._apply_filter(self.search_edit.text()))
        qhl.addWidget(self.status_filter)
        self.search_edit = QtWidgets.QLineEdit()
        self.search_edit.setPlaceholderText("Filter queue...")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setFixedWidth(165)
        self.search_edit.textChanged.connect(self._apply_filter)
        qhl.addWidget(self.search_edit)
        ll.addWidget(qhead)
        self.active_render = QtWidgets.QFrame()
        self.active_render.setObjectName("activeRender")
        ar = QtWidgets.QHBoxLayout(self.active_render)
        ar.setContentsMargins(7, 4, 7, 4)
        ar.setSpacing(8)
        self.active_name = QtWidgets.QLabel("NOW RENDERING   —")
        self.active_name.setObjectName("activeName")
        ar.addWidget(self.active_name, 1)
        self.active_frame = QtWidgets.QLabel("Ready")
        self.active_frame.setObjectName("headerSummary")
        ar.addWidget(self.active_frame)
        self.active_progress = QtWidgets.QProgressBar()
        self.active_progress.setRange(0, 100)
        self.active_progress.setFixedWidth(150)
        self.active_progress.setFixedHeight(8)
        self.active_progress.setTextVisible(False)
        ar.addWidget(self.active_progress)
        self.active_power = QtWidgets.QLabel("NORMAL")
        self.active_power.setObjectName("powerBadge")
        ar.addWidget(self.active_power)
        self.active_render.hide()
        ll.addWidget(self.active_render)
        self.active_render.setMaximumHeight(0)
        self.table = QtWidgets.QTableWidget(0, 15)
        self.table.setHorizontalHeaderLabels(
            [
                "",
                "Script",
                "Frames",
                "Write",
                "Output",
                "Status",
                "Priority",
                "Power",
                "Chunk",
                "Retry",
                "Order",
                "Progress",
                "Finish",
                "Health",
                "Notes",
            ]
        )
        self.status_delegate = StatusDelegate(self.table, self.settings.get("status_colors", {}))
        self.table.setItemDelegateForColumn(5, self.status_delegate)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.table.setDragDropMode(QtWidgets.QAbstractItemView.NoDragDrop)
        self.table.setDragDropOverwriteMode(False)
        self.table.setDefaultDropAction(QtCore.Qt.MoveAction)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(24)
        self.table.setAlternatingRowColors(False)
        self.table.setShowGrid(False)
        self.table.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.cellDoubleClicked.connect(self._edit_queue_cell)
        self.table.itemSelectionChanged.connect(self._remember_selection)
        self.table.itemSelectionChanged.connect(self._update_properties)
        self.table.horizontalHeader().setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.table.horizontalHeader().customContextMenuRequested.connect(self._header_context_menu)
        hdr = self.table.horizontalHeader()
        hdr.setMinimumSectionSize(28)
        hdr.setStretchLastSection(False)
        for c in range(15):
            hdr.setSectionResizeMode(c, QtWidgets.QHeaderView.Interactive)
        # Script column stretches to fill all free width, so the table is always filled edge to edge
        # when the window or a panel is resized. All other columns stay freely draggable.
        hdr.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        self._queue_last_viewport_width = None
        defaults = {
            0: 14,
            1: 620,
            2: 150,
            3: 70,
            4: 180,
            5: 130,
            6: 70,
            7: 72,
            8: 62,
            9: 55,
            10: 55,
            11: 130,
            12: 120,
            13: 52,
            14: 135,
        }
        for c, w in defaults.items():
            self.table.setColumnWidth(c, w)
        self.table.setItemDelegateForColumn(1, ScriptDelegate(self.table))
        self.table.horizontalHeaderItem(12).setText("Finish")
        self.table.horizontalHeaderItem(0).setText("")
        self.progress_delegate = ProgressDelegate(self.table, self.settings.get("status_colors", {}))
        self.table.setItemDelegateForColumn(11, self.progress_delegate)
        self.table.setColumnHidden(6, True)
        self.table.setColumnHidden(4, True)
        self.table.setColumnHidden(8, True)
        self.table.setColumnHidden(9, True)
        self.table.setColumnHidden(10, True)
        self.table.setColumnHidden(7, True)
        self.table.setColumnHidden(14, True)
        ll.addWidget(self.table, 1)
        self.empty_state = QtWidgets.QLabel(
            "BATCH GARTNER\n\nDrop Nuke scripts here\n\nor use + Add / send Write nodes directly from Nuke",
            self.table,
        )
        self.empty_state.setObjectName("emptyState")
        self.empty_state.setAlignment(QtCore.Qt.AlignCenter)
        self.empty_state.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
        self.empty_state.hide()
        self.summary = QtWidgets.QLabel()
        self.summary.setObjectName("statusText")
        self.summary.hide()
        ll.addWidget(self.summary)
        self.work_splitter.addWidget(left)

        # Job Properties pane.
        props = QtWidgets.QFrame()
        props.setObjectName("pane")
        props.setMinimumWidth(245)
        pv = QtWidgets.QVBoxLayout(props)
        pv.setContentsMargins(0, 0, 0, 0)
        pv.setSpacing(0)
        phead = QtWidgets.QFrame()
        phead.setObjectName("paneHeader")
        phl = QtWidgets.QHBoxLayout(phead)
        phl.setContentsMargins(7, 3, 5, 3)
        pt = QtWidgets.QLabel("INSPECTOR")
        pt.setObjectName("paneTitle")
        phl.addWidget(pt)
        phl.addStretch(1)
        self.inspector_toggle = QtWidgets.QToolButton()
        self.inspector_toggle.setText("×")
        self.inspector_toggle.setToolTip("Collapse Inspector")
        self.inspector_toggle.clicked.connect(self._toggle_inspector)
        phl.addWidget(self.inspector_toggle)
        pv.addWidget(phead)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        pv.addWidget(scroll, 1)
        propbody = QtWidgets.QWidget()
        scroll.setWidget(propbody)
        pf = QtWidgets.QFormLayout(propbody)
        pf.setContentsMargins(8, 7, 8, 8)
        pf.setHorizontalSpacing(8)
        pf.setVerticalSpacing(5)
        pf.setLabelAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        pf.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        self.prop_labels = {}
        for key in ("script", "snapshot", "range", "write", "current", "status"):
            w = QtWidgets.QLabel("—")
            w.setWordWrap(True)
            w.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            w.setObjectName("propertyValue")
            self.prop_labels[key] = w
        out_wrap = QtWidgets.QWidget()
        out_l = QtWidgets.QHBoxLayout(out_wrap)
        out_l.setContentsMargins(0, 0, 0, 0)
        out_l.setSpacing(2)
        self.output_edit = QtWidgets.QLineEdit()
        self.output_edit.setPlaceholderText("Use Write node output")
        self.output_edit.editingFinished.connect(self._set_output_override)
        out_l.addWidget(self.output_edit, 1)
        out_browse = QtWidgets.QToolButton()
        out_browse.setText("...")
        out_browse.setToolTip("Choose output path")
        out_browse.clicked.connect(self._browse_output_override)
        out_l.addWidget(out_browse)
        reset_out = QtWidgets.QPushButton("Reset Output")
        reset_out.clicked.connect(self._reset_output_override)
        self.notes_edit = QtWidgets.QLineEdit()
        self.notes_edit.setPlaceholderText("Optional note")
        self.notes_edit.editingFinished.connect(self._set_notes)
        self.job_nuke = QtWidgets.QLineEdit()
        self.job_nuke.setPlaceholderText("Use global Nuke")
        self.job_nuke.editingFinished.connect(self._set_job_nuke)
        self.chunk_label = QtWidgets.QLabel("Off")
        self.chunk_label.setObjectName("propertyValue")
        self.retry_label = QtWidgets.QLabel("0")
        self.retry_label.setObjectName("propertyValue")
        self.power_label = QtWidgets.QLabel("Normal")
        self.power_label.setObjectName("propertyValue")
        self.work_mode = QtWidgets.QCheckBox("Render while I work")
        self.work_mode.setChecked(bool(self.settings.get("render_while_work", False)))
        self.work_mode.toggled.connect(self._set_work_mode)
        health = QtWidgets.QHBoxLayout()
        health.setSpacing(3)
        self.health_label = QtWidgets.QLabel("Not checked")
        self.health_label.setObjectName("monitorValue")
        hb = QtWidgets.QPushButton("Check")
        hb.clicked.connect(self._check_selected_output)
        health.addWidget(self.health_label, 1)
        health.addWidget(hb)
        self.frame_strip = FrameStrip()
        # Built-in preview of finished frames. Files are read into memory and closed at once,
        # so the preview can never lock a frame Nuke is about to write (unlike an external viewer).
        prev = QtWidgets.QWidget()
        pvl = QtWidgets.QVBoxLayout(prev)
        pvl.setContentsMargins(0, 0, 0, 0)
        pvl.setSpacing(3)
        self.preview_label = QtWidgets.QLabel("No preview")
        self.preview_label.setObjectName("previewImage")
        self.preview_label.setAlignment(QtCore.Qt.AlignCenter)
        self.preview_label.setFixedHeight(170)
        self.preview_label.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        self.preview_label.setWordWrap(True)
        prow = QtWidgets.QHBoxLayout()
        prow.setContentsMargins(0, 0, 0, 0)
        prow.setSpacing(4)
        self.preview_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.preview_slider.setEnabled(False)
        self.preview_slider.setToolTip("Scrub through finished frames")
        self.preview_frame_lbl = QtWidgets.QLabel("—")
        self.preview_frame_lbl.setObjectName("monitorValue")
        self.preview_frame_lbl.setMinimumWidth(48)
        self.preview_frame_lbl.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        self.preview_follow = QtWidgets.QCheckBox("Latest")
        self.preview_follow.setChecked(True)
        self.preview_follow.setToolTip("Always show the newest finished frame")
        prow.addWidget(self.preview_slider, 1)
        prow.addWidget(self.preview_frame_lbl)
        prow.addWidget(self.preview_follow)
        pvl.addWidget(self.preview_label)
        pvl.addLayout(prow)
        self._preview_frames = []
        self._preview_job_id = None
        self._preview_shown = None
        self._preview_loading = False
        self._preview_pending = None
        self._preview_last_scan = 0.0
        self._preview_debounce = QtCore.QTimer(self)
        self._preview_debounce.setSingleShot(True)
        self._preview_debounce.setInterval(120)
        self._preview_debounce.timeout.connect(self._load_preview_at_slider)
        self.preview_slider.valueChanged.connect(self._preview_slider_moved)
        self.preview_follow.toggled.connect(lambda on: self._refresh_preview(force=True) if on else None)
        self.open_output_btn = QtWidgets.QPushButton("Open Output Folder")
        self.open_output_btn.clicked.connect(self._open_output_folder)

        # Live render information first.
        pf.addRow(prev)
        pf.addRow("Status", self.prop_labels["status"])
        pf.addRow("Progress", self.prop_labels["current"])
        pf.addRow("Frames", self.frame_strip)
        pf.addRow("Frame Range", self.prop_labels["range"])
        pf.addRow("Write Node", self.prop_labels["write"])
        pf.addRow("Sequence", health)
        pf.addRow("", self.open_output_btn)
        pf.addRow("Script", self.prop_labels["script"])
        pf.addRow("Snapshot", self.prop_labels["snapshot"])
        # Rarely changed settings, in a collapsible section.
        self.settings_toggle = QtWidgets.QToolButton()
        self.settings_toggle.setText("JOB SETTINGS")
        self.settings_toggle.setCheckable(True)
        self.settings_toggle.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.settings_box = QtWidgets.QFrame()
        self.settings_box.setObjectName("settingsBox")
        sf = QtWidgets.QFormLayout(self.settings_box)
        sf.setContentsMargins(0, 2, 0, 4)
        sf.setHorizontalSpacing(8)
        sf.setVerticalSpacing(5)
        sf.setLabelAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        sf.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        sf.addRow("Output", out_wrap)
        sf.addRow("", reset_out)
        sf.addRow("Notes", self.notes_edit)
        sf.addRow("Nuke exe", self.job_nuke)
        sf.addRow("Chunking", self.chunk_label)
        sf.addRow("Auto retry", self.retry_label)
        sf.addRow("Render Power", self.power_label)
        sf.addRow("Workstation", self.work_mode)

        def _toggle_settings(on):
            self.settings_box.setVisible(on)
            self.settings_toggle.setArrowType(QtCore.Qt.DownArrow if on else QtCore.Qt.RightArrow)
            self.settings["job_settings_open"] = bool(on)

        self.settings_toggle.toggled.connect(_toggle_settings)
        opened = bool(self.settings.get("job_settings_open", False))
        self.settings_toggle.setChecked(opened)
        _toggle_settings(opened)
        settings_sep = QtWidgets.QFrame()
        settings_sep.setFrameShape(QtWidgets.QFrame.HLine)
        settings_sep.setObjectName("monitorSeparator")
        pf.addRow(settings_sep)
        pf.addRow(self.settings_toggle)
        pf.addRow(self.settings_box)
        monitor_sep = QtWidgets.QFrame()
        monitor_sep.setFrameShape(QtWidgets.QFrame.HLine)
        monitor_sep.setObjectName("monitorSeparator")
        pf.addRow(monitor_sep)
        self.monitor_toggle = QtWidgets.QToolButton()
        self.monitor_toggle.setText("SYSTEM MONITOR")
        self.monitor_toggle.setCheckable(True)
        self.monitor_toggle.setChecked(False)
        self.monitor_toggle.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.monitor_toggle.setArrowType(QtCore.Qt.RightArrow)
        self.monitor_toggle.toggled.connect(self._toggle_monitor)
        pf.addRow(self.monitor_toggle)
        self.monitor_widget = QtWidgets.QWidget()
        self.monitor_widget.setVisible(False)
        ml = QtWidgets.QVBoxLayout(self.monitor_widget)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(4)
        self.monitor_bars = {}
        for key, label in [("cpu", "CPU"), ("ram", "RAM"), ("gpu", "GPU"), ("vram", "VRAM")]:
            row = QtWidgets.QWidget()
            rl = QtWidgets.QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(4)
            lab = QtWidgets.QLabel(label)
            lab.setFixedWidth(34)
            bar = QtWidgets.QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setTextVisible(False)
            bar.setFixedHeight(7)
            val = QtWidgets.QLabel("N/A")
            val.setObjectName("monitorValue")
            val.setMinimumWidth(76)
            val.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            rl.addWidget(lab)
            rl.addWidget(bar, 1)
            rl.addWidget(val)
            ml.addWidget(row)
            self.monitor_bars[key] = (bar, val)
        self.monitor_graphs = {}
        for key, label in [("cpu", "CPU history"), ("ram", "RAM history"), ("gpu", "GPU history")]:
            g = HistoryGraph()
            self.monitor_graphs[key] = g
            ml.addWidget(g)
        stats = QtWidgets.QFormLayout()
        stats.setContentsMargins(0, 4, 0, 0)
        stats.setSpacing(3)
        self.monitor_stats = {}
        for key, label in [
            ("elapsed", "Elapsed"),
            ("avg", "Avg / frame"),
            ("eta", "ETA"),
            ("power", "Render Power"),
            ("nuke_ram", "Nuke RAM"),
            ("protection", "Protection"),
        ]:
            v = QtWidgets.QLabel("—")
            v.setObjectName("monitorValue")
            self.monitor_stats[key] = v
            stats.addRow(label, v)
        ml.addLayout(stats)
        pf.addRow(self.monitor_widget)
        self.work_splitter.addWidget(props)
        self.inspector_pane = props
        self.inspector_pane.hide()
        self.work_splitter.setStretchFactor(0, 4)
        self.work_splitter.setStretchFactor(1, 1)
        self.work_splitter.setSizes([1180, 0])

        # Detail tabs below the queue.
        self.management_tabs = QtWidgets.QTabWidget()
        self.management_tabs.setObjectName("managementTabs")
        self.management_tabs.setMinimumHeight(70)
        self.management_tabs.setMaximumHeight(16777215)
        self.task_table = QtWidgets.QTableWidget(0, 6)
        self.task_table.setHorizontalHeaderLabels(["Task", "Frames", "Status", "Progress", "Current", "Info"])
        self.task_table.verticalHeader().hide()
        self.task_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.task_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.task_table.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.task_table.customContextMenuRequested.connect(self._task_context_menu)
        self.task_table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        self.task_table.horizontalHeader().setStretchLastSection(True)
        self.task_status_delegate = StatusDelegate(self.task_table, self.settings.get("status_colors", {}))
        self.task_progress_delegate = ProgressDelegate(
            self.task_table, self.settings.get("status_colors", {})
        )
        self.task_table.setItemDelegateForColumn(2, self.task_status_delegate)
        self.task_table.setItemDelegateForColumn(3, self.task_progress_delegate)
        self.management_tabs.addTab(self.task_table, "TASKS")
        self.details_view = QtWidgets.QPlainTextEdit()
        self.details_view.setReadOnly(True)
        self.management_tabs.addTab(self.details_view, "JOB INFO")
        self.errors_view = QtWidgets.QPlainTextEdit()
        self.errors_view.setReadOnly(True)
        self.management_tabs.addTab(self.errors_view, "ERRORS")
        self.performance_view = QtWidgets.QPlainTextEdit()
        self.performance_view.setReadOnly(True)
        self.management_tabs.addTab(self.performance_view, "PERFORMANCE")
        self.output_view = QtWidgets.QPlainTextEdit()
        self.output_view.setReadOnly(True)
        self.management_tabs.addTab(self.output_view, "OUTPUT")
        self.activity_view = QtWidgets.QPlainTextEdit()
        self.activity_view.setReadOnly(True)
        self.management_tabs.addTab(self.activity_view, "ACTIVITY")
        self.session_view = QtWidgets.QPlainTextEdit()
        self.session_view.setReadOnly(True)
        self.management_tabs.addTab(self.session_view, "SESSION")
        wl.addWidget(self.management_tabs, 0)
        self.main_splitter.addWidget(work)

        # Console pane (hidden; the log is shown in the LOG tab).
        consoleBox = QtWidgets.QFrame()
        consoleBox.setObjectName("pane")
        cl = QtWidgets.QVBoxLayout(consoleBox)
        cl.setContentsMargins(5, 2, 5, 5)
        cl.setSpacing(0)
        chead = QtWidgets.QFrame()
        chead.setObjectName("paneHeader")
        chl = QtWidgets.QHBoxLayout(chead)
        chl.setContentsMargins(7, 3, 5, 3)
        ch = QtWidgets.QLabel("RENDER CONSOLE")
        ch.setObjectName("paneTitle")
        chl.addWidget(ch)
        chl.addStretch(1)
        self.console_toggle = QtWidgets.QToolButton()
        self.console_toggle.setText("▾")
        self.console_toggle.setToolTip("Collapse / expand console")
        self.console_toggle.clicked.connect(self._toggle_console)
        chl.addWidget(self.console_toggle)
        cl.addWidget(chead)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(6000)
        cl.addWidget(self.log, 1)
        self.main_splitter.addWidget(consoleBox)
        self.console_box = consoleBox
        self.management_tabs.addTab(self.log, "LOG")
        self.console_box.hide()
        self.main_splitter.setStretchFactor(0, 1)
        self.main_splitter.setStretchFactor(1, 0)
        self.main_splitter.setSizes([715, 0])
        self._console_last_height = 155
        pathbar.hide()
        # Nuke version selector in the toolbar.
        tl.insertSpacing(max(0, tl.count() - 1), 8)
        nuke_title = QtWidgets.QLabel("Nuke")
        nuke_title.setObjectName("toolbarLabel")
        tl.insertWidget(max(0, tl.count() - 1), nuke_title)
        tl.insertWidget(max(0, tl.count() - 1), self.nuke_combo)
        self.nuke_browse_btn = QtWidgets.QToolButton()
        self.nuke_browse_btn.setText("...")
        self.nuke_browse_btn.setToolTip("Browse for the Nuke executable")
        self.nuke_browse_btn.clicked.connect(self._choose_nuke_executable)
        tl.insertWidget(max(0, tl.count() - 1), self.nuke_browse_btn)
        self._sync_nuke_combo()
        self.nuke_combo.setObjectName("toolbarCombo")
        self.nuke_combo.setMinimumWidth(120)
        self.nuke_combo.setMaximumWidth(165)
        self.global_power_combo.setMaximumWidth(92)
        self.work_mode_toolbar.setText("Work mode")

        # Worker status table.
        self.worker_box = QtWidgets.QFrame()
        self.worker_box.setObjectName("pane")
        wv = QtWidgets.QVBoxLayout(self.worker_box)
        wv.setContentsMargins(0, 0, 0, 0)
        wv.setSpacing(0)
        wh = QtWidgets.QFrame()
        wh.setObjectName("paneHeader")
        whl = QtWidgets.QHBoxLayout(wh)
        whl.setContentsMargins(7, 2, 5, 2)
        wt = QtWidgets.QLabel("WORKER")
        wt.setObjectName("paneTitle")
        whl.addWidget(wt)
        whl.addStretch(1)
        self.worker_state_label = QtWidgets.QLabel("LOCAL")
        self.worker_state_label.setObjectName("headerSummary")
        whl.addWidget(self.worker_state_label)
        wv.addWidget(wh)
        self.worker_table = QtWidgets.QTableWidget(1, 7)
        self.worker_table.setHorizontalHeaderLabels(
            ["Name", "Status", "Current Job", "Progress", "CPU", "RAM", "Power"]
        )
        self.worker_table.verticalHeader().hide()
        self.worker_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.worker_table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.worker_table.setShowGrid(False)
        self.worker_table.setMaximumHeight(78)
        self.worker_table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch)
        for c in (0, 1, 3, 4, 5, 6):
            self.worker_table.horizontalHeader().setSectionResizeMode(
                c, QtWidgets.QHeaderView.ResizeToContents
            )
        wv.addWidget(self.worker_table)
        ll.addWidget(self.worker_box, 0)

        self.log_box = QtWidgets.QFrame()
        self.log_box.setObjectName("pane")
        lv = QtWidgets.QVBoxLayout(self.log_box)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(0)
        lh = QtWidgets.QFrame()
        lh.setObjectName("paneHeader")
        lhl = QtWidgets.QHBoxLayout(lh)
        lhl.setContentsMargins(7, 2, 5, 2)
        lhl.addWidget(QtWidgets.QLabel("LOG"))
        lhl.addStretch(1)
        lv.addWidget(lh)
        self.log.setParent(self.log_box)
        self.log.setMaximumHeight(125)
        lv.addWidget(self.log)
        ll.addWidget(self.log_box, 0)

        # Job Properties is always visible.
        pt.setText("JOB PROPERTIES")
        self.inspector_toggle.hide()
        self.inspector_pane.show()
        self.inspector_pane.setMinimumWidth(260)
        self.inspector_pane.setMaximumWidth(16777215)
        self.work_splitter.setSizes([920, 320])

        self.worker_table.setParent(self.management_tabs)
        self.worker_table.setMaximumHeight(16777215)
        self.management_tabs.addTab(self.worker_table, "WORKER")
        self.worker_box.hide()
        self.log_box.hide()
        self.console_box.hide()
        self.log.setMaximumHeight(16777215)
        self.management_tabs.addTab(self.log, "LOG")
        self.management_tabs.setTabText(1, "INFO")
        self._regroup_detail_tabs()
        self.management_tabs.setMinimumHeight(80)
        self.management_tabs.setMaximumHeight(16777215)

        # Queue above, detail tabs below, in a resizable splitter.
        self.queue_top = QtWidgets.QWidget()
        qtv = QtWidgets.QVBoxLayout(self.queue_top)
        qtv.setContentsMargins(0, 0, 0, 0)
        qtv.setSpacing(0)
        self.active_render.setParent(self.queue_top)
        qtv.addWidget(self.active_render)
        self.table.setParent(self.queue_top)
        qtv.addWidget(self.table, 1)
        self.detail_splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self.detail_splitter.setObjectName("detailSplitter")
        self.detail_splitter.setChildrenCollapsible(True)
        self.detail_splitter.addWidget(self.queue_top)
        self.management_tabs.setParent(self.detail_splitter)
        self.detail_splitter.addWidget(self.management_tabs)
        self.detail_splitter.setStretchFactor(0, 3)
        self.detail_splitter.setStretchFactor(1, 1)
        self.detail_splitter.setSizes([560, 180])
        self.detail_splitter.splitterMoved.connect(
            lambda *_: QtCore.QTimer.singleShot(0, self._fill_queue_table_width)
        )
        self.table.viewport().installEventFilter(self)
        ll.addWidget(self.detail_splitter, 1)
        self.main_splitter.setSizes([760, 0])

        self.statusBar().showMessage("Ready")
        self.status_jobs = QtWidgets.QLabel("Jobs: 0")
        self.status_render = QtWidgets.QLabel("Rendering: 0")
        self.status_wait = QtWidgets.QLabel("Queued: 0")
        self.status_done = QtWidgets.QLabel("Complete: 0")
        self.status_fail = QtWidgets.QLabel("Failed: 0")
        self.status_resources = QtWidgets.QLabel("CPU —   RAM —")
        for _w in (
            self.status_jobs,
            self.status_render,
            self.status_wait,
            self.status_done,
            self.status_fail,
            self.status_resources,
        ):
            self.statusBar().addPermanentWidget(_w)

    def _regroup_detail_tabs(self):
        """Nine tabs become four: TASKS · LOG · DETAILS · HISTORY. The same views are reused, only regrouped."""
        tabs = self.management_tabs
        while tabs.count() > 1:
            tabs.removeTab(1)

        def section(title, widget):
            box = QtWidgets.QFrame()
            box.setObjectName("detailSection")
            v = QtWidgets.QVBoxLayout(box)
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(0)
            h = QtWidgets.QLabel(title)
            h.setObjectName("sectionTitle")
            h.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Fixed)
            # Widgets taken out of a QTabWidget stay hidden; show them again in their new place.
            widget.setVisible(True)
            v.addWidget(h)
            v.addWidget(widget, 1)
            return box, h

        details = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        details.setChildrenCollapsible(False)
        err_box, self._errors_title = section("ERRORS", self.errors_view)
        for title, w in (
            ("JOB", self.details_view),
            ("OUTPUT", self.output_view),
            ("PERFORMANCE", self.performance_view),
        ):
            details.addWidget(section(title, w)[0])
        details.addWidget(err_box)
        details.setSizes([300, 260, 220, 300])
        history = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        history.setChildrenCollapsible(False)
        history.addWidget(section("ACTIVITY", self.activity_view)[0])
        right = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        right.setChildrenCollapsible(False)
        right.addWidget(section("SESSION", self.session_view)[0])
        right.addWidget(section("WORKER", self.worker_table)[0])
        right.setSizes([140, 60])
        history.addWidget(right)
        history.setSizes([520, 380])
        self.log.setMaximumHeight(16777215)
        tabs.addTab(self.log, "LOG")
        self._details_tab_index = tabs.addTab(details, "DETAILS")
        tabs.addTab(history, "HISTORY")

    def _update_tab_badges(self):
        """Mark DETAILS in red when the selected job has errors."""
        if not hasattr(self, "_details_tab_index"):
            return
        txt = self.errors_view.toPlainText().strip()
        has_err = bool(txt) and not txt.startswith("No errors") and not txt.startswith("No job selected")
        bar = self.management_tabs.tabBar()
        self.management_tabs.setTabText(self._details_tab_index, "DETAILS  ●" if has_err else "DETAILS")
        bar.setTabTextColor(self._details_tab_index, QtGui.QColor("#e0605a") if has_err else QtGui.QColor())
        if hasattr(self, "_errors_title"):
            self._errors_title.setStyleSheet("color:#e0605a;" if has_err else "")

    def _on_pause_toggled(self, checked):
        if checked:
            if not self._queue_running:
                self.pause_btn.blockSignals(True)
                self.pause_btn.setChecked(False)
                self.pause_btn.blockSignals(False)
                self.statusBar().showMessage("Nothing is rendering", 3000)
                return
            self._pause_after_current()
        elif self.pause_after_job:
            self.pause_after_job = False
            self._write_console(">> Pause cancelled — the queue will continue.")

    def _sync_toolbar_state(self):
        want = bool(getattr(self, "_queue_running", False) and getattr(self, "pause_after_job", False))
        if self.pause_btn.isChecked() != want:
            self.pause_btn.blockSignals(True)
            self.pause_btn.setChecked(want)
            self.pause_btn.blockSignals(False)
        self.pause_btn.setText("PAUSING…" if want else "PAUSE")
        self.render_sel_btn.setEnabled(not self._queue_running)

    def eventFilter(self, obj, event):
        # Reflow the queue whenever its viewport changes size (including splitter moves).
        if hasattr(self, "table") and obj is self.table.viewport() and event.type() == QtCore.QEvent.Resize:
            QtCore.QTimer.singleShot(0, self._fill_queue_table_width)
        return super().eventFilter(obj, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QtCore.QTimer.singleShot(0, self._fill_queue_table_width)
        if hasattr(self, "empty_state") and hasattr(self, "table"):
            self.empty_state.setGeometry(self.table.viewport().rect())

    def _fill_queue_table_width(self):
        # The Script column is in Stretch mode, so Qt fills the width; this only repaints.
        if hasattr(self, "table"):
            self.table.viewport().update()

    def _toggle_inspector(self):
        if not hasattr(self, "inspector_pane"):
            return
        visible = self.inspector_pane.isVisible()
        self.inspector_pane.setVisible(not visible)
        if not visible:
            sizes = self.work_splitter.sizes()
            if len(sizes) >= 2 and sizes[1] < 180:
                self.work_splitter.setSizes([max(500, sizes[0] - 260), 260])

    def _toggle_console(self):
        sizes = self.main_splitter.sizes()
        if len(sizes) < 2:
            return
        if sizes[1] > 35:
            self._console_last_height = sizes[1]
            self.main_splitter.setSizes([sum(sizes) - 30, 30])
            self.console_toggle.setText("▴")
        else:
            h = max(120, getattr(self, "_console_last_height", 155))
            self.main_splitter.setSizes([max(200, sum(sizes) - h), h])
            self.console_toggle.setText("▾")

    def _build_menus(self):
        filem = self.menuBar().addMenu("File")
        filem.addAction("Add Scripts...", self._add_script_files)
        filem.addAction("Add Folder...", self._add_folder)
        filem.addSeparator()
        filem.addAction("Save Queue As...", self._save_queue_as)
        filem.addAction("Load Queue...", self._load_queue)
        filem.addSeparator()
        filem.addAction("Exit", self.close)
        qm = self.menuBar().addMenu("Queue")
        qm.addAction("Edit Job", self._open_job_editor)
        qm.addAction("Move Up", QtGui.QKeySequence("Ctrl+Up"), lambda: self._shift_selected(-1))
        qm.addAction("Move Down", QtGui.QKeySequence("Ctrl+Down"), lambda: self._shift_selected(1))
        qm.addAction("Duplicate Job", self._duplicate_job)
        qm.addAction("Refresh Script (use latest save)", self._refresh_selected_job)
        qm.addAction("Create Read in Nuke", self._create_read_in_nuke)
        qm.addSeparator()
        qm.addAction("Copy Job Settings", self._copy_job_settings)
        qm.addAction("Paste Job Settings", self._paste_job_settings)
        qm.addSeparator()
        qm.addAction("Clear Completed", self._clear_completed)
        qm.addAction("Multi-edit Selected...", self._multi_edit_selected)
        qm.addAction("Remove", self._remove_selected)
        qm.addAction("Clear All", self._clear_queue)
        rm = self.menuBar().addMenu("Render")
        rm.addAction("Start Queue", self._start_render)
        rm.addAction("Render Selected", self._render_selected)
        rm.addAction("Render From Here", self._render_from_here)
        rm.addAction("Render Missing Frames", self._render_missing)
        rm.addSeparator()
        rm.addAction("Render Test Frame", self._render_test_frame)
        rm.addAction("Render First / Middle / Last", self._render_first_middle_last)
        rm.addSeparator()
        rm.addAction("Pause After Current Job", self._pause_after_current)
        rm.addAction("Skip Current Job", self._skip_current_job)
        rm.addAction("Stop", self._request_stop)
        rm.addAction("Retry Failed / Stopped", self._retry_failed)
        vm = self.menuBar().addMenu("View")
        vm.addAction("Toggle Inspector", self._toggle_inspector)
        vm.addSeparator()
        vm.addAction("Job Inspector...", self._show_job_inspector)
        vm.addAction("Render History", self._show_history)
        vm.addAction("Check Selected Output", self._check_selected_output)
        vm.addAction("Preflight Selected...", self._preflight_selected)
        vm.addAction("Render Report...", self._show_render_report)
        tools = self.menuBar().addMenu("Tools")
        tools.addAction("Set Nuke Executable...", self._choose_nuke_executable)
        tools.addAction("Preferences...", self._show_preferences)
        tools.addAction("Save Recovery Snapshot", self._write_recovery_snapshot)
        tools.addAction("Check for Newer Script Version", self._check_newer_selected)
        helpm = self.menuBar().addMenu("Help")
        helpm.addAction("About Batch Gartner", self._show_about)
        pm = self.menuBar().addMenu("Post Render")
        self.shutdown_action = pm.addAction("Shutdown when queue finishes")
        self.shutdown_action.setCheckable(True)
        self.sleep_action = pm.addAction("Sleep when queue finishes")
        self.sleep_action.setCheckable(True)

    def _apply_style(self):
        font_family = self.settings.get("ui_font_family", "Verdana")
        font_size = max(8, min(16, int(self.settings.get("ui_font_size", 11))))
        self.setStyleSheet(f"""
        * {{ font-family:"{font_family}"; font-size:{font_size}px; color:#d0d0d0; }}
        QMainWindow,QWidget {{ background:#1b1b1b; }}
        QMenuBar {{ background:#2b2b2b; border-bottom:1px solid #171717; padding:0px; }}
        QMenuBar::item {{ padding:3px 8px; background:transparent; }}
        QMenuBar::item:selected {{ background:#414141; }}
        QMenu {{ background:#303030; border:1px solid #171717; padding:2px; }}
        QMenu::item {{ padding:4px 26px 4px 18px; }}
        QMenu::item:selected {{ background:#46525a; color:white; }}
        QFrame#topStrip {{ background:#2d2d2d; border-bottom:1px solid #181818; }}
        QFrame#toolStrip {{ background:#303030; border-bottom:1px solid #181818; }}
        QFrame#pane {{ background:#202020; border:0; }}
        QFrame#paneHeader {{ background:#333333; border:0; border-bottom:1px solid #1b1b1b; }}
        QLabel#paneTitle {{ color:#bcbcbc; font-weight:600; letter-spacing:.3px; background:transparent; }}
        QLabel#headerSummary {{ color:#777; background:transparent; padding-left:8px; }}
        QLabel#emptyState {{ color:#747474; background:#232323; font-size:11px; }}
        QLabel#propertyValue {{ background:transparent; color:#d0d0d0; padding:2px 0px; border:0; }}
        QLabel#muted,QLabel#statusText {{ color:#8c8c8c; background:#252525; }}
        QLineEdit,QComboBox {{ background:#181818; border:1px solid #161616; padding:3px 5px; min-height:20px; selection-background-color:#536773; }}
        QLabel#toolbarLabel {{ color:#a8a8a8; background:transparent; padding:0px 2px 0px 6px; }}
        QComboBox#toolbarCombo {{ background:#292929; border:1px solid #161616; padding:2px 6px; min-height:20px; }}
        QComboBox#toolbarCombo:hover {{ background:#323232; border-color:#444; }}
        QComboBox#toolbarCombo:focus {{ border:1px solid #ff991c; }}
        QCheckBox#toolbarCheck {{ spacing:5px; color:#bdbdbd; background:transparent; padding:1px 5px; }}
        QCheckBox#toolbarCheck::indicator {{ width:12px; height:12px; background:#222; border:1px solid #111; }}
        QCheckBox#toolbarCheck::indicator:checked {{ background:#ff991c; border:1px solid #151515; }}
        QLineEdit:focus,QComboBox:focus {{ border:1px solid #666; }}
        QComboBox::drop-down {{ border:0; width:18px; }}
        QPushButton,QToolButton {{ background:#3a3a3a; border:1px solid #191919; padding:2px 7px; min-height:21px; }}
        QPushButton:hover,QToolButton:hover {{ background:#454545; }}
        QToolButton#rowAction {{ background:#303030; border:1px solid #1a1a1a; padding:0px; color:#bcbcbc; }}
        QToolButton#rowAction:hover {{ background:#3c3c3c; color:#f0f0f0; }}
        QToolButton#rowAction:disabled {{ background:#252525; color:#555; }}
        QPushButton:pressed,QToolButton:pressed {{ background:#303030; }}
        QPushButton:disabled,QToolButton:disabled {{ color:#666; background:#303030; }}
        QPushButton#primaryButton {{ background:#a8651b; color:#ffffff; border:1px solid #171717; font-weight:600; padding-left:12px; padding-right:12px; }}
        QPushButton#primaryButton:hover {{ background:#c07525; }}
        QPushButton#primaryButton:disabled {{ background:#4a3a28; color:#9a8a78; }}
        QPushButton#pauseButton:checked {{ background:#8c7946; color:#ffffff; font-weight:600; }}
        QPushButton#renderButton {{ background:#51493d; border:1px solid #171717; font-weight:600; padding-left:11px; padding-right:11px; }}
        QPushButton#renderButton:hover {{ background:#625747; }}
        QTableWidget {{ background:#171717; border:0; outline:0; selection-background-color:#5a5a5a; selection-color:#fff; }}
        QTableWidget::item {{ padding:2px 5px; border:0; border-bottom:1px solid #2b2b2b; }}
        QHeaderView::section {{ background:#2d2d2d; color:#aaa; border:0; border-right:1px solid #242424; border-bottom:1px solid #181818; padding:4px 5px; font-weight:600; }}
        QPlainTextEdit {{ background:#151515; color:#bdbdbd; border:0; font-family:Consolas; font-size:10px; padding:4px; }}
        QScrollArea {{ border:0; background:#282828; }}
        QScrollBar:vertical {{ background:#232323; width:10px; margin:0; }}
        QScrollBar::handle:vertical {{ background:#4a4a4a; min-height:24px; margin:1px; }}
        QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {{ height:0; }}
        QScrollBar:horizontal {{ background:#232323; height:10px; }}
        QScrollBar::handle:horizontal {{ background:#4a4a4a; min-width:24px; margin:1px; }}
        QScrollBar::add-line:horizontal,QScrollBar::sub-line:horizontal {{ width:0; }}
        QProgressBar {{ background:#1d1d1d; border:0; text-align:center; color:#d5d5d5; min-height:8px; }}
        QProgressBar::chunk {{ background:#b9b9b9; }}
        QLabel#monitorValue {{ color:#a5a5a5; font-family:Consolas; font-size:10px; }}
        QLabel#previewImage {{ background:#111; color:#6f6f6f; border:1px solid #242424; }}
        QFrame#monitorSeparator {{ background:#1b1b1b; max-height:1px; border:0; }}
        QStatusBar {{ background:#222; color:#858585; border-top:1px solid #151515; min-height:18px; }}

        QFrame#activeRender {{ background:#292929; border-left:2px solid #ff991c; border-bottom:1px solid #34383b; }}
        QTabWidget#managementTabs::pane {{ border:0; border-top:1px solid #181818; background:#232323; }}
        QTabBar::tab {{ background:#2d2d2d; color:#929292; border:0; border-right:1px solid #202020; padding:4px 10px; }}
        QLabel#sectionTitle {{ background:#262626; color:#8a8a8a; font-weight:600; padding:2px 6px; border-bottom:1px solid #1b1b1b; }}
        QFrame#settingsBox {{ background:transparent; }}
        QTabBar::tab:selected {{ background:#383838; color:#f0f0f0; border-bottom:2px solid #ff991c; }}
        QLabel#activeName {{ font-weight:600; color:#d7d7d7; }}
        QLabel#powerBadge {{ padding:1px 6px; background:#303438; border:1px solid #484d50; }}
        QSplitter::handle {{ background:#181818; }}
        QSplitter::handle:horizontal {{ width:4px; }}
        QSplitter::handle:vertical {{ height:4px; }}
        QSplitter#detailSplitter::handle:vertical {{ background:#303030; border-top:1px solid #111; border-bottom:1px solid #111; }}
        QSplitter#detailSplitter::handle:vertical:hover {{ background:#444; }}
        """)

    def _set_default_power(self, value):
        self.settings["default_render_power"] = value
        self._save_state()

    def _set_work_mode_toolbar(self, checked):
        if hasattr(self, "work_mode") and self.work_mode.isChecked() != checked:
            self.work_mode.blockSignals(True)
            self.work_mode.setChecked(checked)
            self.work_mode.blockSignals(False)
        self._set_work_mode(checked)

    def _update_active_render_header(self):
        if not hasattr(self, "active_render"):
            return
        active = next((j for j in self.jobs if j.status == "Rendering"), None)
        self.active_render.setVisible(active is not None)
        if not active:
            return
        write = active.write_node if active.write_node not in ("", None, "(all)") else "All Writes"
        self.active_name.setText(f"NOW RENDERING   {active.name} / {write}")
        frame = (
            f"Frame {active.current_frame} / {active.last}"
            if active.current_frame is not None and active.last is not None
            else active.frames_label
        )
        avg = (sum(active.frame_durations) / len(active.frame_durations)) if active.frame_durations else None
        eta = ""
        if avg is not None and active.current_frame is not None and active.last is not None:
            remaining = max(0, active.last - active.current_frame) * avg
            finish = (datetime.datetime.now() + datetime.timedelta(seconds=remaining)).strftime("%H:%M")
            eta = f"   {avg:.1f}s/frame   ETA {self._fmt_duration(remaining)}   Finish {finish}"
        self.active_frame.setText(frame + eta)
        self.active_progress.setValue(int(max(0, min(100, active.progress))))
        self.active_power.setText(self._effective_power(active).upper())

    def _toggle_monitor(self, checked):
        self.monitor_widget.setVisible(checked)
        self.monitor_toggle.setArrowType(QtCore.Qt.DownArrow if checked else QtCore.Qt.RightArrow)

    @staticmethod
    def _fmt_duration(seconds):
        if seconds is None or seconds < 0:
            return "—"
        seconds = int(seconds)
        h, rem = divmod(seconds, 3600)
        m, sec = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"

    def _set_monitor_metric(self, key, pct=None, text="N/A"):
        bar, val = self.monitor_bars[key]
        if pct is None:
            bar.setValue(0)
            bar.setEnabled(False)
        else:
            bar.setEnabled(True)
            bar.setValue(max(0, min(100, int(round(pct)))))
        val.setText(text)

    def _query_gpu_async(self):
        if self._gpu_query_running:
            return
        if not (hasattr(self, "monitor_widget") and self.monitor_widget.isVisible()):
            return
        exe = shutil.which("nvidia-smi")
        if not exe:
            self._last_gpu = None
            return
        self._gpu_query_running = True

        def work():
            result = None
            try:
                out = subprocess.check_output(
                    [
                        exe,
                        "--query-gpu=utilization.gpu,memory.used,memory.total",
                        "--format=csv,noheader,nounits",
                    ],
                    text=True,
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                    **_hidden(),
                )
                rows = []
                for line in out.strip().splitlines():
                    parts = [x.strip() for x in line.split(",")]
                    if len(parts) >= 3:
                        rows.append(tuple(float(x) for x in parts[:3]))
                if rows:
                    util = max(r[0] for r in rows)
                    used = sum(r[1] for r in rows)
                    total = sum(r[2] for r in rows)
                    result = (util, used, total)
            except Exception:
                pass
            self._last_gpu = result
            self._gpu_query_running = False

        threading.Thread(target=work, daemon=True).start()

    def _update_system_monitor(self):
        if not hasattr(self, "monitor_bars"):
            return
        self._update_active_render_header()
        if psutil is not None:
            try:
                cpu = psutil.cpu_percent(interval=None)
                vm = psutil.virtual_memory()
                used = (vm.total - vm.available) / (1024**3)
                total = vm.total / (1024**3)
                self._refresh_worker_panel(cpu, vm.percent)
                self._set_monitor_metric("cpu", cpu, f"{cpu:.0f}%")
                self._set_monitor_metric("ram", vm.percent, f"{used:.1f} / {total:.1f} GB")
                active = next((j for j in self.jobs if j.status == "Rendering"), None)
                if active:
                    nuke_gb = self._nuke_ram_gb(active)
                    if nuke_gb is not None:
                        active.peak_ram_gb = max(active.peak_ram_gb, nuke_gb)
                        active.now_ram_gb = nuke_gb
                    active.avg_cpu_samples = (active.avg_cpu_samples + [cpu])[-120:]
                if hasattr(self, "monitor_graphs"):
                    self.monitor_graphs["cpu"].push(cpu)
                    self.monitor_graphs["ram"].push(vm.percent)
            except Exception:
                self._set_monitor_metric("cpu")
                self._set_monitor_metric("ram")
        else:
            self._set_monitor_metric("cpu", None, "N/A (psutil)")
            self._set_monitor_metric("ram", None, "N/A (psutil)")
            self._refresh_worker_panel()
        self._query_gpu_async()
        if self._last_gpu:
            util, used, total = self._last_gpu
            vpct = (100.0 * used / total) if total else 0
            self._set_monitor_metric("gpu", util, f"{util:.0f}%")
            self._set_monitor_metric("vram", vpct, f"{used / 1024:.1f} / {total / 1024:.1f} GB")
            if hasattr(self, "monitor_graphs"):
                self.monitor_graphs["gpu"].push(util)
        else:
            self._set_monitor_metric("gpu")
            self._set_monitor_metric("vram")
        active = next((j for j in self.jobs if j.status == "Rendering"), None)
        if active and active.render_started_at:
            elapsed = time.monotonic() - active.render_started_at
            self.monitor_stats["elapsed"].setText(self._fmt_duration(elapsed))
            avg = (
                (sum(active.frame_durations) / len(active.frame_durations))
                if active.frame_durations
                else None
            )
            self.monitor_stats["avg"].setText(f"{avg:.1f} sec" if avg is not None else "—")
            if avg is not None and active.current_frame is not None and active.last is not None:
                remaining = max(0, active.last - active.current_frame)
                self.monitor_stats["eta"].setText(self._fmt_duration(remaining * avg))
            else:
                self.monitor_stats["eta"].setText("—")
            self.monitor_stats["power"].setText(self._effective_power(active))
            now = getattr(active, "now_ram_gb", None)
            self.monitor_stats["nuke_ram"].setText(
                (f"{now:.1f} GB  ·  " if now is not None else "") + f"peak {active.peak_ram_gb:.1f} GB"
            )
            prot = "Active" if active.smart_protection else "Off"
            if active.last_frame_at and active.frame_durations:
                avg = sum(active.frame_durations) / len(active.frame_durations)
                stalled = (time.monotonic() - active.last_frame_at) > max(60.0, avg * 5)
                if stalled:
                    prot = "Possible stall"
            self.monitor_stats["protection"].setText(prot)
        else:
            for v in self.monitor_stats.values():
                v.setText("—")

    def _nuke_ram_gb(self, job):
        """Memory used by the Nuke process rendering this job (including its child processes)."""
        if psutil is None:
            return None
        proc = (getattr(self, "_procs", {}) or {}).get(job.id) or (
            self.current_process if self.current_job is job else None
        )
        if proc is None:
            return None
        try:
            pp = psutil.Process(proc.pid)
            rss = pp.memory_info().rss
            for c in pp.children(recursive=True):
                try:
                    rss += c.memory_info().rss
                except Exception:
                    pass
            return rss / (1024**3)
        except Exception:
            return None

    def _refresh_worker_panel(self, cpu=None, ram=None):
        if not hasattr(self, "worker_table"):
            return
        active = next((j for j in self.jobs if j.status == "Rendering"), None)
        vals = [
            "local",
            "Rendering" if active else "Idle",
            active.name if active else "—",
            f"{active.progress:.0f}%" if active else "0%",
            f"{cpu:.0f}%" if cpu is not None else "—",
            f"{ram:.0f}%" if ram is not None else "—",
            self._effective_power(active) if active else self.settings.get("default_render_power", "Normal"),
        ]
        for c, v in enumerate(vals):
            it = self.worker_table.item(0, c) or QtWidgets.QTableWidgetItem()
            it.setText(str(v))
            self.worker_table.setItem(0, c, it)
        self.worker_state_label.setText("RENDERING" if active else "IDLE")

    def _installed_nuke_versions(self):
        return find_nuke_installs()

    def _choose_detected_nuke(self, index):
        if index > 0:
            p = self.nuke_combo.itemData(index)
            if p:
                self.nuke_path.setText(p)
                self._save_state()
                self.nuke_combo.setToolTip(p)

    def _sync_nuke_combo(self):
        """Show the executable actually in use (adds it to the list if it was browsed manually)."""
        path = self.nuke_path.text().strip()
        self.nuke_combo.blockSignals(True)
        try:
            ix = self.nuke_combo.findData(path) if path else -1
            if path and ix < 0:
                self.nuke_combo.addItem(Path(path).stem + " (custom)", path)
                ix = self.nuke_combo.count() - 1
            self.nuke_combo.setCurrentIndex(max(0, ix))
            self.nuke_combo.setToolTip(path or "No Nuke executable set")
        finally:
            self.nuke_combo.blockSignals(False)

    def _add_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Add folder of Nuke scripts")
        if not folder:
            return
        paths = sorted(str(p) for p in Path(folder).rglob("*.nk"))
        new_jobs = []
        for p in paths:
            first, last, writes = read_nk_script(p)
            node = writes[0]["name"] if len(writes) == 1 else "(all)"
            j = RenderJob(
                p,
                first,
                last,
                node,
                writes,
                chunk_size=self.settings.get("default_chunk_size", 0),
                retry_limit=self.settings.get("default_retry_limit", 0),
                ram_reserve_gb=self.settings.get("default_ram_reserve_gb", 2),
                cpu_threshold=self.settings.get("default_cpu_threshold", 90),
            )
            j.render_power = self.settings.get("default_render_power", "Normal")
            self.jobs.append(j)
            new_jobs.append(j)
        self._ask_power_for_jobs(new_jobs)
        self._redraw_queue()
        self._save_state()
        self.statusBar().showMessage(f"Added {len(paths)} scripts")

    def _apply_filter(self, text):
        q = text.strip().lower()
        mode = self.status_filter.currentText() if hasattr(self, "status_filter") else "All"
        for r, j in enumerate(self.jobs):
            hay = f"{j.name} {j.path} {j.write_node} {j.output_path} {j.notes} {j.status}".lower()
            st = j.status
            active_states = (
                "Queued",
                "Rendering",
                "Suspended",
                "Blocked",
                "Waiting",
                "Waiting for Assets",
                "Waiting (resources)",
                "Tested",
                "Failed",
                "Stopped",
            )
            visible = not q or q in hay
            if mode == "Active":
                visible = visible and st in active_states
            elif mode == "Waiting":
                visible = visible and st in (
                    "Queued",
                    "Suspended",
                    "Blocked",
                    "Waiting",
                    "Waiting for Assets",
                    "Waiting (resources)",
                    "Tested",
                )
            elif mode == "Completed":
                visible = visible and st == "Done"
            elif mode == "Stopped":
                visible = visible and st == "Stopped"
            elif mode == "Failed":
                visible = visible and st == "Failed"
            self.table.setRowHidden(r, not visible)

    def _set_notes(self):
        idx = self._current_row()
        if idx is not None:
            self.jobs[idx].notes = self.notes_edit.text().strip()
            self._redraw_queue()
            self.table.selectRow(idx)
            self._save_state()

    def _set_job_nuke(self):
        idx = self._current_row()
        if (
            idx is not None
            and self.job_nuke.text().strip() != self.jobs[idx].nuke_exe
            and self._locked_guard([self.jobs[idx]], "change it", quiet=True)
        ):
            self.job_nuke.setText(self.jobs[idx].nuke_exe)
            return
        if idx is not None:
            self.jobs[idx].nuke_exe = self.job_nuke.text().strip()
            self._save_state()

    def _copy_job_settings(self):
        idx = self._current_row()
        if idx is None:
            return
        j = self.jobs[idx]
        self.clipboard_settings = {
            "first": j.first,
            "last": j.last,
            "write_node": j.write_node,
            "output_override": j.output_override,
            "notes": j.notes,
            "nuke_exe": j.nuke_exe,
            "chunk_size": j.chunk_size,
            "retry_limit": j.retry_limit,
            "job_priority": j.job_priority,
            "frame_order": j.frame_order,
            "render_power": j.render_power,
        }

    def _paste_job_settings(self):
        idx = self._current_row()
        if idx is None or not self.clipboard_settings:
            return
        j = self.jobs[idx]
        if self._locked_guard([j], "paste settings into it"):
            return
        settings = dict(self.clipboard_settings)
        names = [w.get("name") for w in j.write_nodes]
        if (
            settings.get("write_node") not in ("(all)", "all", "", None)
            and settings.get("write_node") not in names
        ):
            settings.pop("write_node")
            self.statusBar().showMessage(
                "Write node not pasted: this script has no Write with that name", 5000
            )
        if (
            settings.get("first", j.first),
            settings.get("last", j.last),
            settings.get("write_node", j.write_node),
        ) != (j.first, j.last, j.write_node):
            j.task_states = {}
            j.verified = None
        for k, v in settings.items():
            setattr(j, k, v)
        self._redraw_queue()
        self.table.selectRow(idx)
        self._save_state()

    def _clear_completed(self):
        if self._queue_running:
            return
        self.jobs = [j for j in self.jobs if j.status != "Done"]
        self._redraw_queue()
        self._save_state()

    def _render_selected(self):
        idx = self._current_row()
        if idx is not None:
            self._render_single(idx)

    def _pause_after_current(self):
        if self._queue_running:
            self.pause_after_job = True
            self._write_console(">> Queue will pause after the current job.")

    def _skip_current_job(self):
        if self._queue_running:
            self.skip_current = True

    def _sequence_info(self, job):
        out = job.output_path
        if not out or out.startswith("("):
            return None
        m = re.search(r"(#+|%0?(\d*)d)", out)
        if not m:
            return None
        width = len(m.group(1)) if m.group(1).startswith("#") else int(m.group(2) or 1)
        prefix, suffix = out[: m.start()], out[m.end() :]
        existing = []
        files = []
        for f in glob.glob(glob.escape(prefix) + "*" + glob.escape(suffix)):
            mm = re.match(
                re.escape(prefix.replace("\\", "/"))
                + r"(-?\d+)"
                + re.escape(suffix.replace("\\", "/"))
                + r"$",
                f.replace("\\", "/"),
            )
            if mm:
                existing.append(int(mm.group(1)))
                files.append(f)
        expected = (
            list(range(job.first, job.last + 1)) if job.first is not None and job.last is not None else []
        )
        missing = [f for f in expected if f not in set(existing)]
        return {
            "existing": existing,
            "expected": expected,
            "missing": missing,
            "width": width,
            "files": files,
        }

    def _verify_job_output(self, job, add_history=False):
        out = job.output_path or ""
        uncheckable = not out or out.startswith("(") or "[" in out or "$" in out
        info = None if uncheckable else self._sequence_info(job)
        if uncheckable:
            job.verified = None
            job.missing_frames = []
            job.zero_byte_frames = []
        elif info is None:
            out = job.output_path
            ok = bool(out and not out.startswith("(") and Path(out).exists())
            job.verified = ok
            job.missing_frames = []
        else:
            job.missing_frames = info["missing"]
            job.zero_byte_frames = []
            for fr, fp in zip(info["existing"], info["files"]):
                try:
                    if Path(fp).stat().st_size == 0:
                        job.zero_byte_frames.append(fr)
                except Exception:
                    pass
            job.verified = (
                len(job.missing_frames) == 0 and len(job.zero_byte_frames) == 0 and bool(info["expected"])
            )
        if add_history:
            elapsed = (time.monotonic() - job.render_started_at) if job.render_started_at else None
            self.history.append(
                {
                    "time": datetime.datetime.now().isoformat(timespec="seconds"),
                    "script": job.name,
                    "path": job.path,
                    "write": job.write_node,
                    "range": job.frames_label,
                    "output": job.output_path,
                    "duration": elapsed,
                    "status": "Verified"
                    if job.verified
                    else ("Incomplete" if job.verified is False else "Done (not verifiable)"),
                    "missing": list(job.missing_frames),
                    "zero_byte": list(job.zero_byte_frames),
                    "render_power": job.render_power,
                    "peak_ram_gb": round(job.peak_ram_gb, 2),
                    "avg_cpu": round(sum(job.avg_cpu_samples) / len(job.avg_cpu_samples), 1)
                    if job.avg_cpu_samples
                    else None,
                    "avg_frame": round(sum(job.frame_durations) / len(job.frame_durations), 3)
                    if job.frame_durations
                    else None,
                    "heavy_frames": list(job.heavy_frames[-20:]),
                }
            )
            job.completed_at = datetime.datetime.now().isoformat(timespec="seconds")
            self._save_state()
        return job.verified

    def _health_text(self, job):
        if (
            job.verified is None
            and job.output_path
            and (job.output_path.startswith("(") or "[" in job.output_path)
        ):
            return "Not checkable"
        if job.verified is True:
            return "Complete"
        if job.verified is False:
            if job.missing_frames:
                return f"Missing {len(job.missing_frames)}"
            if job.zero_byte_frames:
                return f"Zero-byte {len(job.zero_byte_frames)}"
            return "Output missing"
        return "Not checked"

    def _check_selected_output(self):
        idx = self._current_row()
        if idx is None:
            return
        j = self.jobs[idx]
        self._verify_job_output(j)
        self._update_properties()
        if j.zero_byte_frames:
            QtWidgets.QMessageBox.warning(
                self,
                "Sequence Check",
                f"Zero-byte frames detected:\n{self._compress_frames(j.zero_byte_frames)}",
            )
        elif j.missing_frames:
            ranges = self._compress_frames(j.missing_frames)
            QtWidgets.QMessageBox.warning(self, "Sequence Check", f"Missing frames:\n{ranges}")
        elif j.verified:
            QtWidgets.QMessageBox.information(self, "Sequence Check", "Output sequence is complete.")
        elif j.verified is None:
            QtWidgets.QMessageBox.information(
                self,
                "Sequence Check",
                "This output cannot be checked automatically (several Write nodes, or a TCL expression / variable in the path).",
            )
        else:
            QtWidgets.QMessageBox.warning(self, "Sequence Check", "Expected output could not be verified.")

    @staticmethod
    def _compress_frames(frames):
        if not frames:
            return "None"
        frames = sorted(set(frames))
        groups = []
        a = b = frames[0]
        for f in frames[1:]:
            if f == b + 1:
                b = f
            else:
                groups.append(str(a) if a == b else f"{a}-{b}")
                a = b = f
        groups.append(str(a) if a == b else f"{a}-{b}")
        return ", ".join(groups)

    def _preflight(self, jobs):
        issues = []
        for j in jobs:
            if not Path(j.path).exists():
                issues.append(f"Missing script: {j.path}")
            if j.first is not None and j.last is not None and j.first > j.last:
                issues.append(f"Invalid range: {j.name}")
            if not j.write_nodes:
                issues.append(f"No Write nodes detected: {j.name}")
            try:
                txt = _read_text(j.path)
                for block in re.findall(r"^Read\s*\{(.*?)^\}", txt, re.MULTILINE | re.DOTALL):
                    fm = re.search(r'^\s*file\s+["{]?([^"}\n]+)', block, re.MULTILINE)
                    if fm:
                        rp = fm.group(1).strip()
                        probe = re.sub(r'(#+|%0\d+d)', '*', rp)
                        if not any(ch in rp for ch in '[]$') and not glob.glob(probe):
                            issues.append(f"Missing Read media: {Path(rp).name} ({j.name})")
            except Exception:
                pass
            out = j.output_path
            if out and not out.startswith("("):
                try:
                    Path(out).expanduser().parent.mkdir(parents=True, exist_ok=True)
                except Exception:
                    issues.append(f"Cannot create output folder: {out}")
        if issues:
            msg = "PRE-RENDER CHECK\n\n" + "\n".join("! " + x for x in issues[:20]) + "\n\nContinue anyway?"
            return (
                QtWidgets.QMessageBox.question(
                    self, "Preflight", msg, QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No
                )
                == QtWidgets.QMessageBox.Yes
            )
        return True

    def _show_history(self):
        d = QtWidgets.QDialog(self)
        d.setWindowTitle("Render History")
        d.resize(900, 420)
        l = QtWidgets.QVBoxLayout(d)
        t = QtWidgets.QTableWidget(0, 7)
        t.setHorizontalHeaderLabels(["Time", "Script", "Write", "Frames", "Duration", "Status", "Output"])
        t.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        for h in reversed(self.history[-200:]):
            r = t.rowCount()
            t.insertRow(r)
            vals = [
                h.get("time", ""),
                h.get("script", ""),
                h.get("write", ""),
                h.get("range", ""),
                self._fmt_duration(h.get("duration")) if h.get("duration") else "—",
                h.get("state", ""),
                h.get("output", ""),
            ]
            for c, v in enumerate(vals):
                t.setItem(r, c, QtWidgets.QTableWidgetItem(str(v)))
        l.addWidget(t)
        d.exec()

    def _notify(self, title, message):
        if not self.settings.get("notifications_enabled", True):
            return
        try:
            if QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
                tray = getattr(self, "_notify_tray", None)
                if tray is None:
                    icon = self.windowIcon()
                    if icon.isNull():
                        icon = self.style().standardIcon(QtWidgets.QStyle.SP_ComputerIcon)
                    tray = QtWidgets.QSystemTrayIcon(icon, self)
                    tray.show()
                    self._notify_tray = tray
                tray.showMessage(title, message, QtWidgets.QSystemTrayIcon.Information, 5000)
        except Exception:
            pass

    def _post_render_power_action(self):
        if self.shutdown_action.isChecked():
            if sys.platform.startswith("win"):
                subprocess.Popen(["shutdown", "/s", "/t", "60"], **_hidden())
                self._notify("Shutdown scheduled", "Windows will shut down in 60 seconds.")
        elif self.sleep_action.isChecked():
            if sys.platform.startswith("win"):
                subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], **_hidden())

    def _current_row(self):
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return rows[0].row() if rows else None

    def _choose_nuke_executable(self):
        f, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Choose the Nuke executable", self.nuke_path.text()
        )
        if f:
            self.nuke_path.setText(f)
            self._save_state()
            self._sync_nuke_combo()

    def _add_script_files(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Add Nuke scripts", "", "Nuke scripts (*.nk);;All files (*.*)"
        )
        new_jobs = []
        for p in paths:
            first, last, writes = read_nk_script(p)
            node = writes[0]["name"] if len(writes) == 1 else "(all)"
            j = RenderJob(
                p,
                first,
                last,
                node,
                writes,
                chunk_size=self.settings.get("default_chunk_size", 0),
                retry_limit=self.settings.get("default_retry_limit", 0),
                ram_reserve_gb=self.settings.get("default_ram_reserve_gb", 2),
                cpu_threshold=self.settings.get("default_cpu_threshold", 90),
            )
            j.render_power = self.settings.get("default_render_power", "Normal")
            self.jobs.append(j)
            new_jobs.append(j)
        self._ask_power_for_jobs(new_jobs)
        self._redraw_queue()
        self._save_state()

    def _open_job_editor(self):
        idx = self._current_row()
        if idx is None:
            return
        if self._locked_guard([self.jobs[idx]], "edit it"):
            return
        job = self.jobs[idx]
        if not job.write_nodes:
            _, _, job.write_nodes = read_nk_script(job.path)
        dlg = JobEditorDialog(job, self)
        if dlg.exec() == QtWidgets.QDialog.Accepted:
            vals = dlg.values()
            if vals != (job.first, job.last, job.write_node):
                job.task_states = {}
                job.verified = None
            job.first, job.last, job.write_node = vals
            self._redraw_queue()
            self.table.selectRow(idx)
            self._save_state()

    def _remove_selected(self):
        rows = self._selected_rows()
        if not rows:
            return
        targets = [self.jobs[r] for r in rows]
        if self._locked_guard(targets, "remove it"):
            return
        if any(j.status == "Rendering" for j in targets):
            QtWidgets.QMessageBox.information(
                self, "Remove", "A job that is rendering cannot be removed. Stop it first."
            )
            return
        if self.settings.get("confirm_remove", False):
            if (
                QtWidgets.QMessageBox.question(
                    self, "Remove", f"Remove {len(targets)} job(s) from the queue?"
                )
                != QtWidgets.QMessageBox.Yes
            ):
                return
        self._snapshot_queue()
        ids = {j.id for j in targets}
        self.jobs = [j for j in self.jobs if j.id not in ids]
        self.table.clearSelection()
        self._redraw_queue()
        self._save_state()

    def _shift_selected(self, direction):
        idx = self._current_row()
        if idx is None:
            return
        ni = idx + direction
        if 0 <= ni < len(self.jobs):
            self.jobs[idx], self.jobs[ni] = self.jobs[ni], self.jobs[idx]
            self._redraw_queue()
            self.table.selectRow(ni)
            self._save_state()

    def _duplicate_job(self):
        idx = self._current_row()
        if idx is None:
            return
        j = self.jobs[idx]
        clone = RenderJob.from_dict(j.to_dict())
        clone.status = "Queued"
        clone.progress = 0.0
        clone.current_frame = None
        clone.completed_at = None
        clone.session_id = SESSION_ID
        clone.locked = False
        clone.id = uuid.uuid4().hex
        clone.task_states = {}
        clone.verified = None
        clone.error = ""
        self.jobs.insert(idx + 1, clone)
        self._redraw_queue()
        self.table.selectRow(idx + 1)
        self._save_state()

    def _refresh_selected_job(self):
        idx = self._current_row()
        if idx is None:
            return
        j = self.jobs[idx]
        if self._locked_guard([j], "refresh it"):
            return
        if j.status == "Rendering":
            QtWidgets.QMessageBox.information(self, "Refresh Script", "This job is rendering.")
            return
        self._update_job_from_script(j)
        self._write_console(f">> {j.name}: snapshot updated to the latest saved script.")
        self._redraw_queue()
        self.table.selectRow(idx)
        self._save_state()

    def _open_path(self, path):
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Open Location", str(e))

    def _open_script_location(self):
        idx = self._current_row()
        if idx is not None:
            self._open_path(Path(self.jobs[idx].path).parent)

    def _open_output_folder(self):
        idx = self._current_row()
        if idx is None:
            return
        out = self.jobs[idx].output_path
        if not out or out.startswith("("):
            QtWidgets.QMessageBox.information(
                self, "Output Folder", "No single output path is available for this job."
            )
            return
        self._open_path(Path(out).expanduser().parent)

    def _retry_failed(self):
        for j in self.jobs:
            if j.status in ("Failed", "Stopped", "Blocked"):
                j.status = "Queued"
                j.progress = 0.0
                j.error = ""
        self._redraw_queue()
        self._save_state()

    def _save_queue_as(self):
        f, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save Queue", "nuke_render_queue.json", "JSON (*.json)"
        )
        if f:
            try:
                Path(f).write_text(
                    json.dumps({"queue": [j.to_dict() for j in self.jobs]}, indent=2), encoding="utf-8"
                )
            except Exception as e:
                QtWidgets.QMessageBox.critical(self, "Save Queue", str(e))

    def _load_queue(self):
        if self._queue_running:
            QtWidgets.QMessageBox.information(
                self, "Load Queue", "Stop the queue before loading another one."
            )
            return
        f, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load Queue", "", "JSON (*.json)")
        if f:
            try:
                data = json.loads(Path(f).read_text(encoding="utf-8"))
                self.jobs = [RenderJob.from_dict(d) for d in data.get("queue", [])]
                self._redraw_queue()
                self._save_state()
            except Exception as e:
                QtWidgets.QMessageBox.critical(self, "Load Queue", str(e))

    def _show_context_menu(self, pos):
        if self.table.itemAt(pos) is None:
            return
        row = self.table.itemAt(pos).row()
        if row not in self._selected_rows():
            self.table.selectRow(row)
        m = QtWidgets.QMenu(self)
        # Most used actions at the top level, everything else under "More".
        m.addAction("Render Job", self._render_selected)
        m.addAction("Render Missing Frames", self._render_missing)
        m.addAction("Render Test Frame...", self._render_test_frame)
        m.addSeparator()
        m.addAction("Reveal Output", self._open_output_folder)
        m.addAction("Create Read in Nuke", self._create_read_in_nuke)
        m.addAction("Open Script in Nuke", self._open_script_in_nuke)
        m.addSeparator()
        m.addAction("Retry", self._retry_selected)
        m.addAction("Edit...", self._open_job_editor)
        m.addAction("Remove", self._remove_selected)
        more = m.addMenu("More")
        for entry in [
            ("Render First / Middle / Last", self._render_first_middle_last),
            ("Render From Here", self._render_from_here),
            None,
            ("Render Power / Resources...", self._edit_render_power),
            ("Scheduling / Tasks...", self._edit_scheduling),
            ("Multi-edit Selected...", self._multi_edit_selected),
            ("Edit Notes...", lambda: self._edit_queue_cell(self._current_row() or 0, 14)),
            None,
            ("Suspend / Resume", self._toggle_suspend),
            ("Lock / Unlock", self._toggle_lock_selected),
            ("Reset Job", self._reset_selected_jobs),
            ("Duplicate", self._duplicate_job),
            ("Move Up", lambda: self._shift_selected(-1)),
            ("Move Down", lambda: self._shift_selected(1)),
            None,
            ("Refresh Script (use latest save)", self._refresh_selected_job),
            ("Check Output", self._check_selected_output),
            ("Reveal Script", self._open_script_location),
            None,
            ("Copy Job Settings", self._copy_job_settings),
            ("Paste Job Settings", self._paste_job_settings),
            ("Copy Nuke Command", self._copy_nuke_command),
            None,
            ("Undo Last Queue Edit", self._undo_queue_edit),
        ]:
            if entry is None:
                more.addSeparator()
            else:
                more.addAction(*entry)
        m.exec(self.table.viewport().mapToGlobal(pos))

    # Create Read in Nuke
    def _send_to_nuke_sessions(self, msg):
        """Send a message to every open Nuke with the Batch Gartner menu. Returns [(port, reply)]."""
        base = int(self.settings.get("receiver_port", 54321)) + 1
        replies = []
        for port in range(base, base + 10):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.3) as sock:
                    sock.settimeout(15.0)
                    sock.sendall(json.dumps(msg).encode("utf-8"))
                    sock.shutdown(socket.SHUT_WR)
                    replies.append((port, sock.recv(4096).decode("utf-8", "replace")))
            except Exception:
                continue
        return replies

    def _create_read_in_nuke(self):
        j = self._selected_job()
        if j is None:
            return
        out = j.output_path or ""
        if not out or out.startswith("("):
            QtWidgets.QMessageBox.information(
                self,
                "Create Read in Nuke",
                "This job renders several Write nodes. Choose one Write node for the job first (double-click the Write column).",
            )
            return
        first, last = j.first, j.last
        r = self._existing_frames(j)
        if r and r["have"]:
            first, last = min(r["have"]), max(r["have"])  # frames that really exist
        msg = {
            "command": "create_read",
            "file": out,
            "first": first,
            "last": last,
            "script": j.path,
            "write": j.write_node,
            "override": bool(j.output_override),
        }
        replies = self._send_to_nuke_sessions(msg)
        done = [rep for _, rep in replies if rep.startswith("OK")]
        if not done and replies:
            port = replies[0][0]
            msg["force"] = True
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5) as sock:
                    sock.settimeout(15.0)
                    sock.sendall(json.dumps(msg).encode("utf-8"))
                    sock.shutdown(socket.SHUT_WR)
                    rep = sock.recv(4096).decode("utf-8", "replace")
                    if rep.startswith("OK"):
                        done = [rep + " (script not open — created in the first Nuke)"]
                    else:
                        replies = [(port, rep)]
            except Exception as e:
                replies = [(port, f"ERROR: {e}")]
        if done:
            self.statusBar().showMessage(f"Read created in Nuke: {done[0][3:]}", 6000)
            self._write_console(f">> Create Read in Nuke: {done[0][3:]}  ({out})")
        elif replies:
            QtWidgets.QMessageBox.warning(self, "Create Read in Nuke", "Nuke answered:\n" + replies[0][1])
        else:
            QtWidgets.QMessageBox.information(
                self,
                "Create Read in Nuke",
                "No open Nuke with the Batch Gartner menu was found.\n"
                "\n"
                "Open Nuke (run install_batch_gartner_integration.py once if the menu is missing) and try again.",
            )

    def _retry_selected(self):
        changed = 0
        for r in self._selected_rows():
            j = self.jobs[r]
            if j.locked or j.status not in ("Failed", "Stopped", "Blocked", "Tested"):
                continue
            j.status = "Queued"
            j.error = ""
            changed += 1
        if changed:
            self._redraw_queue()
            self._save_state()
            self.statusBar().showMessage(
                f"{changed} job(s) queued again — press START (failed/stopped jobs resume from the frames on disk)",
                5000,
            )
        else:
            self.statusBar().showMessage("Nothing to retry in the selection", 3000)

    # Job locking
    def _locked_guard(self, jobs, action, quiet=False):
        """True (and a message) when a locked job would be changed. Lock protects a job from edits,
        removal and re-rendering (e.g. an approved render)."""
        locked = [j for j in jobs if j is not None and j.locked]
        if not locked:
            return False
        msg = f"{locked[0].name} is locked. Unlock it (right-click > More > Lock / Unlock) to {action}."
        if quiet:
            self.statusBar().showMessage(msg, 5000)
        else:
            QtWidgets.QMessageBox.information(self, "Job Locked", msg)
        return True

    def _selected_job(self):
        idx = self._current_row()
        return self.jobs[idx] if idx is not None and 0 <= idx < len(self.jobs) else None

    def _toggle_suspend(self):
        for r in self._selected_rows():
            j = self.jobs[r]
            if j.status == "Rendering" or j.locked:
                continue
            j.suspended = not j.suspended
            if j.suspended:
                j.status = "Suspended"
            elif j.status != "Done":
                j.status = "Queued"
        self._redraw_queue()
        self._save_state()

    def _set_work_mode(self, checked):
        if hasattr(self, "work_mode_toolbar") and self.work_mode_toolbar.isChecked() != checked:
            self.work_mode_toolbar.blockSignals(True)
            self.work_mode_toolbar.setChecked(checked)
            self.work_mode_toolbar.blockSignals(False)
        self.settings["render_while_work"] = bool(checked)
        write_settings(self.settings)
        running = {j.id: j for j in self.jobs}
        for jid, proc in list((getattr(self, "_procs", {}) or {}).items()):
            if jid in running:
                self._apply_process_power(proc, running[jid])
        self._write_console(
            ">> Render While I Work: "
            + ("ON — conservative local scheduling." if checked else "OFF — per-job Render Power applies.")
        )

    def _power_values_dialog(self, job, title=None):
        d = QtWidgets.QDialog(self)
        d.setWindowTitle(title or ("Render Power — " + job.name))
        f = QtWidgets.QFormLayout(d)
        preset = QtWidgets.QComboBox()
        preset.addItems(["Low", "Normal", "High", "Maximum", "Custom"])
        preset.setCurrentText(job.render_power)
        ram = QtWidgets.QDoubleSpinBox()
        ram.setRange(0, 256)
        ram.setDecimals(1)
        ram.setSuffix(" GB free")
        ram.setValue(job.ram_reserve_gb)
        cpu = QtWidgets.QSpinBox()
        cpu.setRange(10, 100)
        cpu.setSuffix(" %")
        cpu.setValue(job.cpu_threshold)
        protect = QtWidgets.QCheckBox("Hold new work when resources are low")
        protect.setChecked(job.smart_protection)
        info = QtWidgets.QLabel(
            "Low keeps the PC responsive. High/Maximum raise Nuke's OS process priority.\n"
            "Resource thresholds protect RAM/CPU; they do not fake-limit Nuke to an exact percentage."
        )
        info.setWordWrap(True)
        info.setObjectName("muted")

        def defaults(name):
            vals = {"Low": (4, 70), "Normal": (2, 90), "High": (1, 97), "Maximum": (0, 100)}
            if name in vals:
                a, b = vals[name]
                ram.setValue(a)
                cpu.setValue(b)

        preset.currentTextChanged.connect(defaults)
        f.addRow("Render Power", preset)
        f.addRow("Keep RAM free", ram)
        f.addRow("CPU threshold", cpu)
        f.addRow("Protection", protect)
        f.addRow(info)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        f.addRow(bb)
        bb.accepted.connect(d.accept)
        bb.rejected.connect(d.reject)
        if d.exec() != QtWidgets.QDialog.Accepted:
            return False
        job.render_power = preset.currentText()
        job.ram_reserve_gb = ram.value()
        job.cpu_threshold = cpu.value()
        job.smart_protection = protect.isChecked()
        return True

    def _edit_render_power(self):
        idx = self._current_row()
        if idx is None:
            return
        if self._locked_guard([self.jobs[idx]], "change it"):
            return
        if self._power_values_dialog(self.jobs[idx]):
            self._redraw_queue()
            self._save_state()

    # ------------------------------------------------------------------
    # Script snapshots: the queue renders the script as it was when queued,
    # so you can keep editing and saving it in Nuke.
    # ------------------------------------------------------------------
    def _snapshots_enabled(self):
        return bool(self.settings.get("snapshot_scripts", True))

    def _take_snapshot(self, job):
        if not self._snapshots_enabled():
            return False
        try:
            src = Path(job.path)
            if not src.is_file():
                return False
            folder = SNAPSHOT_DIR / uuid.uuid4().hex
            folder.mkdir(parents=True, exist_ok=True)
            dst = folder / src.name
            shutil.copy2(src, dst)
            job.snapshot_path = str(dst)
            job.snapshot_at = time.time()
            return True
        except Exception as e:
            self._write_console(f"!! Could not snapshot {job.name}: {e} — the live script will be rendered.")
            return False

    def _snapshot_jobs(self, jobs):
        for j in jobs:
            self._take_snapshot(j)

    def _has_snapshot(self, job):
        return bool(job.snapshot_path) and Path(job.snapshot_path).is_file()

    def _script_changed_since_snapshot(self, job):
        if not self._has_snapshot(job):
            return False
        try:
            return Path(job.path).stat().st_mtime > job.snapshot_at + 1.0
        except Exception:
            return False

    def _snapshot_text(self, job):
        if not self._snapshots_enabled():
            return "Off (renders the live script)"
        if not self._has_snapshot(job):
            return "None yet (taken at render start)"
        when = datetime.datetime.fromtimestamp(job.snapshot_at).strftime("%d %b %H:%M")
        return (
            f"{when} — script saved since"
            if self._script_changed_since_snapshot(job)
            else f"{when} — up to date"
        )

    def _prepare_snapshots_for_render(self, jobs):
        """Before a render: snapshot jobs that have none, and ask what to do if scripts were saved since queuing.
        Returns False if the user cancels."""
        if not self._snapshots_enabled():
            return True
        for j in jobs:
            if not self._has_snapshot(j):
                self._take_snapshot(j)
        changed = [j for j in jobs if self._script_changed_since_snapshot(j)]
        if not changed:
            return True
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Question)
        box.setWindowTitle("Scripts Saved Since Queued")
        names = "\n".join(
            f"• {j.name}  (queued {datetime.datetime.fromtimestamp(j.snapshot_at).strftime('%H:%M')})"
            for j in changed[:12]
        )
        box.setText(
            f"{len(changed)} script(s) were saved after they were queued:\n"
            f"\n"
            f"{names}\n"
            f"\n"
            f"Which version should be rendered?"
        )
        keep = box.addButton("Render Queued Versions", QtWidgets.QMessageBox.AcceptRole)
        latest = box.addButton("Use Latest Saved", QtWidgets.QMessageBox.ActionRole)
        box.addButton(QtWidgets.QMessageBox.Cancel)
        box.setDefaultButton(keep)
        box.exec()
        clicked = box.clickedButton()
        if clicked == latest:
            for j in changed:
                self._update_job_from_script(j)
            self._write_console(f">> Updated {len(changed)} snapshot(s) to the latest saved script.")
            return True
        return clicked == keep

    def _update_job_from_script(self, j):
        """Re-read the script and take a fresh snapshot (Refresh Script)."""
        first, last, writes = read_nk_script(j.path)
        if first is not None:
            j.first = first
        if last is not None:
            j.last = last
        j.write_nodes = writes
        if (
            j.write_node not in [w["name"] for w in writes]
            and j.write_node not in ("(all)", "all", "")
            and "." not in j.write_node
        ):
            j.write_node = writes[0]["name"] if len(writes) == 1 else "(all)"
        j.task_states = {}
        j.verified = None
        self._take_snapshot(j)

    def _cleanup_snapshots(self):
        """Delete snapshot folders no job refers to any more (run at startup)."""
        try:
            if not SNAPSHOT_DIR.exists():
                return
            used = {str(Path(j.snapshot_path).parent) for j in self.jobs if j.snapshot_path}
            for folder in SNAPSHOT_DIR.iterdir():
                if folder.is_dir() and str(folder) not in used:
                    shutil.rmtree(folder, ignore_errors=True)
        except Exception:
            pass

    def _ask_power_for_jobs(self, jobs):
        if not jobs:
            return
        self._snapshot_jobs(jobs)  # every "add jobs" path goes through here
        template = jobs[0]
        if self._power_values_dialog(template, "Render Power — New Render"):
            for j in jobs[1:]:
                j.render_power = template.render_power
                j.ram_reserve_gb = template.ram_reserve_gb
                j.cpu_threshold = template.cpu_threshold
                j.smart_protection = template.smart_protection

    def _effective_power(self, job):
        return "Low" if bool(self.settings.get("render_while_work", False)) else job.render_power

    def _apply_process_power(self, proc, job):
        if psutil is None:
            return
        try:
            pp = psutil.Process(proc.pid)
            power = self._effective_power(job)
            if sys.platform.startswith("win"):
                levels = {
                    "Low": psutil.BELOW_NORMAL_PRIORITY_CLASS,
                    "Normal": psutil.NORMAL_PRIORITY_CLASS,
                    "High": psutil.ABOVE_NORMAL_PRIORITY_CLASS,
                    "Maximum": psutil.HIGH_PRIORITY_CLASS,
                    "Custom": psutil.NORMAL_PRIORITY_CLASS,
                }
                pp.nice(levels.get(power, psutil.NORMAL_PRIORITY_CLASS))
            else:
                pp.nice({"Low": 10, "Normal": 0, "High": -5, "Maximum": -10, "Custom": 0}.get(power, 0))
        except Exception:
            pass

    def _resource_gate(self, job, quiet=False):
        if not job.smart_protection or psutil is None:
            return True
        try:
            vm = psutil.virtual_memory()
            free = vm.available / (1024**3)
            cpu = psutil.cpu_percent(interval=0.15)
            power = self._effective_power(job)
            cpu_limit = min(job.cpu_threshold, 70 if power == "Low" else job.cpu_threshold)
            if free < job.ram_reserve_gb or cpu > cpu_limit:
                if not quiet:
                    self._events.put(
                        (
                            "console",
                            f">> Resource protection: waiting until {job.ram_reserve_gb:.0f} GB RAM is free and CPU < {cpu_limit}%"
                            f" (now {free:.1f} GB free, CPU {cpu:.0f}%). Change this in Render Power / Resources.",
                        )
                    )
                return False
        except Exception:
            pass
        return True

    def _edit_scheduling(self):
        idx = self._current_row()
        if idx is None:
            return
        if self._locked_guard([self.jobs[idx]], "change it"):
            return
        j = self.jobs[idx]
        d = QtWidgets.QDialog(self)
        d.setWindowTitle("Scheduling / Tasks — " + j.name)
        f = QtWidgets.QFormLayout(d)
        pri = QtWidgets.QSpinBox()
        pri.setRange(0, 100)
        pri.setValue(j.job_priority)
        state = QtWidgets.QComboBox()
        state.addItems(["Queued", "Suspended"])
        state.setCurrentText("Suspended" if j.suspended else "Queued")
        dep = QtWidgets.QComboBox()
        dep.addItem("None", "")
        for x in self.jobs:
            if x is not j:
                dep.addItem(x.name, x.path)
        di = dep.findData(j.dependency_path)
        dep.setCurrentIndex(max(0, di))
        assets = QtWidgets.QCheckBox("Wait until detected Read inputs are available")
        assets.setChecked(j.require_assets)
        order = QtWidgets.QComboBox()
        order.addItems(["First → Last", "Last → First", "First / Middle / Last → Remaining"])
        order.setCurrentText(j.frame_order)
        timeout = QtWidgets.QSpinBox()
        timeout.setRange(0, 1440)
        timeout.setSuffix(" min (0 = off)")
        timeout.setValue(j.task_timeout_min)
        chunk = QtWidgets.QSpinBox()
        chunk.setRange(0, 10000)
        chunk.setSuffix(" frames (0 = off)")
        chunk.setValue(j.chunk_size)
        retry = QtWidgets.QSpinBox()
        retry.setRange(0, 10)
        retry.setValue(j.retry_limit)
        schedule = QtWidgets.QDateTimeEdit()
        schedule.setCalendarPopup(True)
        schedule.setDisplayFormat("yyyy-MM-dd HH:mm")
        schedule.setSpecialValueText("Start immediately")
        schedule.setMinimumDateTime(QtCore.QDateTime.currentDateTime().addSecs(-60))
        schedule.setDateTime(
            QtCore.QDateTime.fromString(j.schedule_at, QtCore.Qt.ISODate)
            if j.schedule_at
            else schedule.minimumDateTime()
        )
        f.addRow("Priority", pri)
        f.addRow("Start at", schedule)
        f.addRow("Initial state", state)
        f.addRow("Depends on", dep)
        f.addRow("Required assets", assets)
        f.addRow("Frame order", order)
        f.addRow("Task timeout", timeout)
        f.addRow("Chunk size", chunk)
        f.addRow("Retry task", retry)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        f.addRow(bb)
        bb.accepted.connect(d.accept)
        bb.rejected.connect(d.reject)
        if d.exec() != QtWidgets.QDialog.Accepted:
            return
        j.job_priority = pri.value()
        j.suspended = state.currentText() == "Suspended"
        j.dependency_path = dep.currentData() or ""
        j.require_assets = assets.isChecked()
        j.frame_order = order.currentText()
        j.task_timeout_min = timeout.value()
        j.chunk_size = chunk.value()
        j.retry_limit = retry.value()
        j.schedule_at = (
            schedule.dateTime().toString(QtCore.Qt.ISODate)
            if schedule.dateTime() > schedule.minimumDateTime().addSecs(60)
            else ""
        )
        j.status = "Suspended" if j.suspended else "Queued"
        self._redraw_queue()
        self._save_state()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and any(
            u.toLocalFile().lower().endswith(".nk") or Path(u.toLocalFile()).is_dir()
            for u in event.mimeData().urls()
        ):
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = []
        for u in event.mimeData().urls():
            fp = u.toLocalFile()
            if not fp:
                continue
            if Path(fp).is_dir():
                paths.extend(str(x) for x in sorted(Path(fp).rglob("*.nk")))
            elif fp.lower().endswith(".nk"):
                paths.append(fp)
        new_jobs = []
        for fp in paths:
            if any(j.path == fp for j in self.jobs):
                continue
            first, last, writes = read_nk_script(fp)
            node = writes[0]["name"] if len(writes) == 1 else "(all)"
            j = RenderJob(
                fp,
                first,
                last,
                node,
                writes,
                chunk_size=self.settings.get("default_chunk_size", 0),
                retry_limit=self.settings.get("default_retry_limit", 0),
                ram_reserve_gb=self.settings.get("default_ram_reserve_gb", 2),
                cpu_threshold=self.settings.get("default_cpu_threshold", 90),
            )
            j.render_power = self.settings.get("default_render_power", "Normal")
            self.jobs.append(j)
            new_jobs.append(j)
        if new_jobs:
            self._ask_power_for_jobs(new_jobs)
            self._redraw_queue()
            self._save_state()
            self._write_console(f">> Drag/drop: added {len(new_jobs)} script(s).")
        event.acceptProposedAction()

    def _selected_rows(self):
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        if not rows:
            i = self._current_row()
            rows = [] if i is None else [i]
        return rows

    def _multi_edit_selected(self):
        rows = self._selected_rows()
        if not rows:
            return
        d = QtWidgets.QDialog(self)
        d.setWindowTitle(f"Multi-edit — {len(rows)} jobs")
        f = QtWidgets.QFormLayout(d)
        enabled = QtWidgets.QComboBox()
        enabled.addItems(["Keep", "Enable", "Disable"])
        chunk = QtWidgets.QComboBox()
        chunk.setEditable(True)
        chunk.addItems(["Keep", "Off", "25", "50", "100"])
        retry = QtWidgets.QComboBox()
        retry.addItems(["Keep", "0", "1", "2"])
        power = QtWidgets.QComboBox()
        power.addItems(["Keep", "Low", "Normal", "High", "Maximum", "Custom"])
        priority = QtWidgets.QComboBox()
        priority.setEditable(True)
        priority.addItems(["Keep", "25", "50", "75", "100"])
        status = QtWidgets.QComboBox()
        status.addItems(["Keep", "Queued", "Suspended", "Stopped"])
        nuke = QtWidgets.QLineEdit()
        nuke.setPlaceholderText("Keep current (or enter executable path)")
        f.addRow("Enabled", enabled)
        f.addRow("Status", status)
        f.addRow("Priority", priority)
        f.addRow("Render Power", power)
        f.addRow("Chunk size", chunk)
        f.addRow("Retry failures", retry)
        f.addRow("Nuke executable", nuke)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        f.addRow(bb)
        bb.accepted.connect(d.accept)
        bb.rejected.connect(d.reject)
        if d.exec() != QtWidgets.QDialog.Accepted:
            return
        for r in rows:
            j = self.jobs[r]
            if j.locked:
                continue
            if enabled.currentText() == "Enable":
                j.enabled = True
            elif enabled.currentText() == "Disable":
                j.enabled = False
            c = chunk.currentText().strip()
            if c != "Keep":
                j.chunk_size = 0 if c == "Off" else max(0, int(c or 0))
            if retry.currentText() != "Keep":
                j.retry_limit = int(retry.currentText())
            if power.currentText() != "Keep":
                j.render_power = power.currentText()
            if priority.currentText() != "Keep":
                try:
                    j.job_priority = max(0, min(100, int(priority.currentText())))
                except ValueError:
                    pass
            if status.currentText() != "Keep":
                j.status = status.currentText()
            if nuke.text().strip():
                j.nuke_exe = nuke.text().strip()
        self._redraw_queue()
        self._save_state()

    def _render_test_frame(self):
        idx = self._current_row()
        if idx is None:
            return
        j = self.jobs[idx]
        if j.first is None or j.last is None:
            QtWidgets.QMessageBox.information(self, "Test Frame", "The script has no detected frame range.")
            return
        default = (j.first + j.last) // 2
        frame, ok = QtWidgets.QInputDialog.getInt(
            self, "Render Test Frame", "Frame", default, j.first, j.last
        )
        if ok:
            self._set_frame_spec_and_render([frame])

    def _render_first_middle_last(self):
        idx = self._current_row()
        if idx is None:
            return
        j = self.jobs[idx]
        if j.first is None or j.last is None:
            return
        self._set_frame_spec_and_render(sorted(set([j.first, (j.first + j.last) // 2, j.last])))

    def _classify_error(self, text):
        lines = [l.strip().lower() for l in (text or "").splitlines() if l.strip()]
        # Only lines that report a problem; Nuke's startup banner mentions "license" on every run.
        err = [
            l
            for l in lines
            if any(w in l for w in ("error", "traceback", "exception", "failed", "cannot", "can't", "unable"))
        ] or lines[-15:]

        def has(*needles, where=None):
            return any(n in l for l in (where or err) for n in needles)

        if has("out of memory", "bad allocation", "bad_alloc", "cuda error"):
            return "Out of Memory"
        if any(
            ("cannot open" in l or "can't open" in l or "permission denied" in l) and "write" in l
            for l in err
        ) or has("write error", "cannot write", "disk full", "no space left"):
            return "Write Error (output file locked / not writable)"
        if has("no such file", "file not found", "unable to open", "cannot open"):
            return "Missing File"
        if has("unknown command", "plugin", "gizmo"):
            return "Plugin / Gizmo"
        if any(
            ("license" in l or "licence" in l)
            and any(w in l for w in ("error", "fail", "unavailable", "no license", "checkout", "expired"))
            for l in err
        ):
            return "License"
        if has("task timeout"):
            return "Timeout"
        return "Nuke Error"

    def _dependency_report(self, job):
        try:
            text = _read_text(job.path)
        except Exception as e:
            return {"reads": 0, "missing": [], "gizmos": [], "fonts": [], "error": str(e)}
        reads = []
        for m in re.finditer(r"^(?:Read|ReadGeo\d*|Camera\d*)\s*\{(.*?)^\}", text, re.M | re.S):
            fm = re.search(r"^\s*file\s+(.+)$", m.group(1), re.M)
            if fm:
                reads.append(fm.group(1).strip().strip('"'))
        missing = []
        for x in reads:
            # Only validate literal, non-expression paths. Sequence tokens are checked by parent folder.
            if "[" in x or "$" in x:
                continue
            probe = re.sub(r"%0?\d*d|#+", "", x)
            target = Path(probe)
            check = target.parent if ("%" in x or "#" in x) else target
            if not check.exists():
                missing.append(x)
        gizmos = sorted(
            set(re.findall(r"^([A-Za-z_]\w*)\s*\{", text, re.M))
            - {"Root", "Read", "Write", "Write2", "DeepWrite", "Group", "BackdropNode", "Viewer"}
        )
        fonts = sorted(set(re.findall(r"font\s+\"([^\"]+)\"", text)))
        return {"reads": len(reads), "missing": missing, "gizmos": gizmos[:30], "fonts": fonts}

    def _preflight_selected(self):
        idx = self._current_row()
        if idx is None:
            return
        j = self.jobs[idx]
        r = self._dependency_report(j)
        disk = "N/A"
        try:
            op = j.output_path
            if op and not op.startswith("("):
                folder = Path(re.sub(r"%0?\d*d|#+", "", op)).parent
                base = next((x for x in [folder, *folder.parents] if x.exists()), None)
                if base:
                    free = shutil.disk_usage(base).free / (1024**3)
                    disk = f"{free:.1f} GB free"
        except Exception:
            pass
        missing = "\n".join(r["missing"][:12]) or "None detected"
        QtWidgets.QMessageBox.information(
            self,
            "Preflight — " + j.name,
            f"Read dependencies: {r['reads']}\n"
            f"Missing paths: {len(r['missing'])}\n"
            f"Disk: {disk}\n"
            f"Fonts referenced: {len(r['fonts'])}\n"
            f"\n"
            f"Missing:\n"
            f"{missing}",
        )

    def _open_script_in_nuke(self):
        idx = self._current_row()
        if idx is None:
            return
        j = self.jobs[idx]
        exe = j.nuke_exe.strip() or self.nuke_path.text().strip()
        if exe and Path(exe).exists():
            subprocess.Popen([exe, j.path])

    def _copy_nuke_command(self):
        idx = self._current_row()
        if idx is None:
            return
        j = self.jobs[idx]
        exe = self._nuke_for(j) or "nuke"
        try:
            cmd = self._build_command(
                j,
                exe,
                getattr(j, "_frame_spec", None)
                or (f"{j.first}-{j.last}" if j.first is not None and j.last is not None else None),
            )
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Copy Nuke Command", str(e))
            return
        QtWidgets.QApplication.clipboard().setText(subprocess.list2cmdline(cmd))
        self.statusBar().showMessage("Nuke command copied", 3000)

    def _estimated_output_size(self, job):
        try:
            info = self._sequence_info(job)
            files = info.get("files", []) if isinstance(info, dict) else []
            sizes = []
            for f in files[:20]:
                try:
                    sizes.append(Path(f).stat().st_size)
                except Exception:
                    pass
            if not sizes or job.first is None or job.last is None:
                return "N/A"
            est = (sum(sizes) / len(sizes)) * max(1, job.last - job.first + 1)
            units = ["B", "KB", "MB", "GB", "TB"]
            u = 0
            while est >= 1024 and u < len(units) - 1:
                est /= 1024
                u += 1
            return f"~{est:.1f} {units[u]}"
        except Exception:
            return "N/A"

    def _show_job_inspector(self):
        idx = self._current_row()
        if idx is None:
            return
        j = self.jobs[idx]
        d = QtWidgets.QDialog(self)
        d.setWindowTitle("Job Inspector — " + j.name)
        d.resize(680, 500)
        l = QtWidgets.QVBoxLayout(d)
        tabs = QtWidgets.QTabWidget()
        l.addWidget(tabs)
        avg = sum(j.frame_durations) / len(j.frame_durations) if j.frame_durations else 0
        general = QtWidgets.QPlainTextEdit()
        general.setReadOnly(True)
        general.setPlainText(
            f"Script: {j.path}\n"
            f"Write: {j.write_node}\n"
            f"Frames: {j.frames_label}\n"
            f"Status: {j.status}\n"
            f"Priority: {j.job_priority}\n"
            f"Frame order: {j.frame_order}\n"
            f"Progress: {j.progress:.1f}%\n"
            f"Current frame: {j.current_frame or '—'}\n"
            f"Average / frame: {avg:.2f}s"
            if avg
            else f"Script: {j.path}\n"
            f"Write: {j.write_node}\n"
            f"Frames: {j.frames_label}\n"
            f"Status: {j.status}\n"
            f"Priority: {j.job_priority}\n"
            f"Frame order: {j.frame_order}\n"
            f"Progress: {j.progress:.1f}%\n"
            f"Current frame: {j.current_frame or '—'}\n"
            f"Average / frame: —"
        )
        tabs.addTab(general, "GENERAL")
        tasks = QtWidgets.QPlainTextEdit()
        tasks.setReadOnly(True)
        specs = self._frame_chunks(j)
        lines = []
        for n, spec in enumerate(specs, 1):
            lines.append(f"{n:03d}  {spec or j.frames_label:18}  {j.task_states.get(str(spec), 'QUEUED')}")
        tasks.setPlainText("TASK     FRAMES              STATUS\n" + "-" * 48 + "\n" + "\n".join(lines))
        tabs.addTab(tasks, "TASKS")
        power = QtWidgets.QPlainTextEdit()
        power.setReadOnly(True)
        power.setPlainText(
            f"Preset: {j.render_power}\n"
            f"Effective power: {self._effective_power(j)}\n"
            f"RAM reserve: {j.ram_reserve_gb:.1f} GB\n"
            f"CPU scheduling threshold: {j.cpu_threshold}%\n"
            f"Smart resource protection: {'On' if j.smart_protection else 'Off'}\n"
            f"Peak RAM recorded: {j.peak_ram_gb:.1f} GB"
        )
        tabs.addTab(power, "POWER")
        out = QtWidgets.QPlainTextEdit()
        out.setReadOnly(True)
        rr = self._dependency_report(j)
        out.setPlainText(
            (j.output_path or "No output detected") + f"\n"
            f"\n"
            f"Estimated completed size: {self._estimated_output_size(j)}\n"
            f"Read dependencies: {rr['reads']}\n"
            f"Missing inputs: {len(rr['missing'])}"
            + ("\n\n" + "\n".join(rr['missing']) if rr['missing'] else "")
        )
        tabs.addTab(out, "OUTPUT")
        hist = QtWidgets.QPlainTextEdit()
        hist.setReadOnly(True)
        past = [h for h in self.history if h.get("path") == j.path or h.get("name") == j.name]
        if past:
            lines = []
            for h in past[-20:][::-1]:
                lines.append(
                    f"{h.get('time', '')}   {h.get('status', '')}   {self._fmt_duration(h.get('duration'))}   {h.get('range', '')}"
                )
            hist.setPlainText("Previous renders: " + str(len(past)) + "\n\n" + "\n".join(lines))
        else:
            hist.setPlainText("No previous render history for this job yet.")
        if j.attempt_history:
            hist.appendPlainText(
                "\n\nTASK ATTEMPTS\n"
                + "\n".join(
                    f"{a.get('time', '')}  {a.get('task', '')}  try {a.get('attempt', 1)}  rc={a.get('returncode', '')}  {a.get('duration', 0):.1f}s"
                    for a in j.attempt_history[-30:][::-1]
                )
            )
        tabs.addTab(hist, "HISTORY")
        lg = QtWidgets.QPlainTextEdit()
        lg.setReadOnly(True)
        logp = LOG_DIR / (Path(j.path).stem + ".log")
        try:
            lg.setPlainText(
                logp.read_text(errors="ignore")[-30000:] if logp.exists() else "No render log yet."
            )
        except Exception:
            lg.setPlainText("Unable to read log.")
        tabs.addTab(lg, "LOG")
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        bb.rejected.connect(d.reject)
        bb.clicked.connect(d.accept)
        l.addWidget(bb)
        d.exec()

    def _frame_chunks(self, job, frame_spec=None):
        if frame_spec:
            return [frame_spec]
        if not job.chunk_size or job.first is None or job.last is None:
            return [f"{job.first}-{job.last}" if job.first is not None and job.last is not None else None]
        out = []
        a = job.first
        while a <= job.last:
            b = min(job.last, a + job.chunk_size - 1)
            out.append(f"{a}-{b}")
            a = b + 1
        if job.frame_order == "Last → First":
            out.reverse()
        elif job.frame_order == "First / Middle / Last → Remaining" and len(out) > 2:
            picks = [out[0], out[len(out) // 2], out[-1]]
            out = picks + [x for x in out if x not in picks]
        return out

    def _progress_for_frame(self, job, frame):
        """Return overall job progress from the frame Nuke is currently rendering."""
        spec = getattr(job, "_frame_spec", None)
        base = getattr(job, "_resume_base", 0) or 0
        if spec:
            frames = []
            for part in str(spec).split(","):
                part = part.strip()
                if not part:
                    continue
                m = re.match(r"^(-?\d+)\s*-\s*(-?\d+)$", part)
                if m:
                    a, b = int(m.group(1)), int(m.group(2))
                    step = 1 if b >= a else -1
                    frames.extend(range(a, b + step, step))
                elif re.match(r"^-?\d+$", part):
                    frames.append(int(part))
            if frames:
                try:
                    return max(
                        0.0, min(100.0, 100.0 * (base + frames.index(int(frame)) + 1) / (base + len(frames)))
                    )
                except ValueError:
                    pass
        if job.first is not None and job.last is not None:
            first, last, frame = int(job.first), int(job.last), int(frame)
            total = abs(last - first) + 1
            if total > 0:
                done = (frame - first + 1) if last >= first else (first - frame + 1)
                return max(0.0, min(100.0, 100.0 * done / total))
        return job.progress

    def _update_properties(self):
        idx = self._current_row()
        vals = {k: "—" for k in self.prop_labels}
        self.output_edit.blockSignals(True)
        if idx is not None:
            j = self.jobs[idx]
            current = str(j.current_frame) if j.current_frame is not None else "—"
            if j.current_frame is not None and j.last is not None:
                current = f"{j.current_frame} / {j.last}"
            fin = self._finish_text(j)
            parts = [f"{j.progress:.0f}%"]
            if j.current_frame is not None:
                parts.append("frame " + current)
            if fin not in ("—", ""):
                parts.append(("finish " if j.status == "Rendering" else "done ") + fin)
            current = "   ·   ".join(parts)
            vals = {
                "script": j.path,
                "snapshot": self._snapshot_text(j),
                "range": j.frames_label,
                "write": j.write_node,
                "current": current,
                "status": j.status,
            }
            if not self.output_edit.hasFocus():
                self.output_edit.setText(j.output_path if not j.output_path.startswith("(") else "")
            self.output_edit.setToolTip(
                "Override active" if j.output_override else "Using the Write node path saved in the .nk"
            )
            if not self.notes_edit.hasFocus():
                self.notes_edit.setText(j.notes)
            if not self.job_nuke.hasFocus():
                self.job_nuke.setText(j.nuke_exe)
            self.chunk_label.setText(str(j.chunk_size) + " frames" if j.chunk_size else "Off")
            self.retry_label.setText(str(j.retry_limit))
            self.power_label.setText(j.render_power)
            self.health_label.setText(self._health_text(j))
        else:
            self.output_edit.clear()
            self.notes_edit.clear()
            self.job_nuke.clear()
            self.health_label.setText("Not checked")
        for k, w in self.prop_labels.items():
            w.setText(vals[k])
        if hasattr(self, "frame_strip"):
            self.frame_strip.setJob(self.jobs[idx] if idx is not None else None)
        self._refresh_preview()
        self._update_tab_badges()
        self._refresh_management_views(idx)
        self.output_edit.blockSignals(False)

    def _refresh_management_views(self, idx=None):
        if not hasattr(self, "task_table"):
            return
        if idx is None:
            idx = self._current_row()
        self.task_table.setRowCount(0)
        if idx is None or not (0 <= idx < len(self.jobs)):
            self.details_view.setPlainText("Select a job to inspect its render details.")
            self.errors_view.setPlainText("No job selected.")
            self.performance_view.setPlainText("No job selected.")
            self.output_view.setPlainText("No job selected.")
            return
        j = self.jobs[idx]
        specs = self._frame_chunks(j)
        for n, spec in enumerate(specs, 1):
            r = self.task_table.rowCount()
            self.task_table.insertRow(r)
            state = j.task_states.get(str(spec), "QUEUED")
            current = str(j.current_frame) if state == "RENDERING" and j.current_frame is not None else ""
            info = "Active" if state == "RENDERING" else ("Needs attention" if state == "FAILED" else "")
            # A task-level progress strip makes chunks readable at a glance.
            tpct = 100 if state == "COMPLETE" else 0
            if state == "RENDERING" and j.current_frame is not None:
                try:
                    a, b = (spec.split("-", 1) + [spec])[:2]
                    a = int(a)
                    b = int(b)
                    tpct = max(0, min(99, int((j.current_frame - a + 1) * 100 / max(1, b - a + 1))))
                except Exception:
                    tpct = int(j.progress)
            vals = (f"{n:03d}", spec or j.frames_label, state, tpct, current, info)
            for c, v in enumerate(vals):
                it = QtWidgets.QTableWidgetItem(str(v) if c != 3 else f"{int(v)}%")
                if c == 3:
                    it.setData(QtCore.Qt.UserRole, int(v))
                    it.setData(QtCore.Qt.UserRole + 1, state)
                self.task_table.setItem(r, c, it)
        completed = sum(1 for x in specs if j.task_states.get(str(x)) == "COMPLETE")
        self.management_tabs.setTabText(0, f"TASKS  {completed}/{len(specs)}")
        self.details_view.setPlainText(
            f"Job: {j.name}\n"
            f"Status: {'Completed' if j.status == 'Done' else j.status}\n"
            f"Frames: {j.frames_label}\n"
            f"Write: {j.write_node}\n"
            f"Priority: {j.job_priority}\n"
            f"Power: {j.render_power}\n"
            f"Chunk: {j.chunk_size or 'Off'}\n"
            f"Retries: {j.retry_limit}\n"
            f"Output: {j.output_path or '—'}\n"
            f"Dependency: {j.dependency_path or 'None'}\n"
            f"Scheduled: {j.schedule_at or 'Immediate'}\n"
            f"Snapshot: {self._snapshot_text(j)}\n"
            f"Attempts recorded: {len(j.attempt_history)}"
        )
        errs = []
        if j.error:
            errs.append(j.error)
        failed = [str(x) for x in specs if j.task_states.get(str(x)) == "FAILED"]
        if failed:
            errs.append("Failed tasks: " + ", ".join(failed))
        if j.missing_frames:
            errs.append("Missing output frames: " + ", ".join(map(str, j.missing_frames[:80])))
        if j.zero_byte_frames:
            errs.append("Zero-byte output frames: " + ", ".join(map(str, j.zero_byte_frames[:80])))
        self.errors_view.setPlainText("\n\n".join(errs) if errs else "No errors recorded for this job.")
        if hasattr(self, "output_view"):
            verify = {True: "Complete", False: "Incomplete"}.get(
                j.verified,
                "Not checked (several Writes or an expression in the path)"
                if j.output_path.startswith("(") or "[" in j.output_path
                else "Not checked",
            )
            self.output_view.setPlainText(
                f"Output path: {j.output_path or '—'}\n"
                f"Write node: {j.write_node or '—'}\n"
                f"Verification: {verify}\n"
                f"Missing frames: {len(j.missing_frames)}\n"
                f"Zero-byte frames: {len(j.zero_byte_frames)}"
            )
        avg = (sum(j.frame_durations) / len(j.frame_durations)) if j.frame_durations else 0
        eta = None
        if avg and j.first is not None and j.last is not None:
            eta = avg * max(0, (j.last - j.first + 1) * (1 - j.progress / 100.0))
        finish = (datetime.datetime.now() + datetime.timedelta(seconds=eta)).strftime("%H:%M") if eta else "—"
        self.performance_view.setPlainText(
            f"Render Power: {j.render_power}\n"
            f"Progress: {j.progress:.0f}%\n"
            f"Current frame: {j.current_frame if j.current_frame is not None else '—'}\n"
            f"Average frame time: {avg:.2f}s"
            if avg
            else f"Render Power: {j.render_power}\n"
            f"Progress: {j.progress:.0f}%\n"
            f"Current frame: {j.current_frame if j.current_frame is not None else '—'}\n"
            f"Peak RAM recorded: {j.peak_ram_gb:.1f} GB\n"
            f"Heavy frames: {len(j.heavy_frames)}"
        )
        if avg:
            self.performance_view.appendPlainText(
                f"ETA: {self._fmt_duration(eta)}\n"
                f"Estimated finish: {finish}\n"
                f"Peak RAM: {j.peak_ram_gb:.1f} GB\n"
                f"Heavy frames: {len(j.heavy_frames)}"
            )
        self._refresh_activity_view()
        self._refresh_session_view()

    def _add_activity(self, message):
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.activity.append((stamp, message))
        self.activity = self.activity[-250:]
        self._refresh_activity_view()

    def _refresh_activity_view(self):
        if hasattr(self, "activity_view"):
            self.activity_view.setPlainText(
                "\n".join(f"{t}   {m}" for t, m in reversed(self.activity[-100:]))
                or "No activity in this session yet."
            )

    def _refresh_session_view(self):
        if not hasattr(self, "session_view"):
            return
        current = [j for j in self.jobs if j.session_id == SESSION_ID]
        done = [j for j in current if j.status == "Done"]
        failed = [j for j in current if j.status == "Failed"]
        frames = sum(max(0, j.last - j.first + 1) for j in done if j.first is not None and j.last is not None)
        elapsed = time.monotonic() - self.session_started_at
        avgs = [sum(j.frame_durations) / len(j.frame_durations) for j in current if j.frame_durations]
        avg = sum(avgs) / len(avgs) if avgs else 0
        text = (
            f"SESSION\n"
            f"\n"
            f"Jobs completed     {len(done)}\n"
            f"Frames completed   {frames}\n"
            f"Failed jobs        {len(failed)}\n"
            f"Session elapsed    {self._fmt_duration(elapsed)}\n"
            f"Average frame      {avg:.2f}s"
            if avg
            else f"SESSION\n"
            f"\n"
            f"Jobs completed     {len(done)}\n"
            f"Frames completed   {frames}\n"
            f"Failed jobs        {len(failed)}\n"
            f"Session elapsed    {self._fmt_duration(elapsed)}\n"
            f"Average frame      —"
        )
        if current and len(done) == len(current) and not failed:
            text += "\n\nALL CURRENT-SESSION RENDERS COMPLETE"
        self.session_view.setPlainText(text)

    def _task_context_menu(self, pos):
        idx = self._current_row()
        row = self.task_table.rowAt(pos.y())
        if idx is None or row < 0:
            return
        j = self.jobs[idx]
        specs = self._frame_chunks(j)
        if row >= len(specs):
            return
        spec = specs[row]
        m = QtWidgets.QMenu(self)
        retry = m.addAction("Retry Task")
        retry_failed = m.addAction("Retry All Failed Tasks")
        queued = m.addAction("Mark Queued")
        log = m.addAction("View Job Log")
        act = m.exec(self.task_table.viewport().mapToGlobal(pos))
        if act == retry_failed:
            for x in specs:
                if j.task_states.get(str(x)) == "FAILED":
                    j.task_states[str(x)] = "QUEUED"
            j.status = "Queued"
            j.progress = min(j.progress, 99.0)
            self._save_state()
            self._redraw_queue()
            self.table.selectRow(idx)
        elif act == retry or act == queued:
            j.task_states[str(spec)] = "QUEUED"
            j.status = "Queued" if j.status in ("Failed", "Stopped", "Done") else j.status
            j.progress = min(j.progress, 99.0)
            self._save_state()
            self._redraw_queue()
            self.table.selectRow(idx)
        elif act == log:
            self._show_job_inspector()

    def _set_output_override(self):
        idx = self._current_row()
        if idx is None:
            return
        value = self.output_edit.text().strip()
        new = value if value != self.jobs[idx].source_output_path else ""
        if new != self.jobs[idx].output_override and self._locked_guard(
            [self.jobs[idx]], "change its output", quiet=True
        ):
            self.output_edit.setText(self.jobs[idx].output_path)
            return
        self.jobs[idx].output_override = new
        self._redraw_queue()
        self.table.selectRow(idx)
        self._save_state()

    def _browse_output_override(self):
        idx = self._current_row()
        if idx is None:
            return
        current = self.output_edit.text().strip() or self.jobs[idx].source_output_path
        f, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Set render output", current or "")
        if f:
            self.output_edit.setText(f)
            self._set_output_override()

    def _reset_output_override(self):
        idx = self._current_row()
        if idx is None:
            return
        if self._locked_guard([self.jobs[idx]], "change its output"):
            return
        self.jobs[idx].output_override = ""
        self._redraw_queue()
        self.table.selectRow(idx)
        self._save_state()

    def _make_job_controls(self, row, job):
        # Narrow status strip at the left of each row.
        w = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(w)
        lay.setContentsMargins(5, 2, 5, 2)
        lay.setSpacing(0)
        status_key = "Completed" if job.status == "Done" else job.status
        palette = self._effective_status_colors()
        col = {"Rendering": "#ff991c", "Failed": palette.get("Failed", "#b04a45")}.get(
            status_key, "transparent"
        )
        strip = QtWidgets.QFrame()
        strip.setFixedSize(3, 14)
        strip.setToolTip(status_key)
        strip.setStyleSheet(f"background:{col}; border:0;")
        lay.addWidget(strip)
        lay.addStretch(1)
        return w

    def _clear_queue(self):
        if self._queue_running:
            QtWidgets.QMessageBox.warning(
                self, "Render running", "Stop the render before clearing the queue."
            )
            return
        self.jobs.clear()
        self._redraw_queue()
        self._save_state()

    def _remember_selection(self):
        if getattr(self, "_refreshing_table", False):
            return
        sm = self.table.selectionModel()
        rows = [i.row() for i in sm.selectedRows()] if sm else []
        self._sel_ids = {self.jobs[r].id for r in rows if 0 <= r < len(self.jobs)}

    def _redraw_queue(self, light=False):
        if light and self.table.rowCount() == len(self.jobs):
            self._refresh_rendering_rows()
            return
        self._refreshing_table = True
        try:
            self._redraw_queue_full()
        finally:
            self._refreshing_table = False
        self._update_properties()
        self._update_active_render_header()
        self._apply_filter(self.search_edit.text() if hasattr(self, "search_edit") else "")

    def _refresh_rendering_rows(self):
        """Cheap per-frame update: only the rendering row's Status / Progress / Finish cells."""
        for r, j in enumerate(self.jobs):
            if j.status != "Rendering":
                continue
            st = self.table.item(r, 5)
            pr = self.table.item(r, 11)
            fin = self.table.item(r, 12)
            frm = self.table.item(r, 2)
            if frm:
                frm.setText(self._frames_text(j))
            if st:
                st.setText(
                    f"Rendering • Frame {j.current_frame}" if j.current_frame is not None else "Rendering"
                )
            if pr:
                pr.setData(QtCore.Qt.UserRole, j.progress)
                pr.setText(f"{j.progress:.0f}%")
            if fin:
                fin.setText(self._finish_text(j))
            idx = self._current_row()
            if idx == r:
                self._update_properties()
        self._update_active_render_header()
        active = next((j for j in self.jobs if j.status == "Rendering"), None)
        if active:
            self.statusBar().showMessage(
                f"Rendering {active.name}  |  Frame {active.current_frame}  |  {active.progress:.0f}%"
            )

    def _redraw_queue_full(self):
        sel_ids = set(getattr(self, "_sel_ids", set()))
        self.table.setRowCount(len(self.jobs))
        for r, j in enumerate(self.jobs):
            self.table.setCellWidget(r, 0, self._make_job_controls(r, j))
            status = "Completed" if j.status == "Done" else j.status
            if status == "Rendering" and j.current_frame is not None:
                status = f"Rendering • Frame {j.current_frame}"
            health, health_tip = self._job_health(j)
            finish = self._finish_text(j)
            order_short = {
                "First → Last": "Forward",
                "Last → First": "Reverse",
                "First / Middle / Last → Remaining": "Test First",
            }.get(j.frame_order, j.frame_order)
            vals = [
                j.name,
                self._frames_text(j),
                j.write_node,
                j.output_path,
                status,
                str(j.job_priority),
                (
                    j.render_power
                    if j.render_power != self.settings.get("default_render_power", "Normal")
                    else ""
                ),
                (str(j.chunk_size) if j.chunk_size else "Off"),
                str(j.retry_limit),
                order_short,
                "",
                finish,
                health,
                j.notes,
            ]
            for c, v in enumerate(vals, start=1):
                item = QtWidgets.QTableWidgetItem(str(v))
                item.setToolTip(
                    j.path
                    if c == 1
                    else (
                        j.output_path
                        if c == 4
                        else (health_tip if c == 13 else (f"Frames {j.frames_label}" if c == 2 else str(v)))
                    )
                )
                self.table.setItem(r, c, item)
            self.table.item(r, 1).setData(QtCore.Qt.UserRole + 2, self._job_detail_text(j))
            p = self.table.item(r, 11)
            state_key = "Completed" if j.status == "Done" else j.status
            show_progress = j.status in (
                "Rendering",
                "Done",
                "Failed",
                "Stopped",
                "Suspended",
                "Blocked",
                "Queued",
                "Waiting (resources)",
            )
            p.setData(QtCore.Qt.UserRole, j.progress if show_progress else None)
            p.setData(QtCore.Qt.UserRole + 1, state_key)
            p.setText(f"{j.progress:.0f}%" if show_progress else "")
            is_previous = j.session_id != SESSION_ID
            if j.status == "Done" or is_previous:
                for c in range(1, 15):
                    it = self.table.item(r, c)
                    if it:
                        it.setForeground(QtGui.QColor("#777777" if j.status == "Done" else "#929292"))
            elif j.status == "Rendering":
                for c in range(1, 15):
                    it = self.table.item(r, c)
                    if it:
                        it.setBackground(QtGui.QColor("#303030"))
                        it.setForeground(QtGui.QColor("#eeeeee"))
            if not j.enabled:
                for c in range(1, 15):
                    it = self.table.item(r, c)
                    if it:
                        it.setForeground(QtGui.QColor("#666666"))
        # Power column only appears when some job uses a non-default power.
        self.table.setColumnHidden(
            7,
            not any(j.render_power != self.settings.get("default_render_power", "Normal") for j in self.jobs),
        )
        sm = self.table.selectionModel()
        selection = QtCore.QItemSelection()
        for r, j in enumerate(self.jobs):
            if j.id in sel_ids:
                selection.select(
                    self.table.model().index(r, 0), self.table.model().index(r, self.table.columnCount() - 1)
                )
        sm.select(selection, QtCore.QItemSelectionModel.ClearAndSelect | QtCore.QItemSelectionModel.Rows)
        counts = {
            s: sum(j.status == s for j in self.jobs)
            for s in ("Queued", "Rendering", "Done", "Failed", "Stopped", "Suspended", "Blocked")
        }
        disabled = sum(not j.enabled for j in self.jobs)
        total_frames = sum(
            max(0, j.last - j.first + 1) for j in self.jobs if j.first is not None and j.last is not None
        )
        self.summary.setText(
            f"{len(self.jobs)} jobs   |   {total_frames:,} frames   |   {counts['Rendering']} rendering"
            f"   |   {counts['Queued']} queued   |   {counts['Done']} done   |   {counts['Failed']} failed   |   {disabled} disabled"
        )
        if hasattr(self, "queue_header_summary"):
            self.queue_header_summary.setText(
                f"{len(self.jobs)} Jobs  •  {counts['Rendering']} Rendering  •  {counts['Queued']} Waiting  •  {counts['Done']} Completed"
            )
        if hasattr(self, "empty_state"):
            self.empty_state.setVisible(len(self.jobs) == 0)
            self.empty_state.setGeometry(self.table.viewport().rect())
        overall = (
            (sum(j.progress for j in self.jobs if j.enabled) / max(1, sum(1 for j in self.jobs if j.enabled)))
            if self.jobs
            else 0
        )
        active = next((j for j in self.jobs if j.status == "Rendering"), None)
        frame_txt = (
            f"Frame {active.current_frame}" if active and active.current_frame is not None else "Ready"
        )
        self.statusBar().showMessage(f"{overall:.0f}% overall  |  {frame_txt}")
        if hasattr(self, "status_jobs"):
            self.status_jobs.setText(f"Jobs: {len(self.jobs)}")
            self.status_render.setText(f"Rendering: {counts['Rendering']}")
            self.status_wait.setText(f"Queued: {counts['Queued']}")
            self.status_done.setText(f"Complete: {counts['Done']}")
            self.status_fail.setText(f"Failed: {counts['Failed']}")
            try:
                cpu = psutil.cpu_percent(interval=None) if psutil else 0
                mem = psutil.virtual_memory().percent if psutil else 0
                self.status_resources.setText(f"CPU {cpu:.0f}%   RAM {mem:.0f}%")
            except Exception:
                self.status_resources.setText("CPU —   RAM —")

    def _job_health(self, j):
        issues = []
        if not Path(j.path).exists():
            issues.append("Script missing")
        if j.first is not None and j.last is not None and j.first > j.last:
            issues.append("Invalid frame range")
        names = [w.get("name") for w in j.write_nodes]
        if not j.write_nodes:
            issues.append("No Write nodes detected")
        elif j.write_node not in ("(all)", "all", "", None) and j.write_node not in names:
            issues.append("Selected Write node missing")
        out = j.output_path
        if (
            out and not out.startswith("(") and "[" not in out and "$" not in out
        ):  # expressions can't be checked here
            parent = Path(out).expanduser().parent
            if not parent.exists():
                issues.append("Output folder does not exist")
        if issues:
            return ("!", "\n".join(issues))
        if j.locked:
            return ("🔒", "Locked — protected from edits, removal and re-rendering")
        if self._script_changed_since_snapshot(j):
            return (
                "↻",
                "Script saved after it was queued — the queued snapshot will be rendered.\n"
                "Use Queue > Refresh Script to render the latest save.",
            )
        return ("✓", "Preflight looks healthy")

    def _snapshot_queue(self):
        self.queue_undo.append([j.to_dict() for j in self.jobs])
        self.queue_undo = self.queue_undo[-20:]

    def _undo_queue_edit(self):
        if not self.queue_undo:
            return
        if self._queue_running:
            self.statusBar().showMessage("Undo is unavailable while rendering", 3000)
            return
        self.jobs = [RenderJob.from_dict(d) for d in self.queue_undo.pop()]
        for j in self.jobs:
            j.session_id = SESSION_ID
        self._redraw_queue()
        self._save_state()

    def _frames_text(self, j):
        """'1001-1127' normally; '1001-1127 · 41/127' while a job is in progress."""
        if j.first is None or j.last is None:
            return j.frames_label
        total = abs(j.last - j.first) + 1
        if j.status in ("Rendering", "Stopped", "Failed", "Waiting (resources)") and 0 < j.progress < 100:
            return f"{j.frames_label} · {int(round(total * j.progress / 100.0))}/{total}"
        return j.frames_label

    def _job_detail_text(self, j):
        """Dim text after the script name: output folder, notes, when it was queued."""
        parts = []
        out = j.output_path or ""
        if out and not out.startswith("("):
            folder = Path(re.sub(r"(#+|%0?\d*d)", "0", out)).parent.name
            if folder:
                parts.append("→ " + folder)
        if j.notes and j.notes != "Sent from Nuke":
            parts.append(j.notes)
        if j.snapshot_at:
            parts.append("queued " + datetime.datetime.fromtimestamp(j.snapshot_at).strftime("%d %b %H:%M"))
        return "   ·   ".join(parts)

    def _render_duration(self, j):
        h = next(
            (
                h
                for h in reversed(list(getattr(self, "history", []) or []))
                if h.get("path") == j.path and h.get("write") == j.write_node and h.get("duration")
            ),
            None,
        )
        return self._fmt_duration(h.get("duration")) if h else ""

    def _finish_text(self, j):
        if j.status == "Done" and j.completed_at:
            dur = self._render_duration(j)
            return str(j.completed_at).split("T")[-1][:5] + (f" · {dur}" if dur else "")
        if j.status == "Rendering" and j.frame_durations and j.first is not None and j.last is not None:
            avg = sum(j.frame_durations) / len(j.frame_durations)
            remain = max(0, (j.last - j.first + 1) * (1 - j.progress / 100.0))
            eta = avg * remain
            return "~" + (datetime.datetime.now() + datetime.timedelta(seconds=eta)).strftime("%H:%M")
        return "—"

    def _edit_queue_cell(self, row, col):
        if not (0 <= row < len(self.jobs)):
            return
        j = self.jobs[row]
        if j.locked:
            QtWidgets.QMessageBox.information(self, "Job Locked", "Unlock this job before editing it.")
            return
        if self._queue_running and j.status == "Rendering":
            QtWidgets.QMessageBox.information(
                self, "Job Rendering", "Stop or finish this job before changing its render settings."
            )
            return
        self._snapshot_queue()
        changed = True
        if col == 2:
            value, ok = QtWidgets.QInputDialog.getText(
                self, "Frames — " + j.name, "Frame range", text=j.frames_label
            )
            if ok:
                frames = parse_frame_range(value)
                if frames is None:
                    QtWidgets.QMessageBox.warning(self, "Frames", "Enter a frame range such as 1001-1100.")
                    changed = False
                else:
                    a, b = frames
                    if a > b:
                        QtWidgets.QMessageBox.warning(
                            self, "Frames", "The first frame has to come before the last frame."
                        )
                        changed = False
                    else:
                        j.first, j.last = a, b
                        j.task_states = {}
                        j.progress = 0.0
                        j.current_frame = None
                        j.status = "Queued" if j.status == "Done" else j.status
            else:
                changed = False
        elif col == 3:
            if not j.write_nodes:
                _, _, j.write_nodes = read_nk_script(j.path)
            choices = ["(all)"] + [w.get("name", "") for w in j.write_nodes]
            value, ok = QtWidgets.QInputDialog.getItem(
                self,
                "Write — " + j.name,
                "Write node",
                choices,
                max(0, choices.index(j.write_node) if j.write_node in choices else 0),
                False,
            )
            if ok:
                j.write_node = value
                j.progress = 0.0
                j.task_states = {}
                j.verified = None
            else:
                changed = False
        elif col == 4:
            value, ok = QtWidgets.QInputDialog.getText(
                self,
                "Output — " + j.name,
                "Output override (blank = Write node output)",
                text=j.output_override,
            )
            if ok:
                j.output_override = value.strip()
                j.verified = None
            else:
                changed = False
        elif col == 5:
            choices = ["Queued", "Suspended", "Stopped"]
            value, ok = QtWidgets.QInputDialog.getItem(
                self,
                "Status — " + j.name,
                "Status",
                choices,
                max(0, choices.index(j.status) if j.status in choices else 0),
                False,
            )
            if ok:
                j.status = value
                j.suspended = value == "Suspended"
            else:
                changed = False
        elif col == 6:
            value, ok = QtWidgets.QInputDialog.getInt(
                self, "Priority — " + j.name, "Priority (0-100)", j.job_priority, 0, 100
            )
            j.job_priority = value if ok else j.job_priority
            changed = ok
        elif col == 7:
            choices = ["Low", "Normal", "High", "Maximum", "Custom"]
            value, ok = QtWidgets.QInputDialog.getItem(
                self, "Render Power — " + j.name, "Power", choices, choices.index(j.render_power), False
            )
            j.render_power = value if ok else j.render_power
            changed = ok
        elif col == 8:
            value, ok = QtWidgets.QInputDialog.getInt(
                self, "Chunk — " + j.name, "Frames per task (0 = Off)", j.chunk_size, 0, 10000
            )
            j.chunk_size = value if ok else j.chunk_size
            j.task_states = {} if ok else j.task_states
            changed = ok
        elif col == 9:
            value, ok = QtWidgets.QInputDialog.getInt(
                self, "Retry — " + j.name, "Automatic retries", j.retry_limit, 0, 10
            )
            j.retry_limit = value if ok else j.retry_limit
            changed = ok
        elif col == 10:
            labels = ["Forward", "Reverse", "Test First"]
            mapping = {
                "Forward": "First → Last",
                "Reverse": "Last → First",
                "Test First": "First / Middle / Last → Remaining",
            }
            cur = {v: k for k, v in mapping.items()}.get(j.frame_order, "Forward")
            value, ok = QtWidgets.QInputDialog.getItem(
                self, "Frame Order — " + j.name, "Order", labels, labels.index(cur), False
            )
            j.frame_order = mapping[value] if ok else j.frame_order
            j.task_states = {} if ok else j.task_states
            changed = ok
        elif col == 13:
            self.queue_undo.pop()
            self._preflight_selected()
            return
        elif col == 14:
            value, ok = QtWidgets.QInputDialog.getText(self, "Notes — " + j.name, "Notes", text=j.notes)
            j.notes = value if ok else j.notes
            changed = ok
        else:
            self.queue_undo.pop()
            self._open_job_editor()
            return
        if not changed:
            self.queue_undo.pop()
            return
        self._redraw_queue()
        self.table.selectRow(row)
        self._save_state()

    def _toggle_lock_selected(self):
        rows = self._selected_rows()
        if not rows:
            return
        target = not all(self.jobs[r].locked for r in rows)
        for r in rows:
            self.jobs[r].locked = target
        self._redraw_queue()
        self._save_state()

    def _reset_selected_jobs(self):
        rows = self._selected_rows()
        if not rows:
            return
        for r in rows:
            j = self.jobs[r]
            if j.locked:
                continue
            j.status = "Queued"
            j.suspended = False
            j.progress = 0.0
            j.error = ""
            j.current_frame = None
            j.completed_at = None
            j.task_states = {}
            j.missing_frames = []
            j.verified = None
            j.session_id = SESSION_ID
        self._redraw_queue()
        self._save_state()

    def _header_context_menu(self, pos):
        m = QtWidgets.QMenu(self)
        labels = [self.table.horizontalHeaderItem(i).text() for i in range(self.table.columnCount())]
        for i, label in enumerate(labels):
            if not label:
                continue
            a = m.addAction(label)
            a.setCheckable(True)
            a.setChecked(not self.table.isColumnHidden(i))
            if i in (1, 2, 3, 5):
                a.setEnabled(False)
            a.toggled.connect(lambda checked, c=i: self.table.setColumnHidden(c, not checked))
        m.exec(self.table.horizontalHeader().mapToGlobal(pos))

    def keyPressEvent(self, event):
        if event.matches(QtGui.QKeySequence.Undo):
            self._undo_queue_edit()
            return
        if event.key() == QtCore.Qt.Key_F2:
            r = self.table.currentRow()
            c = self.table.currentColumn()
            if r >= 0:
                self._edit_queue_cell(r, c)
                return
        if event.key() == QtCore.Qt.Key_Delete and self.table.hasFocus():
            self._remove_selected()
            return
        if event.modifiers() & QtCore.Qt.ControlModifier and event.key() == QtCore.Qt.Key_D:
            self._duplicate_job()
            return
        if event.key() == QtCore.Qt.Key_Space and self.table.hasFocus():
            for r in self._selected_rows():
                self.jobs[r].enabled = not self.jobs[r].enabled
            self._redraw_queue()
            self._save_state()
            return
        super().keyPressEvent(event)

    def _write_recovery_snapshot(self):
        try:
            data = {
                "version": APP_VERSION,
                "saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "queue": [j.to_dict() for j in self.jobs],
            }
            tmp = RECOVERY_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, RECOVERY_FILE)
        except Exception:
            pass

    def _offer_recovery(self):
        if not self._unclean_start:
            return
        interrupted = [
            j for j in self.jobs if j.status in ("Stopped", "Failed") and j.progress > 0 and j.progress < 100
        ]
        if not interrupted:
            return
        names = "\n".join(
            f"• {j.name} — {j.progress:.0f}% — last frame {j.current_frame or '—'}" for j in interrupted[:8]
        )
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Recovered Session")
        box.setIcon(QtWidgets.QMessageBox.Warning)
        box.setText("Batch Gartner did not close normally.\n\nRecovered render state:\n" + names)
        resume = box.addButton("Resume Remaining", QtWidgets.QMessageBox.AcceptRole)
        inspect = box.addButton("Inspect", QtWidgets.QMessageBox.ActionRole)
        box.addButton("Keep Stopped", QtWidgets.QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() == resume:
            for j in interrupted:
                j.status = "Queued"
                j.suspended = False
            self._redraw_queue()
            self._save_state()
        elif box.clickedButton() == inspect and interrupted:
            try:
                self.table.selectRow(self.jobs.index(interrupted[0]))
                self._update_properties()
            except Exception:
                pass

    def _restore_ui_state(self):
        try:
            g = self.settings.get("geometry") if self.settings.get("remember_geometry", True) else None
            st = self.settings.get("window_state") if self.settings.get("remember_geometry", True) else None
            if g:
                self.restoreGeometry(QtCore.QByteArray.fromBase64(g.encode("ascii")))
            if st:
                self.restoreState(QtCore.QByteArray.fromBase64(st.encode("ascii")))
            if self.settings.get("remember_columns", True):
                for i, w in enumerate(self.settings.get("column_widths", [])):
                    if i < self.table.columnCount():
                        self.table.setColumnWidth(i, int(w))
            ws = (
                self.settings.get("work_splitter_sizes")
                if self.settings.get("remember_layout", True)
                else None
            )
            if ws and hasattr(self, "work_splitter"):
                self.work_splitter.setSizes([int(x) for x in ws])
            ds = (
                self.settings.get("detail_splitter_sizes")
                if self.settings.get("remember_layout", True)
                else None
            )
            if ds and hasattr(self, "detail_splitter"):
                self.detail_splitter.setSizes([int(x) for x in ds])
            QtCore.QTimer.singleShot(0, self._fill_queue_table_width)
        except Exception:
            pass

    def _status_color_defaults(self):
        return dict(STATUS_COLORS)

    def _apply_interface_preferences(self):
        row = max(20, min(36, int(self.settings.get("queue_row_height", 24))))
        if hasattr(self, "table"):
            self.table.verticalHeader().setDefaultSectionSize(row)
        if hasattr(self, "task_table"):
            self.task_table.verticalHeader().setDefaultSectionSize(row)
        colors = self._effective_status_colors()
        for name in (
            "status_delegate",
            "progress_delegate",
            "task_status_delegate",
            "task_progress_delegate",
        ):
            d = getattr(self, name, None)
            if d and hasattr(d, "set_colors"):
                d.set_colors(colors)

    def _effective_status_colors(self):
        c = self._status_color_defaults()
        c.update(
            {
                k: v
                for k, v in (self.settings.get("status_colors", {}) or {}).items()
                if str(v).lower() != LEGACY_STATUS_COLORS.get(k, "").lower()
            }
        )
        c["Done"] = c.get("Completed", c["Done"])
        return c

    def _show_preferences(self):
        d = QtWidgets.QDialog(self)
        d.setWindowTitle("Batch Gartner Preferences")
        d.resize(760, 540)
        root = QtWidgets.QVBoxLayout(d)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)
        body = QtWidgets.QHBoxLayout()
        body.setSpacing(10)
        root.addLayout(body, 1)
        cats = QtWidgets.QListWidget()
        cats.setFixedWidth(150)
        cats.addItems(
            [
                "Appearance",
                "Queue",
                "Rendering",
                "Performance",
                "Notifications",
                "Post Render",
                "Interface",
                "Advanced",
            ]
        )
        body.addWidget(cats)
        stack = QtWidgets.QStackedWidget()
        body.addWidget(stack, 1)
        controls = {}

        def page(title):
            w = QtWidgets.QWidget()
            v = QtWidgets.QVBoxLayout(w)
            v.setContentsMargins(12, 8, 12, 8)
            h = QtWidgets.QLabel(title)
            h.setStyleSheet(
                "font-weight:600; font-size:12px; border-bottom:1px solid #333; padding-bottom:5px;"
            )
            v.addWidget(h)
            f = QtWidgets.QFormLayout()
            f.setLabelAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            f.setHorizontalSpacing(12)
            f.setVerticalSpacing(7)
            v.addLayout(f)
            v.addStretch(1)
            stack.addWidget(w)
            return f

        def check(form, key, label, default=False):
            x = QtWidgets.QCheckBox()
            x.setChecked(bool(self.settings.get(key, default)))
            form.addRow(label, x)
            controls[key] = x
            return x

        def combo(form, key, label, items, default):
            x = QtWidgets.QComboBox()
            x.addItems(items)
            x.setCurrentText(str(self.settings.get(key, default)))
            form.addRow(label, x)
            controls[key] = x
            return x

        def spin(form, key, label, lo, hi, default, suffix=""):
            x = QtWidgets.QSpinBox()
            x.setRange(lo, hi)
            x.setValue(int(self.settings.get(key, default)))
            x.setSuffix(suffix)
            form.addRow(label, x)
            controls[key] = x
            return x

        # Appearance
        f = page("Appearance")
        combo(f, "ui_font_family", "Font", ["Verdana", "Arial", "Segoe UI"], "Verdana")
        spin(f, "ui_font_size", "Font size", 8, 16, 11, " px")
        spin(f, "queue_row_height", "Queue row height", 20, 36, 24, " px")
        preview = QtWidgets.QWidget()
        ph = QtWidgets.QHBoxLayout(preview)
        ph.setContentsMargins(0, 0, 0, 0)
        ph.setSpacing(4)
        color_buttons = {}
        colors = self._effective_status_colors()
        for state in ["Queued", "Rendering", "Completed", "Suspended", "Stopped", "Failed"]:
            b = QtWidgets.QPushButton(state)
            b.setMinimumWidth(70)
            b.setProperty("hex", colors[state])
            b.setStyleSheet(f"background:{colors[state]}; color:white; font-weight:600;")

            def choose(_, btn=b, st=state):
                c = QtWidgets.QColorDialog.getColor(QtGui.QColor(btn.property("hex")), d, f"{st} colour")
                if c.isValid():
                    btn.setProperty("hex", c.name())
                    btn.setStyleSheet(f"background:{c.name()}; color:white; font-weight:600;")

            b.clicked.connect(choose)
            ph.addWidget(b)
            color_buttons[state] = b
        f.addRow("Status colours", preview)
        controls["status_color_buttons"] = color_buttons
        reset_colors = QtWidgets.QPushButton("Restore softer defaults")
        reset_colors.clicked.connect(
            lambda: [
                (
                    b.setProperty("hex", self._status_color_defaults()[st]),
                    b.setStyleSheet(
                        f"background:{self._status_color_defaults()[st]}; color:white; font-weight:600;"
                    ),
                )
                for st, b in color_buttons.items()
            ]
        )
        f.addRow("", reset_colors)

        f = page("Queue")
        check(f, "restore_queue", "Restore queue on launch", True)
        check(f, "confirm_remove", "Confirm before removing jobs", False)
        check(f, "check_newer_versions", "Warn about newer script versions", True)
        spin(f, "default_chunk_size", "Default chunk size", 0, 1000, 0)
        spin(f, "default_retry_limit", "Default auto retry", 0, 20, 0)
        f = page("Rendering")
        combo(
            f, "default_render_power", "Default render power", ["Low", "Normal", "High", "Maximum"], "Normal"
        )
        check(f, "render_while_work", "Render while I work", False)
        check(f, "snapshot_scripts", "Render the script as it was when queued (snapshot)", True)
        check(f, "auto_resume", "Resume failed / stopped jobs from the frames already on disk", True)
        check(f, "confirm_overwrite", "Ask before overwriting frames that already exist", True)
        f = page("Performance")
        spin(f, "monitor_refresh_ms", "System monitor refresh", 250, 5000, 1000, " ms")
        spin(f, "default_cpu_threshold", "CPU protection threshold", 50, 100, 90, " %")
        spin(f, "default_ram_reserve_gb", "RAM reserve", 0, 64, 2, " GB")
        spin(f, "resource_wait_max_min", "Max wait for free resources", 0, 240, 10, " min (0 = no limit)")
        par = spin(f, "parallel_renders", "Simultaneous renders", 1, 4, 1, " Nuke process(es)")
        par.setToolTip(
            "Render several jobs at the same time. Only for machines with plenty of cores and RAM:\n"
            "each Nuke gets an equal share of threads (-m) and cache memory (-c). Keep at 1 otherwise."
        )
        f = page("Notifications")
        check(f, "notifications_enabled", "Desktop notifications", True)
        check(f, "notify_completed", "Notify when a render completes", True)
        check(f, "notify_failed", "Notify when a render fails", True)
        f = page("Post Render")
        check(f, "verify_outputs", "Verify rendered sequence", True)
        check(f, "open_output_after_render", "Open output folder after successful queue", False)
        f = page("Interface")
        check(f, "remember_geometry", "Remember window size and position", True)
        check(f, "remember_layout", "Remember panel sizes", True)
        check(f, "remember_columns", "Remember column widths", True)
        check(f, "show_inspector_startup", "Show Job Properties on launch", True)
        reset_layout = QtWidgets.QPushButton("Reset saved layout")
        f.addRow("Layout", reset_layout)
        controls["reset_layout_button"] = reset_layout
        f = page("Advanced")
        port = spin(f, "receiver_port", "Nuke receiver port", 1024, 65535, 54321)
        port.setToolTip(
            "The Nuke menu reads this port automatically. Restart Batch Gartner after changing it."
        )
        cfg_lbl = QtWidgets.QLineEdit(str(SETTINGS_FILE))
        cfg_lbl.setReadOnly(True)
        f.addRow("Configuration file", cfg_lbl)
        check(f, "write_recovery", "Write recovery snapshots", True)

        cats.currentRowChanged.connect(stack.setCurrentIndex)
        cats.setCurrentRow(0)
        reset_requested = [False]

        def reset_layout_now():
            reset_requested[0] = True
            QtWidgets.QMessageBox.information(
                d, "Layout", "Saved layout will be reset when you press Apply or OK."
            )

        reset_layout.clicked.connect(reset_layout_now)
        bb = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok
            | QtWidgets.QDialogButtonBox.Apply
            | QtWidgets.QDialogButtonBox.Cancel
            | QtWidgets.QDialogButtonBox.RestoreDefaults
        )
        root.addWidget(bb)

        def apply_changes():
            for key, w in controls.items():
                if key in ("status_color_buttons", "reset_layout_button"):
                    continue
                if isinstance(w, QtWidgets.QCheckBox):
                    self.settings[key] = w.isChecked()
                elif isinstance(w, QtWidgets.QComboBox):
                    self.settings[key] = w.currentText()
                elif isinstance(w, QtWidgets.QSpinBox):
                    self.settings[key] = w.value()
            self.settings["status_colors"] = {st: b.property("hex") for st, b in color_buttons.items()}
            if reset_requested[0]:
                for k in (
                    "geometry",
                    "window_state",
                    "column_widths",
                    "work_splitter_sizes",
                    "detail_splitter_sizes",
                ):
                    self.settings.pop(k, None)
                self._reset_layout_now()
                reset_requested[0] = False
            self._apply_style()
            self._apply_interface_preferences()
            self.global_power_combo.setCurrentText(self.settings.get("default_render_power", "Normal"))
            self.work_mode_toolbar.setChecked(bool(self.settings.get("render_while_work", False)))
            self.priority_combo.setCurrentText(self.settings.get("priority", "Normal"))
            self.monitor_timer.setInterval(max(250, int(self.settings.get("monitor_refresh_ms", 1000))))
            write_settings(self.settings)
            self._redraw_queue()

        def defaults():
            self.settings.update(
                {
                    "ui_font_family": "Verdana",
                    "ui_font_size": 11,
                    "queue_row_height": 24,
                    "status_colors": self._status_color_defaults(),
                    "default_render_power": "Normal",
                    "render_while_work": False,
                    "priority": "Normal",
                    "verify_outputs": True,
                    "check_newer_versions": True,
                    "monitor_refresh_ms": 1000,
                    "notifications_enabled": True,
                    "remember_geometry": True,
                    "remember_layout": True,
                    "remember_columns": True,
                    "show_inspector_startup": True,
                }
            )
            d.accept()
            self._show_preferences()

        bb.button(QtWidgets.QDialogButtonBox.Apply).clicked.connect(apply_changes)
        bb.button(QtWidgets.QDialogButtonBox.RestoreDefaults).clicked.connect(defaults)
        bb.accepted.connect(lambda: (apply_changes(), d.accept()))
        bb.rejected.connect(d.reject)
        d.exec()

    def _reset_layout_now(self):
        defaults = {
            0: 14,
            1: 620,
            2: 150,
            3: 70,
            4: 180,
            5: 130,
            6: 70,
            7: 72,
            8: 62,
            9: 55,
            10: 55,
            11: 130,
            12: 120,
            13: 52,
            14: 135,
        }
        for c, w in defaults.items():
            self.table.setColumnWidth(c, w)
        self._queue_last_viewport_width = None
        self.work_splitter.setSizes([920, 320])
        self.detail_splitter.setSizes([560, 180])
        self.resize(1280, 760)
        QtCore.QTimer.singleShot(0, self._fill_queue_table_width)

    def _newer_script_version(self, j):
        p = Path(j.path)
        m = re.search(r"(?i)(.*?)[._-]v(\d+)([^/]*)$", p.stem)
        if not m:
            return None
        base, num, tail = m.group(1), int(m.group(2)), m.group(3)
        best = None
        bestn = num
        for f in p.parent.glob("*.nk"):
            mm = re.search(r"(?i)(.*?)[._-]v(\d+)([^/]*)$", f.stem)
            if mm and mm.group(1) == base and mm.group(3) == tail and int(mm.group(2)) > bestn:
                bestn = int(mm.group(2))
                best = f
        return best

    def _check_newer_selected(self):
        idx = self._current_row()
        if idx is None:
            return
        j = self.jobs[idx]
        newer = self._newer_script_version(j)
        if not newer:
            QtWidgets.QMessageBox.information(
                self, "Script Version", "No newer numbered .nk version was found beside this script."
            )
            return
        ans = QtWidgets.QMessageBox.question(
            self,
            "Newer Script Version",
            f"Queued:\n{j.path}\n\nNewer version:\n{newer}\n\nUpdate this job to the newer script?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        )
        if ans == QtWidgets.QMessageBox.Yes:
            first, last, writes = read_nk_script(str(newer))
            j.path = str(newer)
            j.first = first if first is not None else j.first
            j.last = last if last is not None else j.last
            j.write_nodes = writes
            j.status = "Queued"
            j.progress = 0
            j.task_states = {}
            j.verified = None
            self._take_snapshot(j)
            self._redraw_queue()
            self._save_state()

    def _show_render_report(self):
        idx = self._current_row()
        if idx is None:
            return
        j = self.jobs[idx]
        self._verify_job_output(j)
        total = (j.last - j.first + 1) if j.first is not None and j.last is not None else 0
        rendered = total - len(j.missing_frames) if total else 0
        avg = (sum(j.frame_durations) / len(j.frame_durations)) if j.frame_durations else None
        lines = [
            j.name,
            "",
            f"Status              {j.status}",
            f"Verification        {'COMPLETE & VERIFIED' if j.verified else ('INCOMPLETE' if j.verified is False else 'NOT CHECKED')}",
            f"Write               {j.write_node}",
            f"Frames              {j.frames_label}",
            f"Expected            {total}",
            f"Present             {rendered}",
            f"Missing             {len(j.missing_frames)}",
            f"Render Power        {j.render_power}",
            f"Peak RAM            {j.peak_ram_gb:.1f} GB",
            f"Average / frame     {avg:.2f}s" if avg else "Average / frame     —",
            f"Completed           {j.completed_at or '—'}",
            "",
            f"Output\n{j.output_path or '—'}",
        ]
        d = QtWidgets.QDialog(self)
        d.setWindowTitle("Render Report")
        d.resize(600, 500)
        l = QtWidgets.QVBoxLayout(d)
        e = QtWidgets.QPlainTextEdit()
        e.setReadOnly(True)
        e.setPlainText("\n".join(lines))
        l.addWidget(e)
        b = QtWidgets.QPushButton("Copy Report")
        b.clicked.connect(lambda: QtWidgets.QApplication.clipboard().setText(e.toPlainText()))
        l.addWidget(b)
        d.exec()

    def _show_about(self):
        QtWidgets.QMessageBox.about(
            self,
            "About Batch Gartner",
            f"BATCH GARTNER\n"
            f"Nuke Render Queue Manager\n"
            f"\n"
            f"Version {APP_VERSION}\n"
            f"\n"
            f"Local render management for Foundry Nuke.",
        )

    def _save_state(self):
        self.settings["nuke_exe"] = self.nuke_path.text()
        self.settings["queue"] = [j.to_dict() for j in self.jobs]
        self.settings["history"] = self.history[-500:]
        self.settings["priority"] = self.priority_combo.currentText()
        self.settings["render_while_work"] = (
            bool(self.work_mode.isChecked())
            if hasattr(self, "work_mode")
            else bool(self.settings.get("render_while_work", False))
        )
        self.settings["default_render_power"] = (
            self.global_power_combo.currentText()
            if hasattr(self, "global_power_combo")
            else self.settings.get("default_render_power", "Normal")
        )
        if self.settings.get("remember_geometry", True):
            self.settings["geometry"] = bytes(self.saveGeometry().toBase64()).decode("ascii")
            self.settings["window_state"] = bytes(self.saveState().toBase64()).decode("ascii")
        if self.settings.get("remember_columns", True):
            self.settings["column_widths"] = (
                [self.table.columnWidth(i) for i in range(self.table.columnCount())]
                if hasattr(self, "table")
                else []
            )
        if self.settings.get("remember_layout", True):
            self.settings["work_splitter_sizes"] = (
                self.work_splitter.sizes() if hasattr(self, "work_splitter") else []
            )
            self.settings["detail_splitter_sizes"] = (
                self.detail_splitter.sizes() if hasattr(self, "detail_splitter") else []
            )
        write_settings(self.settings)
        if self.settings.get("write_recovery", True):
            self._write_recovery_snapshot()

    def _write_console(self, line):
        self.log.appendPlainText(line.rstrip())
        sb = self.log.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ------------------------------------------------------------------
    # Render launching
    # ------------------------------------------------------------------
    def _nuke_for(self, job):
        exe = (job.nuke_exe or "").strip() or self.nuke_path.text().strip()
        return exe if exe and Path(exe).exists() else ""

    def _check_executables(self, jobs):
        bad = [j.name for j in jobs if not self._nuke_for(j)]
        if bad:
            QtWidgets.QMessageBox.critical(
                self,
                "Nuke Executable",
                "Set a valid Nuke executable first (Tools > Set Nuke Executable...).\n\nNo valid executable for:\n"
                + "\n".join(bad[:10]),
            )
            return False
        return True

    def _checkable_output(self, job):
        out = job.output_path or ""
        return bool(out) and not out.startswith("(") and "[" not in out and "$" not in out

    def _existing_frames(self, job):
        """Frames of this job's range already on disk (non-empty files), sorted newest-last by mtime.
        Returns None when the output can't be checked (several Writes, expressions, movie files)."""
        if not self._checkable_output(job) or job.first is None or job.last is None:
            return None
        info = self._sequence_info(job)
        if not info:
            return None
        expected = set(info["expected"])
        found = []
        for fr, fp in zip(info["existing"], info["files"]):
            if fr not in expected:
                continue
            try:
                st = Path(fp).stat()
                if st.st_size > 0:
                    found.append((st.st_mtime, fr))
            except Exception:
                pass
        found.sort()
        return {"have": [fr for _, fr in found], "expected": sorted(expected)}

    def _set_resume(self, job, have, expected, recheck_last=True):
        """Render only the frames that are missing. The most recently written frame is redone too,
        because a crash can leave it half-written."""
        have = list(have)
        if recheck_last and have:
            have = have[:-1]
        missing = sorted(set(expected) - set(have))
        if not missing:
            return False
        job._frame_spec = self._compress_frames(missing).replace(" ", "")
        job._partial_kind = "resume"
        job._resume_base = len(expected) - len(missing)
        self._write_console(
            f">> Resuming {job.name}: {job._resume_base} of {len(expected)} frames already rendered — rendering the remaining {len(missing)}."
        )
        return True

    def _plan_existing_output(self, jobs):
        """Before a full render: resume failed/stopped jobs automatically and ask before overwriting
        frames that already exist. Returns False if the user cancels."""
        auto = self.settings.get("auto_resume", True)
        confirm = self.settings.get("confirm_overwrite", True)
        ask = []
        self._plan_done_ids = set()
        for j in jobs:
            if getattr(j, "_frame_spec", None):
                continue
            r = self._existing_frames(j)
            if not r or not r["have"]:
                continue
            if auto and j.status in ("Failed", "Stopped") and len(r["have"]) < len(r["expected"]):
                self._set_resume(j, r["have"], r["expected"])
                continue
            if confirm:
                ask.append((j, r))
        if not ask:
            return True
        lines = []
        for j, r in ask[:12]:
            n, t = len(r["have"]), len(r["expected"])
            lines.append(f"• {j.name} / {j.write_node} — {'all' if n == t else n} of {t} frames exist")
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Warning)
        box.setWindowTitle("Frames Already Exist")
        box.setText(
            "These jobs already have rendered frames in their output folder:\n\n"
            + "\n".join(lines)
            + "\n\nWhat should happen to the existing frames?"
        )
        missing_btn = box.addButton("Render Missing Only", QtWidgets.QMessageBox.AcceptRole)
        over_btn = box.addButton("Overwrite All", QtWidgets.QMessageBox.DestructiveRole)
        box.addButton(QtWidgets.QMessageBox.Cancel)
        box.setDefaultButton(missing_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked == over_btn:
            for j, _ in ask:
                j.task_states = {}
            self._write_console(f">> Overwriting existing frames for {len(ask)} job(s).")
            return True
        if clicked != missing_btn:
            return False
        for j, r in ask:
            if not self._set_resume(j, r["have"], r["expected"], recheck_last=False):
                j.status = "Done"
                j.progress = 100.0
                j.verified = True
                j.missing_frames = []
                self._plan_done_ids.add(j.id)
                self._write_console(f">> {j.name}: all frames already exist — marked Done.")
        return True

    def _clear_temp_render_flags(self):
        for j in self.jobs:
            for attr in ("_frame_spec", "_partial_kind", "_resume_base"):
                if hasattr(j, attr):
                    delattr(j, attr)

    def _launch_render(self, only_ids=None):
        self._stop_event.clear()
        self.pause_after_job = False
        self.skip_current = False
        self._queue_running = True
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self._redraw_queue()
        self._save_state()
        self._render_thread = threading.Thread(
            target=self._render_loop, args=(self.nuke_path.text().strip(), only_ids), daemon=True
        )
        self._render_thread.start()

    def _start_render(self):
        if self._queue_running:
            return
        if not self.jobs:
            QtWidgets.QMessageBox.information(
                self, "Nothing to render", "The queue is empty. Add a Nuke script to get started."
            )
            return
        enabled = [
            j for j in self.jobs if j.enabled and not j.suspended and not j.locked and j.status != "Done"
        ]
        if not self._check_executables(enabled):
            return
        if self.settings.get("check_newer_versions", True):
            newer = [(j, self._newer_script_version(j)) for j in enabled]
            newer = [x for x in newer if x[1]]
            if newer:
                txt = "\n".join(f"{j.name}  →  {p.name}" for j, p in newer[:10])
                if (
                    QtWidgets.QMessageBox.question(
                        self,
                        "Newer Script Versions Found",
                        "Newer script versions exist:\n\n" + txt + "\n\nContinue with the queued versions?",
                        QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                    )
                    != QtWidgets.QMessageBox.Yes
                ):
                    return
        if not self._preflight(enabled):
            return
        if not self._prepare_snapshots_for_render(enabled):
            return
        self._clear_temp_render_flags()
        if not self._plan_existing_output(enabled):
            return
        for j in self.jobs:
            if not j.enabled:
                j.status = "Disabled"
                continue
            if j.suspended:
                j.status = "Suspended"
                continue
            if j.status != "Done":
                j.status = "Queued"
                j.progress = 0.0
                j.current_frame = None
        self._launch_render(None)

    def _render_single(self, row, frame_spec=None, kind=None):
        """Render one job. frame_spec renders only those frames (test / missing frames)."""
        if self._queue_running or not (0 <= row < len(self.jobs)):
            return
        j = self.jobs[row]
        if self._locked_guard([j], "render it again"):
            return
        if not self._check_executables([j]):
            return
        if not self._preflight([j]):
            return
        if not self._prepare_snapshots_for_render([j]):
            return
        self._clear_temp_render_flags()
        if frame_spec:
            j._frame_spec = frame_spec
            j._partial_kind = kind or "test"
        else:
            if not self._plan_existing_output([j]):
                return
            if j.id in self._plan_done_ids:
                self._redraw_queue()
                self._save_state()
                self.statusBar().showMessage(f"{j.name}: all frames already exist — nothing to render", 5000)
                return
            if j.status == "Done" and not getattr(j, "_frame_spec", None):
                j.task_states = {}  # a finished job is being rendered again from scratch
        j.enabled = True
        j.suspended = False
        j.status = "Queued"
        j.progress = 0.0
        j.current_frame = None
        j.error = ""
        self._launch_render([j.id])

    def _render_from_here(self):
        idx = self._current_row()
        if idx is None or self._queue_running:
            return
        ids = [j.id for j in self.jobs[idx:] if j.enabled and not j.suspended and not j.locked]
        if not ids:
            return
        if not self._check_executables([j for j in self.jobs if j.id in ids]):
            return
        if not self._preflight([j for j in self.jobs if j.id in ids]):
            return
        if not self._prepare_snapshots_for_render([j for j in self.jobs if j.id in ids]):
            return
        self._clear_temp_render_flags()
        if not self._plan_existing_output([j for j in self.jobs if j.id in ids and j.status != "Done"]):
            return
        for j in self.jobs[idx:]:
            if j.id in ids and j.status != "Done":
                j.status = "Queued"
                j.progress = 0.0
                j.current_frame = None
        self._launch_render(ids)

    def _set_frame_spec_and_render(self, frames):
        idx = self._current_row()
        if idx is None or self._queue_running:
            return
        self._render_single(idx, ",".join(str(x) for x in frames), "test")

    def _render_missing(self):
        idx = self._current_row()
        if idx is None or self._queue_running:
            return
        j = self.jobs[idx]
        self._verify_job_output(j)
        if j.verified is None:
            QtWidgets.QMessageBox.information(
                self,
                "Render Missing",
                "The output path of this job cannot be checked (several Writes, or an expression in the path).",
            )
            return
        if not j.missing_frames:
            QtWidgets.QMessageBox.information(self, "Render Missing", "No missing frames detected.")
            return
        self._render_single(idx, self._compress_frames(j.missing_frames).replace(" ", ""), "missing")

    def _request_stop(self):
        if not self._queue_running:
            return
        self._stop_event.set()
        self._write_console(">> Stop requested — terminating the current Nuke process.")

    # ------------------------------------------------------------------
    # Render worker (background thread). Jobs are tracked by id, never by row,
    # so editing the queue while rendering cannot redirect a render.
    # ------------------------------------------------------------------
    def _render_loop(self, nuke_exe, only_ids=None):
        reason = "complete"
        self._worker_job = None
        try:
            reason = self._render_loop_serial(nuke_exe, only_ids)
        except Exception as exc:
            import traceback

            reason = "error"
            self._events.put(("console", f"!! Render engine error: {exc}\n{traceback.format_exc()}"))
            j = self._worker_job
            if j is not None:
                self._events.put(("error_text", j.id, f"Render engine error: {exc}"))
                self._events.put(("state", j.id, "Failed", j.progress))
        finally:
            self.current_process = None
            self.current_job = None
            self._worker_job = None
            self._events.put(("queue_end", reason, getattr(self, "_run_failed", 0)))

    def _sleep_unless_stopped(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self._stop_event.is_set():
            time.sleep(0.25)

    def _wait_reason(self, job, only_ids):
        """'' = ready, 'blocked' = can never run in this pass, anything else = waiting status."""
        if job.schedule_at:
            try:
                target = datetime.datetime.fromisoformat(job.schedule_at)
                now = datetime.datetime.now(target.tzinfo) if target.tzinfo else datetime.datetime.now()
                if now < target:
                    return "Waiting"
            except Exception:
                pass
        if job.dependency_path:
            dep = next((x for x in self.jobs if x.path == job.dependency_path and x is not job), None)
            if dep is not None and dep.status != "Done":
                if (
                    dep.status in ("Failed", "Stopped", "Disabled", "Suspended", "Blocked")
                    or not dep.enabled
                    or dep.suspended
                ):
                    return "blocked"
                if only_ids is not None and dep.id not in only_ids:
                    return "blocked"
                return "Waiting"
        if job.require_assets:
            if self._dependency_report(job).get("missing"):
                return "Waiting for Assets"
        return ""

    def _render_loop_serial(self, nuke_exe, only_ids):
        log_dir = LOG_DIR
        log_dir.mkdir(parents=True, exist_ok=True)
        self._write_override_wrapper(log_dir)
        handled = set()
        shown = {}
        self._run_failed = 0
        slots = max(1, min(4, int(self.settings.get("parallel_renders", 1) or 1)))
        self._parallel_slots = slots
        if slots > 1:
            return self._render_loop_parallel(only_ids, slots, log_dir)
        runnable = (
            "Queued",
            "Waiting",
            "Waiting for Assets",
            "Waiting (resources)",
            "Tested",
            "Stopped",
            "Failed",
        )
        while not self._stop_event.is_set():
            # Re-scan every pass: jobs added from Nuke during a render are picked up too.
            src = [j for j in list(self.jobs) if only_ids is None or j.id in only_ids]
            pending = [
                j
                for j in src
                if j.id not in handled
                and j.enabled
                and not j.suspended
                and not j.locked
                and j.status in runnable
            ]
            pending.sort(key=lambda j: -j.job_priority)  # stable: queue order within same priority
            if not pending:
                break
            progressed = False
            for job in pending:
                if self._stop_event.is_set():
                    break
                why = self._wait_reason(job, only_ids)
                if why == "blocked":
                    handled.add(job.id)
                    job.status = "Blocked"
                    self._run_failed += 1
                    self._events.put(("state", job.id, "Blocked", job.progress))
                    self._events.put(
                        ("console", f"!! {job.name}: blocked — its dependency did not complete.")
                    )
                    continue
                if why:
                    if shown.get(job.id) != why:
                        shown[job.id] = why
                        self._events.put(("state", job.id, why, job.progress))
                    continue
                handled.add(job.id)
                progressed = True
                result = self._render_job(job, log_dir)
                if result == "stopped":
                    return "stopped"
                if self.pause_after_job:
                    self.pause_after_job = False
                    self._events.put(("console", ">> Queue paused after current job."))
                    return "paused"
                break  # re-evaluate priorities, dependencies and new jobs after every job
            if not progressed:
                self._sleep_unless_stopped(2.0)
        return "stopped" if self._stop_event.is_set() else "complete"

    # ------------------------------------------------------------------
    # Simultaneous renders (Preferences > Performance). Only used when set above 1;
    # otherwise jobs render one at a time.
    # ------------------------------------------------------------------
    def _threads_per_render(self):
        return max(1, (os.cpu_count() or 4) // max(1, getattr(self, "_parallel_slots", 1)))

    def _cache_per_render(self):
        if psutil is None:
            return None
        try:
            return max(
                1,
                int(
                    psutil.virtual_memory().total
                    / (1024**3)
                    * 0.7
                    / max(1, getattr(self, "_parallel_slots", 1))
                ),
            )
        except Exception:
            return None

    def _render_loop_parallel(self, only_ids, slots, log_dir):
        handled = set()
        shown = {}
        running = {}
        runnable = (
            "Queued",
            "Waiting",
            "Waiting for Assets",
            "Waiting (resources)",
            "Tested",
            "Stopped",
            "Failed",
        )
        cache = self._cache_per_render()
        self._events.put(
            (
                "console",
                f">> Simultaneous renders: up to {slots} Nuke processes, {self._threads_per_render()} threads"
                + (f" and {cache} GB cache" if cache else "")
                + " each.",
            )
        )
        while True:
            for jid, t in list(running.items()):
                if not t.is_alive():
                    running.pop(jid)
            if self._stop_event.is_set():
                for t in list(running.values()):
                    t.join(timeout=60)
                return "stopped"
            if self.pause_after_job:
                if not running:
                    self.pause_after_job = False
                    self._events.put(("console", ">> Queue paused after the running jobs."))
                    return "paused"
                time.sleep(0.5)
                continue
            src = [j for j in list(self.jobs) if only_ids is None or j.id in only_ids]
            pending = [
                j
                for j in src
                if j.id not in handled
                and j.enabled
                and not j.suspended
                and not j.locked
                and j.status in runnable
            ]
            pending.sort(key=lambda j: -j.job_priority)
            if not pending and not running:
                break
            for job in pending:
                if len(running) >= slots or self._stop_event.is_set() or self.pause_after_job:
                    break
                why = self._wait_reason(job, only_ids)
                if why == "blocked":
                    handled.add(job.id)
                    job.status = "Blocked"
                    self._run_failed += 1
                    self._events.put(("state", job.id, "Blocked", job.progress))
                    self._events.put(
                        ("console", f"!! {job.name}: blocked — its dependency did not complete.")
                    )
                    continue
                if why:
                    if shown.get(job.id) != why:
                        shown[job.id] = why
                        self._events.put(("state", job.id, why, job.progress))
                    continue
                handled.add(job.id)
                t = threading.Thread(target=self._render_job_guarded, args=(job, log_dir), daemon=True)
                running[job.id] = t
                t.start()
                self._sleep_unless_stopped(1.0)  # stagger Nuke start-up
            self._sleep_unless_stopped(0.5)
        return "stopped" if self._stop_event.is_set() else "complete"

    def _render_job_guarded(self, job, log_dir):
        try:
            self._render_job(job, log_dir)
        except Exception as exc:
            import traceback

            job.status = "Failed"
            self._run_failed = getattr(self, "_run_failed", 0) + 1
            self._events.put(("console", f"!! Render error in {job.name}: {exc}\n{traceback.format_exc()}"))
            self._events.put(("error_text", job.id, f"Render engine error: {exc}"))
            self._events.put(("state", job.id, "Failed", job.progress))

    def _render_job(self, job, log_dir):
        self._worker_job = job
        frame_spec = getattr(job, "_frame_spec", None)
        partial = bool(frame_spec)
        job.status = "Rendering"
        self._events.put(("state", job.id, "Rendering", 0.0))
        self._events.put(
            (
                "console",
                f">> Rendering {job.name} · frames {frame_spec or job.frames_label} · {job.write_node}",
            )
        )
        job_nuke = self._nuke_for(job)
        specs = self._frame_chunks(job, frame_spec)
        if not partial and specs and all(job.task_states.get(str(s)) == "COMPLETE" for s in specs):
            job.task_states = {}  # everything was already complete: this is a fresh re-render
        log_path = log_dir / (Path(job.path).stem + ".log")
        failed = False
        error_text = ""
        chunks_done = 0
        for spec in specs:
            if self._stop_event.is_set():
                break
            key = str(spec)
            if not partial and job.task_states.get(key) == "COMPLETE":
                chunks_done += 1
                continue
            attempts = 0
            outcome = "failed"
            while attempts <= job.retry_limit:
                if not self._resource_gate(job):
                    self._events.put(("state", job.id, "Waiting (resources)", job.progress))
                    max_wait = float(self.settings.get("resource_wait_max_min", 10) or 0) * 60
                    waited_from = time.monotonic()
                    while not self._stop_event.is_set() and not self._resource_gate(job, quiet=True):
                        if max_wait and time.monotonic() - waited_from > max_wait:
                            self._events.put(
                                (
                                    "console",
                                    f">> Resource protection: waited {max_wait / 60:.0f} min — starting anyway.",
                                )
                            )
                            break
                        self._sleep_unless_stopped(2.0)
                    self._events.put(("state", job.id, "Rendering", job.progress))
                if self._stop_event.is_set():
                    break
                attempts += 1
                try:
                    cmd = self._build_command(job, job_nuke, spec)
                except Exception as e:
                    error_text = str(e)
                    outcome = "failed"
                    self._events.put(("console", f"!! {e}"))
                    break
                if not partial:
                    job.task_states[key] = "RENDERING"
                self._events.put(
                    (
                        "console",
                        f">> Chunk {spec or 'script range'}"
                        + (f" — attempt {attempts}" if attempts > 1 else ""),
                    )
                )
                started = time.monotonic()
                rc, error_text, outcome = self._run_process(cmd, job, log_path)
                job.attempt_history.append(
                    {
                        "time": datetime.datetime.now().isoformat(timespec="seconds"),
                        "task": key,
                        "attempt": attempts,
                        "returncode": rc,
                        "duration": round(time.monotonic() - started, 3),
                    }
                )
                job.attempt_history = job.attempt_history[-200:]
                if outcome == "ok":
                    break
                if not partial:
                    job.task_states[key] = "FAILED"
                if outcome in ("stopped", "skipped"):
                    break
                if attempts <= job.retry_limit:
                    self._events.put(("console", f"!! Chunk failed; retrying ({attempts}/{job.retry_limit})"))
            if self._stop_event.is_set():
                if not partial and job.task_states.get(key) == "RENDERING":
                    job.task_states[key] = "QUEUED"
                break
            if outcome == "skipped":
                job.status = "Stopped"
                self._events.put(("error_text", job.id, "Skipped by user."))
                self._events.put(("state", job.id, "Stopped", job.progress))
                self._events.put(("console", f">> Skipped: {job.name}"))
                return "skipped"
            if outcome != "ok":
                failed = True
                break
            if not partial:
                job.task_states[key] = "COMPLETE"
            chunks_done += 1
            self._events.put(
                ("state", job.id, "Rendering", min(99.0, 100.0 * chunks_done / max(1, len(specs))))
            )
        if self._stop_event.is_set():
            job.status = "Stopped"
            self._events.put(("state", job.id, "Stopped", job.progress))
            return "stopped"
        if failed:
            et = self._classify_error(error_text)
            job.last_error_type = et
            job.status = "Failed"
            self._run_failed = getattr(self, "_run_failed", 0) + 1
            self._events.put(("error_text", job.id, f"{et}\n\n{error_text[-6000:]}"))
            self._events.put(("state", job.id, "Failed", job.progress))
            self._events.put(("console", f"!! {et}: {job.name}"))
            return "failed"
        if (
            partial and getattr(job, "_partial_kind", "test") == "test"
        ):  # "missing" and "resume" finish as Done
            # A test render is not a finished job: START will still render the full range.
            job.status = "Tested"
            self._events.put(("state", job.id, "Tested", 0.0))
            self._events.put(("console", f">> Test frames finished: {job.name} ({frame_spec})"))
            return "tested"
        job.status = "Done"
        self._events.put(("state", job.id, "Done", 100.0))
        self._events.put(("verify", job.id))
        self._events.put(("console", f">> Completed: {job.name}"))
        return "done"

    def _build_command(self, job, job_nuke, spec):
        # Nuke frame lists are separate ranges; pass each one as its own -F argument.
        ranges = [p.strip() for p in str(spec).split(",") if p.strip()] if spec else []
        limits = []
        if getattr(self, "_parallel_slots", 1) > 1:  # share the machine between simultaneous renders
            limits = ["-m", str(self._threads_per_render())]
            cache = self._cache_per_render()
            if cache:
                limits += ["-c", f"{cache}G"]
        use_snapshot = self._snapshots_enabled() and self._has_snapshot(job)
        if job.snapshot_path and self._snapshots_enabled() and not use_snapshot:
            self._events.put(
                ("console", f"!! Snapshot missing for {job.name}; rendering the live script instead.")
            )
        if job.output_override or use_snapshot:
            target = job.write_node if job.write_node not in ("", None, "all") else "(all)"
            if job.output_override and target == "(all)":
                if len(job.write_nodes) != 1:
                    raise RuntimeError("Choose a specific Write node before using an output override.")
                target = job.write_nodes[0]["name"]
            # The helper opens the snapshot (or the script) and restores root.name to the original path,
            # so [value root.name] expressions and relative paths behave exactly as in the GUI.
            script = job.snapshot_path if use_snapshot else job.path
            original = job.path if use_snapshot else ""
            return (
                [job_nuke, "-i"]
                + limits
                + [
                    "-t",
                    str(self._override_wrapper_path()),
                    script,
                    original,
                    target,
                    job.output_override or "",
                    " ".join(ranges),
                ]
            )
        only_write = [] if job.renders_all_writes else ["-X", job.write_node]
        frame_args = [arg for r in ranges for arg in ("-F", r)]
        return [job_nuke, "-i", *limits, "-x", *only_write, *frame_args, job.path]

    @staticmethod
    def _override_wrapper_path():
        return LOG_DIR / "batch_gartner_override_render.py"

    def _write_override_wrapper(self, log_dir):
        try:
            self._override_wrapper_path().write_text(OVERRIDE_WRAPPER, encoding="utf-8")
        except Exception as e:
            self._events.put(("console", f"!! Could not write override helper: {e}"))

    @staticmethod
    def _kill_process(proc):
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass

    def _run_process(self, cmd, job, log_path):
        """Run one Nuke process. Stop / skip / timeout are checked every 0.25 s, even if Nuke prints nothing."""
        kwargs = dict(
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        kwargs.update(_hidden())
        if Path(job.path).parent.is_dir():
            kwargs["cwd"] = str(Path(job.path).parent)
        self.skip_current = False
        try:
            proc = subprocess.Popen(cmd, **kwargs)
        except Exception as e:
            return None, f"Could not start Nuke: {e}", "failed"
        self.current_process = proc
        self.current_job = job
        if not hasattr(self, "_procs"):
            self._procs = {}
        self._procs[job.id] = proc
        self._apply_process_power(proc, job)
        out_q = pyqueue.Queue()
        tail = []

        def reader():
            try:
                for line in proc.stdout:
                    out_q.put(line)
            except Exception as e:
                out_q.put(f"[output read error: {e}]\n")
            finally:
                out_q.put(None)

        threading.Thread(target=reader, daemon=True).start()
        started = time.monotonic()
        limit = job.task_timeout_min * 60
        outcome = None
        note = ""
        last_done_wall = time.time()  # time the last frame was reported finished
        with log_path.open("a", encoding="utf-8", errors="replace") as lf:
            lf.write(f"\n\n=== {datetime.datetime.now().isoformat()} | {subprocess.list2cmdline(cmd)} ===\n")
            while True:
                try:
                    line = out_q.get(timeout=0.25)
                except pyqueue.Empty:
                    line = ""
                if line is None:
                    break
                if line:
                    tail.append(line)
                    tail = tail[-200:]
                    lf.write(line)
                    self._events.put(
                        (
                            "console",
                            f"[{job.name} / {job.write_node}] {line}"
                            if getattr(self, "_parallel_slots", 1) > 1
                            else line,
                        )
                    )
                    fm = re.search(r"\bFrame\s*[:#]?\s*(-?\d+)", line, re.I)
                    if fm:
                        self._events.put(("frame", job.id, int(fm.group(1))))
                        last_done_wall = time.time()
                    elif re.search(r"^Writing .* took ", line):
                        last_done_wall = time.time()
                if self._stop_event.is_set():
                    outcome = "stopped"
                    break
                if self.skip_current:
                    self.skip_current = False
                    outcome = "skipped"
                    note = "Skipped by user"
                    break
                if limit and time.monotonic() - started > limit:
                    outcome = "timeout"
                    note = f"Task timeout after {job.task_timeout_min} minutes"
                    self._events.put(("console", f"!! {note}"))
                    break
            if outcome:
                self._kill_process(proc)
                self._delete_partial_frames(job, last_done_wall)
            else:
                try:
                    proc.wait(timeout=30)
                except Exception:
                    self._kill_process(proc)
        self.current_process = None
        self.current_job = None
        self._procs.pop(job.id, None)
        if outcome is None:
            outcome = "ok" if proc.returncode == 0 else "failed"
        text = "".join(tail)
        if note:
            text = note + "\n" + text
        return proc.returncode, text, outcome

    PREVIEW_EXTS = {".tif", ".tiff", ".png", ".jpg", ".jpeg", ".bmp", ".tga", ".webp", ".gif", ".exr"}

    def _set_preview_message(self, text):
        self.preview_label.setPixmap(QtGui.QPixmap())
        self.preview_label.setText(text)
        self.preview_slider.setEnabled(False)
        self.preview_frame_lbl.setText("—")
        self._preview_shown = None

    def _preview_frames_for(self, job):
        """[(frame, path)] of finished frames only — never the frame Nuke is writing right now."""
        info = self._sequence_info(job)
        if not info:
            return []
        pairs = sorted(zip(info["existing"], info["files"]))
        if job.status == "Rendering":
            # Finished = written no later than the last frame Nuke reported as done
            # (or before this render started, for frames from an earlier run).
            cutoff = getattr(job, "last_frame_wall", None)
            if cutoff is None and job.render_started_at is not None:
                cutoff = time.time() - (time.monotonic() - job.render_started_at)
            if cutoff is not None:
                ok = []
                for fr, fp in pairs:
                    try:
                        if os.path.getmtime(fp) <= cutoff:
                            ok.append((fr, fp))
                    except Exception:
                        pass
                pairs = ok
        return pairs

    def _refresh_preview(self, force=False):
        if not hasattr(self, "preview_label"):
            return
        idx = self._current_row()
        j = self.jobs[idx] if idx is not None and 0 <= idx < len(self.jobs) else None
        if j is None:
            self._preview_job_id = None
            self._preview_frames = []
            self._set_preview_message("No job selected")
            return
        changed_job = j.id != self._preview_job_id
        now = time.monotonic()
        if not force and not changed_job and now - self._preview_last_scan < 1.5:
            return  # throttle during renders
        self._preview_last_scan = now
        self._preview_job_id = j.id
        out = j.output_path or ""
        if not self._checkable_output(j):
            self._preview_frames = []
            self._set_preview_message(
                "Preview needs a single image-sequence output\n(one Write node, no expressions in the path)"
            )
            return
        ext = Path(re.sub(r"(#+|%0?\d*d)", "0", out)).suffix.lower()
        if ext not in self.PREVIEW_EXTS:
            self._preview_frames = []
            self._set_preview_message(
                f"No preview for {ext or 'this'} files\n(EXR, TIFF, PNG, JPG are supported)"
            )
            return
        frames = self._preview_frames_for(j)
        if not frames:
            self._preview_frames = []
            self._set_preview_message("No finished frames yet")
            return
        keep = (
            self._preview_shown[1]
            if (self._preview_shown and not changed_job and not self.preview_follow.isChecked())
            else None
        )
        self._preview_frames = frames
        target = len(frames) - 1
        if keep is not None:
            target = next((i for i, (fr, _) in enumerate(frames) if fr == keep), target)
        self.preview_slider.blockSignals(True)
        self.preview_slider.setRange(0, len(frames) - 1)
        self.preview_slider.setValue(target)
        self.preview_slider.setEnabled(len(frames) > 1)
        self.preview_slider.blockSignals(False)
        self._show_preview_index(target)

    def _preview_slider_moved(self, value):
        if self.preview_follow.isChecked() and value != len(self._preview_frames) - 1:
            self.preview_follow.blockSignals(True)
            self.preview_follow.setChecked(False)
            self.preview_follow.blockSignals(False)
        if 0 <= value < len(self._preview_frames):
            self.preview_frame_lbl.setText(str(self._preview_frames[value][0]))
        self._preview_debounce.start()

    def _load_preview_at_slider(self):
        self._show_preview_index(self.preview_slider.value())

    def _show_preview_index(self, i):
        if not (0 <= i < len(self._preview_frames)):
            return
        fr, fp = self._preview_frames[i]
        try:
            key = (self._preview_job_id, fr, os.path.getmtime(fp))
        except Exception:
            key = (self._preview_job_id, fr, 0)
        self.preview_frame_lbl.setText(str(fr))
        if key == self._preview_shown:
            return
        self._preview_pending = (key, fp)
        if not self._preview_loading:
            self._start_preview_load()

    def _start_preview_load(self):
        req = self._preview_pending
        self._preview_pending = None
        if not req:
            return
        self._preview_loading = True
        w = max(64, self.preview_label.width())
        h = max(64, self.preview_label.height())

        def work():
            img = None
            err = ""
            try:
                with open(req[1], "rb") as fh:
                    data = fh.read()  # the file is closed immediately
                if req[1].lower().endswith(".exr"):
                    img = _exr_preview_image(req[1], data, w * 2, h * 2)
                    self._events.put(("preview", req, img, ""))
                    return
                buf = QtCore.QBuffer()
                buf.setData(QtCore.QByteArray(data))
                buf.open(QtCore.QIODevice.ReadOnly)
                reader = QtGui.QImageReader(buf)
                size = reader.size()
                if size.isValid() and (size.width() > w * 2 or size.height() > h * 2):
                    reader.setScaledSize(size.scaled(w * 2, h * 2, QtCore.Qt.KeepAspectRatio))
                img = reader.read()
                if img.isNull():
                    err = reader.errorString() or "unsupported image"
            except Exception as e:
                err = str(e)
            self._events.put(("preview", req, img, err))

        threading.Thread(target=work, daemon=True).start()

    def _preview_loaded(self, req, img, err):
        self._preview_loading = False
        key, fp = req
        if key[0] == self._preview_job_id:
            if img is not None and not img.isNull():
                pix = QtGui.QPixmap.fromImage(img).scaled(
                    self.preview_label.size(), QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation
                )
                self.preview_label.setText("")
                self.preview_label.setPixmap(pix)
                self._preview_shown = key
                self.preview_label.setToolTip(f"Frame {key[1]}\n{fp}")
            else:
                self.preview_label.setPixmap(QtGui.QPixmap())
                self.preview_label.setText(f"Can't read frame {key[1]}:\n{err}")
        if self._preview_pending:
            self._start_preview_load()

    def _delete_partial_frames(self, job, since):
        """After killing Nuke, delete frames written after the last finished one: they are incomplete."""
        try:
            if not self._checkable_output(job):
                return
            info = self._sequence_info(job)
            if not info:
                return
            for fr, fp in zip(info["existing"], info["files"]):
                try:
                    if os.path.getmtime(fp) > since:
                        os.remove(fp)
                        self._events.put(("console", f">> Removed partially written frame {fr}: {fp}"))
                except Exception:
                    pass
        except Exception:
            pass

    def _job_by_id(self, job_id):
        return next((j for j in self.jobs if j.id == job_id), None)

    def _drain_events(self):
        """Yield every message waiting in the event queue without blocking."""
        while True:
            try:
                yield self._events.get_nowait()
            except pyqueue.Empty:
                return

    def _process_events(self):
        """Apply messages from the listener and render threads (runs on the UI thread)."""
        for kind, *rest in self._drain_events():
            if kind == "console":
                self._write_console(rest[0])
            elif kind == "state":
                job_id, status, pct = rest
                j = self._job_by_id(job_id)
                if j is None:
                    continue
                if status == "Rendering" and j.status not in ("Rendering", "Waiting (resources)"):
                    j.render_started_at = time.monotonic()
                    j.last_frame_at = None
                    j.last_frame_wall = None
                    j.frame_durations = []
                    self._add_activity(f"STARTED  {j.name}")
                changed = j.status != status
                j.status = status
                j.progress = pct
                self._redraw_queue()
                if changed:
                    self._save_state()  # persist real state so crash recovery sees it
                if status == "Failed":
                    self._add_activity(f"FAILED   {j.name}")
                    (
                        self._notify("Nuke render failed", j.name)
                        if self.settings.get("notify_failed", True)
                        else None
                    )
                elif status == "Done":
                    self._add_activity(f"COMPLETE {j.name}")
                    (
                        self._notify("Nuke render complete", j.name)
                        if self.settings.get("notify_completed", True)
                        else None
                    )
                elif status == "Stopped":
                    self._add_activity(f"STOPPED  {j.name}")
                elif status == "Tested":
                    self._add_activity(f"TESTED   {j.name}")
                if changed and hasattr(self, "_preview_last_scan"):
                    self._preview_last_scan = 0.0
                elif status == "Blocked":
                    self._add_activity(f"BLOCKED  {j.name}")
            elif kind == "error_text":
                j = self._job_by_id(rest[0])
                if j is not None:
                    j.error = rest[1]
            elif kind == "frame":
                job_id, frame = rest
                j = self._job_by_id(job_id)
                if j is None:
                    continue
                now = time.monotonic()
                if j.last_frame_at is not None and j.current_frame != frame:
                    dt = now - j.last_frame_at
                    if 0 < dt < 3600:
                        j.frame_durations.append(dt)
                        j.frame_durations = j.frame_durations[-30:]
                    if len(j.frame_durations) >= 5:
                        baseline = sum(j.frame_durations[:-1]) / max(1, len(j.frame_durations) - 1)
                        if baseline > 0 and dt > baseline * 3:
                            j.heavy_frames = (j.heavy_frames + [(j.current_frame, round(dt, 2))])[-30:]
                j.last_frame_at = now
                j.current_frame = frame
                j.last_frame_wall = time.time()
                j.progress = self._progress_for_frame(j, frame)
                self._redraw_queue(light=True)
            elif kind == "verify":
                j = self._job_by_id(rest[0])
                if j is not None:
                    if self.settings.get("verify_outputs", True):
                        self._verify_job_output(j, add_history=True)
                    else:
                        j.completed_at = datetime.datetime.now().isoformat(timespec="seconds")
                        self._save_state()
            elif kind == "external_jobs":
                self._bring_to_front()
                self._add_external_jobs(rest[0])
            elif kind == "preview":
                self._preview_loaded(*rest)
            elif kind == "show_window":
                self._bring_to_front()
            elif kind == "queue_end":
                reason = rest[0] if rest else "complete"
                failed_count = rest[1] if len(rest) > 1 else 0
                self._queue_running = False
                self.start_button.setEnabled(True)
                self.stop_button.setEnabled(False)
                self._clear_temp_render_flags()
                for j in self.jobs:
                    if j.status in ("Waiting", "Waiting for Assets", "Waiting (resources)", "Rendering"):
                        j.status = "Stopped" if reason != "complete" else "Queued"
                self._redraw_queue()
                self._save_state()
                self._refresh_session_view()
                messages = {
                    "complete": "Queue complete",
                    "stopped": "Queue stopped",
                    "paused": "Queue paused — press START to continue",
                    "error": "Queue halted by an internal error (see LOG)",
                }
                msg = messages.get(reason, "Queue finished")
                if reason == "complete" and failed_count:
                    msg = f"Queue finished — {failed_count} job{'s' if failed_count != 1 else ''} failed (see the ERRORS tab)"
                self._add_activity(msg.upper())
                self._write_console(">> " + msg + ".")
                self.statusBar().showMessage(msg)
                if reason == "complete":
                    self._notify("Batch Gartner", msg)
                    if self.settings.get("open_output_after_render", False):
                        done = next(
                            (
                                x
                                for x in reversed(self.jobs)
                                if x.status == "Done" and x.output_path and not x.output_path.startswith("(")
                            ),
                            None,
                        )
                        if done:
                            try:
                                self._open_path(Path(done.output_path).parent)
                            except Exception:
                                pass
                    # Shutdown / sleep only after a queue that really finished, never after STOP or pause.
                    self._post_render_power_action()
                elif self.shutdown_action.isChecked() or self.sleep_action.isChecked():
                    self._write_console(
                        ">> Post-render shutdown/sleep skipped because the queue did not complete."
                    )

    def _bring_to_front(self):
        # Restore a minimized/hidden instance and request foreground activation.
        self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()
        QtCore.QTimer.singleShot(120, self.raise_)
        QtCore.QTimer.singleShot(140, self.activateWindow)

    def closeEvent(self, event):
        if self._queue_running:
            ans = QtWidgets.QMessageBox.question(
                self, "Render running", "Nuke is still rendering.\n\nStop the render and quit?"
            )
            if ans != QtWidgets.QMessageBox.Yes:
                event.ignore()
                return
            self._stop_event.set()
            procs = list((getattr(self, "_procs", {}) or {}).values()) or (
                [self.current_process] if self.current_process is not None else []
            )
            for proc in procs:
                self._kill_process(proc)
            for j in self.jobs:
                if j.status == "Rendering":
                    j.status = "Stopped"
        if hasattr(self, "_ipc_stop"):
            self._ipc_stop.set()
        self._save_state()
        try:
            CLEAN_EXIT_FILE.write_text(datetime.datetime.now().isoformat())
        except Exception:
            pass
        event.accept()


def _forward_to_running_instance(port):
    """If Batch Gartner is already running, ask it to come to the front and return True."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5) as sock:
            sock.sendall(json.dumps({"version": 1, "command": "show", "jobs": []}).encode("utf-8"))
            sock.shutdown(socket.SHUT_WR)
            return sock.recv(64).startswith(b"OK")
    except Exception:
        return False


def main():
    try:
        port = int(read_settings().get("receiver_port", 54321))
    except Exception:
        port = 54321
    if _forward_to_running_instance(port):
        print("Batch Gartner is already running — brought the existing window to the front.")
        return
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("Batch Gartner")
    app.setStyle("Fusion")
    log_dir = LOG_DIR

    def report(title, text):
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            with (log_dir / "batch_gartner_errors.log").open("a", encoding="utf-8") as fh:
                fh.write(
                    f"\n=== {datetime.datetime.now().isoformat()} {title} (v{APP_VERSION}) ===\n{text}\n"
                )
        except Exception:
            pass

    def excepthook(etype, value, tb):
        import traceback

        text = "".join(traceback.format_exception(etype, value, tb))
        report("Unhandled error", text)
        sys.__stderr__.write(text) if sys.__stderr__ else None

    sys.excepthook = excepthook
    try:
        win = BatchRenderApp()
    except Exception:
        import traceback

        text = traceback.format_exc()
        report("Startup failed", text)
        QtWidgets.QMessageBox.critical(
            None,
            "Batch Gartner could not start",
            "Batch Gartner hit an error while starting.\n\nThe details were saved to:\n"
            f"{log_dir / 'batch_gartner_errors.log'}\n\n" + text[-1500:],
        )
        sys.exit(1)
    win.showNormal()
    win.show()
    win.raise_()
    win.activateWindow()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
