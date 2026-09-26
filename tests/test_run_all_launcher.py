"""启动器 `run_all.py` 的可移植性与容错（D4）。

三个真实缺陷：

1. **POSIX 上会把自己杀掉**：`Service.terminate()` 用
   `os.killpg(os.getpgid(self.proc.pid), SIGTERM)`，而 `Service.start()` 没有
   `start_new_session=True` —— 子进程与启动器同进程组，杀子进程 = 杀自己。
2. **`find_python()` 只认 Windows 布局**（`venv/Scripts/python.exe`），
   在 macOS / Linux 上会静默退回系统解释器，于是依赖装在 venv 里却用系统 Python 启动。
3. **`find_node()` 返回 `(node, None)` 时**，`subprocess.call([str(npm), "install"])`
   会拿字符串 `"None"` 去 exec，抛一个没头没尾的 FileNotFoundError。

前两条用源码/纯函数断言，第三条驱动纯函数本身。
全部离线：不启动任何进程。
"""

import re
from pathlib import Path

import pytest

import run_all

SOURCE = Path(run_all.__file__).read_text(encoding="utf-8")


# ========== 1. POSIX 进程组隔离 ==========

def test_popen_opens_a_new_session_on_posix():
    """判别式：`Popen(...)` 必须带 `start_new_session=not IS_WINDOWS`。

    只断言"函数里出现了 start_new_session"是不够的——写成 `False` 照样出现。
    这里同时钉住"与 IS_WINDOWS 相反"这个语义。
    """
    call = re.search(r"subprocess\.Popen\(([\s\S]*?)\n\s*\)", SOURCE)
    assert call, "没找到 Popen 调用"
    assert "start_new_session=not IS_WINDOWS" in call.group(1), (
        "POSIX 上没有开新会话：killpg 会把启动器自己一起杀掉"
    )


def test_terminate_uses_killpg_only_on_posix():
    """对偶：killpg 本身要保留（POSIX 上它才是杀干净子孙进程的正确手段）。"""
    assert "os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)" in SOURCE


# ========== 2. find_python 认两种 venv 布局 ==========

def test_find_python_prefers_windows_layout(tmp_path):
    (tmp_path / "venv" / "Scripts").mkdir(parents=True)
    win = tmp_path / "venv" / "Scripts" / "python.exe"
    win.write_text("", encoding="utf-8")

    assert run_all.find_python(tmp_path) == win


def test_find_python_finds_posix_layout(tmp_path):
    """只有 `venv/bin/python`（macOS / Linux 布局）时也必须认出来。"""
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    posix = tmp_path / "venv" / "bin" / "python"
    posix.write_text("", encoding="utf-8")

    assert run_all.find_python(tmp_path) == posix


def test_find_python_falls_back_to_current_interpreter(tmp_path):
    """两种布局都没有时退回当前解释器，而不是抛异常。"""
    import sys

    assert run_all.find_python(tmp_path) == Path(sys.executable)


# ========== 3. 只有 node 没有 npm ==========

def test_frontend_command_uses_npm_when_available():
    # 用 Path(...) 而不是字面量做期望值：Windows 上 Path("/usr/bin/npm") 会规范化成
    # `\usr\bin\npm`，写死字符串的断言会在 Windows 上假红。
    node, npm = Path("node"), Path("npm")
    assert run_all.frontend_command(node, npm) == [str(npm), "run", "dev"]


def test_frontend_command_falls_back_to_vite_when_npm_missing():
    """核心断言：只有 node 时必须直接跑 vite 的 JS 入口，而不是拿 None 去 exec。"""
    node = Path("node")
    cmd = run_all.frontend_command(node, None)

    assert cmd is not None
    assert cmd[0] == str(node)
    assert cmd[1].endswith(str(Path("node_modules") / "vite" / "bin" / "vite.js"))
    assert "None" not in cmd


def test_frontend_command_returns_none_when_both_missing():
    assert run_all.frontend_command(None, None) is None


def test_install_frontend_deps_without_npm_prints_guidance(capsys):
    """npm 缺失时给可读指引并返回非零，**不能**抛 FileNotFoundError。"""
    rc = run_all.install_frontend_deps(None)

    out = capsys.readouterr().out
    assert rc != 0
    assert "没找到 npm" in out
    assert "FLAWSCOPE_NPM" in out, "指引里没有告诉用户怎么指到 npm"
    assert "npm install" in out


def test_install_frontend_deps_never_execs_a_none_path(monkeypatch):
    """反向证明：npm 为 None 时**不得**调用 subprocess.call。"""
    called = []
    monkeypatch.setattr(run_all.subprocess, "call", lambda *a, **kw: called.append(a) or 0)

    run_all.install_frontend_deps(None)

    assert called == [], f"拿 None 去执行了命令：{called}"


def test_install_frontend_deps_invokes_npm_when_present(monkeypatch, tmp_path):
    """对偶：npm 存在时确实会去装依赖。"""
    called = []

    def _fake_call(cmd, **kw):
        called.append((cmd, kw))
        return 0

    monkeypatch.setattr(run_all.subprocess, "call", _fake_call)
    rc = run_all.install_frontend_deps(Path("npm"))

    assert rc == 0
    assert called and called[0][0] == [str(Path("npm")), "install"]


def test_build_services_never_passes_a_none_command(monkeypatch, capsys):
    """`build_services` 不能把 None 当命令塞进 Service。

    只找到 node 时它应当用 vite 入口起前端；两者都没有时应当退出并打印指引。
    """
    monkeypatch.setattr(run_all, "find_node", lambda: (Path("node"), None))

    backend, frontend = run_all.build_services()

    assert backend.cmd[0] == str(run_all.find_python())
    assert frontend.cmd[0] == str(Path("node"))
    assert all(isinstance(part, str) and part != "None" for part in frontend.cmd)


def test_build_services_exits_with_guidance_when_node_absent(monkeypatch, capsys):
    monkeypatch.setattr(run_all, "find_node", lambda: (None, None))

    with pytest.raises(SystemExit) as exc:
        run_all.build_services()

    assert exc.value.code == 1
    assert "Node.js" in capsys.readouterr().out
