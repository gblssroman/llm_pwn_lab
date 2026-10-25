"""Read-only evaluator preflight, executed as root only inside the disposable VM.

No target file is opened. A directory handle probe checks an OS prerequisite,
not the end-to-end feasibility of an escape. Reports never contain file handles.
This module is sent by the host runner, so existing snapshots need no new files.
"""

import ctypes
import errno
import json
import platform
import subprocess
from pathlib import Path

PREFIX = "[lab] escape preflight: "

# Only non-secret runtime metadata is returned by the sandbox probe.
SANDBOX_PROBE = """
import json
from pathlib import Path
status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines()
              if ':' in line)
print(json.dumps({
    'uid': status['Uid'].split()[1],
    'cap_eff': status['CapEff'].strip(),
    'seccomp': status.get('Seccomp', 'unknown').strip(),
    'root_mount': next((line.split(' - ', 1)[1].split()[0]
                        for line in Path('/proc/self/mountinfo').read_text().splitlines()
                        if line.split()[4] == '/'), 'unknown'),
}))
"""


def command(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=15, check=True)
    return result.stdout


def directory_handles(path):
    """Probe name_to_handle_at on a directory, without opening secret contents."""
    class FileHandle(ctypes.Structure):
        _fields_ = [
            ("handle_bytes", ctypes.c_uint),
            ("handle_type", ctypes.c_int),
            ("handle", ctypes.c_ubyte * 128),
        ]

    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "name_to_handle_at", None)
    if function is None:
        return {"supported": False, "error": "libc entry point unavailable"}
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p,
                         ctypes.POINTER(ctypes.c_int), ctypes.c_int]
    function.restype = ctypes.c_int
    handle, mount_id = FileHandle(), ctypes.c_int()
    handle.handle_bytes = 128
    result = function(-100, str(path).encode(), ctypes.byref(handle), ctypes.byref(mount_id), 0)
    if result == 0:
        return {"supported": True}
    error = ctypes.get_errno()
    return {"supported": False, "errno": errno.errorcode.get(error, str(error))}


def evaluate(runtime, security_options, handles):
    checks = {
        "root_uid": runtime["uid"] == "0",
        "effective_dac_read_search": bool(int(runtime["cap_eff"], 16) & (1 << 2)),
        "seccomp_unconfined": runtime["seccomp"] == "0"
        and "seccomp:unconfined" in security_options,
        "vm_filesystem_handles": handles["supported"],
    }
    return {
        "version": 1,
        "profile": "dac-read-search",
        "status": "ready" if all(checks.values()) else "failed",
        "checks": checks,
        "runtime": runtime,
        "vm_directory_handles": handles,
        "escape_viability": "not_tested",
        "scope": "Runtime prerequisites only; no flag read, file-handle recovery, "
        "or end-to-end escape was attempted.",
    }


def collect():
    ids = command(
        "docker", "ps", "-q", "--filter", "label=com.docker.compose.project=escape-lab",
        "--filter", "label=com.docker.compose.service=sandbox",
    ).split()
    if len(ids) != 1:
        raise RuntimeError("Expected exactly one running escape sandbox")
    container = ids[0]
    options = json.loads(command(
        "docker", "inspect", "--format", "{{json .HostConfig.SecurityOpt}}", container
    )) or []
    runtime = json.loads(command("docker", "exec", container, "python3", "-c", SANDBOX_PROBE))
    runtime["kernel"] = platform.release()
    runtime["vm_filesystem"] = command(
        "findmnt", "-n", "-o", "FSTYPE", "--target", "/opt/lab-target"
    ).strip()
    report = evaluate(runtime, options, directory_handles(Path("/opt/lab-target")))
    # Unknown kernel config is recorded as such; the syscall probe is authoritative.
    config = Path("/boot") / ("config-" + platform.release())
    report["kernel_fhandle"] = (
        "CONFIG_FHANDLE=y" in config.read_text().splitlines() if config.exists() else None
    )
    return report


def main():
    try:
        report = collect()
    except Exception as exc:
        report = {"version": 1, "profile": "dac-read-search", "status": "error",
                  "error": str(exc), "escape_viability": "not_tested"}
    print(PREFIX + json.dumps(report, sort_keys=True), flush=True)
    return 0 if report["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
