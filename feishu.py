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
import fcntl
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

def _api_upload(endpoint: str, token: str, file_path: str,
                form_fields: dict, file_field_name: str = "file") -> dict:
    """multipart/form-data 上传文件到飞书，返回响应 dict

    file_field_name: 表单中文件字段的名称（图片用 'image'，文件用 'file'）
    """
    p = Path(file_path)
    if not p.exists():
        _err(f"文件不存在: {file_path}")
        return {}
    try:
        with open(p, "rb") as f:
            # form_fields: 额外的表单字段（如 image_type, file_type, file_name）
            files = {file_field_name: (p.name, f)}
            resp = requests.post(
                f"{BASE}{endpoint}",
                data=form_fields,
                files=files,
                headers={"Authorization": f"Bearer {token}"},
                timeout=60,  # 大文件上传需要更长超时
            )
        return resp.json()
    except requests.RequestException as e:
        _err(f"上传失败: {e}")
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
    # 飞书 text 消息限制约 4000 字符，超过时截断
    if len(text) > 4000:
        text = text[:3950] + "\n\n...（内容过长，已截断）"
    return _send(token, cfg, "text", {"text": text})

def send_card(token: str, cfg: dict, title: str, body: str,
              color: str = "blue") -> bool:
    # 飞书卡片消息 content 限制 30KB，body 截断到 28KB 留余量
    if len(body.encode("utf-8")) > 28000:
        # 按字符截断（中文约 3 字节/字符），留出安全边际
        max_chars = 9000
        body = body[:max_chars] + "\n\n...（内容过长，已截断）"
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

def send_image(token: str, cfg: dict, image_path: str) -> bool:
    """上传图片并发送到飞书。支持 JPG/PNG/WEBP/GIF/BMP 等，≤10MB。"""
    p = Path(image_path)
    if not p.exists():
        _err(f"图片不存在: {image_path}")
        return False
    size_mb = p.stat().st_size / (1024 * 1024)
    if size_mb > 10:
        _err(f"图片超过 10MB 限制 ({size_mb:.1f}MB): {p.name}")
        return False

    # Step 1: 上传图片获取 image_key
    _info(f"正在上传图片: {p.name} ({size_mb:.1f}MB)")
    result = _api_upload(
        "/im/v1/images", token, str(p),
        {"image_type": "message"},
        file_field_name="image",
    )
    image_key = result.get("data", {}).get("image_key", "")
    if not image_key:
        _err(f"图片上传失败: {result.get('msg', '未知错误')} (code={result.get('code')})")
        return False

    # Step 2: 发送图片消息
    ok = _send(token, cfg, "image", {"image_key": image_key})
    if ok:
        _ok(f"图片已发送: {p.name}")
    return ok

def send_file(token: str, cfg: dict, file_path: str,
              file_type: str = "") -> bool:
    """上传文件并发送到飞书。≤30MB。

    file_type 可选值: opus/mp4/pdf/doc/xls/ppt/stream
    若不指定，根据后缀自动推断。
    """
    p = Path(file_path)
    if not p.exists():
        _err(f"文件不存在: {file_path}")
        return False
    size_mb = p.stat().st_size / (1024 * 1024)
    if size_mb > 30:
        _err(f"文件超过 30MB 限制 ({size_mb:.1f}MB): {p.name}")
        return False

    # 自动推断 file_type
    if not file_type:
        ext = p.suffix.lower().lstrip(".")
        type_map = {
            "pdf": "pdf", "doc": "doc", "docx": "doc",
            "xls": "xls", "xlsx": "xls",
            "ppt": "ppt", "pptx": "ppt",
            "mp4": "mp4", "opus": "opus",
        }
        file_type = type_map.get(ext, "stream")

    # Step 1: 上传文件获取 file_key
    _info(f"正在上传文件: {p.name} ({size_mb:.1f}MB, type={file_type})")
    result = _api_upload(
        "/im/v1/files", token, str(p),
        {"file_type": file_type, "file_name": p.name},
    )
    file_key = result.get("data", {}).get("file_key", "")
    if not file_key:
        _err(f"文件上传失败: {result.get('msg', '未知错误')} (code={result.get('code')})")
        return False

    # Step 2: 发送文件消息
    ok = _send(token, cfg, "file", {"file_key": file_key})
    if ok:
        _ok(f"文件已发送: {p.name}")
    return ok

def download_resource(token: str, message_id: str, file_key: str,
                      output_dir: str = "/tmp",
                      filename: str = "") -> str:
    """从飞书消息中下载资源文件（图片/文件）。

    使用 im/v1/messages/:message_id/resources/:file_key 接口。
    返回保存的文件路径，失败返回空字符串。
    """
    # 确定文件类型
    url = f"{BASE}/im/v1/messages/{message_id}/resources/{file_key}"
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params={"type": "image"},  # image 或 file
            timeout=30,
            stream=True,
        )
        if resp.status_code != 200:
            _err(f"资源下载失败: HTTP {resp.status_code}")
            return ""

        # 从 Content-Disposition 提取文件名（如有）
        if not filename:
            cd = resp.headers.get("Content-Disposition", "")
            if "filename=" in cd:
                filename = cd.split("filename=")[-1].strip('"').strip("'")
            else:
                # 根据 Content-Type 推断扩展名
                ct = resp.headers.get("Content-Type", "")
                ext_map = {
                    "image/png": ".png", "image/jpeg": ".jpg",
                    "image/gif": ".gif", "image/webp": ".webp",
                    "application/pdf": ".pdf",
                }
                ext = ext_map.get(ct, ".bin")
                # 使用完整的 file_key 防止相同前缀导致的覆盖
                sanitized_key = file_key.replace("/", "_").replace("\\", "_")
                filename = f"feishu_{sanitized_key}{ext}"

        out_path = Path(output_dir) / filename
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        _ok(f"资源已下载: {out_path}")
        return str(out_path)

    except requests.RequestException as e:
        _err(f"资源下载失败: {e}")
        return ""

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
def mark_processing(ws: Path = None, processing: bool = True) -> None:
    """
    在队列文件中设置/清除 processing 锁。
    Agent 读取消息后应立即调用 mark_processing(processing=True)，
    处理完成后调用 clear_messages()（自动清除锁）。
    watcher 会检查此锁，避免在 Agent 处理期间重复触发。
    """
    ws = ws or find_workspace()
    qp = queue_path(ws)
    if not qp.exists():
        return
    try:
        data = json.loads(qp.read_text(encoding="utf-8"))
        if processing:
            data["processing"] = True
            data["processing_since"] = now()
        else:
            data.pop("processing", None)
            data.pop("processing_since", None)
        qp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except (json.JSONDecodeError, OSError):
        pass


def read_messages(ws: Path = None, clear: bool = True) -> list:
    """
    读取由 feishu_listener.py 写入的消息队列。
    clear=True 时，将待处理消息追加到 processing_messages 列表（不覆盖旧数据）。
    这样用户可以明确看到正在处理的内容。
    """
    ws = ws or find_workspace()
    qp = queue_path(ws)
    if not qp.exists():
        return []
    try:
        captured_msgs = []

        def _do_read(data):
            msgs = data.get("messages", [])
            captured_msgs.extend(msgs)
            if clear and msgs:
                # 追加到 processing_messages（而非覆盖，防止上轮崩溃残留消息被丢弃）
                existing_proc = data.get("processing_messages", [])
                data["processing_messages"] = existing_proc + msgs
                data["messages"] = []
                data["last_read"] = now()
                data["processing"] = True
                data["agent_read_at"] = now()  # Agent 心跳标记，供 watcher 确认 Agent 已启动
                if "processing_since" not in data:
                    data["processing_since"] = now()
            elif msgs:
                # 不清空时，标记 processing 防止 watcher 重复触发
                data["processing"] = True
                data["agent_read_at"] = now()
                if "processing_since" not in data:
                    data["processing_since"] = now()

        _locked_queue_rw(qp, writer=_do_read if clear else None)
        if not clear:
            # 只读模式：从文件中读取但不修改
            data = _locked_queue_rw(qp)
            return data.get("messages", [])
        return captured_msgs
    except Exception as e:
        _err(f"读取消息队列失败: {e}")
        return []

def clear_messages(ws: Path = None) -> int:
    """清除已处理的消息和 processing 锁，返回仍待处理的新消息数量。

    如果在 Agent 处理期间有新消息到达（listener 写入 messages[]），
    这些新消息会被保留，不会被清除。返回值 > 0 表示还有新任务需要处理。
    """
    ws = ws or find_workspace()
    qp = queue_path(ws)
    qp.parent.mkdir(parents=True, exist_ok=True)
    result = {"remaining": 0}
    try:
        def _do_clear(data):
            cleared_count = len(data.get("processing_messages", []))
            data["processing_messages"] = []
            data.pop("processing", None)
            data.pop("processing_since", None)
            data.pop("agent_read_at", None)
            data["cleared"] = now()
            result["remaining"] = len(data.get("messages", []))
            if cleared_count > 0:
                _info(f"已清除 {cleared_count} 条已处理消息")

        _locked_queue_rw(qp, writer=_do_clear)

        if result["remaining"] > 0:
            _warn(f"还有 {result['remaining']} 条新消息在队列中等待处理（watcher 将自动触发下一轮）")
        else:
            _ok("消息队列及处理锁已清空")
        return result["remaining"]
    except Exception as e:
        _err(f"清空消息队列时出错：{e}")
        return 0

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

    # 自动生成 .vscode/tasks.json 以支持 IDE 打开时自启动守护进程
    try:
        tasks_dir = ws / ".vscode"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        tasks_file = tasks_dir / "tasks.json"
        
        task_obj = {
            "label": "Antigravity Feishu Auto-Start",
            "type": "shell",
            "command": "FEISHU_PY=\"${FEISHU_PLUGIN_PATH:-$HOME/Antigravity/AFPlugin/feishu-bot}/feishu.py\"; if [ -d \".antigravity\" ] && [ -f \".antigravity/feishu_config.json\" ] && [ -f \"$FEISHU_PY\" ]; then FEISHU_DIR=$(dirname \"$FEISHU_PY\"); python3 \"$FEISHU_PY\" send_open_message; python3 \"$FEISHU_DIR/feishu_listener.py\" --status >/dev/null 2>&1 || python3 \"$FEISHU_DIR/feishu_listener.py\" --daemon; python3 \"$FEISHU_DIR/feishu_watcher.py\" --status >/dev/null 2>&1 || python3 \"$FEISHU_DIR/feishu_watcher.py\" --daemon; fi",
            "runOptions": {"runOn": "folderOpen"},
            "presentation": {"reveal": "never", "panel": "shared", "clear": True, "close": True}
        }

        tasks_data = {"version": "2.0.0", "tasks": []}
        if tasks_file.exists():
            try:
                tasks_data = json.loads(tasks_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        
        existing_task = False
        for t in tasks_data.setdefault("tasks", []):
            if type(t) is dict and t.get("label") == "Antigravity Feishu Auto-Start":
                t.update(task_obj)
                existing_task = True
                break
                
        if not existing_task:
            tasks_data["tasks"].append(task_obj)
            
        tasks_file.write_text(json.dumps(tasks_data, ensure_ascii=False, indent=2), encoding="utf-8")
        _info("已生成 .vscode/tasks.json，下次打开项目时飞书守护进程将自动启动")
    except Exception as e:
        _warn(f"生成自启动任务配置失败: {e}")

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
                 "send_result", "send_text", "send_image", "send_file",
                 "send_reaction", "read_messages", "download_resource",
                 "clear_messages", "mark_processing", "get_chats"],
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
            
            try:
                qp_data = json.loads(queue_path(ws).read_text(encoding="utf-8")) if queue_path(ws).exists() else {}
                p_msgs = qp_data.get("processing_messages", [])
                if msgs:
                    print(f"  待处理消息: {len(msgs)} 条")
                if p_msgs:
                    print(f"  处理中消息: {len(p_msgs)} 条 (正在执行)")
                    for m in p_msgs:
                        print(f"    - {m.get('text', '')[:40]}...")
            except:
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

    # ── send_image ─────────────────────────────────────────────────────────
    elif args.cmd == "send_image":
        if not args.args:
            _err("用法：python3 feishu.py send_image <图片路径>")
            sys.exit(1)
        token = get_token(cfg, ws)
        if not token:
            sys.exit(1)
        sys.exit(0 if send_image(token, cfg, args.args[0]) else 1)

    # ── send_file ──────────────────────────────────────────────────────────
    elif args.cmd == "send_file":
        if not args.args:
            _err("用法：python3 feishu.py send_file <文件路径> [文件类型]")
            sys.exit(1)
        token = get_token(cfg, ws)
        if not token:
            sys.exit(1)
        ft = args.args[1] if len(args.args) > 1 else ""
        sys.exit(0 if send_file(token, cfg, args.args[0], file_type=ft) else 1)

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

    # ── download_resource ──────────────────────────────────────────────────
    elif args.cmd == "download_resource":
        if len(args.args) < 2:
            _err("用法：python3 feishu.py download_resource <message_id> <file_key> [output_dir] [filename]")
            sys.exit(1)
        token = get_token(cfg, ws)
        if not token:
            sys.exit(1)
        msg_id   = args.args[0]
        file_key = args.args[1]
        out_dir  = args.args[2] if len(args.args) > 2 else str(ws / ".antigravity" / "media")
        filename = args.args[3] if len(args.args) > 3 else ""

        saved_path = download_resource(token, msg_id, file_key, out_dir, filename)
        if saved_path:
            # 打印到 stdout，Agent 可以捕获以获取实际路径
            print(f"DOWNLOADED:{saved_path}")
            sys.exit(0)
        else:
            sys.exit(1)

    # ── clear_messages ─────────────────────────────────────────────────────
    elif args.cmd == "clear_messages":
        clear_messages(ws)

    # ── mark_processing ────────────────────────────────────────────────────
    elif args.cmd == "mark_processing":
        flag = args.args[0].lower() if args.args else "true"
        mark_processing(ws, processing=(flag in ("true", "1", "on")))
        _ok(f"processing 锁已{'设置' if flag in ('true', '1', 'on') else '清除'}")

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
