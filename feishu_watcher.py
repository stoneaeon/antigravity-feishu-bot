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
import fcntl
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
PROCESSING_CONFIRM_TIMEOUT = 120  # Agent 启动确认超时（秒），设置锁后 Agent 未读取消息则视为未启动
POST_PROC_DELAY   = 15         # 处理完毕后等待秒数，让 Antigravity 完全结束本轮对话再触发下一轮
MAX_CONSECUTIVE_ERRORS = 3     # 连续重试失败上限（3次×10秒），超过后释放锁让用户可通过飞书恢复
ERROR_BACKOFF_BASE = 10        # 错误重试间隔（秒），固定10秒
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


def _locked_queue_rw(qp: Path, writer=None):
    """跨进程安全地读写队列文件。

    使用 fcntl.flock() 实现进程间互斥，防止 listener/watcher/Agent
    同时读写 JSON 文件导致数据竞态。

    writer=None: 只读，返回 data dict
    writer=callable: 读取后调用 writer(data)，写回文件，返回 data
    """
    qp.parent.mkdir(parents=True, exist_ok=True)
    lock_file = qp.with_suffix(".lock")
    with open(lock_file, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            data = json.loads(qp.read_text(encoding="utf-8")) if qp.exists() else {"messages": []}
            if writer:
                writer(data)
                qp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return data
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


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
def get_pending_messages(ws: Path) -> tuple[list, bool, float, int, bool]:
    """
    返回 (messages, is_processing, processing_elapsed_sec, processing_msg_count, agent_has_read)。
    is_processing: Agent 是否正在处理队列（processing 锁）
    processing_elapsed_sec: 锁已持续的秒数（未锁定时为 0）
    processing_msg_count: 正在处理中的消息数量（processing_messages 列表长度）
    agent_has_read: Agent 是否已读取消息（agent_read_at 是否存在）
    """
    qp = queue_path(ws)
    if not qp.exists():
        return [], False, 0.0, 0, False
    try:
        data = _locked_queue_rw(qp)
        msgs = data.get("messages", [])
        is_processing = bool(data.get("processing", False))
        proc_msg_count = len(data.get("processing_messages", []))
        agent_has_read = bool(data.get("agent_read_at"))
        elapsed = 0.0
        if is_processing and data.get("processing_since"):
            try:
                since = datetime.datetime.strptime(
                    data["processing_since"], "%Y-%m-%d %H:%M:%S"
                )
                elapsed = (datetime.datetime.now() - since).total_seconds()
            except (ValueError, TypeError):
                pass
        return msgs, is_processing, elapsed, proc_msg_count, agent_has_read
    except Exception:
        return [], False, 0.0, 0, False


def reset_processing_lock(ws: Path, restore_messages: bool = False) -> None:
    """重置 processing 锁（超时后由 watcher 调用，防止死锁）。
    
    restore_messages=True 时，将 processing_messages 放回 messages 头部，
    防止配额切换/连续错误后消息被永远搁置。
    """
    qp = queue_path(ws)
    if not qp.exists():
        return
    try:
        def _do_reset(data):
            if restore_messages:
                proc_msgs = data.pop("processing_messages", [])
                if proc_msgs:
                    # 放回 messages 头部，优先处理旧消息
                    data["messages"] = proc_msgs + data.get("messages", [])
                    log(f"🔄 已将 {len(proc_msgs)} 条消息回迁到待处理队列")
            else:
                data.pop("processing_messages", None)
            data.pop("processing", None)
            data.pop("processing_since", None)
            data.pop("agent_read_at", None)

        _locked_queue_rw(qp, writer=_do_reset)
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
        def _do_set(data):
            data["processing"] = True
            data["processing_since"] = now()
            data.pop("agent_read_at", None)  # 清除旧心跳，等待新一轮 Agent 确认

        _locked_queue_rw(qp, writer=_do_set)
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
    keystroke "a" using command down
    delay 0.1
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
    "rate limit", "rate_limit", "too many requests", "error 429", "status 429",
    "用量上限", "请求过多", "模型额度", "频率限制",
    # 服务器忙 / 连接异常
    "our servers are experiencing high traffic", "agent terminated due to error",
    "server busy", "service unavailable", "error 503", "status 503", "overload", "overloaded",
    "internal server error", "error 500", "status 500", "bad gateway", "error 502", "status 502",
    "gateway timeout", "error 504", "status 504",
    "服务器繁忙", "服务器错误", "服务不可用", "超载", "超负荷",
    # 明确的错误提示（需要包含完整短语，避免子串误匹配）
    "something went wrong", "an error occurred", "unexpected error",
    "出现错误", "发生异常", "请求失败",
    # 需要用户操作的阻断提示
    "try again later", "please try again",
    "请稍后重试", "请重试",
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
        return "", ""

    out = __run_vision("detect")
    all_text = " ".join(
        line.replace("FOUND: ", "").strip()
        for line in out.splitlines()
        if line.startswith("FOUND: ")
    ).lower()

    if not all_text:
        return "", ""

    matched_error = ""
    for pattern in ERROR_PATTERNS:
        if pattern.lower() in all_text:
            matched_error = pattern
            break

    return matched_error, all_text



def try_click_retry(app_name: str) -> bool:
    """
    处理 Antigravity 错误弹窗，恢复到可操作状态。

    已知问题：Retry 按钮是蓝色背景+白色文字，macOS Vision OCR
    无法识别 "Retry" 文本。

    解决方案（Dismiss + 重触发）：
      1. OCR 能可靠识别到 "Dismiss" 按钮
      2. 点击 Dismiss 关闭错误弹窗，恢复到正常输入界面
      3. 返回 "DISMISSED" 告知调用方需要通过输入框重新触发任务

    返回值含义：
      True  = 弹窗已关闭（通过 Dismiss），调用方应重新触发任务
      False = 未能关闭弹窗
    """
    # 直接通过 OCR 点击 Dismiss 按钮（OCR 能可靠识别）
    out = __run_vision("click", "Dismiss")
    if "CLICKING at" in out:
        log("    → ✅ 已通过 OCR 点击 Dismiss 关闭错误弹窗")
        return True

    # 备选：中文界面的关闭按钮
    out2 = __run_vision("click", "关闭", "取消")
    if "CLICKING at" in out2:
        log("    → ✅ 通过中文按钮关闭弹窗")
        return True

    log("    → ❌ 未能定位任何可点击的按钮")
    return False


def try_handle_quota(app_name: str, target_models: list = ["high", "gemini", "claude", "gpt", "sonnet"]) -> bool:
    """
    处理模型配额耗尽异常：
    1. 点击底部模型选择按钮（根据现有模型展示词点击）
    2. 在弹出的菜单中依次点击目标备用模型名单，点中为止。
    """
    # 查找并点击模型选择按钮。用全称匹配，避免误点聊天记录中的相似文字
    out = __run_vision("click", "claude", "gemini", "gpt", "sonnet", "opus", "high")
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


def _classify_error(error_text: str) -> str:
    """将错误文本分类为用户可读的错误类型"""
    et = error_text.lower()
    if any(q in et for q in ["quota", "usage limit", "用量上限", "模型额度"]):
        return "🔴 模型配额耗尽"
    if any(q in et for q in ["rate limit", "rate_limit", "too many requests", "error 429", "status 429", "请求过多", "频率限制"]):
        return "🟡 请求频率限制"
    if any(q in et for q in ["error 503", "status 503", "high traffic", "overload", "服务器繁忙", "超载"]):
        return "🟠 服务器繁忙/过载"
    if any(q in et for q in ["error 500", "status 500", "internal server error", "服务器错误"]):
        return "🔴 服务器内部错误"
    if any(q in et for q in ["error 502", "status 502", "error 504", "status 504", "bad gateway", "gateway timeout"]):
        return "🟠 网关错误/超时"
    if any(q in et for q in ["agent terminated", "terminated due to error"]):
        return "🔴 Agent 异常终止"
    if any(q in et for q in ["something went wrong", "an error occurred", "unexpected error", "出现错误"]):
        return "🔴 未知错误"
    return f"⚪ 其他异常: {error_text}"


def notify_error_via_feishu(ws: Path, error_text: str,
                            auto_handled: bool,
                            retry_count: int = 0,
                            lock_released: bool = False) -> None:
    """通过飞书发送异常通知（调用 feishu.py send_text），包含具体异常分类"""
    feishu_py = Path(__file__).parent / "feishu.py"
    if not feishu_py.exists():
        return

    error_category = _classify_error(error_text)
    status = "✅ 已自动重试" if auto_handled else "⚠️ 需要人工处理"
    
    parts = [
        f"🚨 Antigravity 任务异常\n",
        f"异常分类：{error_category}",
        f"原始信息：{error_text}",
        f"处理状态：{status}",
    ]
    if retry_count > 0:
        parts.append(f"已重试次数：{retry_count}")
    if lock_released:
        parts.append(f"\n🔓 processing 锁已释放，你可以直接发消息给我来恢复操作")
    parts.extend([
        f"工作区：{ws.name}",
        f"时间：{now()}",
    ])
    
    msg = "\n".join(parts)
    try:
        subprocess.Popen(
            [sys.executable, str(feishu_py), "send_text", msg,
             "--workspace", str(ws)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


# ── 特权控制指令 ──────────────────────────────────────────────────────────────
def notify_via_feishu(ws: Path, msg: str) -> None:
    """发送普通的文本通知（调用 feishu.py send_text）"""
    feishu_py = Path(__file__).parent / "feishu.py"
    if not feishu_py.exists():
        return
    try:
        subprocess.Popen(
            [sys.executable, str(feishu_py), "send_text", msg,
             "--workspace", str(ws)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass

def switch_model_ui(app_name: str, target_model: str) -> bool:
    """仅激活窗口并通过 OCR 切模型"""
    script_activate = f'tell application "{app_name}" to activate'
    subprocess.run(["osascript", "-e", script_activate], timeout=5)
    time.sleep(1.5)
    
    # 点击当前模型下拉框
    __run_vision("click", "claude", "gemini", "gpt", "sonnet", "opus", "high")
    time.sleep(1.5)
    
    # 点击目标模型
    clicked_out = __run_vision("click", target_model)
    return "CLICKING at" in clicked_out

def intercept_special_commands(ws: Path, messages: list, app_name: str) -> bool:
    """拦截特权控制字，返回 True 表示已拦截处理"""
    if not messages:
        return False
        
    last_msg = messages[-1]
    text = last_msg.get("text", "").strip()
    
    if text in ["#重置", "#打断", "#reset", "强行打断", "清空队列", "重置", "打断"]:
        log("🔄 收到手工清空/打断指令，执行重置...")
        def _do_clear_cmd(data):
            data.pop("processing_messages", None)
            data.pop("processing", None)
            data.pop("processing_since", None)
            data.pop("agent_read_at", None)
            data["messages"] = [m for m in data.get("messages", []) if m.get("message_id") != last_msg.get("message_id")]
        
        _locked_queue_rw(queue_path(ws), writer=_do_clear_cmd)
        notify_via_feishu(ws, "✅ 已收到干预指令，为您强制清空锁定并且终结原有执行记录。")
        return True
        
    is_switch = False
    target_model_raw = ""
    for prefix in ["#切模型", "切模型"]:
        if text.startswith(prefix):
            is_switch = True
            target_model_raw = text[len(prefix):].strip()
            break
            
    if is_switch:
        # 智能简写映射：防患大模型精确字眼OCR不上的情况
        tm_lower = target_model_raw.lower()
        if "gemini" in tm_lower or "3.1" in tm_lower:
            target_model = "Gemini 3.1 Pro"
        elif "opus" in tm_lower or "4.6" in tm_lower:
            target_model = "Claude Opus 4.6"
        elif "sonnet" in tm_lower or "3.5" in tm_lower:
            target_model = "Claude 3.5 Sonnet"
        else:
            target_model = target_model_raw if target_model_raw else "Claude Opus 4.6"
            
        log(f"🔄 收到切模型指令：[{target_model_raw}] -> 将试图映射定位: {target_model}")
        
        notify_via_feishu(ws, f"正在为您切换至模型: {target_model}...")
        ok = switch_model_ui(app_name, target_model)
        
        def _do_clear_cmd2(data):
            data["messages"] = [m for m in data.get("messages", []) if m.get("message_id") != last_msg.get("message_id")]
        _locked_queue_rw(queue_path(ws), writer=_do_clear_cmd2)
        
        if ok:
            notify_via_feishu(ws, f"✅ 已成功切换至模型: {target_model}")
        else:
            notify_via_feishu(ws, f"⚠️ 切换模型失败，未能通过视觉定位到 [{target_model}]")
        return True
        
    return False


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
    consecutive_errors = 0       # 连续错误计数（用于退避和上限判断）
    was_processing     = False   # 上一轮是否在处理中（用于检测处理完毕→新消息的转换）
    consecutive_trigger_fails = 0 # 连续触发失败计数（用于防锁屏通知刷屏）

    log(f"∎ 监控启动 · 工作区: {ws}")
    log(f"  目标应用: {app_name}  |  队列: {queue_path(ws)}")
    log(f"  Chat 快捷键: Cmd+Shift+keycode({CHAT_KEYCODE})")

    while True:
        try:
            messages, is_processing, proc_elapsed, proc_msg_count, agent_has_read = get_pending_messages(ws)
            msg_count = len(messages)

            # ── 特殊指令强制拦截区 ────
            if msg_count > 0 and intercept_special_commands(ws, messages, app_name):
                time.sleep(1)
                continue

            # ── Bug 2 修复：跳过仅含 pending_instruction 的图片消息 ────
            # 如果所有消息都是 pending_instruction=true（图片等待后续文字指令），
            # 不触发 Agent，等用户发送文字指令后再一起处理
            actionable_count = sum(
                1 for m in messages
                if not m.get("pending_instruction", False)
            )
            if msg_count > 0 and actionable_count == 0 and not is_processing:
                # 所有消息都在等后续指令，不触发
                time.sleep(POLL_INTERVAL)
                continue

            # ── Agent 正在处理中（processing 锁）──────────────────────
            if is_processing:
                was_processing = True  # 跟踪处理状态，用于检测 完毕→新消息 的自动触发

                # ── Agent 启动确认检查 ──────────────────────────────────
                # processing 锁设置后，若 Agent 始终未读取消息（agent_read_at 不存在），
                # 超过确认超时则判定 Agent 未启动
                # 常见原因：会话不活跃、模型切换间隙、Agent 在处理其他任务
                if (proc_elapsed > PROCESSING_CONFIRM_TIMEOUT
                        and not agent_has_read
                        and (msg_count > 0 or proc_msg_count > 0)):
                    log(f"⚠️  processing 锁已 {proc_elapsed:.0f}s，但 Agent 未读取消息（无 agent_read_at）")
                    log(f"   疑似 Agent 未启动（会话不活跃/模型切换），重置锁并重新触发")
                    reset_processing_lock(ws, restore_messages=True)
                    error_check_count = 0
                    was_processing = False
                    last_trigger_ts = 0.0  # 重置冷却，允许立即重新触发
                    send_notification(
                        title="🔄 自动重新触发",
                        body=f"Agent 未响应，重新激活处理（{msg_count + proc_msg_count} 条消息）"
                    )
                    # 不 continue，落入下方触发逻辑
                elif proc_elapsed < PROCESSING_TIMEOUT:
                    # 锁未超时 → Agent 仍在处理
                    # 每隔 5 个轮询周期（约 10 秒）检测一次 UI 异常
                    error_check_count += 1
                    if error_check_count % 5 == 0 and is_app_running(app_name):
                        error_text, buttons_str = detect_app_error(app_name)

                        # 自动检测并接受代码变更
                        if "accept all" in buttons_str:
                            log("💡 检测到 Accept all 按钮，正在自动点击接受全部变更...")
                            out_click = __run_vision("click", "Accept all")
                            if "CLICKING at" in out_click:
                                log("  ✅ 已成功自动接受变更")
                            else:
                                log("  ⚠️  试图点击 Accept all 但视觉定位未成功")

                        if error_text:
                            consecutive_errors += 1
                            error_category = _classify_error(error_text)
                            log(f"🚨 检测到异常 [{consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}]: {error_category} ({error_text})")
                            auto_handled = False
                            
                            is_quota = any(q in error_text.lower() for q in ["quota", "usage limit", "rate limit", "用量上限"])
                            
                            if is_quota:
                                log(f"  尝试自动切换模型 (配额超限)...")
                                if try_handle_quota(app_name):
                                    log("  ✅ 已自动切换备用模型")
                                    auto_handled = True
                                    consecutive_errors = 0
                                    # 释放锁并回迁消息，防止消息被搁置在 processing_messages
                                    reset_processing_lock(ws, restore_messages=True)
                                    notify_error_via_feishu(
                                        ws, error_text, auto_handled=True,
                                        retry_count=consecutive_errors,
                                        lock_released=True,
                                    )
                                    log("  📨 已通知用户模型已切换，消息已回迁待重新处理")
                                    continue
                                else:
                                    log("  ⚠️  自动切换模型失败")
                            else:
                                # 对于其它异常（服务器忙、Agent 终止等）
                                # 点击 Dismiss 关闭弹窗，保持 processing 锁
                                # 不释放锁、不重触发（避免 trigger text 堆积，即 Bug 4）
                                # 如果弹窗关闭后 Agent 能恢复，锁会在队列清空时自然释放
                                # 如果连续失败达到上限，下方逻辑会释放锁
                                log("  尝试关闭错误弹窗（Dismiss）...")
                                if try_click_retry(app_name):
                                    log("  ✅ 弹窗已关闭，保持 processing 锁等待恢复")
                                    auto_handled = True
                                else:
                                    log("  ⚠️  未能关闭弹窗")
                            
                            # ── 连续错误超限：释放 processing 锁 ──────────
                            # 非模型配额类错误连续多次失败后，释放锁并回迁消息，
                            # 让用户可以通过飞书发消息来恢复操作
                            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS and not is_quota:
                                log(f"⚠️  连续 {consecutive_errors} 次异常，释放 processing 锁")
                                log(f"  🔓 用户可通过飞书发送消息来恢复操作")
                                reset_processing_lock(ws, restore_messages=True)
                                consecutive_errors = 0
                                # 立即发飞书通知，告知用户锁已释放
                                notify_error_via_feishu(
                                    ws, error_text, auto_handled=False,
                                    retry_count=MAX_CONSECUTIVE_ERRORS,
                                    lock_released=True,
                                )
                                send_notification(
                                    title="🔓 Processing 锁已释放",
                                    body=f"连续 {MAX_CONSECUTIVE_ERRORS} 次异常 · 发飞书消息可恢复"
                                )
                                last_error_notify = time.time()
                                log("  📨 已发送飞书通知（锁释放）")
                                # 退避等待，给服务器恢复时间
                                backoff = ERROR_BACKOFF_BASE * MAX_CONSECUTIVE_ERRORS
                                log(f"  ⏳ 退避等待 {backoff}s...")
                                time.sleep(backoff)
                                continue  # 跳过下方的常规通知
                            
                            # ── 固定间隔等待（10秒）──────────────────────
                            if consecutive_errors >= 1:
                                log(f"  ⏳ 等待 {ERROR_BACKOFF_BASE}s 后重试...")
                                time.sleep(ERROR_BACKOFF_BASE)
                            
                            # 中间重试阶段不发飞书通知（只记日志），
                            # 仅在最终释放锁时发一条汇总通知（见上方连续错误超限逻辑）
                        else:
                            # 无错误检测到，重置连续错误计数
                            if consecutive_errors > 0:
                                log(f"  ✅ 异常已恢复（之前连续 {consecutive_errors} 次）")
                                consecutive_errors = 0
                    time.sleep(POLL_INTERVAL)
                    continue
                else:
                    # 锁已超时（超过 10 分钟）→ 疑似死锁，重置并重新触发
                    log(f"⚠️  processing 锁已超时（{proc_elapsed:.0f}s），重置并重新触发")
                    reset_processing_lock(ws, restore_messages=True)
                    error_check_count = 0
                    was_processing = False  # 修复漏洞3：超时重置时同步清除 was_processing
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
                was_processing  = False
                time.sleep(POLL_INTERVAL)
                continue

            # messages 为空但仍有 processing_messages 或 processing 锁
            # → Agent 正在处理中，等待完成（不要误判为清空）
            if msg_count == 0 and (proc_msg_count > 0 or is_processing):
                was_processing = True
                time.sleep(POLL_INTERVAL)
                continue

            # ── 处理刚完毕，有新消息待处理 → 等待后自动触发下一轮 ───
            # Agent 刚完成上一轮任务，等待 UI 就绪后直接触发（跳过冷却期）
            if was_processing and msg_count > 0:
                log(f"🔄 上轮处理刚完成，检测到 {msg_count} 条新消息待处理")
                log(f"⏳ 等待 {POST_PROC_DELAY}s 让 Antigravity 就绪...")
                was_processing = False
                time.sleep(POST_PROC_DELAY)
                # 重新检查队列状态（等待期间可能已被其他方式处理）
                messages, is_processing, _, _, _ = get_pending_messages(ws)
                msg_count = len(messages)
                if msg_count == 0 or is_processing:
                    log("   → 等待期间队列已清空或新 processing 锁已设置，跳过本轮")
                    continue
                last_trigger_ts = 0.0  # 重置冷却期，跳过下方冷却检查
                log(f"   → 自动触发下一轮处理（{msg_count} 条待处理）")
                # 不 continue，直接落入下方触发逻辑

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
                consecutive_trigger_fails = 0
                log(f"✅ 已激活并设置 processing 锁，Agent 处理完毕前不会重复触发")
            else:
                consecutive_trigger_fails += 1
                log(f"⚠️  自动触发失败 [{consecutive_trigger_fails}]，请手动切换到 {app_name}")
                # UX体验优化：通知退避机制，防止锁屏无限发飞书刷屏
                if consecutive_trigger_fails == 1 or consecutive_trigger_fails % 10 == 0:
                    notify_error_via_feishu(
                        ws, f"AppleScript/OCR 触发失败 (第 {consecutive_trigger_fails} 次)，Chat 框未定位",
                        auto_handled=False, lock_released=True,
                    )
                    send_notification(
                        title="⚠️ 飞书任务触发失败",
                        body=f"{msg_count} 条消息待处理，请检查屏幕是否锁定"
                    )
                else:
                    log(f"   已进入崩溃通知静默期，暂不向飞书推送过载报警")

            last_trigger_ts = time.time()

        except Exception as e:
            log(f"监控循环异常: {e}")
            # 修复漏洞5：外层异常发飞书通知，避免故障静默
            try:
                notify_error_via_feishu(
                    ws, f"Watcher 循环异常: {e}",
                    auto_handled=False, lock_released=False,
                )
            except Exception:
                pass  # 通知失败不能让 watcher 崩溃

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
