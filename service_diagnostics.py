import argparse
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
MAX_SECTION_CHARS = 64 * 1024


def _run(command: list[str], timeout: float = 5) -> str:
    if shutil.which(command[0]) is None:
        return f"命令不可用：{command[0]}"
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
        output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
        return (output or f"命令退出码：{result.returncode}")[-MAX_SECTION_CHARS:]
    except Exception as exc:
        return f"采集失败：{type(exc).__name__}: {exc}"


def _read(path: Path, limit: int = MAX_SECTION_CHARS) -> str:
    try:
        data = path.read_bytes()
        return data[-limit:].decode("utf-8", errors="replace")
    except Exception as exc:
        return f"读取失败：{type(exc).__name__}: {exc}"


def _data_dir() -> Path:
    path = Path(os.getenv("APP_DATA_DIR", "data"))
    return path if path.is_absolute() else ROOT_DIR / path


def _cgroup_memory_files() -> list[Path]:
    cgroup_file = Path("/proc/self/cgroup")
    if not cgroup_file.is_file():
        return []
    files: list[Path] = []
    for line in _read(cgroup_file).splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        controllers, relative = parts[1], parts[2].lstrip("/")
        if parts[0] == "0" and not controllers:
            base = Path("/sys/fs/cgroup") / relative
            names = ("memory.current", "memory.max", "memory.events", "memory.pressure")
        elif "memory" in controllers.split(","):
            base = Path("/sys/fs/cgroup/memory") / relative
            names = ("memory.usage_in_bytes", "memory.limit_in_bytes", "memory.failcnt", "memory.oom_control")
        else:
            continue
        files.extend(path for name in names if (path := base / name).is_file())
    return files


def write_crash_report(
    service: str,
    pid: int | None,
    returncode: int | None,
    reason: str,
    *,
    data_dir: Path | None = None,
) -> Path | None:
    """Persist a bounded diagnostic snapshot without copying environment secrets."""
    try:
        now = datetime.now().astimezone()
        report_dir = (data_dir or _data_dir()) / "logs" / "crash-reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        safe_service = "".join(char if char.isalnum() or char in "-_" else "_" for char in service)
        report_path = report_dir / f"{now:%Y%m%d-%H%M%S}-{safe_service}.log"

        sections: list[tuple[str, str]] = [
            ("事件", f"time={now.isoformat()}\nservice={service}\npid={pid}\nreturncode={returncode}\nreason={reason}"),
            ("运行环境", f"platform={platform.platform()}\npython={sys.version}\ncwd={Path.cwd()}"),
            ("Node.js", _run(["node", "--version"])),
            ("npm", _run(["npm", "--version"])),
        ]
        app_log = (data_dir or _data_dir()) / "logs" / "app.log"
        if app_log.is_file():
            sections.append(("应用日志末尾", _read(app_log)))

        if os.name == "nt":
            sections.extend([
                ("进程", _run(["tasklist"])),
                ("端口", _run(["netstat", "-ano"])),
            ])
        else:
            for path in (
                Path("/proc/loadavg"),
                Path("/proc/meminfo"),
                Path("/proc/self/cgroup"),
                *_cgroup_memory_files(),
            ):
                if path.is_file():
                    sections.append((str(path), _read(path)))
            unit = os.getenv("SYSTEMD_SERVICE_NAME", "dbagent.service")
            sections.extend([
                ("磁盘", _run(["df", "-h"])),
                ("进程", _run(["ps", "-eo", "pid,ppid,stat,%cpu,%mem,rss,etime,cmd", "--sort=-rss"])),
                ("端口", _run(["ss", "-lntp"])),
                ("systemd 资源限制", _run(["systemctl", "show", unit, "-p", "Result", "-p", "NRestarts", "-p", "MemoryCurrent", "-p", "MemoryMax", "-p", "TasksCurrent", "-p", "TasksMax", "-p", "OOMPolicy"])),
                ("systemd 单元日志", _run(["journalctl", "-u", unit, "--since", "-15min", "-n", "300", "--no-pager", "-o", "short-precise"])),
                ("内核日志", _run(["journalctl", "-k", "--since", "-15min", "-n", "200", "--no-pager", "-o", "short-precise"])),
            ])

        report_path.write_text(
            "\n\n".join(f"===== {title} =====\n{content.rstrip()}" for title, content in sections) + "\n",
            encoding="utf-8",
        )
        return report_path
    except Exception as exc:
        print(f"保存崩溃现场失败：{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="保存 DBAgent 服务诊断快照")
    parser.add_argument("--service", default="manual", help="异常服务名称")
    parser.add_argument("--pid", type=int, default=None, help="异常进程 PID")
    parser.add_argument("--returncode", type=int, default=None, help="进程退出码")
    parser.add_argument("--reason", default="manual diagnostic snapshot", help="采集原因")
    parser.add_argument("--systemd-stop", action="store_true", help="仅在 systemd 异常停止时采集")
    args = parser.parse_args()
    reason = args.reason
    if args.systemd_stop:
        service_result = os.getenv("SERVICE_RESULT", "unknown")
        if service_result == "success":
            return 0
        reason = (
            f"systemd SERVICE_RESULT={service_result}; "
            f"EXIT_CODE={os.getenv('EXIT_CODE', 'unknown')}; "
            f"EXIT_STATUS={os.getenv('EXIT_STATUS', 'unknown')}"
        )
    report = write_crash_report(args.service, args.pid, args.returncode, reason)
    if report is None:
        return 1
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
