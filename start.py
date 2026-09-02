import argparse
import os
import signal
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = ROOT_DIR / "backend"
FRONTEND_DIR = ROOT_DIR / "frontend"


def get_backend_python() -> Path:
    return Path(sys.executable)


def build_commands(production: bool = True):
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    backend_python = get_backend_python()
    if not backend_python.exists():
        raise RuntimeError(f"当前 Python 解释器不可用：{backend_python}")
    if npm is None:
        raise RuntimeError("未找到 npm，请先安装 Node.js。")
    if not (FRONTEND_DIR / "node_modules").exists():
        raise RuntimeError("未找到 frontend/node_modules，请先在 frontend 目录执行 npm install。")

    backend_host = os.getenv("APP_HOST", "127.0.0.1")
    backend_port = os.getenv("APP_PORT", "14499")
    frontend_host = os.getenv("FRONTEND_HOST", "127.0.0.1")
    frontend_port = os.getenv("FRONTEND_PORT", "6477")
    frontend_script = "start" if production else "dev"

    backend_command = [
        str(backend_python),
        "-m",
        "uvicorn",
        "main:app",
        "--app-dir",
        str(BACKEND_DIR),
        "--host",
        backend_host,
        "--port",
        backend_port,
    ]
    if not production:
        backend_command.extend(["--reload", "--reload-dir", str(BACKEND_DIR)])
    frontend_command = [
        npm,
        "run",
        frontend_script,
        "--",
        "--hostname",
        frontend_host,
        "--port",
        frontend_port,
    ]
    return backend_command, frontend_command, backend_port, frontend_port


def wait_until_ready(url: str, processes, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for name, process in processes:
            if process.poll() is not None:
                raise RuntimeError(
                    f"{name} 服务启动失败，进程退出码：{process.returncode}"
                )
        try:
            with urllib.request.urlopen(url, timeout=1):
                return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError(f"等待服务启动超时：{url}")


def start_service(command, cwd: Path):
    options = {"cwd": cwd}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    return subprocess.Popen(command, **options)


def signal_process_tree(process, force: bool = False) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        command = ["taskkill", "/PID", str(process.pid), "/T"]
        if force:
            command.append("/F")
        subprocess.run(command, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    try:
        os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
    except ProcessLookupError:
        return


def stop_processes(processes) -> None:
    for _name, process in processes:
        signal_process_tree(process)
    deadline = time.monotonic() + 5
    for _name, process in processes:
        if process.poll() is None:
            try:
                process.wait(max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                signal_process_tree(process, force=True)
                try:
                    process.wait(2)
                except subprocess.TimeoutExpired:
                    pass


def install_signal_handlers() -> None:
    def request_shutdown(signum, _frame):
        print(f"收到停止信号 {signal.Signals(signum).name}，正在清理前后端进程树。", file=sys.stderr, flush=True)
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, request_shutdown)
    if os.name != "nt" and hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)


def main() -> int:
    parser = argparse.ArgumentParser(description="统一启动 auto-agent 前后端服务")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dev", action="store_true", help="启动开发模式和热更新")
    mode.add_argument(
        "--production",
        action="store_true",
        help="兼容旧命令：构建并启动生产模式",
    )
    parser.add_argument("--build", action="store_true", help="启动前重新构建生产版前端")
    args = parser.parse_args()
    if args.dev and args.build:
        parser.error("--dev 不能与 --build 同时使用")

    load_dotenv(ROOT_DIR / ".env", override=False)
    install_signal_handlers()
    try:
        production = not args.dev
        backend_command, frontend_command, backend_port, frontend_port = build_commands(
            production
        )
        if args.build or args.production:
            npm = frontend_command[0]
            subprocess.run([npm, "run", "build"], cwd=FRONTEND_DIR, check=True)
        elif production and not (FRONTEND_DIR / ".next" / "BUILD_ID").is_file():
            raise RuntimeError("未找到生产构建，请先执行 python start.py --build。")

        processes = [
            ("backend", start_service(backend_command, ROOT_DIR)),
            ("frontend", start_service(frontend_command, FRONTEND_DIR)),
        ]
        try:
            wait_until_ready(f"http://127.0.0.1:{backend_port}/docs", processes)
            wait_until_ready(f"http://127.0.0.1:{frontend_port}/", processes)
            print("\nauto-agent 前后端已启动", flush=True)
            print(f"网页入口：http://127.0.0.1:{frontend_port}", flush=True)
            print(f"接口文档：http://127.0.0.1:{backend_port}/docs", flush=True)
            data_dir = Path(os.getenv("APP_DATA_DIR", "data"))
            if not data_dir.is_absolute():
                data_dir = ROOT_DIR / data_dir
            print(f"后端日志：{data_dir.resolve() / 'logs' / 'app.log'}", flush=True)
            print("按 Ctrl+C 同时停止前后端。\n", flush=True)

            while True:
                exited = next(
                    (
                        (name, process.returncode)
                        for name, process in processes
                        if process.poll() is not None
                    ),
                    None,
                )
                if exited is not None:
                    name, returncode = exited
                    print(
                        f"{name} 服务意外退出，进程退出码：{returncode}",
                        file=sys.stderr,
                        flush=True,
                    )
                    return returncode or 1
                time.sleep(0.5)
        finally:
            stop_processes(processes)
    except KeyboardInterrupt:
        return 0
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
