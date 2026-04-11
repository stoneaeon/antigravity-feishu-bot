#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations  # 兼容 Python 3.9 的类型注解
"""
feishu.py — 飞书 App Bot 核心脚本
支持 P2P 私聊 和 群聊。首次收到飞书消息后自动记录用户 ID，无需手动配置。

用法:
  python3 feishu.py setup --app-id=cli_xxx --app-secret=yyy [--project=项目名]
  python3 feishu.py status [--json]
  python3 feishu.py test
  python3 feishu.py send_open_message
  python3 feishu.py send_result "摘要" ["详情"]
  python3 feishu.py send_text "任意消息"
  python3 feishu.py read_messages [--json]
  python3 feishu.py clear_messages
"""

import os
import sys
import json
import time
import datetime
import argparse
from pathlib import Path

# ── 检查 requests 依赖 ──────────────────────────────────────────────────────
try:
    import requests
except ImportError:
    print("[飞书] ❌ 缺少依赖，请先运行：pip3 install requests lark-oapi", file=sys.stderr)
    sys.exit(1)

# ── 飞书 API ─────────────────────────────────────────────────────────────────
BASE      = "https://open.feishu.cn/open-apis"
TOKEN_URL = f"{BASE}/auth/v3/tenant_access_token/internal"

# ── 工具 ─────────────────────────────────────────────────────────────────────
def now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _ok(m: str):   print(f"[飞书] ✅ {m}")
def _err(m: str):  print(f"[飞书] ❌ {m}", file=sys.stderr)
def _warn(m: str): print(f"[飞书] ⚠️  {m}")
def _info(m: str): print(f"[飞书] ℹ️  {m}")

# ── 路径管理 ──────────────────────────────────────────────────────────────────
def find_workspace(workspace: str = None) -> Path:
    """查找工作区根目录（.antigravity 所在目录），未指定则向上搜索"""
    if workspace:
        return Path(workspace).resolve()
    cwd = Path.cwd()
    for p in [cwd] + list(cwd.parents):
        if (p / ".antigravity").is_dir():
            return p
    return cwd  # 使用当前目录（首次 setup 时 .antigravity 还不存在）

def cfg_path(ws: Path) -> Path:
    return ws / ".antigravity" / "feishu_config.json"

def queue_path(ws: Path) -> Path:
    return ws / ".antigravity" / "feishu_messages.json"

def token_cache_path(ws: Path) -> Path:
    return ws / ".antigravity" / ".feishu_token_cache.json"

# ── 配置管理 ──────────────────────────────────────────────────────────────────
DEFAULTS: dict = {
    "enabled":              True,
    "app_id":               "",
    "app_secret":           "",
    "project_name":         "",
    "target_id":            "",   # 首次收到飞书消息后自动填充（open_id 或 chat_id）
    "target_type":          "",   # "p2p" 或 "group"
    "notify_on_open":       True,
    "notify_on_completion": True,
    "listen_incoming":      True,
    "use_card_format":      True,
}

def load_config(ws: Path) -> dict:
    p = cfg_path(ws)
    if p.exists():
        try:
            return {**DEFAULTS, **json.loads(p.read_text(encoding="utf-8"))}
        except (json.JSONDecodeError, OSError) as e:
            _warn(f"配置读取失败（使用默认值）: {e}")
    return dict(DEFAULTS)

def save_config(cfg: dict, ws: Path) -> None:
    p = cfg_path(ws)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

def is_configured(cfg: dict) -> bool:
    """是否已填写 app_id 和 app_secret"""
    return bool(cfg.get("app_id") and cfg.get("app_secret"))

def has_target(cfg: dict) -> bool:
    """是否已记录飞书消息接收目标（用户发过第一条消息后才有）"""
    return bool(cfg.get("target_id"))

def get_project_name(cfg: dict, ws: Path) -> str:
    return cfg.get("project_name") or ws.name

# ── Token 获取（带本地缓存，2 小时有效期，提前 5 分钟刷新）──────────────────
def get_token(cfg: dict, ws: Path, force: bool = False) -> str:
    """
    获取 tenant_access_token。
    返回 token 字符串，失败时返回空字符串（而非 None，避免类型问题）。
    """
    cache = token_cache_path(ws)

    # 尝试读缓存
    if not force and cache.exists():
        try:
            c = json.loads(cache.read_text(encoding="utf-8"))
            if time.time() < c.get("expire_at", 0) - 300:
                return c["token"]
        except (json.JSONDecodeError, KeyError, OSError):
            pass  # 缓存损坏，重新获取

    # 向飞书请求新 token
    try:
        resp = requests.post(
            TOKEN_URL,
            json={"app_id": cfg["app_id"], "app_secret": cfg["app_secret"]},
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        data = resp.json()
    except requests.RequestException as e:
        _err(f"网络请求失败: {e}")
        return ""

    if data.get("code") != 0:
        _err(f"Token 获取失败: {data.get('msg')} (code={data.get('code')})")
        _err("请检查 App ID / App Secret 是否正确，以及应用是否已发布")
        return ""

    token = data["tenant_access_token"]
    expire_at = time.time() + data.get("expire", 7200)

    # 缓存写入
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps({"token": token, "expire_at": expire_at}),
            encoding="utf-8",
        )
    except OSError:
        pass  # 缓存写入失败不影响功能

    return token

# ── API 请求工具 ───────────────────────────────────────────────────────────────
def _api_post(endpoint: str, token: str, body: dict) -> dict:
    """POST 请求飞书 API，返回响应 dict，失败返回空 dict"""
    try:
        resp = requests.post(
            f"{BASE}{endpoint}",
            json=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        return resp.json()
    except requests.RequestException as e:
        _err(f"API 请求失败: {e}")
        return {}

# ── 消息发送（统一入口，自动处理 p2p / group）────────────────────────────────
def _send(token: str, cfg: dict, msg_type: str, content_obj: dict) -> bool:
    """
    向配置的 target 发送消息。
    target_type == "p2p"   → receive_id_type=open_id
    target_type == "group" → receive_id_type=chat_id
    """
    target_id   = cfg.get("target_id", "")
    target_type = cfg.get("target_type", "p2p")

    if not target_id:
        _warn("尚未激活双向通信。请先在飞书中向机器人发送任意消息（如「你好」）。")
        return False

    rid_type = "open_id" if target_type == "p2p" else "chat_id"
    result = _api_post(
        f"/im/v1/messages?receive_id_type={rid_type}",
        token,
        {
            "receive_id": target_id,
            "msg_type":   msg_type,
            "content":    json.dumps(content_obj, ensure_ascii=False),
        },
    )

    if result.get("code") == 0:
        return True
    _err(f"消息发送失败: {result.get('msg')} (code={result.get('code')})")
    return False

def send_text(token: str, cfg: dict, text: str) -> bool:
    return _send(token, cfg, "text", {"text": text})

def send_card(token: str, cfg: dict, title: str, body: str,
              color: str = "blue") -> bool:
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title":    {"tag": "plain_text", "content": title},
            "template": color,
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": body}}
        ],
    }
    return _send(token, cfg, "interactive", card)

def send_reaction(token: str, message_id: str, emoji: str = "OK") -> bool:
    """给指定消息添加表情回复（Reaction），如 OK, DONE, THUMBSUP 等"""
    result = _api_post(
        f"/im/v1/messages/{message_id}/reactions",
        token,
        {"reaction_type": {"emoji_type": emoji}},
    )
    if result.get("code") == 0:
        return True
    _err(f"添加 Reaction 失败: {result.get('msg')} (code={result.get('code')})")
    return False

# ── 功能 1：打开项目通知 ─────────────────────────────────────────────────────
def send_open_message(cfg: dict = None, ws: Path = None) -> bool:
    ws  = ws or find_workspace()
    cfg = cfg or load_config(ws)
    if not cfg.get("enabled") or not cfg.get("notify_on_open"):
        return True
    if not is_configured(cfg):
        _warn("项目未绑定飞书，跳过通知")
        return False
    token = get_token(cfg, ws)
    if not token:
        return False
    pn = get_project_name(cfg, ws)
    if cfg.get("use_card_format"):
        return send_card(
            token, cfg,
            title=f"🚀 {pn} · 准备就绪",
            body=(
                f"**项目**：{pn}\n"
                f"**状态**：✅ Antigravity 已就绪，可以开始工作\n"
                f"**时间**：{now()}\n\n"
                "---\n"
                "> 💡 直接给我发消息，将作为 AI 下一轮对话输入"
            ),
            color="green",
        )
    return send_text(token, cfg, f"🚀 {pn} 已就绪，时间：{now()}")

# ── 功能 3：对话完成推送 ─────────────────────────────────────────────────────
def send_result(summary: str, cfg: dict = None, ws: Path = None,
                details: str = None, files: list = None) -> bool:
    ws  = ws or find_workspace()
    cfg = cfg or load_config(ws)
    if not cfg.get("enabled") or not cfg.get("notify_on_completion"):
        return True
    if not is_configured(cfg):
        _warn("项目未绑定飞书，跳过推送")
        return False
    token = get_token(cfg, ws)
    if not token:
        return False
    pn = get_project_name(cfg, ws)
    if cfg.get("use_card_format"):
        parts = [f"**📋 摘要**\n{summary}"]
        if details:
            parts.append(f"\n**📝 详情**\n{details}")
        if files:
            file_lines = "\n".join(f"• `{f}`" for f in files[:8])
            parts.append(f"\n**📁 文件**\n{file_lines}")
        parts.append(f"\n---\n**⏰ 时间**：{now()}")
        ok = send_card(token, cfg, f"✅ {pn} · 任务完成",
                       "\n".join(parts), color="blue")
    else:
        ok = send_text(token, cfg, f"✅ [{pn}] {summary}\n时间：{now()}")
    if ok:
        _ok("结果已推送到飞书")
    return ok

# ── 功能 4：读取消息队列 ─────────────────────────────────────────────────────
def read_messages(ws: Path = None, clear: bool = True) -> list:
    """
    读取由 feishu_listener.py 写入的消息队列。
    clear=True 时读取后自动清空队列。
    """
    ws = ws or find_workspace()
    qp = queue_path(ws)
    if not qp.exists():
        return []
    try:
        data = json.loads(qp.read_text(encoding="utf-8"))
        msgs = data.get("messages", [])
        if clear and msgs:
            qp.write_text(
                json.dumps({"messages": [], "last_read": now()},
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return msgs
    except (json.JSONDecodeError, OSError) as e:
        _err(f"读取消息队列失败: {e}")
        return []

def clear_messages(ws: Path = None) -> None:
    ws = ws or find_workspace()
    qp = queue_path(ws)
    qp.parent.mkdir(parents=True, exist_ok=True)
    qp.write_text(
        json.dumps({"messages": [], "cleared": now()},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _ok("消息队列已清空")

# ── 一键绑定（magic prompt 由 SKILL.md 触发）─────────────────────────────────
def setup(app_id: str, app_secret: str,
          project: str = None, ws: Path = None) -> bool:
    """
    绑定飞书机器人到当前项目。
    SKILL.md 检测到「绑定飞书」魔法词后调用此函数。
    """
    ws = ws or find_workspace()
    pn = project or ws.name

    print(f"\n[飞书] 🔧 正在绑定项目：{pn}")
    print(f"[飞书]   App ID: {app_id}")

    cfg = {
        **DEFAULTS,
        "app_id":       app_id,
        "app_secret":   app_secret,
        "project_name": pn,
    }

    # 验证凭证（先获取 token，确认 app_id/secret 有效）
    token = get_token(cfg, ws, force=True)
    if not token:
        _err("绑定失败：凭证无效，请检查 App ID 和 App Secret")
        return False

    save_config(cfg, ws)
    _ok(f"绑定成功！配置已写入 {cfg_path(ws)}")

    print()
    print("  ┌──────────────────────────────────────────────────────┐")
    print("  │  🎯 最后一步：激活双向通信                            │")
    print("  │                                                      │")
    print("  │  在飞书中搜索你的机器人，发送任意消息（如「你好」）    │")
    print("  │  机器人会自动回复确认，双向通信立即激活 ✅             │")
    print("  └──────────────────────────────────────────────────────┘")
    return True

# ── 获取机器人所在群列表（辅助工具）─────────────────────────────────────────
def get_chats(cfg: dict, ws: Path) -> list:
    token = get_token(cfg, ws)
    if not token:
        return []
    try:
        resp = requests.get(
            f"{BASE}/im/v1/chats",
            headers={"Authorization": f"Bearer {token}"},
            params={"page_size": 50},
            timeout=10,
        )
        data = resp.json()
        if data.get("code") == 0:
            return data.get("data", {}).get("items", [])
        _err(f"获取群列表失败: {data.get('msg')}")
    except requests.RequestException as e:
        _err(f"网络请求失败: {e}")
    return []

# ── 测试连接 ──────────────────────────────────────────────────────────────────
def test(cfg: dict = None, ws: Path = None) -> bool:
    ws  = ws or find_workspace()
    cfg = cfg or load_config(ws)

    if not is_configured(cfg):
        _err("未绑定项目，请先运行：python3 feishu.py setup --app-id=xxx --app-secret=yyy")
        return False

    print("[飞书] 🧪 Step 1: 验证凭证...")
    token = get_token(cfg, ws, force=True)
    if not token:
        return False
    _ok("凭证有效")

    if not has_target(cfg):
        _warn("尚未激活双向通信（请先在飞书向机器人发消息）")
        _info("凭证验证通过，绑定配置正确")
        return True

    print("[飞书] 🧪 Step 2: 发送测试消息...")
    pn = get_project_name(cfg, ws)
    ok = send_card(
        token, cfg,
        title="🧪 连接测试",
        body=(
            f"**项目**：{pn}\n"
            f"**状态**：✅ 飞书双向通信正常\n"
            f"**时间**：{now()}"
        ),
        color="turquoise",
    )
    if ok:
        _ok("测试消息发送成功！请查看飞书")
    return ok

# ── CLI 入口 ──────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="飞书 App Bot 集成插件",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "cmd", nargs="?", default="status",
        choices=["setup", "status", "test", "send_open_message",
                 "send_result", "send_text", "send_reaction", "read_messages",
                 "clear_messages", "get_chats"],
    )
    parser.add_argument("args", nargs="*", help="命令附加参数")
    parser.add_argument("--app-id",    dest="app_id",    default="")
    parser.add_argument("--app-secret",dest="app_secret",default="")
    parser.add_argument("--project",   default="")
    parser.add_argument("--workspace", "-w", default=None)
    parser.add_argument("--json",      action="store_true", help="以 JSON 格式输出")
    args = parser.parse_args()

    ws  = find_workspace(args.workspace)
    cfg = load_config(ws)

    # ── setup ──────────────────────────────────────────────────────────────
    if args.cmd == "setup":
        aid = args.app_id or (args.args[0] if args.args else "")
        sec = args.app_secret or (args.args[1] if len(args.args) > 1 else "")
        if not aid or not sec:
            _err("用法：python3 feishu.py setup --app-id=xxx --app-secret=yyy")
            sys.exit(1)
        ok = setup(aid, sec, args.project or None, ws)
        sys.exit(0 if ok else 1)

    # ── status ─────────────────────────────────────────────────────────────
    elif args.cmd == "status":
        pn   = get_project_name(cfg, ws)
        msgs = read_messages(ws, clear=False)
        if args.json:
            print(json.dumps({
                "configured":       is_configured(cfg),
                "has_target":       has_target(cfg),
                "target_type":      cfg.get("target_type", ""),
                "project_name":     pn,
                "notify_on_open":   cfg.get("notify_on_open"),
                "notify_on_completion": cfg.get("notify_on_completion"),
                "listen_incoming":  cfg.get("listen_incoming"),
                "pending_messages": len(msgs),
                "messages":         msgs,
            }, ensure_ascii=False, indent=2))
        else:
            print(f"\n  项目名称  : {pn}")
            print(f"  凭证状态  : {'✅ 已配置' if is_configured(cfg) else '❌ 未配置（运行 setup）'}")
            print(f"  双向通信  : {'✅ 已激活 (' + cfg.get('target_type','') + ')' if has_target(cfg) else '⏳ 等待首次飞书消息激活'}")
            print(f"  打开通知  : {'✅' if cfg.get('notify_on_open') else '❌'}")
            print(f"  完成推送  : {'✅' if cfg.get('notify_on_completion') else '❌'}")
            if msgs:
                print(f"  待处理消息: {len(msgs)} 条")

    # ── test ───────────────────────────────────────────────────────────────
    elif args.cmd == "test":
        sys.exit(0 if test(cfg, ws) else 1)

    # ── send_open_message ──────────────────────────────────────────────────
    elif args.cmd == "send_open_message":
        sys.exit(0 if send_open_message(cfg, ws) else 1)

    # ── send_result ────────────────────────────────────────────────────────
    elif args.cmd == "send_result":
        summary = args.args[0] if args.args else ""
        if not summary:
            _err("用法：python3 feishu.py send_result '摘要' ['详情']")
            sys.exit(1)
        details = args.args[1] if len(args.args) > 1 else None
        sys.exit(0 if send_result(summary, cfg, ws, details=details) else 1)

    # ── send_text ──────────────────────────────────────────────────────────
    elif args.cmd == "send_text":
        if not args.args:
            _err("用法：python3 feishu.py send_text '消息内容'")
            sys.exit(1)
        token = get_token(cfg, ws)
        if not token:
            sys.exit(1)
        sys.exit(0 if send_text(token, cfg, " ".join(args.args)) else 1)

    # ── send_reaction ──────────────────────────────────────────────────────
    elif args.cmd == "send_reaction":
        if not args.args:
            _err("用法：python3 feishu.py send_reaction <message_id> [emoji]")
            sys.exit(1)
        token = get_token(cfg, ws)
        if not token:
            sys.exit(1)
        msg_id = args.args[0]
        emoji = args.args[1] if len(args.args) > 1 else "OK"
        sys.exit(0 if send_reaction(token, msg_id, emoji) else 1)

    # ── read_messages ──────────────────────────────────────────────────────
    elif args.cmd == "read_messages":
        msgs = read_messages(ws, clear=True)
        if args.json:
            print(json.dumps(msgs, ensure_ascii=False, indent=2))
        else:
            if msgs:
                print(f"\n📨 {len(msgs)} 条待处理消息：\n")
                for m in msgs:
                    print(f"  [{m.get('time','')}] {m.get('text','')}")
            else:
                _info("暂无待处理消息")

    # ── clear_messages ─────────────────────────────────────────────────────
    elif args.cmd == "clear_messages":
        clear_messages(ws)

    # ── get_chats ──────────────────────────────────────────────────────────
    elif args.cmd == "get_chats":
        chats = get_chats(cfg, ws)
        if args.json:
            print(json.dumps(chats, ensure_ascii=False, indent=2))
        else:
            if chats:
                print(f"\n{'#':<3} {'群名称':<30} chat_id")
                print("-" * 65)
                for i, c in enumerate(chats, 1):
                    mark = " ← 当前配置" if c.get("chat_id") == cfg.get("target_id") else ""
                    print(f"  {i:<2} {c.get('name','?')[:28]:<30} {c.get('chat_id','')}{mark}")
            else:
                _warn("未找到群。请确认机器人已被邀请进群。")


if __name__ == "__main__":
    main()
