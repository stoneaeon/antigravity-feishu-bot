---
name: feishu-bot
description: >
  飞书机器人双向集成。打开项目自动通知飞书，对话完成后推送结果，
  接收飞书消息作为下一轮对话输入。支持 P2P 私聊（无需建群）和群聊。
version: 2.1.0
author: Antigravity Plugin
tags: [feishu, notification, automation]
---

# 飞书机器人集成技能 v2.1

## 🗺️ 插件路径定位（每次执行命令前必须先做这一步）

**在执行任何 feishu 命令前，无论何种场景，必须先用以下方法确定 `FEISHU_PY` 的真实路径：**

```bash
# 方法1：从环境变量读取（最快，新机器配置好后优先用这个）
FEISHU_PY="$FEISHU_PLUGIN_PATH/feishu.py"

# 若环境变量未设置，执行方法2：自动搜索
if [ ! -f "$FEISHU_PY" ]; then
  FEISHU_PY=$(find "$HOME" -maxdepth 8 \
    -name "feishu.py" -path "*/feishu-bot/feishu.py" \
    2>/dev/null | head -1)
fi

# 若还找不到，报错提示
if [ ! -f "$FEISHU_PY" ]; then
  echo "❌ 找不到 feishu-bot 插件，请确认已克隆到本机"
  exit 1
fi

FEISHU_DIR=$(dirname "$FEISHU_PY")
echo "✅ 插件路径: $FEISHU_DIR"
```

**找到路径后，后续所有命令统一使用 `$FEISHU_PY` 和 `$FEISHU_DIR`。**

---

## ⚡ 魔法提示词（优先级最高）

> 当用户输入以下格式时，**立即执行**对应操作，无需确认。

### 🔧 安装插件（全局一次性）

**触发模式**：消息包含 `安装飞书插件` 或 `install feishu plugin`，且包含 `app-id` / `app_id` 和 `app-secret` / `app_secret`

**解析规则**：从消息中提取 App ID 和 App Secret（格式灵活：`app-id=xxx`、`App ID: xxx`、`app_id:xxx` 均可）

**执行步骤**（按顺序）：
```bash
# 第0步：定位插件（见上方「插件路径定位」）
FEISHU_PY=$(find "$HOME" -maxdepth 8 -name "feishu.py" -path "*/feishu-bot/feishu.py" 2>/dev/null | head -1)
FEISHU_DIR=$(dirname "$FEISHU_PY")

# 第1步：安装 Python 依赖
pip3 install requests lark-oapi

# 第2步：绑定项目（在当前工作区）
python3 "$FEISHU_PY" setup \
  --app-id={解析的app_id} \
  --app-secret={解析的app_secret}

# 第3步：写入环境变量，让以后每次都能直接找到插件
echo "export FEISHU_PLUGIN_PATH=\"$FEISHU_DIR\"" >> ~/.zshrc
echo "export FEISHU_PLUGIN_PATH=\"$FEISHU_DIR\"" >> ~/.bashrc
source ~/.zshrc 2>/dev/null || source ~/.bashrc 2>/dev/null

# 第4步：启动后台监听器
python3 "$FEISHU_DIR/feishu_listener.py" --daemon
```

**完成后告知用户**：
> ✅ 飞书插件安装完成！环境变量已写入 ~/.zshrc，以后每次启动都能自动找到插件。
> 最后一步：在飞书中搜索你的机器人，发送任意消息（如「你好」），双向通信将自动激活。

---

### 🔗 绑定到当前项目

**触发模式**：消息包含 `绑定飞书` 或 `bind feishu`，且包含 app-id 和 app-secret

**执行步骤**：
```bash
# 第0步：定位插件
FEISHU_PY="${FEISHU_PLUGIN_PATH}/feishu.py"
[ ! -f "$FEISHU_PY" ] && FEISHU_PY=$(find "$HOME" -maxdepth 8 -name "feishu.py" -path "*/feishu-bot/feishu.py" 2>/dev/null | head -1)
FEISHU_DIR=$(dirname "$FEISHU_PY")

# 第1步：在当前工作区写入配置
python3 "$FEISHU_PY" setup \
  --app-id={解析的app_id} \
  --app-secret={解析的app_secret} \
  --workspace .

# 第2步：启动监听器（接收飞书消息 → 写入队列，若未运行则启动）
python3 "$FEISHU_DIR/feishu_listener.py" --status 2>/dev/null || \
python3 "$FEISHU_DIR/feishu_listener.py" --daemon

# 第3步：启动 watcher（监控队列 → 自动激活 Antigravity，若未运行则启动）
python3 "$FEISHU_DIR/feishu_watcher.py" --status 2>/dev/null || \
python3 "$FEISHU_DIR/feishu_watcher.py" --daemon
```

**完成后告知用户**：
> ✅ 已绑定到本项目「{项目名}」！在飞书中向机器人发一条消息以激活通知。

---

## 📋 自动行为规则

### 规则 1：每次 Antigravity 启动时

```bash
# Step 1. 定位插件
FEISHU_PY="${FEISHU_PLUGIN_PATH}/feishu.py"
[ ! -f "$FEISHU_PY" ] && \
  FEISHU_PY=$(find "$HOME" -maxdepth 8 -name "feishu.py" \
              -path "*/feishu-bot/feishu.py" 2>/dev/null | head -1)

# 找不到插件 → 静默跳过，绝不报错
[ ! -f "$FEISHU_PY" ] && exit 0

FEISHU_DIR=$(dirname "$FEISHU_PY")

# Step 2. 读取当前工作区配置
STATUS=$(python3 "$FEISHU_PY" status --json 2>/dev/null)
CONFIGURED=$(echo "$STATUS" | python3 -c \
  "import sys,json; print(json.load(sys.stdin).get('configured','false'))" 2>/dev/null)

# Step 3. 未绑定飞书 → 完全跳过，不打扰用户
[ "$CONFIGURED" != "True" ] && exit 0

# Step 4. 已绑定 → 发送项目启动通知
python3 "$FEISHU_PY" send_open_message

# Step 5. 确保监听器在运行（若已在运行则跳过，若未运行则后台启动）
python3 "$FEISHU_DIR/feishu_listener.py" --status 2>/dev/null \
  || python3 "$FEISHU_DIR/feishu_listener.py" --daemon

# Step 6. 确保 watcher 在运行（监控队列 → 消息到达时自动激活 Antigravity）
python3 "$FEISHU_DIR/feishu_watcher.py" --status 2>/dev/null \
  || python3 "$FEISHU_DIR/feishu_watcher.py" --daemon
```

> 注意：若多个项目共用同一个飞书 App ID，同时只允许一个监听器运行（先 stop 旧项目的再 start 新项目的），否则飞书只向一个连接推送消息。

### 规则 2：每轮对话开始前

```bash
FEISHU_PY="${FEISHU_PLUGIN_PATH}/feishu.py"
[ ! -f "$FEISHU_PY" ] && exit 0
python3 "$FEISHU_PY" read_messages --json
```

若返回消息不为空 → 将飞书消息作为本轮**主要任务输入**处理。

### 规则 3：每次 Agent 完成响应后（**必须执行，不可遗漏**）

> ⚠️ **这是强制步骤**。你需要主动使用 terminal/run_command 工具执行下面的脚本，将结果推送到飞书。如果你不执行，用户在飞书里就看不到结果！

```bash
FEISHU_PY="${FEISHU_PLUGIN_PATH}/feishu.py"
[ ! -f "$FEISHU_PY" ] && exit 0

# 摘要：本次完成的核心工作（一句话，必填）
# 详情：可选，多行内容（代码变更/分析结论/下一步等）
python3 "$FEISHU_PY" send_result "一句话摘要" "可选详情" \
  --workspace "$(pwd)"
```

**必须推送的场景（有这些情况就必须发）：**
- 处理了飞书消息队列中的任何任务
- 完成了代码修改、文件创建/删除
- 完成了分析、调研、规划类任务并输出了结论
- 执行了用户通过飞书发来的任何指令

**可跳过的场景（只有这几种才能跳过）：**
- 用户在本轮对话中说了"不用通知"/"不用发飞书"
- 飞书插件未配置（`status --json` 返回 `configured: false`）
- 本轮是纯问答闲聊，无任何实际操作或输出


---

## 🔑 配置格式（仅供参考）

```json
{
  "app_id":              "cli_xxx",
  "app_secret":          "yyy",
  "project_name":        "自动从文件夹名获取",
  "target_id":           "（首次收到飞书消息后自动填充）",
  "target_type":         "p2p 或 group",
  "notify_on_open":      true,
  "notify_on_completion": true,
  "listen_incoming":     true,
  "use_card_format":     true
}
```

> ⚠️ 安全提示：`feishu_config.json` 含密钥，务必加入 `.gitignore`

---

## 注意事项

1. App Secret 等凭证不得出现在对话消息输出中
2. 插件未找到时静默跳过，不报错、不中断工作流
3. 推送失败时仅简短提示，不中断工作流
4. 同一条消息（by message_id）只处理一次（内置去重）

