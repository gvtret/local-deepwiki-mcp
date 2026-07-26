"""Multi-repo registry and lifecycle for local-deepwiki Hub."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
_WIKI_PORT_BASE = int(os.environ.get("LOCAL_DEEPWIKI_WIKI_PORT_BASE", "5600"))
_MAX_WIKI_PORTS = 50


def _root() -> Path:
    return Path(os.environ.get("LOCAL_DEEPWIKI_ROOT", Path.cwd())).resolve()


def data_dir() -> Path:
    d = _root() / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def repos_dir() -> Path:
    d = data_dir() / "repos"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _deepwiki_bin() -> str:
    """Prefer project venv entrypoint (works under systemd without uv in PATH)."""
    candidates = [
        _root() / ".venv" / "bin" / "deepwiki",
        Path(os.environ.get("HOME", "")) / ".local" / "bin" / "deepwiki",
    ]
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    # Fallback: uv from common install locations
    for uv in (
        Path(os.environ.get("HOME", "")) / ".local" / "bin" / "uv",
        Path("/usr/local/bin/uv"),
        Path("/usr/bin/uv"),
    ):
        if uv.is_file() and os.access(uv, os.X_OK):
            return str(uv)  # caller must use uv-run form — see _deepwiki_cmd
    return "deepwiki"


def _deepwiki_cmd(*args: str) -> list[str]:
    bin_path = _deepwiki_bin()
    if bin_path.endswith("/uv") or bin_path == "uv":
        return [bin_path, "run", "deepwiki", *args]
    return [bin_path, *args]


def _proc_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("LOCAL_DEEPWIKI_ROOT", str(_root()))
    # Ensure venv + user-local bins are visible to child processes.
    extras = [
        str(_root() / ".venv" / "bin"),
        str(Path(os.environ.get("HOME", "")) / ".local" / "bin"),
    ]
    path = env.get("PATH", "")
    for e in reversed(extras):
        if e and e not in path.split(":"):
            path = f"{e}:{path}" if path else e
    env["PATH"] = path
    return env


def registry_path() -> Path:
    return data_dir() / "repos.json"


@dataclass
class RepoRecord:
    name: str
    git_url: str
    path: str
    created_at: float
    status: str = "idle"  # idle | cloning | indexing | ready | error
    message: str = ""
    last_indexed_at: float | None = None
    wiki_port: int | None = None

    @property
    def wiki_path(self) -> Path:
        return Path(self.path) / ".deepwiki"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RepoManager:
    """Thread-safe registry of cloned/indexed repositories."""

    _lock: threading.RLock = field(default_factory=threading.RLock)
    _repos: dict[str, RepoRecord] = field(default_factory=dict)
    _jobs: dict[str, subprocess.Popen] = field(default_factory=dict)
    _wiki_procs: dict[str, subprocess.Popen] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.load()

    def load(self) -> None:
        path = registry_path()
        if not path.exists():
            self._repos = {}
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._repos = {}
            return
        repos: dict[str, RepoRecord] = {}
        for item in raw.get("repos", []):
            rec = RepoRecord(**item)
            if rec.status in ("cloning", "indexing"):
                rec.status = "error"
                rec.message = "Interrupted by server restart"
            repos[rec.name] = rec
        self._repos = repos

    def save(self) -> None:
        path = registry_path()
        payload = {"repos": [r.to_dict() for r in self._repos.values()]}
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def list_repos(self) -> list[dict[str, Any]]:
        with self._lock:
            out = []
            for r in sorted(self._repos.values(), key=lambda x: x.name):
                d = r.to_dict()
                d["has_wiki"] = r.wiki_path.is_dir() and (r.wiki_path / "index.md").exists()
                out.append(d)
            return out

    def get(self, name: str) -> RepoRecord | None:
        with self._lock:
            return self._repos.get(name)

    @staticmethod
    def _validate_name(name: str) -> None:
        if not _NAME_RE.match(name):
            raise ValueError(
                "name must be 1-64 chars: letters, digits, . _ - (start alphanumeric)"
            )

    @staticmethod
    def _validate_git_url(url: str) -> None:
        if not url or len(url) > 2048:
            raise ValueError("git_url is required")
        if url.startswith(("git@", "ssh://")):
            return
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https", "git"):
            raise ValueError("git_url must be http(s), git://, or git@…")

    def add_repo(self, name: str, git_url: str) -> RepoRecord:
        self._validate_name(name)
        self._validate_git_url(git_url.strip())
        git_url = git_url.strip()
        with self._lock:
            if name in self._repos:
                raise ValueError(f"repo already exists: {name}")
            dest = repos_dir() / name
            if dest.exists():
                raise ValueError(f"directory already exists: {dest}")
            rec = RepoRecord(
                name=name,
                git_url=git_url,
                path=str(dest),
                created_at=time.time(),
                status="cloning",
                message="Cloning…",
            )
            self._repos[name] = rec
            self.save()
        threading.Thread(target=self._clone_and_index, args=(name,), daemon=True).start()
        return rec

    def _clone_and_index(self, name: str) -> None:
        with self._lock:
            rec = self._repos.get(name)
            if rec is None:
                return
            dest = Path(rec.path)
            url = rec.git_url
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(
                ["git", "clone", "--depth", "1", url, str(dest)],
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "git clone failed")
            with self._lock:
                rec = self._repos[name]
                rec.status = "indexing"
                rec.message = "Indexing…"
                self.save()
            self._run_index(name, full_rebuild=True)
        except Exception as exc:  # noqa: BLE001 — boundary for background job
            with self._lock:
                rec = self._repos.get(name)
                if rec:
                    rec.status = "error"
                    rec.message = str(exc)[:500]
                    self.save()

    def _run_index(self, name: str, *, full_rebuild: bool = False) -> None:
        with self._lock:
            rec = self._repos.get(name)
            if rec is None:
                return
            repo_path = rec.path
            rec.status = "indexing"
            rec.message = "Indexing…"
            self.save()

        cmd = _deepwiki_cmd("update", repo_path)
        if full_rebuild:
            cmd.append("--full-rebuild")
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(_root()),
                capture_output=True,
                text=True,
                timeout=int(os.environ.get("LOCAL_DEEPWIKI_INDEX_TIMEOUT", "7200")),
                check=False,
                env=_proc_env(),
            )
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "index failed").strip()
                wiki_ok = (Path(repo_path) / ".deepwiki" / "index.md").exists()
                # Late optional steps (codemap/onboarding) can fail on LLM overload
                # after the wiki is already usable — keep the repo ready.
                if wiki_ok:
                    with self._lock:
                        rec = self._repos[name]
                        rec.status = "ready"
                        rec.message = f"Ready (warnings): {err[-240:]}"
                        rec.last_indexed_at = time.time()
                        self.save()
                    return
                raise RuntimeError(err[-500:])
            with self._lock:
                rec = self._repos[name]
                rec.status = "ready"
                rec.message = "Ready"
                rec.last_indexed_at = time.time()
                self.save()
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                rec = self._repos.get(name)
                if rec:
                    wiki_ok = (Path(rec.path) / ".deepwiki" / "index.md").exists()
                    if wiki_ok and rec.status == "indexing":
                        rec.status = "ready"
                        rec.message = f"Ready (warnings): {str(exc)[:240]}"
                        rec.last_indexed_at = time.time()
                    else:
                        rec.status = "error"
                        rec.message = str(exc)[:500]
                    self.save()

    def reindex(self, name: str, *, full_rebuild: bool = False) -> RepoRecord:
        with self._lock:
            rec = self._repos.get(name)
            if rec is None:
                raise KeyError(name)
            if rec.status in ("cloning", "indexing"):
                raise ValueError("job already running")
            if not Path(rec.path).is_dir():
                raise ValueError("repo path missing")
            rec.status = "indexing"
            rec.message = "Indexing…"
            self.save()
        threading.Thread(
            target=self._run_index, args=(name,), kwargs={"full_rebuild": full_rebuild}, daemon=True
        ).start()
        with self._lock:
            return self._repos[name]

    def delete_repo(self, name: str) -> None:
        with self._lock:
            rec = self._repos.pop(name, None)
            if rec is None:
                raise KeyError(name)
            self._stop_wiki_locked(name)
            self.save()
            path = Path(rec.path)
        if path.is_dir() and path.resolve().is_relative_to(repos_dir().resolve()):
            shutil.rmtree(path, ignore_errors=True)

    def _allocate_port_locked(self) -> int:
        used = {r.wiki_port for r in self._repos.values() if r.wiki_port}
        for i in range(_MAX_WIKI_PORTS):
            port = _WIKI_PORT_BASE + i
            if port not in used:
                return port
        raise RuntimeError("no free wiki ports")

    def _kill_listeners_on_port(self, port: int) -> None:
        """Best-effort free a wiki port left by a previous Hub process."""
        try:
            subprocess.run(
                ["fuser", "-k", f"{port}/tcp"],
                check=False,
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    def ensure_wiki_server(self, name: str) -> int:
        with self._lock:
            rec = self._repos.get(name)
            if rec is None:
                raise KeyError(name)
            wiki = rec.wiki_path
            if not wiki.is_dir():
                raise ValueError("wiki not generated yet — wait for indexing")
            proc = self._wiki_procs.get(name)
            if proc is not None and proc.poll() is None and rec.wiki_port:
                return rec.wiki_port
            port = rec.wiki_port or self._allocate_port_locked()
            rec.wiki_port = port
            self.save()
            # Stale serve from a previous Hub instance may still hold the port.
            self._kill_listeners_on_port(port)
            # Bind loopback only; Hub proxies from the public port.
            cmd = _deepwiki_cmd(
                "serve",
                str(wiki),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            )
            log_dir = data_dir() / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / f"wiki-{name}.log"
            log_f = open(log_path, "a", encoding="utf-8")  # noqa: SIM115 — kept for process lifetime
            self._wiki_procs[name] = subprocess.Popen(
                cmd,
                cwd=str(_root()),
                env=_proc_env(),
                stdout=log_f,
                stderr=subprocess.STDOUT,
            )
            return port

    def _stop_wiki_locked(self, name: str) -> None:
        proc = self._wiki_procs.pop(name, None)
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        rec = self._repos.get(name)
        if rec:
            rec.wiki_port = None


_MANAGER: RepoManager | None = None
_MANAGER_LOCK = threading.Lock()


def get_manager() -> RepoManager:
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = RepoManager()
        return _MANAGER
