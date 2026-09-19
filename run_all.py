#!/usr/bin/env python
"""FlawScope 一键启动（单窗口）。

## 为什么要自己写，而不是继续用 .bat

原来的 `启动全部.bat` 用 `start cmd /k` 拉起两个服务，于是**屏幕上会出现三个黑窗口**
（一个启动器 + 后端 + 前端），关窗口的顺序还影响能否干净停止服务：
关掉启动器窗口，两个服务照旧活着；关掉服务窗口，启动器不知道。
用户反馈"跳出命令行窗口不太美观"，根子在这里。

现在把两个服务都作为**本进程的子进程**跑，输出合并到同一个窗口并加前缀，
Ctrl+C 一次性停掉全部（含 npm 派生的 node 子进程）。

## 与 `/health/ready` 的配合

后端 import + 初始化要十几秒（详见 `startup_status.py` 与 `api.py` 的 lifespan）。
启动器**不阻塞等后端**——它只等前端端口起来就打开浏览器，
让页面自己去显示后端进度（顶栏状态灯会依次显示"正在启动 1/5 …"直到变绿）。
这样等待过程是可见的，而不是"双击之后盯着黑窗口猜还要多久"。

## 两个容易踩的坑（都已处理）

1. **本地探活必须绕过代理**。本机 `~/.gitconfig` / 环境变量里配了
   `http_proxy`，`urllib` 默认会走它，于是 `http://127.0.0.1:8000` 的请求
   会被代理拦掉（报 502 或空响应）。所以显式装一个空的 ProxyHandler。
2. **Windows 上必须杀进程树**。`npm run dev` 会派生 node 子进程，
   只 terminate 父进程会留下孤儿 node 占着 5173 端口，
   下次启动就报"端口被占用"。
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
BACKEND_PORT = 8000
FRONTEND_PORT = 5173
BACKEND_URL = f"http://127.0.0.1:{BACKEND_PORT}"
FRONTEND_URL = f"http://127.0.0.1:{FRONTEND_PORT}"

IS_WINDOWS = os.name == "nt"
# 子进程不弹自己的窗口。缺了这个，两个服务仍会各开一个黑窗口——
# 那正是要消除的现象。
_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0

# 已启动的服务。控制台关闭信号的处理函数需要一份全局引用
# （信号处理发生在 main() 的栈之外，拿不到局部变量）。
_SERVICES: list["Service"] = []
# ctypes 回调的唯一引用持有者（被 GC 后触发信号会崩）
_CTRL_HANDLER = None
# Job Object 句柄：父进程退出时由系统替我们杀掉所有子进程
_JOB = None


# ---------------------------------------------------------------- 终端着色

def _enable_ansi() -> bool:
    """在 Windows 控制台启用 ANSI 转义序列。失败就返回 False（降级为纯文本）。"""
    if not IS_WINDOWS:
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # -11 = STD_OUTPUT_HANDLE，0x0004 = ENABLE_VIRTUAL_TERMINAL_PROCESSING
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


_ANSI = _enable_ansi()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _ANSI else text


def dim(t: str) -> str:
    return _c("90", t)


def bold(t: str) -> str:
    return _c("1", t)


def green(t: str) -> str:
    return _c("32", t)


def blue(t: str) -> str:
    return _c("36", t)


def yellow(t: str) -> str:
    return _c("33", t)


def red(t: str) -> str:
    return _c("31", t)


# ---------------------------------------------------------------- 工具

def port_in_use(port: int) -> bool:
    """端口是否已被占用（用来提前发现"上次没退干净"）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _no_proxy_opener() -> urllib.request.OpenerDirector:
    """绕开系统代理的 opener。

    必须显式置空 ProxyHandler：本机配了 http_proxy，urllib 默认会走它，
    于是连 127.0.0.1 的探活请求都会发给代理然后失败。
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


_OPENER = _no_proxy_opener()


def probe_ready(timeout: float = 1.0) -> tuple[int | None, dict]:
    """探一次 /health/ready。返回 (状态码, 响应体)；连不上时状态码为 None。"""
    try:
        with _OPENER.open(f"{BACKEND_URL}/health/ready", timeout=timeout) as resp:
            import json

            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 503 是后端"有意表达的未就绪"，body 里带着进度，必须读出来
        try:
            import json

            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}
    except Exception:
        return None, {}


# ---------------------------------------------------------------- 子进程管理

class Service:
    """一个被托管的子进程 + 它的日志转发线程。"""

    def __init__(
        self,
        name: str,
        tag: str,
        cmd: list[str],
        cwd: Path,
        color,
        env_extra: dict[str, str] | None = None,
    ):
        self.name = name
        self.tag = tag
        self.cmd = cmd
        self.cwd = cwd
        self.color = color
        self.env_extra = env_extra or {}
        self.proc: subprocess.Popen | None = None
        self.exited = threading.Event()
        self.exit_code: int | None = None

    def start(self) -> None:
        env = {**os.environ, **self.env_extra}
        self.proc = subprocess.Popen(
            self.cmd,
            cwd=str(self.cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=_NO_WINDOW,
            env=env,
        )
        _SERVICES.append(self)
        # 必须在子进程刚起来就挂进 job：晚一步它可能已经派生出孙进程
        # （npm → node），而孙进程会因为 job 的可继承性一起进来。
        _assign_to_job(self.proc.pid)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        """把子进程输出逐行加上前缀转发到本窗口。

        不转发的话输出就被 PIPE 吞了，用户看不到任何日志——
        出了问题无从排查，等于把"多窗口"换成了"没窗口"。
        """
        assert self.proc is not None and self.proc.stdout is not None
        prefix = self.color(f"[{self.tag}]")
        for line in self.proc.stdout:
            sys.stdout.write(f"{prefix} {line.rstrip()}\n")
            sys.stdout.flush()
        self.proc.wait()
        self.exit_code = self.proc.returncode
        self.exited.set()

    def terminate(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        if IS_WINDOWS:
            # /T 连带子进程一起杀。npm run dev 会派生 node，
            # 只杀父进程会留下占着 5173 的孤儿进程。
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_NO_WINDOW,
            )
        else:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except Exception:
                self.proc.terminate()


def _stop_all_services() -> None:
    """把所有已启动的子进程停掉。控制台关闭信号的处理函数会调它。"""
    for s in list(_SERVICES):
        try:
            s.terminate()
        except Exception:
            pass


def install_console_handler() -> None:
    """让"点窗口的 X"也能干净退出。

    为什么必须显式处理：
      子进程用 CREATE_NO_WINDOW 创建，**不挂在本控制台上**，
      所以点 X 时它们收不到任何信号。而父进程收到的是 CTRL_CLOSE_EVENT，
      Python **不会**把它转成 KeyboardInterrupt（只有 Ctrl+C 才会），
      于是 finally 没机会跑 —— 两个服务变成孤儿进程继续占着 8000 / 5173，
      下次启动直接撞"端口已被占用"。

    分工：
      - Ctrl+C / Ctrl+Break：返回 False 交给 Python 自己处理
        （抛 KeyboardInterrupt → 走 main() 的 finally，能打印完整收尾信息）
      - 关闭窗口 / 注销 / 关机：这里收不到异常，必须自己清理，然后返回 True。
    """
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        from ctypes import wintypes

        CTRL_C_EVENT = 0
        CTRL_BREAK_EVENT = 1
        # 这三个 Python 都不会转成异常：进程会被直接结束，finally 没机会跑
        CTRL_CLOSE_EVENT = 2      # 点窗口右上角的 X
        CTRL_LOGOFF_EVENT = 5     # 注销
        CTRL_SHUTDOWN_EVENT = 6   # 关机

        # 五类关闭信号**一律自己收尾**，不依赖 Python 的异常机制。
        #
        # 为什么不把 Ctrl+C 交给 Python（KeyboardInterrupt → finally）：
        #   实测下来不可靠——主循环正卡在 `Event.wait()` 里，
        #   Python 有时还没来得及抛异常进程就被系统按
        #   STATUS_CONTROL_C_EXIT 结束了，finally 一次都没跑，
        #   两个服务全成孤儿进程。而 Ctrl+Break 更是从不抛异常。
        #   与其赌它什么时候醒，不如在处理器里直接做掉——
        #   反正收尾动作就一件事：把子进程停掉。
        #
        # main() 里的 finally 仍然保留，作为正常退出 / 子进程崩溃时的兜底。
        WE_MUST_HANDLE = (
            CTRL_C_EVENT,
            CTRL_BREAK_EVENT,
            CTRL_CLOSE_EVENT,
            CTRL_LOGOFF_EVENT,
            CTRL_SHUTDOWN_EVENT,
        )

        HandlerRoutine = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        @HandlerRoutine
        def handler(event: int) -> bool:
            if event in WE_MUST_HANDLE:
                _stop_all_services()
                try:
                    sys.stdout.write("\n" + dim("已收到停止信号，两个服务均已停止。") + "\n")
                    sys.stdout.flush()
                except Exception:
                    pass
                # 子进程已停，直接退出。不用 SystemExit/raise 是因为此时
                # 主循环还堵在 Event.wait()，等它自己醒不可靠。
                os._exit(0)
            return False

        # 必须把回调对象存到模块级变量：ctypes 不会持有引用，
        # 被 GC 之后触发信号会直接崩溃。
        global _CTRL_HANDLER
        _CTRL_HANDLER = handler
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_CTRL_HANDLER, True)
    except Exception:
        # 装不上也不能影响启动——最坏情况是退回"只能用 Ctrl+C 退出"
        pass


def ensure_kill_on_close_job():
    """建一个「父进程一死，子进程全灭」的 Job Object。

    ## 为什么还需要它——信号处理器不够

    控制台信号处理器只覆盖五类**信号**（Ctrl+C / Break / 关窗 / 注销 / 关机），
    覆盖不到这些真实情况：
      - 父进程被 `taskkill /F` 强杀（没有信号，直接 TerminateProcess）
      - 父进程自己崩了
      - 控制台信号没能送达（Ctrl+C 的送达在本机就遇到过不可靠的情况）

    而**孤儿进程的代价很直接**：后端/前端继续占着 8000/5173，
    下次启动直接撞「端口已被占用」，用户得自己翻 netstat 去杀。

    Job Object 是操作系统级别的保证：所有子进程都挂进这个 job，
    设 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 之后，
    **只要 job 的最后一个句柄被关闭（父进程退出时必然发生），
    job 内所有进程一律被系统终止**——不管父进程是怎么死的。

    这是"防御纵深"：信号处理器负责优雅收尾并打印提示，
    Job Object 负责兜底保证。两者不冲突，少了任何一个都有漏洞。
    """
    global _JOB
    if not IS_WINDOWS or _JOB is not None:
        return _JOB
    try:
        import ctypes
        from ctypes import wintypes

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JobObjectExtendedLimitInformation = 9

        k32 = ctypes.windll.kernel32
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = k32.SetInformationJobObject(
            job, JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info),
        )
        if not ok:
            k32.CloseHandle(job)
            return None

        _JOB = job
        return _JOB
    except Exception:
        # 建不出来就退回"只靠信号处理器"，不影响启动
        return None


def _assign_to_job(pid: int) -> None:
    """把子进程挂进 job。子进程自己拿不到 job 句柄，
    所以父进程一退出，job 的最后一个句柄就没了，子进程会被系统终止。"""
    if _JOB is None:
        return
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        PROCESS_SET_QUOTA_TERMINATE = 0x0100 | 0x0001
        k32.OpenProcess.restype = ctypes.c_void_p
        h = k32.OpenProcess(PROCESS_SET_QUOTA_TERMINATE, False, pid)
        if not h:
            return
        try:
            k32.AssignProcessToJobObject(ctypes.c_void_p(_JOB), ctypes.c_void_p(h))
        finally:
            k32.CloseHandle(ctypes.c_void_p(h))
    except Exception:
        pass


# ---------------------------------------------------------------- 主流程

def print_header() -> None:
    line = "=" * 58
    print(blue(line))
    print(bold("  FlawScope 一键启动") + dim("  （单窗口 · Ctrl+C 停止全部）"))
    print(blue(line))
    print(f"  后端 API    {BACKEND_URL}    {dim('接口文档 /docs')}")
    print(f"  前端页面    {FRONTEND_URL}")
    print(blue(line))
    print()


def find_python() -> Path:
    """后端用的解释器：优先项目 venv，缺失时退回当前解释器。"""
    python = ROOT / "venv" / "Scripts" / "python.exe"
    return python if python.exists() else Path(sys.executable)


def find_node() -> tuple[Path | None, Path | None]:
    """找 node.exe 与 npm，返回 (node, npm)；找不到的一侧为 None。

    为什么要绕这么一圈找：`shutil.which("npm")` 在**双击 .bat 启动**的场景下
    经常是 None——Node 可能装在 PATH 里、但 Explorer 派生的进程拿到的 PATH
    是登录时的旧副本；也可能装在 nvm / scoop / volta 这类位置，
    压根没进 PATH。只靠 which() 会让用户看到一个没头没尾的"找不到 npm"。

    查找顺序：环境变量覆盖 → PATH → 常见安装位置。
    """
    def first_existing(*paths: Path) -> Path | None:
        for p in paths:
            if p and p.exists():
                return p
        return None

    # 1) 显式覆盖：装在非标准位置时用它
    node = first_existing(*[Path(os.environ[k]) for k in ("FLAWSCOPE_NODE",) if os.environ.get(k)])
    npm = first_existing(*[Path(os.environ[k]) for k in ("FLAWSCOPE_NPM",) if os.environ.get(k)])
    if node and npm:
        return node, npm

    # 2) PATH
    if not node:
        node = first_existing(*[Path(p) for p in (shutil.which("node"), shutil.which("node.exe")) if p])
    if not npm:
        npm = first_existing(*[Path(p) for p in (shutil.which("npm"), shutil.which("npm.cmd")) if p])

    # 3) 常见安装位置（nvm / fnm / volta / scoop / 官方安装器）
    # Path.home() 在 USERPROFILE 缺失时会抛 RuntimeError（某些精简环境/计划任务下
    # 就是这样）。这里必须兜住：探测 Node 这种"锦上添花"的步骤，
    # 绝不能把整个启动器崩掉——用户只会看到一个看不懂的 traceback。
    try:
        home = Path.home()
    except Exception:
        home = Path(os.environ.get("USERPROFILE") or os.environ.get("HOMEPATH") or ".")
    candidates = [
        Path(os.environ.get("ProgramFiles", "C:\\Program Files")) / "nodejs",
        Path(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")) / "nodejs",
        home / "AppData" / "Roaming" / "nvm" / "current",
        home / ".nvm" / "versions" / "node",
        home / ".volta" / "bin",
        home / ".fnm" / "node-versions",
        home / "scoop" / "apps" / "nodejs" / "current",
    ]
    for base in candidates:
        if not base.exists():
            continue
        if not node:
            # nvm/fnm 下有一层版本目录，往下找一层
            found = first_existing(base / "node.exe", *(base / "node.exe" for _ in ()))
            if not found and base.is_dir():
                for sub in sorted(base.iterdir(), reverse=True):
                    if (sub / "node.exe").exists():
                        found = sub / "node.exe"
                        break
            node = found
        if not npm:
            npm = first_existing(base / "npm.cmd", base / "npm")
        if node and npm:
            break

    return node, npm


def print_node_missing() -> None:
    """缺 Node.js 时的可执行指引。

    写清楚三件事，缺一件用户就得再来问一遍：
      1. 为什么需要（不是"你的项目坏了"）
      2. 去哪装（给 URL，不给"请安装依赖"这种空话）
      3. 装完怎么办 / 装在别处怎么办
    """
    print(red("[错误] 没找到 Node.js，前端起不来。"))
    print()
    print("      本项目的 React 前端需要有 Node.js 才能跑（只是运行前端用，")
    print("      不需要你写前端代码）。后端 API 不依赖它。")
    print()
    print(yellow("      安装：https://nodejs.org/   选 LTS 版，一路下一步即可。"))
    print(dim("      装完重新双击「启动全部.bat」，会自动补装前端依赖。"))
    print()
    print(dim("      若装到了非默认位置，设一次环境变量（改完重开窗口生效）："))
    print(dim('        setx FLAWSCOPE_NODE "D:\\path\\to\\node.exe"'))
    print(dim('        setx FLAWSCOPE_NPM  "D:\\path\\to\\npm.cmd"'))


def build_services() -> tuple[Service, Service]:
    python = find_python()
    node, npm = find_node()

    if node is None and npm is None:
        print_node_missing()
        sys.exit(1)

    backend = Service(
        "后端",
        "后端",
        [str(python), "-m", "uvicorn", "api:app", "--host", "127.0.0.1", "--port", str(BACKEND_PORT)],
        ROOT,
        blue,
        # transformers 每次启动都会打印 "PyTorch was not found. Models won't be
        # available..." —— 这是**预期状态**（我们刻意不装 torch，见 requirements.txt），
        # 不是需要用户处理的警告。留着它只会让人以为哪里配错了。
        env_extra={"TRANSFORMERS_VERBOSITY": "error"},
    )

    # 优先 npm（ canonical 入口）；只有 node 时直接跑 vite 的 JS 入口，
    # 绕开 npm.cmd 这层 Windows 脚本（它偶尔会带来额外的控制台窗口）。
    if npm is not None:
        frontend_cmd = [str(npm), "run", "dev"]
    else:
        frontend_cmd = [str(node), str(WEB_DIR / "node_modules" / "vite" / "bin" / "vite.js")]

    frontend = Service("前端", "前端", frontend_cmd, WEB_DIR, yellow)
    return backend, frontend


def readiness_reporter(stop: threading.Event) -> None:
    """轮询 /health/ready，把后端启动进度打印出来。

    **只在步骤变化时打印一行**，不做原地刷新（`\\r`）。
    原地刷新看着更"高级"，但它和后端日志是并发写同一个终端的：
    后端日志一插进来，光标位置就错位，屏幕上会留下半截残行——
    用户的原话是"不太美观"，那就别再造新的不美观。
    启动一共 5 步，最多 5 行，本来也不值得为省这几行去冒错位的风险。
    """
    started = time.monotonic()
    last_key = ""
    while not stop.is_set():
        code, body = probe_ready()
        if code == 200:
            elapsed = time.monotonic() - started
            print(green(f"[就绪] 后端可用，用时 {elapsed:.1f}s（版本 {body.get('version', '?')}）"))
            print()
            return

        if code == 503:
            if body.get("error"):
                print(red(f"[失败] 后端启动失败：{body['error']}"))
                return
            idx, total = body.get("step_index", 0), body.get("step_total", 0)
            step = body.get("step", "初始化中")
            msg = f"[启动] {step}（{idx}/{total}）" if total else f"[启动] {step}"
            # 步骤名变化即打印，去重键就是消息本身
            key = msg
        elif code is None:
            # 这段是**等待的大头**：Python 解释器与依赖 import 都发生在
            # uvicorn 绑定端口之前，此时 /health/ready 根本连不上，
            # 拿不到任何步骤信息。所以这里要把预期时长说出来，
            # 否则用户盯着一行不动的提示会以为卡死了。
            waited = time.monotonic() - started
            msg = f"[启动] 正在加载依赖（约 6 秒，已等 {waited:.0f}s）…"
            # 带秒数的提示每轮都在变，直接拿 msg 当去重键会变成每秒刷一行。
            # 按 3 秒分桶：用户能看到"还在走"，又不至于把屏幕刷满。
            key = f"loading-{int(waited // 3)}"
        else:
            msg = f"[启动] 后端返回 HTTP {code}，仍在等待…"
            key = msg

        if key != last_key:
            print(dim(msg))
            sys.stdout.flush()
            last_key = key
        stop.wait(0.8)


def warn_if_torch_present(python: Path) -> None:
    """检查 torch 是否被装进来了——它是冷启动变慢的头号原因。

    链条：`langchain_core` 顶层无条件 `from transformers import GPT2TokenizerFast`
    → `transformers` 导入 `modeling_gguf_pytorch_utils`
    → 该模块检测到 torch 可用就 `import torch`。

    于是本地凭空多加载一个 torch（约 2.5s），冷启动从 ~6s 涨到 ~9s。
    本项目**不用本地推理**（推理与嵌入都走在线 API），所以这是纯浪费。

    这里只提示、不自动卸载：卸载依赖是用户的决定，
    工具悄悄改环境比慢几秒更糟。自查命令写在提示里，想处理一秒钟就能处理。
    """
    try:
        probe = subprocess.run(
            [
                str(python),
                "-c",
                "import importlib.util as u; print(bool(u.find_spec('torch')))",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=_NO_WINDOW,
        )
        present = probe.stdout.strip() == "True"
    except Exception:
        return  # 探测失败不该影响启动

    if present:
        print(yellow("[提示] 检测到本地装了 torch，它会让后端启动慢约 3 秒。"))
        print(dim("       本项目不用本地推理（推理/嵌入都走在线 API），可以卸掉："))
        print(dim("         venv\\Scripts\\pip uninstall -y torch torchvision sentence-transformers"))
        print()


def main() -> int:
    # 依赖没装的话先装——否则 vite 会立刻失败，
    # 而错误信息（"vite: not found"）对不熟悉前端的人没有指引作用。
    # 注意：这步**必须在 Node 存在性检查之后**——没装 Node 时 npm 也不存在，
    # 先报"Node 没装"才是用户真正能照着做的那一步。
    _node, npm = find_node()
    if _node is None and npm is None:
        print_node_missing()
        return 1

    if not (WEB_DIR / "node_modules").exists():
        print(yellow("前端依赖未安装，首次启动需要几分钟…"))
        print()
        rc = subprocess.call([str(npm), "install"], cwd=str(WEB_DIR))
        if rc != 0:
            print(red("npm install 失败，请手动在 web 目录执行后重试。"))
            return rc

    for port, what in ((BACKEND_PORT, "后端"), (FRONTEND_PORT, "前端")):
        if port_in_use(port):
            print(red(f"端口 {port} 已被占用（{what}）。"))
            print(dim("  可能是上次启动的服务没退干净。查占用："))
            print(dim(f"    netstat -ano | findstr :{port}"))
            print(dim("  确认后可用 taskkill /F /T /PID <PID> 结束它。"))
            return 1

    # 必须在启动子进程**之前**装好：装上之后即使立刻被关窗也能收尾。
    install_console_handler()
    # 兜底保证：父进程怎么死的，子进程都会被系统收掉（见函数注释）
    job = ensure_kill_on_close_job()
    if job is None:
        # 建不出来要让用户知道，否则他会以为"关了窗口就干净了"
        print(dim("[提示] 未能启用进程组兜底，退出时请优先使用 Ctrl+C。"))
        print()

    print_header()
    warn_if_torch_present(find_python())
    backend, frontend = build_services()
    services = [backend, frontend]

    print(dim("正在启动两个服务…（后端初始化约需十几秒，页面会实时显示进度）"))
    print()
    backend.start()
    frontend.start()

    stop = threading.Event()
    threading.Thread(target=readiness_reporter, args=(stop,), daemon=True).start()

    # 等前端端口起来就开浏览器——**不等后端**。
    # 页面自己会显示"后端正在启动"，这比让用户对着黑窗口等更好：
    # 打开即见界面，进度在界面里，而且后端一好页面就自动变绿。
    opened = False
    try:
        while True:
            for s in services:
                if s.exited.is_set():
                    print()
                    print(red(f"[{s.tag}] 进程已退出（退出码 {s.exit_code}），正在停止其余服务…"))
                    return s.exit_code or 1
            if not opened and port_in_use(FRONTEND_PORT):
                print()
                print(green(f"[就绪] 前端页面已启动：{FRONTEND_URL}"))
                try:
                    webbrowser.open(FRONTEND_URL)
                    print(dim("       已在浏览器中打开（若没弹出请手动访问上面的地址）"))
                except Exception:
                    print(dim("       请手动在浏览器打开上面的地址"))
                print()
                opened = True
            stop.wait(0.3)
    except KeyboardInterrupt:
        print()
        print(dim("收到中断信号，正在停止服务…"))
        print(dim("（以后直接在本窗口按 Ctrl+C 即可，或点右上角关闭）"))
        return 0
    finally:
        # 收尾必须放 finally：正常退出、Ctrl+C、子进程崩掉三条路径
        # 都要保证不留孤儿进程（否则下次启动会撞"端口被占用"）。
        stop.set()
        for s in services:
            s.terminate()
        # 给 taskkill 一点时间落地，再报最终状态
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and any(
            s.proc and s.proc.poll() is None for s in services
        ):
            time.sleep(0.1)
        if all(s.proc is None or s.proc.poll() is not None for s in services):
            print(dim("两个服务均已停止。"))


if __name__ == "__main__":
    sys.exit(main())
