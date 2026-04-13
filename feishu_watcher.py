#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
"""
feishu_watcher.py  —  飞书消息自动激活器

当检测到飞书新消息时，自动：
  1. 将 Antigravity 窗口调到最前
  2. 聚焦 AI Chat 输入框（Cmd+Shift+I）
  3. 输入触发字符并回车 → Agent 读取飞书消息队列并处理

支持 Mac 息屏：自动唤醒显示器（caffeinate -u），无需手动操作。
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
PROCESSING_TIMEOUT = 600       # processing 锁超时时间（秒），超时后视为死锁并重新触发
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


# ── 显示器休眠检测 ─────────────────────────────────────────────────────────────
def is_display_asleep() -> bool:
    """
    检测 Mac 显示器是否处于休眠（熄屏）状态。

    息屏时 AppleScript 的 keystroke 会被 macOS 静默丢弃，
    必须先唤醒显示器再操作。

    方法1（首选）：ctypes 调用 CoreGraphics CGDisplayIsAsleep()。
      - 无需第三方库，直接调用系统动态库，在 macOS 26.x 上验证可用。

    方法2（备用）：读取 ioreg 中显示器电源状态。
      - CurrentPowerState = 4 表示全亮；< 4 表示熄屏。
      - 新版 macOS 可能已移除 IODisplayWrangler 节点。

    方法3（备用）：pmset -g assertions 检查 UserIsActive 标志。
      - UserIsActive = 0 近似表示用户无操作（显示器可能已休眠）。

    所有方法都失败时返回 False（假定未休眠，尽量不阻塞激活流程）。
    """
    # 方法1：CGDisplayIsAsleep via ctypes（最可靠，macOS 26.x 验证通过）
    try:
        import ctypes
        import ctypes.util
        cg_path = ctypes.util.find_library('CoreGraphics')
        if cg_path:
            cg = ctypes.cdll.LoadLibrary(cg_path)
            cg.CGMainDisplayID.restype = ctypes.c_uint32
            cg.CGDisplayIsAsleep.restype = ctypes.c_bool
            cg.CGDisplayIsAsleep.argtypes = [ctypes.c_uint32]
            return bool(cg.CGDisplayIsAsleep(cg.CGMainDisplayID()))
    except Exception:
        pass

    # 方法2：ioreg IODisplayWrangler（旧版 macOS）
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

    # 方法3：pmset assertions（近似检测）
    try:
        result = subprocess.run(
            ["pmset", "-g", "assertions"],
            capture_output=True, text=True, timeout=3
        )
        # UserIsActive = 0 时，用户无活动，显示器很可能已休眠
        match = re.search(r'UserIsActive\s+(\d+)', result.stdout)
        if match:
            return int(match.group(1)) == 0
    except Exception:
        pass

    return False  # 无法判断，默认当作未休眠


def wake_display() -> bool:
    """
    使用 caffeinate -u 唤醒显示器。

    caffeinate -u 会创建一个 "user is active" 断言，
    模拟用户活动，从而唤醒已休眠的显示器。
    -t 5 表示保持 5 秒后自动释放。

    注意：caffeinate -u -t N 会阻塞 N 秒，因此用 Popen 异步执行。
    此方法不需要密码或用户交互（仅唤醒显示器，不解锁）。
    """
    try:
        subprocess.Popen(
            ["caffeinate", "-u", "-t", "5"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(2)  # 等待显示器完全亮起
        return not is_display_asleep()
    except Exception:
        return False


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
def get_pending_messages(ws: Path) -> tuple[list, bool, float]:
    """
    返回 (messages, is_processing, processing_elapsed_sec)。
    is_processing: Agent 是否正在处理队列（processing 锁）
    processing_elapsed_sec: 锁已持续的秒数（未锁定时为 0）
    """
    qp = queue_path(ws)
    if not qp.exists():
        return [], False, 0.0
    try:
        data = json.loads(qp.read_text(encoding="utf-8"))
        msgs = data.get("messages", [])
        is_processing = bool(data.get("processing", False))
        elapsed = 0.0
        if is_processing and data.get("processing_since"):
            try:
                since = datetime.datetime.strptime(
                    data["processing_since"], "%Y-%m-%d %H:%M:%S"
                )
                elapsed = (datetime.datetime.now() - since).total_seconds()
            except (ValueError, TypeError):
                pass
        return msgs, is_processing, elapsed
    except Exception:
        return [], False, 0.0


def reset_processing_lock(ws: Path) -> None:
    """重置 processing 锁（超时后由 watcher 调用，防止死锁）"""
    qp = queue_path(ws)
    if not qp.exists():
        return
    try:
        data = json.loads(qp.read_text(encoding="utf-8"))
        data.pop("processing", None)
        data.pop("processing_since", None)
        qp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def set_processing_lock(ws: Path) -> None:
    """
    设置 processing 锁（watcher 触发成功后立即调用）。

    作用：防止 watcher 在 Agent 处理期间重复触发。
    Agent 处理完毕会清空队列（messages=[]}），watcher 检测到队列为空后
    自然停止触发。如果 Agent 意外退出未清空队列，超时机制会在
    PROCESSING_TIMEOUT 秒后自动重置锁并重新触发。
    """
    qp = queue_path(ws)
    if not qp.exists():
        return
    try:
        data = json.loads(qp.read_text(encoding="utf-8"))
        data["processing"] = True
        data["processing_since"] = now()
        qp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


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



# ── Antigravity 异常检测 ──────────────────────────────────────────────────────
# 已知的错误模式（需要足够具体，避免匹配到正常 UI 文本导致误报）
ERROR_PATTERNS = [
    # 模型用量上限（高优先级，通常需要切换模型）
    "baseline model quota reached", "quota exceeded", "usage limit",
    "rate limit", "rate_limit", "too many requests", "429",
    "用量上限", "请求过多", "模型额度", "频率限制",
    # 服务器忙 / 连接异常
    "our servers are experiencing high traffic", "agent terminated due to error",
    "server busy", "service unavailable", "503",
    "internal server error", "500", "bad gateway", "502",
    "gateway timeout", "504",
    "服务器繁忙", "服务器错误", "服务不可用",
    # 明确的错误提示（需要包含完整短语，避免子串误匹配）
    "something went wrong", "an error occurred", "unexpected error",
    "出现错误", "发生异常", "请求失败",
    # 需要用户操作的阻断提示
    "try again later", "please try again",
    "请稍后重试", "请重试",
]

# 可点击的重试按钮文案（用于精确匹配按钮 title）
RETRY_BUTTON_PATTERNS = [
    "Retry", "Try Again", "Try again", "retry",
    "重试", "再试一次",
]

ERROR_NOTIFY_COOLDOWN = 300  # 错误通知冷却（秒），避免刷屏


def detect_app_error(app_name: str) -> tuple[str, bool]:
    """
    通过 AppleScript 读取 Antigravity 窗口中的 UI 文本，
    检测是否存在错误状态。

    返回 (error_text, buttons_str):
      - error_text: 匹配到的错误文案（空字符串表示无错误）
      - buttons_str: 窗口中所有按钮的文本集合字符串
    """
    # 使用 AXUIElement 提取窗口中所有可见文本
    # 这个 AppleScript 会遍历 UI 元素树，收集所有 AXValue 和 AXTitle
    applescript = f'''
tell application "System Events"
    if not (exists process "{app_name}") then
        return "APP_NOT_RUNNING"
    end if
    tell process "{app_name}"
        set allText to ""
        set buttonNames to ""
        set elemCount to 0
        try
            set frontWin to front window
            set uiElements to entire contents of frontWin
            repeat with elem in uiElements
                -- 限制扫描元素数量，Electron 应用可能有上千个 UI 元素
                set elemCount to elemCount + 1
                if elemCount > 500 then exit repeat
                try
                    set elemRole to role of elem
                    if elemRole is "AXStaticText" then
                        set v to value of elem
                        if v is not missing value then
                            set allText to allText & v & "|||"
                        end if
                    end if
                    if elemRole is "AXButton" then
                        set btnTitle to title of elem
                        if btnTitle is not missing value then
                            set buttonNames to buttonNames & btnTitle & "|||"
                        end if
                    end if
                end try
            end repeat
        end try
        return allText & "###BUTTONS###" & buttonNames
    end tell
end tell
'''
    try:
        result = subprocess.run(
            ["osascript", "-e", applescript],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return "", False

        output = result.stdout.strip()
        if output == "APP_NOT_RUNNING":
            return "", False

        # 分离文本和按钮
        parts = output.split("###BUTTONS###")
        ui_text = parts[0].lower() if parts else ""
        button_text = parts[1].lower() if len(parts) > 1 else ""

        # 检查是否匹配错误模式
        matched_error = ""
        for pattern in ERROR_PATTERNS:
            if pattern.lower() in ui_text:
                matched_error = pattern
                break

        return matched_error, button_text

    except (subprocess.TimeoutExpired, Exception):
        return "", False


def try_click_retry(app_name: str) -> bool:
    """
    尝试通过 Accessibility API 点击 Antigravity 窗口中的「重试」按钮。
    返回 True 表示成功点击。
    """
    # 尝试多种按钮文案
    for btn_name in RETRY_BUTTON_PATTERNS:
        applescript = f'''
tell application "System Events"
    tell process "{app_name}"
        try
            set frontWin to front window
            set retryBtn to first button of frontWin whose title is "{btn_name}"
            click retryBtn
            return "CLICKED"
        end try
        -- 深层搜索：在所有子元素中查找
        try
            set allBtns to every button of entire contents of front window
            repeat with btn in allBtns
                try
                    if title of btn is "{btn_name}" then
                        click btn
                        return "CLICKED"
                    end if
                end try
            end repeat
        end try
    end tell
end tell
return "NOT_FOUND"
'''
        try:
            result = subprocess.run(
                ["osascript", "-e", applescript],
                capture_output=True, text=True, timeout=8
            )
            if "CLICKED" in result.stdout:
                return True
        except Exception:
            continue
    return False


def try_handle_quota(app_name: str, target_model: str = "Gemini 3.1 Pro (High)") -> bool:
    """
    处理模型配额耗尽异常：
    1. 点击 Dismiss 弹窗
    2. 点击底部模型选择按钮
    3. 在弹出的菜单中点击切换到目标模型
    """
    applescript = f'''
tell application "System Events"
    tell process "{app_name}"
        -- 1. 点击 Dismiss 按钮
        try
            set allBtns to every button of entire contents of front window
            repeat with btn in allBtns
                try
                    if title of btn is "Dismiss" then
                        click btn
                        delay 0.5
                        exit repeat
                    end if
                end try
            end repeat
        end try

        -- 2. 查找并点击模型选择按钮
        -- 在输入框区域的模型按钮，它的 title 或者里面通常包含 "Claude", "Gemini", "GPT" 等
        try
            set allBtns to every button of entire contents of front window
            repeat with btn in allBtns
                try
                    set btnTitle to title of btn
                    if btnTitle is not missing value then
                        if btnTitle contains "Claude" or btnTitle contains "Gemini" or btnTitle contains "GPT" or btnTitle contains "Sonnet" or btnTitle contains "Opus" then
                            click btn
                            delay 1
                            exit repeat
                        end if
                    end if
                end try
            end repeat
        end try

        -- 3. 在弹出的列表中点击指定模型
        try
            set allElems to entire contents of front window
            repeat with elem in allElems
                try
                    -- 可能是 AXButton 或 AXStaticText
                    set elemRole to role of elem
                    if elemRole is "AXStaticText" or elemRole is "AXButton" then
                        set t to value of elem
                        if t is missing value then set t to title of elem
                        if t contains "{target_model}" then
                            click elem
                            return "SWITCHED"
                        end if
                    end if
                end try
            end repeat
        end try
    end tell
end tell
return "NOT_SWITCHED"
'''
    try:
        result = subprocess.run(
            ["osascript", "-e", applescript],
            capture_output=True, text=True, timeout=15
        )
        if "SWITCHED" in result.stdout:
            return True
    except Exception:
        pass
    return False


def notify_error_via_feishu(ws: Path, error_text: str,
                            auto_handled: bool) -> None:
    """通过飞书发送异常通知（调用 feishu.py send_text）"""
    feishu_py = Path(__file__).parent / "feishu.py"
    if not feishu_py.exists():
        return

    status = "✅ 已自动重试" if auto_handled else "⚠️ 需要人工处理"
    msg = (
        f"🚨 Antigravity 任务异常\n\n"
        f"错误类型：{error_text}\n"
        f"处理状态：{status}\n"
        f"工作区：{ws.name}\n"
        f"时间：{now()}"
    )
    try:
        subprocess.Popen(
            [sys.executable, str(feishu_py), "send_text", msg,
             "--workspace", str(ws)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


# ── 主监控循环 ────────────────────────────────────────────────────────────────
def watch_loop(ws: Path, app_name: str) -> None:
    """
    主循环逻辑：
      - 每 POLL_INTERVAL 秒检查一次消息队列
      - 有新消息时，根据屏幕/应用状态决定下一步
      - 强制冷却时间（COOLDOWN_SEC），避免同批消息反复触发
      - processing 期间定期检测 Antigravity UI 异常，发现则飞书通知 + 尝试自动恢复
    """
    last_msg_count     = 0
    last_trigger_ts    = 0.0
    last_error_notify  = 0.0     # 上次错误通知时间戳
    error_check_count  = 0       # processing 期间的检测计数器

    log(f"∎ 监控启动 · 工作区: {ws}")
    log(f"  目标应用: {app_name}  |  队列: {queue_path(ws)}")
    log(f"  Chat 快捷键: Cmd+Shift+keycode({CHAT_KEYCODE})")

    while True:
        try:
            messages, is_processing, proc_elapsed = get_pending_messages(ws)
            msg_count = len(messages)

            # ── 队列为空 ──────────────────────────────────────────────────
            if msg_count == 0:
                if last_msg_count > 0:
                    log("队列已清空（Agent 已处理）")
                last_msg_count  = 0
                last_trigger_ts = 0.0
                time.sleep(POLL_INTERVAL)
                continue

            # ── 有新消息（数量增加时记录日志）──────────────────────────
            if msg_count != last_msg_count:
                preview = messages[-1].get("text", "")[:50]
                log(f"📨 检测到 {msg_count} 条待处理消息：「{preview}」")
                last_msg_count = msg_count

            # ── Agent 正在处理中（processing 锁）──────────────────────
            if is_processing:
                if proc_elapsed < PROCESSING_TIMEOUT:
                    # 锁未超时 → Agent 仍在处理
                    # 每隔 5 个轮询周期（约 10 秒）检测一次 UI 异常
                    error_check_count += 1
                    if error_check_count % 5 == 0 and is_app_running(app_name):
                        error_text, buttons_str = detect_app_error(app_name)
                        if error_text:
                            log(f"🚨 检测到异常: {error_text}")
                            auto_handled = False
                            
                            is_quota = any(q in error_text.lower() for q in ["quota", "usage limit", "rate limit", "用量上限"])
                            
                            if is_quota:
                                log(f"  尝试自动切换模型 (配额超限)...")
                                if try_handle_quota(app_name):
                                    log("  ✅ 已自动切换备用模型")
                                    # 切换后需要点击重试以恢复任务
                                    log("  尝试点击重试以恢复任务...")
                                    time.sleep(2)
                                    if try_click_retry(app_name):
                                        log("  ✅ 已成功续传任务")
                                    auto_handled = True
                                else:
                                    log("  ⚠️  自动切换模型失败")
                            else:
                                # 尝试点击重试按钮
                                has_retry = any(btn.lower() in buttons_str for btn in RETRY_BUTTON_PATTERNS)
                                if has_retry or "retry" in buttons_str.lower():
                                    log("  尝试自动点击重试按钮...")
                                    if try_click_retry(app_name):
                                        log("  ✅ 已自动点击重试")
                                        auto_handled = True
                                    else:
                                        log("  ⚠️  自动点击失败")
                            # 飞书通知（带冷却，避免刷屏）
                            if time.time() - last_error_notify > ERROR_NOTIFY_COOLDOWN:
                                notify_error_via_feishu(ws, error_text, auto_handled)
                                send_notification(
                                    title="🚨 Antigravity 异常",
                                    body=f"{error_text} · {'已自动重试' if auto_handled else '需人工处理'}"
                                )
                                last_error_notify = time.time()
                                log("  📨 已发送飞书异常通知")
                    time.sleep(POLL_INTERVAL)
                    continue
                else:
                    # 锁已超时（超过 10 分钟）→ 疑似死锁，重置并重新触发
                    log(f"⚠️  processing 锁已超时（{proc_elapsed:.0f}s），重置并重新触发")
                    reset_processing_lock(ws)
                    error_check_count = 0
                    # 不 continue，落入下方触发逻辑

            # ── 冷却期内不重复触发 ───────────────────────────────────────
            elapsed = time.time() - last_trigger_ts
            if last_trigger_ts > 0 and elapsed < COOLDOWN_SEC:
                time.sleep(POLL_INTERVAL)
                continue

            # ── 检查显示器是否休眠，需要时主动唤醒 ─────────────────────
            woke_from_sleep = False
            if is_display_asleep():
                log("💡 显示器休眠中，正在唤醒...")
                woke_from_sleep = True
                if not wake_display():
                    log("⚠️  唤醒失败，等待下次重试")
                    time.sleep(POLL_INTERVAL)
                    continue
                # 唤醒后二次确认
                if is_display_asleep():
                    log("⚠️  唤醒后显示器仍未亮起，跳过本轮")
                    time.sleep(POLL_INTERVAL)
                    continue
                log("✅ 显示器已唤醒")
                time.sleep(1)  # 额外等待显示器稳定

            # ── 检查应用是否在运行 ───────────────────────────────────────
            if not is_app_running(app_name):
                log(f"⚠️  {app_name} 未运行，消息暂存队列")
                time.sleep(POLL_INTERVAL)
                continue

            # ── 发送通知（仅息屏唤醒时）+ 激活应用 ─────────────────────
            preview  = messages[-1].get("text", "")[:40]

            # 仅在息屏唤醒场景发送系统通知（作为备用提醒）
            # 亮屏时直接激活 App 即可，不打扰用户
            if woke_from_sleep:
                notif_body = f"{msg_count} 条待处理：{preview}" if preview else f"共 {msg_count} 条"
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
                set_processing_lock(ws)
                log(f"✅ 已激活并设置 processing 锁，Agent 处理完毕前不会重复触发")
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
