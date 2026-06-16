"""
Docker-based code execution sandbox.

Usage:
    from docker_runner import run_in_docker, DockerUnavailableError

    try:
        stdout, stderr, retcode = run_in_docker(
            code="print(input())",
            lang="py",
            test_input="hello",
            timeout=5,
        )
    except DockerUnavailableError:
        # fallback to local execution
        ...
"""

import os
import shutil
import subprocess
import sys
import tempfile

# ---------------------------------------------------------------------------
# Docker image map per language
# ---------------------------------------------------------------------------
DOCKER_IMAGES = {
    "py":   "python:3.10-slim",
    "cpp":  "gcc:latest",
    "java": "openjdk:17-slim",
    "js":   "node:20-slim",
}

DOCKER_MEMORY = "256m"
DOCKER_CPUS = "0.5"
DOCKER_TIMEOUT_KILL_AFTER = 3  # extra seconds after soft timeout to force kill


class DockerUnavailableError(RuntimeError):
    """Raised when Docker CLI is not found or daemon is unreachable."""
    pass


# ---------------------------------------------------------------------------
# Availability check (cached)
# ---------------------------------------------------------------------------

def docker_available() -> bool:
    """Return True if the docker CLI is reachable."""
    if not hasattr(docker_available, "_cache"):
        try:
            subprocess.run(
                ["docker", "info"],
                capture_output=True,
                timeout=5,
            )
            docker_available._cache = True
        except Exception:
            docker_available._cache = False
    return docker_available._cache


# ---------------------------------------------------------------------------
# Core runner
# ---------------------------------------------------------------------------

def run_in_docker(
    code: str,
    lang: str,
    test_input: str = "",
    timeout: int = 5,
) -> tuple[str, str, int]:
    """Execute user code inside a disposable Docker container.

    Returns (stdout, stderr, return_code).
    Raises DockerUnavailableError if Docker cannot be used.
    Raises subprocess.TimeoutExpired if the container times out (caller
    should catch and convert to TLE).
    """
    if not docker_available():
        raise DockerUnavailableError("Docker CLI not found or daemon not running")

    image = DOCKER_IMAGES.get(lang)
    if image is None:
        raise ValueError(f"Unsupported language: {lang}")

    tmpdir = tempfile.mkdtemp(prefix="oj_docker_")
    try:
        # --- write source file ---
        if lang == "py":
            src_name = "solution.py"
            with open(os.path.join(tmpdir, src_name), "w", encoding="utf-8") as f:
                f.write(code)
            cmd_parts = ["python", src_name]
            read_only = True

        elif lang == "cpp":
            src_name = "solution.cpp"
            with open(os.path.join(tmpdir, src_name), "w", encoding="utf-8") as f:
                f.write(code)
            # Compile + run in one shell invocation inside the container
            cmd_parts = [
                "sh", "-c",
                f"g++ -std=c++17 -O2 -Wall {src_name} -o solution && ./solution",
            ]
            read_only = False  # need write for the compiled binary

        elif lang == "java":
            src_name = "Main.java"
            bin_name = "Main.class"
            with open(os.path.join(tmpdir, src_name), "w", encoding="utf-8") as f:
                f.write(code)
            cmd_parts = [
                "sh", "-c",
                f"javac {src_name} && java Main",
            ]
            read_only = False

        elif lang == "js":
            src_name = "solution.js"
            with open(os.path.join(tmpdir, src_name), "w", encoding="utf-8") as f:
                f.write(code)
            cmd_parts = ["node", src_name]
            read_only = True

        else:
            raise ValueError(f"Unsupported language: {lang}")

        # --- build docker run command ---
        mode = "ro" if read_only else "rw"
        docker_cmd = [
            "docker", "run", "--rm",
            "--memory=" + DOCKER_MEMORY,
            "--cpus=" + DOCKER_CPUS,
            "-v", f"{tmpdir}:/workspace:{mode}",
            "-w", "/workspace",
            "-i",  # keep stdin open
            image,
        ] + cmd_parts

        proc = subprocess.run(
            docker_cmd,
            input=test_input,
            capture_output=True,
            text=True,
            timeout=timeout + DOCKER_TIMEOUT_KILL_AFTER,
        )
        return proc.stdout, proc.stderr, proc.returncode

    except subprocess.TimeoutExpired:
        raise
    except FileNotFoundError:
        raise DockerUnavailableError("docker command not found on PATH")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Convenience: try Docker, fallback to local subprocess
# ---------------------------------------------------------------------------

def _local_run(cmd, test_input, timeout, cwd):
    """Run a subprocess locally with resource limits on POSIX.

    On Unix uses ``resource.setrlimit`` to cap CPU time and address space.
    On Windows falls back to plain ``subprocess.run`` with timeout.
    """
    kwargs = {
        "input": test_input,
        "capture_output": True,
        "text": True,
        "timeout": timeout,
        "cwd": cwd,
    }

    if sys.platform != "win32":
        import resource

        def _limit():
            # CPU time – soft limit = timeout, hard limit = timeout + 2
            resource.setrlimit(
                resource.RLIMIT_CPU,
                (max(1, int(timeout)), max(2, int(timeout) + 2)),
            )
            # Address space – 256 MB
            resource.setrlimit(
                resource.RLIMIT_AS,
                (256 * 1024 * 1024, 256 * 1024 * 1024),
            )

        kwargs["preexec_fn"] = _limit

    return subprocess.run(cmd, **kwargs)


def run_sandboxed(
    code: str,
    lang: str,
    test_input: str = "",
    timeout: int = 5,
    prefer_docker: bool = True,
) -> tuple[str, str, int, str]:
    """Run code in Docker if available and desired, else fall back to local.

    Returns (stdout, stderr, return_code, mode) where *mode* is
    ``"docker"`` or ``"local"``.
    """
    if prefer_docker and docker_available():
        try:
            out, err, rc = run_in_docker(code, lang, test_input, timeout)
            return out, err, rc, "docker"
        except (DockerUnavailableError, subprocess.TimeoutExpired):
            pass  # fall through

    # --- local fallback ---
    tmpdir = tempfile.mkdtemp(prefix="oj_local_")
    try:
        if lang == "py":
            src = os.path.join(tmpdir, "solution.py")
            with open(src, "w", encoding="utf-8") as f:
                f.write(code)
            cmd = [sys.executable, src]

        elif lang == "cpp":
            src = os.path.join(tmpdir, "solution.cpp")
            exe = os.path.join(tmpdir, "solution.exe" if sys.platform == "win32" else "solution")
            with open(src, "w", encoding="utf-8") as f:
                f.write(code)
            comp = subprocess.run(
                ["g++", src, "-o", exe, "-std=c++17", "-O2", "-Wall"],
                capture_output=True, text=True, timeout=15,
            )
            if comp.returncode != 0:
                return "", comp.stderr, comp.returncode, "local"
            cmd = [exe]

        elif lang == "java":
            src = os.path.join(tmpdir, "Main.java")
            with open(src, "w", encoding="utf-8") as f:
                f.write(code)
            comp = subprocess.run(
                ["javac", src],
                capture_output=True, text=True, timeout=15,
            )
            if comp.returncode != 0:
                return "", comp.stderr, comp.returncode, "local"
            cmd = ["java", "-cp", tmpdir, "Main"]

        elif lang == "js":
            src = os.path.join(tmpdir, "solution.js")
            with open(src, "w", encoding="utf-8") as f:
                f.write(code)
            cmd = ["node", src]

        else:
            raise ValueError(f"Unsupported language: {lang}")

        proc = _local_run(cmd, test_input, timeout, tmpdir)
        return proc.stdout, proc.stderr, proc.returncode, "local"

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
