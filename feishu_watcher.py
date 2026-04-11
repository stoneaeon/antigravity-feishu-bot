#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
"""
feishu_watcher.py  —  飞书消息自动激活器

当检测到飞书新消息时，自动：
  1. 将 Antigravity 窗口调到最前
  2. 聚焦 AI Chat 输入框（Cmd+Shift+I）
  3. 输入触发字符并回车 → Agent 读取飞书消息队列并处理

支持 Mac 锁屏：锁屏时等待，解锁后立即激活。
支持守护进程模式（--daemon）。

⚠️  首次运行需要授权（弹出一次权限请求）：
    系统设置 → 隐私与安全性 → 辅助功能 → 添加终端（Terminal.app）

⚠️  Antigravity 的 Chat 快捷键（可能需要调整）：
    默认使用 Cmd+Shift+I，若不正确，修改下方 CHAT_KEYCODE。
    查找方法：Antigravity → 菜单 → 帮助 → 键盘快捷方式 → 搜索 "chat"

用法：
  python3 feishu_watcher.py                      # 前台运行（测试）
  python3 feishu_watcher.py --daemon             # 后台守护进程
  python3 feishu_watcher.py --status             # 查看状态
  python3 feishu_watcher.py --stop               # 停止守护进程
  python3 feishu_watcher.py --workspace /path    # 指定项目路径
  python3 feishu_watcher.py --app MyApp          # 指定应用名称
"""

import os
import re
import sys
import json
import time
import signal
import argparse
import datetime
import subprocess
from pathlib import Path

# ── 配置（修改这里适配你的 Antigravity 快捷键）─────────────────────────────
APP_NAME      = "Antigravity"  # 应用名（Activity Monitor 中显示的名称）
CHAT_KEYCODE  = 34             # 'i' 的 macOS keycode（Cmd+Shift+I → 打开 AI Chat）
CHAT_MODIFIER = "command down, shift down"
POLL_INTERVAL = 2              # 队列检查间隔（秒）
COOLDOWN_SEC  = 30             # 同批次消息最短触发间隔（秒），避免反复激活
TRIGGER_TEXT  = "."            # 触发 Agent 检查队列的输入（Agent 会忽略此文字，优先处理飞书消息）


def now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def log(msg: str) -> None:
    print(f"[watcher {now()}] {msg}", flush=True)


# ── 路径管理 ──────────────────────────────────────────────────────────────────
def find_workspace(workspace: str = None) -> Path:
    if workspace:
        return Path(workspace).resolve()
    cwd = Path.cwd()
    for p in [cwd] + list(cwd.parents):
        if (p / ".antigravity").is_dir():
            return p
    return cwd

def queue_path(ws: Path) -> Path:
    return ws / ".antigravity" / "feishu_messages.json"

def pid_path(ws: Path) -> Path:
    return ws / ".antigravity" / "feishu_watcher.pid"

def log_path(ws: Path) -> Path:
    return ws / ".antigravity" / "feishu_watcher.log"


# ── 屏幕锁定检测 ──────────────────────────────────────────────────────────────
def is_screen_locked() -> bool:
    """
    检测 Mac 屏幕是否处于锁定状态（锁屏或密码保护的屏保）。

    方法1（首选）：调用系统 Python + Quartz，读取 CGSession 状态。
      - /usr/bin/python3 是 macOS 系统自带的，有 Quartz 框架访问权。
      - 这是最准确的方法，能区分「关闭显示器」和「真正锁定」。

    方法2（备用）：读取 ioreg 中显示器电源状态。
      - CurrentPowerState = 4 表示全亮；< 4 表示熄屏（近似等于锁定）。
      - 不能区分用户手动关显示器 vs 锁定，但作为备用足够。

    两种方法都失败时返回 False（假定未锁定，尽量不阻塞激活流程）。
    """
    # 方法1：Quartz CGSession（最准确）
    try:
        quartz_code = (
            "from Quartz import CGSessionCopyCurrentDictionary;"
            "d = CGSessionCopyCurrentDictionary();"
            "print(int(bool(d and d.get('CGSSessionScreenIsLocked', 0))))"
        )
        result = subprocess.run(
            ["/usr/bin/python3", "-c", quartz_code],
            capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0 and result.stdout.strip() in ("0", "1"):
            return result.stdout.strip() == "1"
    except Exception:
        pass

    # 方法2：ioreg（备用近似检测）
    try:
        result = subprocess.run(
            ["ioreg", "-n", "IODisplayWrangler"],
            capture_output=True, text=True, timeout=3
        )
        match = re.search(r'"CurrentPowerState"=(\d+)', result.stdout)
        if match:
            return int(match.group(1)) < 4
    except Exception:
        pass

    return False  # 无法判断，默认当作未锁定


# ── 应用状态 ──────────────────────────────────────────────────────────────────
def is_app_running(app_name: str) -> bool:
    """
    检查指定应用是否正在运行。

    Antigravity 是 Electron 应用，主进程名为 "Electron" 而非 "Antigravity"。
    pgrep -x Antigravity 会失败，改用两种方法：
      1. lsappinfo（macOS 专用，按 bundle name 查找，最准确）
      2. pgrep 按路径匹配（备用）
    """
    # 方法1：lsappinfo（按 bundle ID 查找，不受进程名影响）
    # Antigravity 实际 bundle ID 为 com.google.antigravity（通过 mdls 确认）
    try:
        result = subprocess.run(
            ["lsappinfo", "find", "bundleid=com.google.antigravity"],
            capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0 and result.stdout.strip():
            return True
    except Exception:
        pass

    # 方法2：按 .app 路径中的名称匹配进程
    try:
        result = subprocess.run(
            ["pgrep", "-f", f"{app_name}.app/Contents/MacOS/"],
            capture_output=True, text=True, timeout=3
        )
        if result.stdout.strip():
            return True
    except Exception:
        pass

    # 方法3：精确名称匹配（原逻辑，保底）
    try:
        result = subprocess.run(
            ["pgrep", "-x", app_name],
            capture_output=True, text=True, timeout=3
        )
        return bool(result.stdout.strip())
    except Exception:
        return False


# ── 消息队列读取 ──────────────────────────────────────────────────────────────
def get_pending_messages(ws: Path) -> list:
    qp = queue_path(ws)
    if not qp.exists():
        return []
    try:
        data = json.loads(qp.read_text(encoding="utf-8"))
        return data.get("messages", [])
    except Exception:
        return []


# ── macOS 系统通知 ────────────────────────────────────────────────────────────
def send_notification(title: str, body: str) -> None:
    """
    发送 macOS 系统通知横幅。
    即使 AppleScript 键盘模拟失败，用户至少能看到通知，手动切换到 Antigravity。
    锁屏时通知会在锁屏界面显示（或解锁后显示）。
    """
    try:
        body_escaped = body.replace('"', '\\"')
        script = (
            f'display notification "{body_escaped}" '
            f'with title "{title}" sound name "Ping"'
        )
        subprocess.run(["osascript", "-e", script], timeout=3, capture_output=True)
    except Exception:
        pass  # 通知失败不影响主流程


# ── 激活 Antigravity + 触发对话 ──────────────────────────────────────────────
def activate_and_trigger(app_name: str, text: str) -> bool:
    """
    用 AppleScript 激活 Antigravity 并向 Chat 输入触发消息。

    操作步骤（参考用户验证可行的命令）：
      1. 在 System Events 内激活 App（inline tell，不用 tell process）
      2. delay 1 → 等窗口渲染
      3. Cmd+1 → 强制焦点回到代码编辑区第1列（已知稳定状态）
      4. Cmd+L → 从代码区出发，100% 跳到 Antigravity Chat 输入框
      5. delay 0.5 → 等 Chat 面板打开/聚焦
      6. 将消息写入剪贴板，Cmd+V 粘贴（适配中文和特殊字符）
      7. 按回车触发 Agent

    ⚠️  首次需要辅助功能授权（系统设置 → 隐私与安全性 → 辅助功能 → 添加 Terminal）
    ⚠️  息屏但不锁屏状态下同样有效（macOS 激活 App 时会自动唤醒显示器）
    """
    # 转义文本中的双引号和反斜杠，避免 AppleScript 字符串出错
    safe_text = text.replace("\\", "\\\\").replace('"', '\\"')

    applescript = f"""
tell application "System Events"
    tell application "{app_name}" to activate
    delay 1

    -- Step 1：Cmd+1 强制焦点到代码编辑区第1列（稳定基准点）
    -- 从此出发，Cmd+L 必定落到 Chat 输入框，不会跳走
    keystroke "1" using command down
    delay 0.1

    -- Step 2：Cmd+L 打开/聚焦 Antigravity Chat 输入框
    keystroke "l" using command down
    delay 0.5

    -- Step 3：通过剪贴板粘贴触发文本（比 keystroke 更可靠，支持中文）
    set the clipboard to "{safe_text}"
    keystroke "v" using command down
    delay 0.2

    -- Step 4：回车提交，Agent 读取飞书消息队列并处理
    keystroke return
end tell
"""
    try:
        result = subprocess.run(
            ["osascript", "-e", applescript],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            err = result.stderr.strip()
            if "1002" in err or "not allowed" in err.lower() or "accessibility" in err.lower():
                log("❌ 辅助功能未授权！")
                log("   系统设置 → 隐私与安全性 → 辅助功能 → 添加 Terminal.app")
                log("   授权后重新运行 watcher 即可，无需修改代码")
            else:
                log(f"⚠️  AppleScript 报错: {err[:150]}")
            return False
        return True
    except subprocess.TimeoutExpired:
        log("⚠️  AppleScript 超时（App 可能未响应）")
        return False
    except Exception as e:
        log(f"⚠️  执行异常: {e}")
        return False



# ── 主监控循环 ────────────────────────────────────────────────────────────────
def watch_loop(ws: Path, app_name: str) -> None:
    """
    主循环逻辑：
      - 每 POLL_INTERVAL 秒检查一次消息队列
      - 有新消息时，根据屏幕/应用状态决定下一步
      - 强制冷却时间（COOLDOWN_SEC），避免同批消息反复触发
    """
    last_msg_count  = 0
    last_trigger_ts = 0.0
    was_locked      = False

    log(f"∎ 监控启动 · 工作区: {ws}")
    log(f"  目标应用: {app_name}  |  队列: {queue_path(ws)}")
    log(f"  Chat 快捷键: Cmd+Shift+keycode({CHAT_KEYCODE})")

    while True:
        try:
            messages   = get_pending_messages(ws)
            msg_count  = len(messages)

            # ── 队列为空 ──────────────────────────────────────────────────
            if msg_count == 0:
                if last_msg_count > 0:
                    log("队列已清空（Agent 已处理）")
                last_msg_count  = 0
                last_trigger_ts = 0.0
                was_locked      = False
                time.sleep(POLL_INTERVAL)
                continue

            # ── 有新消息（数量增加时记录日志）──────────────────────────
            if msg_count != last_msg_count:
                preview = messages[-1].get("text", "")[:50]
                log(f"📨 检测到 {msg_count} 条待处理消息：「{preview}」")
                last_msg_count = msg_count

            # ── 冷却期内不重复触发 ───────────────────────────────────────
            elapsed = time.time() - last_trigger_ts
            if last_trigger_ts > 0 and elapsed < COOLDOWN_SEC:
                time.sleep(POLL_INTERVAL)
                continue

            # ── 检查屏幕锁定状态 ─────────────────────────────────────────
            locked = is_screen_locked()

            if locked:
                if not was_locked:
                    log("🔒 屏幕已锁定，等待解锁后激活...")
                    was_locked = True
                time.sleep(POLL_INTERVAL)
                continue

            if was_locked:
                # 刚刚解锁
                log("🔓 屏幕已解锁")
                was_locked = False
                time.sleep(1.5)  # 等待解锁动画完成，避免过早操作

            # ── 检查应用是否在运行 ───────────────────────────────────────
            if not is_app_running(app_name):
                log(f"⚠️  {app_name} 未运行，消息暂存队列")
                time.sleep(POLL_INTERVAL)
                continue

            # ── 发送通知 + 激活应用 ──────────────────────────────────────
            preview  = messages[-1].get("text", "")[:40]
            notif_body = f"{msg_count} 条待处理：{preview}" if preview else f"共 {msg_count} 条"

            # 先发通知（无论 AppleScript 是否成功，用户都能看到）
            send_notification(
                title=f"📨 飞书任务 · {ws.name}",
                body=notif_body
            )

            # 构造触发文本：描述飞书消息，让 Agent 知道背景
            # （Agent 会读取队列中的完整内容，这里只是激活触发）
            trigger = f"检查飞书消息队列（{msg_count} 条待处理）"
            log(f"激活 {app_name} 并触发对话...")
            ok = activate_and_trigger(app_name, trigger)

            if ok:
                log(f"✅ 已激活，Agent 将在下一轮对话中处理飞书消息")
            else:
                log(f"⚠️  自动触发失败，已发送通知，请手动切换到 {app_name}")

            last_trigger_ts = time.time()

        except Exception as e:
            log(f"监控循环异常: {e}")

        time.sleep(POLL_INTERVAL)


# ── 守护进程 ──────────────────────────────────────────────────────────────────
def daemonize(ws: Path, app_name: str) -> None:
    """
    使用 subprocess.Popen 启动全新子进程（start_new_session=True），
    与 feishu_listener.py 保持一致，避免 fork 继承问题。
    """
    pp = pid_path(ws)
    lp = log_path(ws)
    lp.parent.mkdir(parents=True, exist_ok=True)

    # 检查是否已在运行
    if pp.exists():
        try:
            existing = int(pp.read_text().strip())
            os.kill(existing, 0)
            log(f"watcher 已在运行 (PID={existing})，无需重启")
            return
        except (ProcessLookupError, ValueError):
            pp.unlink(missing_ok=True)

    with open(lp, "a", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()),
             "--_foreground", "--workspace", str(ws), "--app", app_name],
            stdout=log_file,
            stderr=log_file,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    log(f"✅ watcher 已后台启动 (PID={proc.pid})")
    log(f"   日志: tail -f {lp}")
    log(f"   停止: python3 feishu_watcher.py --stop")


# ── CLI ───────────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(description="飞书消息自动激活器")
    ap.add_argument("--workspace", "-w", default=None, help="项目工作区路径")
    ap.add_argument("--app",             default=APP_NAME, help="Antigravity 应用名称")
    ap.add_argument("--daemon",  "-d",   action="store_true", help="后台守护进程")
    ap.add_argument("--stop",            action="store_true", help="停止守护进程")
    ap.add_argument("--status",          action="store_true", help="查看守护进程状态")
    ap.add_argument("--_foreground",     action="store_true", help=argparse.SUPPRESS)  # 内部用
    args = ap.parse_args()

    ws = find_workspace(args.workspace)
    pp = pid_path(ws)

    if args.stop:
        if not pp.exists():
            log("watcher 未运行")
            return
        try:
            pid = int(pp.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            pp.unlink(missing_ok=True)
            log(f"✅ 已停止 (PID={pid})")
        except Exception as e:
            log(f"停止失败: {e}")
            pp.unlink(missing_ok=True)
        return

    if args.status:
        if pp.exists():
            try:
                pid = int(pp.read_text().strip())
                os.kill(pid, 0)
                log(f"✅ watcher 运行中 (PID={pid})")
                log(f"   日志: {log_path(ws)}")
                return
            except (ProcessLookupError, ValueError):
                pp.unlink(missing_ok=True)
        log("❌ watcher 未运行")
        sys.exit(1)

    # ── --_foreground（由 daemonize 的子进程调用）────────────────────────
    if args._foreground:
        pp.write_text(str(os.getpid()), encoding="utf-8")
        signal.signal(signal.SIGINT,  lambda s, f: (pp.unlink(missing_ok=True), sys.exit(0)))
        signal.signal(signal.SIGTERM, lambda s, f: (pp.unlink(missing_ok=True), sys.exit(0)))
        try:
            watch_loop(ws, args.app)
        finally:
            pp.unlink(missing_ok=True)
        return

    if args.daemon:
        if sys.platform == "win32":
            log("Windows 不支持 --daemon")
            sys.exit(1)
        daemonize(ws, args.app)
        return

    # 前台运行（Ctrl+C 退出）
    signal.signal(signal.SIGINT,  lambda s, f: sys.exit(0))
    signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))
    log("前台模式运行，Ctrl+C 可退出")
    watch_loop(ws, args.app)


if __name__ == "__main__":
    main()
