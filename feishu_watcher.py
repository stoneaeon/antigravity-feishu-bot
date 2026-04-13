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
POST_PROC_DELAY   = 15         # 处理完毕后等待秒数，让 Antigravity 完全结束本轮对话再触发下一轮
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
def get_pending_messages(ws: Path) -> tuple[list, bool, float, int]:
    """
    返回 (messages, is_processing, processing_elapsed_sec, processing_msg_count)。
    is_processing: Agent 是否正在处理队列（processing 锁）
    processing_elapsed_sec: 锁已持续的秒数（未锁定时为 0）
    processing_msg_count: 正在处理中的消息数量（processing_messages 列表长度）
    """
    qp = queue_path(ws)
    if not qp.exists():
        return [], False, 0.0, 0
    try:
        data = json.loads(qp.read_text(encoding="utf-8"))
        msgs = data.get("messages", [])
        is_processing = bool(data.get("processing", False))
        proc_msg_count = len(data.get("processing_messages", []))
        elapsed = 0.0
        if is_processing and data.get("processing_since"):
            try:
                since = datetime.datetime.strptime(
                    data["processing_since"], "%Y-%m-%d %H:%M:%S"
                )
                elapsed = (datetime.datetime.now() - since).total_seconds()
            except (ValueError, TypeError):
                pass
        return msgs, is_processing, elapsed, proc_msg_count
    except Exception:
        return [], False, 0.0, 0


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
    安全激活 Antigravity 并触发对话（借助 Vision OCR 防呆）。
    """
    safe_text = text.replace("\\", "\\\\").replace('"', '\\"')
    
    # 1. 激活应用并把窗口带到最前
    script_activate = f'tell application "{app_name}" to activate'
    subprocess.run(["osascript", "-e", script_activate], timeout=5)
    time.sleep(1) # 等待窗口显示与渲染
    
    # 2. 第一次尝试直接 OCR 点击输入框（如果面板已经处于展开状态，直接聚焦，防误关）
    clicked = False
    out = __run_vision("click", "Ask anything", "@ to mention")
    if "CLICKING at" in out:
        clicked = True
        log("✅ 通过 Vision OCR 成功锁定并聚焦 Chat 输入框")
        
    # 3. 如果没点到，说明面板可能被折叠/关闭。此时盲按一次 Cmd+L 展开它，然后重新尝试点击
    if not clicked:
        log("⚠️ 未发现输入框，尝试 Cmd+L 展开面板...")
        script_toggle = 'tell application "System Events" to keystroke "l" using command down'
        subprocess.run(["osascript", "-e", script_toggle], timeout=5)
        time.sleep(1) # 等待面板展开动画
        
        out2 = __run_vision("click", "Ask anything", "@ to mention")
        if "CLICKING at" in out2:
            clicked = True
            log("✅ 重新展开后成功聚焦 Chat 输入框")
            
    # 4. 如果两次都没找到，直接放弃（总好过贴到代码安全区里导致后续处理锁死）
    if not clicked:
        log("❌ OCR 均未能锁定 Chat 输入框，放弃当前触发，避免污染代码区。")
        # 返回 False 后，外层 watcher 不会上锁，下个轮询会自动重试。
        return False
        
    # 5. 确认已成功聚焦后，粘贴文本并回车提交以触发 Agent 启动
    script_paste = f"""
tell application "System Events"
    set the clipboard to "{safe_text}"
    keystroke "v" using command down
    delay 0.2
    keystroke return
end tell
    """
    try:
        subprocess.run(["osascript", "-e", script_paste], timeout=10)
        return True
    except subprocess.TimeoutExpired:
        log("⚠️  AppleScript 粘贴操作超时")
        return False
    except Exception as e:
        log(f"⚠️  执行粘贴异常: {e}")
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
    "server busy", "service unavailable", "503", "overload", "overloaded",
    "internal server error", "500", "bad gateway", "502",
    "gateway timeout", "504",
    "服务器繁忙", "服务器错误", "服务不可用", "超载", "超负荷",
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


def __run_vision(mode: str, *targets: str) -> str:
    """内部辅助方法：截屏并运行 mac_vision 二进制进行 OCR 提取和点击"""
    screen_path = "/tmp/ag_vision_tmp.png"
    # 静默全屏截取（可绕过应用无障碍限制）
    subprocess.run(["screencapture", "-x", screen_path])
    
    mac_vision_bin = Path(__file__).parent.parent / ".antigravity" / "mac_vision"
    if not mac_vision_bin.exists():
        return ""
    
    cmd = [str(mac_vision_bin), screen_path, mode] + list(targets)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
        return result.stdout.strip()
    except Exception:
        return ""

def detect_app_error(app_name: str) -> tuple[str, bool]:
    """
    通过 macOS 原生 Vision OCR (基于截屏) 读取 Antigravity 窗口文本，
    检测是否存在错误状态，不再依赖 Electron 失效的 DOM Tree。

    返回 (error_text, all_text):
      - error_text: 匹配到的错误文案（空字符串表示无错误）
      - all_text: 屏幕上收集到的所有文本（用于后续按钮辅助匹配）
    """
    if not is_app_running(app_name):
        return "", False

    out = __run_vision("detect")
    all_text = " ".join(
        line.replace("FOUND: ", "").strip()
        for line in out.splitlines()
        if line.startswith("FOUND: ")
    ).lower()

    if not all_text:
        return "", False

    matched_error = ""
    for pattern in ERROR_PATTERNS:
        if pattern.lower() in all_text:
            matched_error = pattern
            break

    return matched_error, all_text


def try_click_retry(app_name: str) -> bool:
    """
    尝试通过 Vision OCR 点击「重试」相关的文字坐标。
    返回 True 表示成功点拨。
    """
    out = __run_vision("click", "retry", "重试", "再试一次", "try again")
    return "CLICKING at" in out


def try_handle_quota(app_name: str, target_models: list = ["high", "gemini", "claude", "gpt", "sonnet"]) -> bool:
    """
    处理模型配额耗尽异常：
    1. 点击底部模型选择按钮（根据现有模型展示词点击）
    2. 在弹出的菜单中依次点击目标备用模型名单，点中为止。
    """
    # 查找并点击模型选择按钮。优先匹配带 ^ 符号的底边栏按钮名称，防止点击到含有这些词的聊天记录
    out = __run_vision("click", "^ claude", "^ gemini", "^ gpt", "^ sonnet", "^ opus", "^ high", "claude", "gemini")
    if "CLICKING at" not in out:
        pass # 找不到也不要直接退出，可能有别的方式触发展开或者本来就是展开的
        
    time.sleep(1.5)  # 等待模型列表弹窗渲染
    
    # 备选的高精度全称列表（防误触聊天记录）
    safe_target_models = ["Gemini 3.1 Pro", "Gemini 3.1", "Claude Opus 4.6", "Claude 3.5 Sonnet"]
    
    for tm in safe_target_models:
        clicked_out = __run_vision("click", tm)
        if "CLICKING at" in clicked_out:
            return True
            
    # 全称未识别到时则彻底放弃选模型，直接抛出失败，不再进行盲目的键盘注入兜底，以保证不会产生副作用。
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
    was_processing     = False   # 上一轮是否在处理中（用于检测处理完毕→新消息的转换）

    log(f"∎ 监控启动 · 工作区: {ws}")
    log(f"  目标应用: {app_name}  |  队列: {queue_path(ws)}")
    log(f"  Chat 快捷键: Cmd+Shift+keycode({CHAT_KEYCODE})")

    while True:
        try:
            messages, is_processing, proc_elapsed, proc_msg_count = get_pending_messages(ws)
            msg_count = len(messages)

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
                                    # 切换完模型后，重新输入任务重新触发
                                    log("  尝试重新输入任务发回车...")
                                    time.sleep(2)
                                    resume_trigger = f"检查飞书消息队列（{msg_count} 条待处理） --workspace {ws}"
                                    if activate_and_trigger(app_name, resume_trigger):
                                        log("  ✅ 已成功重新输入任务发回车")
                                        auto_handled = True
                                    else:
                                        log("  ⚠️  重新发回车失败")
                                        auto_handled = True # 依然当做被处理了，防止无限循环

                                else:
                                    log("  ⚠️  自动切换模型失败")
                            else:
                                # 对于其它异常，直接去点击上方的重试按钮
                                log("  尝试找 Retry 按钮点击重试...")
                                if try_click_retry(app_name):
                                    log("  ✅ 已成功点击 Retry 按钮")
                                    auto_handled = True
                                else:
                                    log("  ⚠️  自动点击 Retry 失败")
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

            # ── 队列为空判定 ─────────────────────────────────────────────
            # 必须三个条件同时满足才算真正处理完毕：
            #   1. messages[] 为空（无新消息）
            #   2. processing_messages[] 为空（无正在处理的消息）
            #   3. processing 锁已释放
            # 否则只是 read_messages 搬运了数据，Agent 还在处理中
            if msg_count == 0 and proc_msg_count == 0 and not is_processing:
                if last_msg_count > 0:
                    log("✅ 队列已清空，所有任务处理完毕")
                last_msg_count  = 0
                last_trigger_ts = 0.0
                time.sleep(POLL_INTERVAL)
                continue

            # messages 为空但仍有 processing_messages 或 processing 锁
            # → Agent 正在处理中，等待完成（不要误判为清空）
            if msg_count == 0 and (proc_msg_count > 0 or is_processing):
                was_processing = True
                time.sleep(POLL_INTERVAL)
                continue

            # ── 处理刚完毕，有新消息待处理 → 等待 Antigravity 完全结束对话 ─
            # Agent 刚完成上一轮任务，Antigravity UI 可能还没准备好接收新输入
            # 如果立刻触发，文本会堆到 Pending messages 而不是被提交
            if was_processing and msg_count > 0:
                log(f"⏳ 上轮处理刚完毕，等待 {POST_PROC_DELAY}s 再触发下一轮（{msg_count} 条待处理）")
                was_processing = False
                time.sleep(POST_PROC_DELAY)
                continue  # 重新进入循环检查最新状态

            # ── 冷却期与防重检查 ───────────────────────────────────────
            elapsed = time.time() - last_trigger_ts
            if last_trigger_ts > 0 and elapsed < COOLDOWN_SEC:
                time.sleep(POLL_INTERVAL)
                continue

            # ── 有新消息（且处于冷却期外）────────────────────────────
            # 只要是没有处理锁且队内有消息，即刻开始排队执行下一轮任务流
            preview = messages[-1].get("text", "")[:50]
            log(f"📨 检测到 {msg_count} 条待处理消息：「{preview}」")
            last_msg_count = msg_count

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
            # 带上 --workspace 路径，防止多项目冲突时 Agent 读错队列
            trigger = f"检查飞书消息队列（{msg_count} 条待处理） --workspace {ws}"
            log(f"激活 {app_name} 并触发对话...（工作区: {ws.name}）")
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
