"""Portable bounded processes, atomic files and native file locking."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shlex
import signal
import stat
import subprocess
import tempfile
import threading
import time


def encode(obj):
    return (json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")


def loads(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    def invalid(_):
        raise ValueError("non-finite JSON value")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def safe(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("absolute clean path required")
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("symlink or reparse point rejected")
        if item == path and not stat.S_ISREG(info.st_mode):
            raise ValueError("regular file required")


def read(path):
    path = Path(path)
    safe(path)
    try:
        if path.stat().st_size > 64 * 1024 * 1024:
            raise ValueError("file exceeds read limit")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(path, flags), "rb") as handle:
            data = handle.read(64 * 1024 * 1024 + 1)
        if len(data) > 64 * 1024 * 1024:
            raise ValueError("file exceeds read limit")
        return data
    except FileNotFoundError:
        return None


def atomic(path, data, mode=0o600):
    path = Path(path)
    safe(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix=".native-write-", dir=path.parent)
    with os.fdopen(fd, "wb") as handle:
        if os.name != "nt":
            os.fchmod(handle.fileno(), mode)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    safe(path)
    os.replace(temp, path)


def _initialize_windows_lock_byte(handle):
    handle.write(b"\0")
    handle.flush()


def _open_existing_windows_lock_file(path, flags, mode, deadline):
    while True:
        try:
            fd = os.open(path, flags, mode)
        except PermissionError as error:
            # The exclusive creator closes its handle before handing the file
            # to CRT locking. Retry only Windows sharing violations in that
            # short handoff window; ACL and other permission errors stay fatal.
            if getattr(error, "winerror", None) not in {32, 33} or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)
            continue
        try:
            handle = os.fdopen(fd, "r+b", buffering=0)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        try:
            size = os.fstat(handle.fileno()).st_size
        except BaseException:
            try:
                handle.close()
            except OSError:
                pass
            raise
        if size > 0:
            return handle
        handle.close()
        if time.monotonic() >= deadline:
            raise RuntimeError("lock file initialization timed out")
        # Another process has created the pathname but has not published the
        # stable byte yet. It alone owns initialization.
        time.sleep(0.01)


def _open_windows_lock_file(path, flags, mode, timeout):
    """Create the CRT lock byte once, or wait for its creator to finish."""
    deadline = time.monotonic() + timeout
    try:
        fd = os.open(path, flags | os.O_EXCL, mode)
    except FileExistsError:
        return _open_existing_windows_lock_file(path, flags, mode, deadline), deadline

    # O_EXCL chooses exactly one initializer. Close its exclusive-create handle
    # before opening the stable file for the byte-range lock.
    with os.fdopen(fd, "r+b", buffering=0) as handle:
        _initialize_windows_lock_byte(handle)
    return _open_existing_windows_lock_file(path, flags, mode, deadline), deadline


@contextmanager
def file_lock(path, timeout=5):
    path = Path(path)
    safe(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    if os.name == "nt":
        handle, deadline = _open_windows_lock_file(path, flags, 0o600, timeout)
    else:
        handle = os.fdopen(os.open(path, flags, 0o600), "r+b", buffering=0)
        deadline = time.monotonic() + timeout
    with handle:
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("resource is locked") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def clean_env():
    excluded = {"OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_API_KEY", "CODEX_HOME",
                "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONINSPECT"}
    env = {k: v for k, v in os.environ.items() if k.upper() not in excluded}
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    return env


def command_text(argv):
    if os.name == "nt":
        return "& " + " ".join("'" + str(x).replace("'", "''") + "'" for x in argv)
    return shlex.join([str(x) for x in argv])


def _windows_job(proc):
    # The process is created suspended and assigned before any descendant can
    # start. Closing the job kills the full tree, even after the root exits.
    import ctypes as c
    from ctypes import wintypes as w
    kernel = c.WinDLL("kernel32", use_last_error=True)
    class Basic(c.Structure):
        _fields_ = [("per_process", c.c_int64), ("per_job", c.c_int64),
                    ("flags", w.DWORD), ("min_working", c.c_size_t),
                    ("max_working", c.c_size_t), ("active", w.DWORD),
                    ("affinity", c.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]
    class IO(c.Structure):
        _fields_ = [(name, c.c_uint64) for name in ("readops", "writeops", "otherops", "readbytes", "writebytes", "otherbytes")]
    class Extended(c.Structure):
        _fields_ = [("basic", Basic), ("io", IO), ("processmem", c.c_size_t),
                    ("jobmem", c.c_size_t), ("peakprocess", c.c_size_t), ("peakjob", c.c_size_t)]
    kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
    kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise RuntimeError("Windows process job unavailable")
    info = Extended()
    info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    try:
        if not kernel.SetInformationJobObject(job, 9, c.byref(info), c.sizeof(info)):
            raise RuntimeError("Windows process job configuration failed")
        if not kernel.AssignProcessToJobObject(job, w.HANDLE(int(proc._handle))):
            raise RuntimeError("Windows process tree containment unavailable")
        resume = c.WinDLL("ntdll").NtResumeProcess
        resume.argtypes = [w.HANDLE]
        resume.restype = c.c_long
        if resume(w.HANDLE(int(proc._handle))) != 0:
            raise RuntimeError("Windows process resume failed")
    except BaseException:
        kernel.CloseHandle(job)
        raise
    proc._native_job = (kernel, job)


def spawn(argv, *, cwd=None, env=None):
    argv = [str(x) for x in argv]
    options = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                   cwd=cwd, env=env if env is not None else clean_env())
    if os.name == "nt":
        # npm exposes codex.cmd. cmd.exe expands metacharacters even with shell=False;
        # reject those tokens instead of executing ambiguous batch command text.
        if Path(argv[0]).suffix.lower() in (".cmd", ".bat"):
            if any(any(c in x for c in '\r\n"&|<>^%!') for x in argv):
                raise RuntimeError("batch launcher has unsafe arguments; use the official codex.exe path")
            # Supply cmd.exe its command line directly. Passing the /c payload
            # as a list element makes Popen escape its quotes with backslashes;
            # cmd.exe does not use the C runtime's backslash-quote convention.
            launcher = subprocess.list2cmdline([os.environ.get("COMSPEC", "cmd.exe")])
            argv = launcher + ' /d /s /c "' + subprocess.list2cmdline(argv) + '"'
        options["creationflags"] = 0x4 | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    try:
        if os.name == 'nt':
            proc = subprocess.Popen(argv, **options)
        else:
            from .process_tree import launch
            proc = launch(argv, options)
    except OSError:
        raise RuntimeError("command could not start") from None
    if os.name == "nt":
        try:
            _windows_job(proc)
        except BaseException:
            proc.kill()
            proc.wait()
            raise
    return proc


def stop(proc):
    if os.name == "nt":
        job = getattr(proc, "_native_job", None)
        if job:
            job[0].CloseHandle(job[1])
            proc._native_job = None
        elif proc.poll() is None:
            proc.kill()
    else:
        control = getattr(proc, '_native_control', None)
        if control is not None:
            os.close(control)
            proc._native_control = None
            result_fd = proc._native_cleanup_result
            proc._native_cleanup_result = None
            try:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=3)
                    raise RuntimeError('process supervisor did not stop') from None
                if os.read(result_fd, 2) != b'1':
                    raise RuntimeError('process cleanup was not confirmed')
            finally:
                os.close(result_fd)
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        raise RuntimeError("process tree did not stop") from None


def finish(proc, timeout=3):
    try:
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            stop(proc)
            return proc.returncode
    finally:
        stop(proc)


def run(argv, *, cwd=None, env=None, input=None, timeout=10, limit=2*1024*1024):
    proc = spawn(argv, cwd=cwd, env=env)
    buffers = [bytearray(), bytearray()]
    overflow = threading.Event()
    lock = threading.Lock()
    def drain(pipe, index):
        try:
            while True:
                chunk = pipe.read1(16384)
                if not chunk:
                    break
                with lock:
                    if sum(map(len, buffers)) + len(chunk) > limit:
                        overflow.set()
                        break
                    buffers[index].extend(chunk)
        finally:
            pipe.close()
    def feed():
        try:
            if input:
                proc.stdin.write(input)
                proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            proc.stdin.close()
    threads = [threading.Thread(target=drain, args=(proc.stdout, 0), daemon=True),
               threading.Thread(target=drain, args=(proc.stderr, 1), daemon=True),
               threading.Thread(target=feed, daemon=True)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    try:
        while any(t.is_alive() for t in threads) or proc.poll() is None:
            if overflow.is_set():
                raise RuntimeError("command output exceeded limit")
            if time.monotonic() >= deadline:
                raise RuntimeError("command timed out")
            time.sleep(0.01)
        if overflow.is_set():
            raise RuntimeError("command output exceeded limit")
        return subprocess.CompletedProcess(argv, proc.returncode, bytes(buffers[0]), bytes(buffers[1]))
    finally:
        try:
            stop(proc)
        finally:
            for thread in threads:
                thread.join(timeout=1)
            # A pipe reader owns its buffered lock. Closing it from this
            # thread while read() is blocked can defeat the timeout entirely.
            for thread, pipe in zip(threads, (proc.stdout, proc.stderr, proc.stdin)):
                if not thread.is_alive() and not pipe.closed:
                    pipe.close()
        if any(thread.is_alive() for thread in threads):
            raise RuntimeError('command streams did not stop after cleanup')
