#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations  # 兼容 Python 3.9 的类型注解
"""
feishu_listener.py — 飞书消息监听 Daemon

使用飞书官方 SDK 的 WebSocket 长连接接收消息，无需公网地址。
收到的消息写入 {workspace}/.antigravity/feishu_messages.json。

特性：
  - 首次收到消息时自动记录 open_id / chat_id，无需手动配置目标
  - 支持 P2P 私聊 和 群聊
  - 支持前台运行和后台守护进程（macOS/Linux）

用法:
  python3 feishu_listener.py                     # 前台运行（调试）
  python3 feishu_listener.py --daemon            # 后台守护进程
  python3 feishu_listener.py --status            # 查看守护进程状态
  python3 feishu_listener.py --stop              # 停止守护进程
  python3 feishu_listener.py --workspace /path   # 指定工作区
"""

import os
import sys
import json
import signal
import logging
import datetime
import argparse
import threading
from pathlib import Path

# ── 检查 lark-oapi 依赖 ──────────────────────────────────────────────────────
try:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
except ImportError:
    print(
        "❌ 缺少飞书 SDK，请运行：pip3 install lark-oapi",
        file=sys.stderr,
    )
    sys.exit(1)

# ── 日志配置 ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("feishu_listener")


# ── 工具 ─────────────────────────────────────────────────────────────────────
def now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def find_workspace(workspace: str = None) -> Path:
    if workspace:
        return Path(workspace).resolve()
    cwd = Path.cwd()
    for p in [cwd] + list(cwd.parents):
        if (p / ".antigravity").is_dir():
            return p
    return cwd


def cfg_path(ws: Path) -> Path:
    return ws / ".antigravity" / "feishu_config.json"


def queue_path(ws: Path) -> Path:
    return ws / ".antigravity" / "feishu_messages.json"


def pid_path(ws: Path) -> Path:
    return ws / ".antigravity" / "feishu_listener.pid"


def log_path(ws: Path) -> Path:
    return ws / ".antigravity" / "feishu_listener.log"


# ── 配置读写 ─────────────────────────────────────────────────────────────────
def load_config(ws: Path) -> dict:
    p = cfg_path(ws)
    if not p.exists():
        log.error(f"配置文件不存在: {p}")
        log.error("请先运行：python3 feishu.py setup --app-id=xxx --app-secret=yyy")
        sys.exit(1)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.error(f"配置读取失败: {e}")
        sys.exit(1)


def save_config(cfg: dict, ws: Path) -> None:
    p = cfg_path(ws)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 消息内容解析 ─────────────────────────────────────────────────────────────
def parse_text(msg_type: str, content_raw: str) -> str:
    """将飞书消息 content（JSON 字符串）解析为纯文本

    飞书消息的 content 字段通常是 JSON 字符串，解析后为 dict。
    但某些消息类型（如系统通知）的 content 解析后可能是 str 而非 dict，
    需要防御性处理，避免对 str 调用 .get() 导致 AttributeError。
    """
    try:
        content = json.loads(content_raw)
    except (json.JSONDecodeError, TypeError):
        return content_raw or ""

    # content 解析后不是 dict（例如纯字符串），直接返回其字符串形式
    if not isinstance(content, dict):
        return str(content).strip()

    if msg_type == "text":
        return content.get("text", "").strip()

    if msg_type == "post":
        # 富文本结构：{"zh_cn": {"title": "...", "content": [[{tag, text}, ...], ...]}}
        # 防御性解析：content.values() 为各语言版本，每个应为 dict
        parts = []
        try:
            for lang_content in content.values():
                if not isinstance(lang_content, dict):
                    # 非 dict 的值（不符合预期的 post 结构），跳过
                    continue
                title = lang_content.get("title", "")
                if title:
                    parts.append(title)
                for row in lang_content.get("content", []):
                    if not isinstance(row, list):
                        continue
                    for elem in row:
                        if not isinstance(elem, dict):
                            continue
                        # 提取所有带 text 字段的元素（text、a、at 等标签都可能含文本）
                        t = elem.get("text", "")
                        if t:
                            parts.append(t)
        except Exception:
            # post 结构不符合预期时，回退到原始字符串
            return content_raw.strip() if content_raw else ""
        return " ".join(parts).strip()

    if msg_type == "image":
        # 图片消息：提取 image_key 供后续下载
        image_key = content.get("image_key", "")
        return f"[image:{image_key}]" if image_key else "[image]"

    if msg_type == "file":
        # 文件消息：提取 file_key 和文件名
        file_key = content.get("file_key", "")
        file_name = content.get("file_name", "")
        if file_key:
            return f"[file:{file_key}:{file_name}]" if file_name else f"[file:{file_key}]"
        return "[file]"

    # 其他类型（音频、视频、表情等）
    return f"[{msg_type}]"


# ── 消息队列写入（线程安全）───────────────────────────────────────────────────
_queue_lock = threading.Lock()


def enqueue_message(ws: Path, record: dict) -> tuple[bool, int]:
    """线程安全地向消息队列追加一条消息，内置 message_id 去重，返回 (是否正在处理, 队列总长)"""
    qp = queue_path(ws)
    is_proc = False
    queue_len = 0
    with _queue_lock:
        qp.parent.mkdir(parents=True, exist_ok=True)

        # 读取现有队列
        if qp.exists():
            try:
                data = json.loads(qp.read_text(encoding="utf-8"))
                is_proc = bool(data.get("processing", False))
            except (json.JSONDecodeError, OSError):
                data = {"messages": []}
        else:
            data = {"messages": []}

        # 去重：检查 message_id 是否已存在
        existing_ids = {m.get("message_id") for m in data["messages"]}
        if record.get("message_id") in existing_ids:
            log.debug(f"重复消息，跳过: {record.get('message_id')}")
            return is_proc, len(data["messages"])

        data["messages"].append(record)
        data["last_updated"] = now()
        queue_len = len(data["messages"])

        qp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return is_proc, queue_len


# ── 消息处理器 ───────────────────────────────────────────────────────────────
class MessageHandler:
    def __init__(self, ws: Path) -> None:
        self.ws  = ws
        self.cfg = load_config(ws)
        # 内存去重集合（防止短时间内重复处理同一条消息）
        self._seen_ids: set = set()

    def handle(self, data: P2ImMessageReceiveV1) -> None:
        """飞书消息事件回调，由 SDK 在收到消息时调用"""
        try:
            event   = data.event
            message = event.message
            sender  = event.sender

            # 过滤机器人自身发送的消息
            if sender.sender_type == "bot":
                return

            msg_id    = message.message_id
            chat_type = message.chat_type   # "p2p" 或 "group"
            chat_id   = message.chat_id
            open_id   = sender.sender_id.open_id if sender.sender_id else ""

            # 内存去重
            if msg_id in self._seen_ids:
                return
            self._seen_ids.add(msg_id)
            # 控制内存：保留最近 500 条 ID
            if len(self._seen_ids) > 500:
                self._seen_ids = set(list(self._seen_ids)[-250:])

            # 解析消息文本
            msg_type = message.message_type
            text = parse_text(msg_type, message.content)
            if not text.strip():
                return  # 忽略空消息

            # ── 自动记录目标（首次收到消息时）──────────────────────────────
            if not self.cfg.get("target_id"):
                if chat_type == "p2p" and open_id:
                    self.cfg["target_id"]   = open_id
                    self.cfg["target_type"] = "p2p"
                elif chat_type == "group" and chat_id:
                    self.cfg["target_id"]   = chat_id
                    self.cfg["target_type"] = "group"
                else:
                    log.warning(f"无法确定目标 ID，跳过（chat_type={chat_type}）")
                    return

                save_config(self.cfg, self.ws)
                pn = self.cfg.get("project_name") or self.ws.name
                log.info(f"🎯 已自动记录目标 [{chat_type}]: {self.cfg['target_id'][:20]}...")
                self._send_activation(pn)

            # ── 图片/文件消息：暂存并回复确认，等待后续指令 ──────────────────
            is_media = msg_type in ("image", "file")
            if is_media:
                # 存入队列但标记为待指令，不触发 Agent 立即处理
                record = {
                    "message_id":  msg_id,
                    "chat_type":   chat_type,
                    "open_id":     open_id,
                    "chat_id":     chat_id if chat_type == "group" else "",
                    "msg_type":    msg_type,
                    "text":        text,
                    "time":        now(),
                    "pending_instruction": True,
                }
                enqueue_message(self.ws, record)
                log.info(f"📎 [{chat_type}] 收到{msg_type}，等待用户指令: {text[:60]}")
                self._send_reaction(msg_id)
                # 回复用户：已收到，等待指令
                media_label = "图片" if msg_type == "image" else "文件"
                self._reply_text(f"✅ 已收到{media_label}，需要怎么处理？")
                return

            # ── 写入消息队列（文本/富文本等常规消息）──────────────────────────
            record = {
                "message_id":  msg_id,
                "chat_type":   chat_type,
                "open_id":     open_id,
                "chat_id":     chat_id if chat_type == "group" else "",
                "msg_type":    msg_type,
                "text":        text,
                "time":        now(),
            }
            is_proc, q_len = enqueue_message(self.ws, record)
            log.info(f"📨 [{chat_type}] {text[:60]}")
            self._send_reaction(msg_id)
            if is_proc:
                log.info(f"⏸️ 当前有任务在处理，向用户发送排队提示 (第 {q_len} 位)")
                self._reply_text(f"⏸️ [系统忙碌] 当前有个极耗时的任务正占用机器人跑动中！\n您的新指令已成功入队 (当前等待顺位：{q_len})，等手头一忙完立刻自动无缝接力为您执行！")

        except Exception as e:
            log.error(f"消息处理异常: {e}", exc_info=True)

    def _send_activation(self, project_name: str) -> None:
        """
        发送激活确认消息（子进程调用 feishu.py）。
        在守护进程中使用子进程，避免阻塞事件循环。
        此时 feishu_config.json 中的 target_id 已更新，
        feishu.py 读取配置后能正确发送到目标。
        """
        import subprocess
        feishu_py = Path(__file__).parent / "feishu.py"
        if not feishu_py.exists():
            log.warning(f"feishu.py 未找到: {feishu_py}，跳过激活消息")
            return
        pn = project_name or self.ws.name
        try:
            subprocess.Popen(
                [
                    sys.executable,
                    str(feishu_py),
                    "send_text",
                    (
                        f"✅ 双向通信已激活！\n"
                        f"我是「{pn}」的 Antigravity AI 助手。\n"
                        f"发指令给我，我会立即处理并回复结果。"
                    ),
                    "--workspace", str(self.ws),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as e:
            log.warning(f"激活消息发送失败（不影响监听）: {e}")

    def _send_reaction(self, msg_id: str) -> None:
        """调用子进程添加表情回复（OK），表示消息已收到/已读"""
        import subprocess
        feishu_py = Path(__file__).parent / "feishu.py"
        if not feishu_py.exists():
            return
        try:
            subprocess.Popen(
                [
                    sys.executable,
                    str(feishu_py),
                    "send_reaction",
                    msg_id,
                    "OK",
                    "--workspace", str(self.ws),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass

    def _reply_text(self, text: str) -> None:
        """回复用户一条文本消息（子进程调用 feishu.py send_text）"""
        import subprocess
        feishu_py = Path(__file__).parent / "feishu.py"
        if not feishu_py.exists():
            return
        try:
            subprocess.Popen(
                [
                    sys.executable,
                    str(feishu_py),
                    "send_text", text,
                    "--workspace", str(self.ws),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass


# ── WebSocket 监听主循环 ─────────────────────────────────────────────────────
def run_listener(ws: Path, cfg: dict) -> None:
    handler = MessageHandler(ws)

    # 注册事件处理器
    # encrypt_key 和 verification_token 在 WebSocket 模式下不需要，传空字符串
    dispatcher = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(handler.handle)
        .build()
    )

    # 创建 WebSocket 客户端
    ws_client = lark.ws.Client(
        cfg["app_id"],
        cfg["app_secret"],
        event_handler=dispatcher,
        log_level=lark.LogLevel.WARNING,  # 避免 SDK 内部日志过多
    )

    pn = cfg.get("project_name") or ws.name
    log.info("=" * 50)
    log.info(f"  飞书消息监听器 · {pn}")
    if cfg.get("target_id"):
        log.info(f"  目标: [{cfg.get('target_type','')}] {cfg['target_id'][:20]}...")
    else:
        log.info("  ⏳ 等待首次飞书消息以激活双向通信")
    log.info("  模式: WebSocket 长连接（无需公网地址）")
    log.info("=" * 50)

    # 优雅退出
    def _on_signal(sig, frame) -> None:
        log.info("收到退出信号，正在关闭...")
        sys.exit(0)

    signal.signal(signal.SIGINT,  _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    log.info("✅ 监听器就绪，等待飞书消息...\n")
    ws_client.start()  # 阻塞直到退出


# ── 守护进程（macOS / Linux）─────────────────────────────────────────────────
def daemonize(ws: Path, cfg: dict) -> None:
    """
    使用 subprocess.Popen 启动全新子进程作为守护进程。
    避免 fork + asyncio 在 Python 3.9/macOS 上的 "Bad file descriptor" 问题：
    使用 fork 时，子进程会继承父进程的 asyncio selector fd，导致崩溃。
    改用 Popen 启动全新进程，不继承任何 event loop 状态。
    """
    import subprocess

    pp = pid_path(ws)

    # 检查是否已有实例在运行
    if pp.exists():
        try:
            existing_pid = int(pp.read_text().strip())
            os.kill(existing_pid, 0)
            log.warning(
                f"监听器已在运行 (PID={existing_pid})，无需重复启动\n"
                f"  停止命令: python3 feishu_listener.py --stop"
            )
            return
        except (ProcessLookupError, PermissionError):
            pp.unlink(missing_ok=True)
        except ValueError:
            pp.unlink(missing_ok=True)

    lp = log_path(ws)
    lp.parent.mkdir(parents=True, exist_ok=True)

    # 以全新子进程运行（传入 --_foreground 标志，避免再次 daemonize）
    with open(lp, "a", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()),
             "--_foreground", "--workspace", str(ws)],
            stdout=log_file,
            stderr=log_file,
            stdin=subprocess.DEVNULL,
            start_new_session=True,   # 相当于 setsid，脱离终端
        )

    log.info(f"✅ 监听器已在后台启动 (PID={proc.pid})")
    log.info(f"   日志: tail -f {lp}")
    log.info(f"   停止: python3 feishu_listener.py --stop")


# ── CLI ───────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="飞书消息监听 Daemon")
    parser.add_argument("--workspace", "-w", default=None, help="工作区路径")
    parser.add_argument("--daemon",  "-d", action="store_true", help="后台守护进程运行")
    parser.add_argument("--stop",          action="store_true", help="停止守护进程")
    parser.add_argument("--status",        action="store_true", help="查看守护进程状态")
    parser.add_argument("--_foreground",   action="store_true", help=argparse.SUPPRESS)  # 内部用：子进程前台运行
    args = parser.parse_args()

    ws = find_workspace(args.workspace)
    pp = pid_path(ws)

    # ── --stop ────────────────────────────────────────────────────────────
    if args.stop:
        if not pp.exists():
            log.info("监听器未运行")
            return
        try:
            pid = int(pp.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            pp.unlink(missing_ok=True)
            log.info(f"✅ 已停止 (PID={pid})")
        except (ProcessLookupError, ValueError):
            log.warning("进程不存在（可能已崩溃），清理 PID 文件")
            pp.unlink(missing_ok=True)
        return

    # ── --status ──────────────────────────────────────────────────────────
    if args.status:
        if pp.exists():
            try:
                pid = int(pp.read_text().strip())
                os.kill(pid, 0)
                log.info(f"✅ 监听器运行中 (PID={pid})")
                return
            except (ProcessLookupError, ValueError):
                pp.unlink(missing_ok=True)
        log.info("❌ 监听器未运行")
        sys.exit(1)

    # ── 加载配置并验证 ────────────────────────────────────────────────────
    cfg = load_config(ws)
    if not cfg.get("app_id") or not cfg.get("app_secret"):
        log.error("配置不完整，请先运行：python3 feishu.py setup --app-id=xxx --app-secret=yyy")
        sys.exit(1)

    # ── --_foreground（由 daemonize 的子进程调用）─────────────────────────
    if args._foreground:
        pp = pid_path(ws)
        pp.write_text(str(os.getpid()), encoding="utf-8")
        try:
            run_listener(ws, cfg)
        finally:
            pp.unlink(missing_ok=True)
        return

    # ── --daemon ──────────────────────────────────────────────────────────
    if args.daemon:
        if sys.platform == "win32":
            log.error("Windows 不支持 --daemon 模式，请直接运行（前台）")
            sys.exit(1)
        daemonize(ws, cfg)
        return

    # ── 前台运行（默认）──────────────────────────────────────────────────
    run_listener(ws, cfg)


if __name__ == "__main__":
    main()
