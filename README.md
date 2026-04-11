# feishu-bot · Antigravity 飞书集成插件

> 将 Antigravity AI 与飞书双向打通。项目启动自动通知、对话完成自动推送、飞书消息自动触发 AI 工作——包括 Mac 息屏时自动唤醒屏幕执行任务。

**平台要求**：macOS（watcher 的自动唤醒功能依赖 AppleScript）  
**Python 要求**：3.9 或更高

---

## ⚡️ 极简五步极速启动

没时间看长篇大论？没问题，只需 5 步直接起飞：

1. **一键拉取依赖**：把本项目的 GitHub 链接丢给左侧的 Antigravity 聊天框，对它说：“*帮我安装这个飞书插件和它的环境依赖*”。它会自动拉代码并配好环境。
2. **三秒创建机器**：点击 [飞书一键建机直通车](https://open.feishu.cn/page/openclaw?form=multiAgent) 飞速建好应用，并把 App ID 与 App Secret 复制出来，点亮“长连接”权限。
3. **重启重力引擎**：彻底退出 Antigravity 编辑器并再次打开，让底层的魔法句柄重新注入。
4. **念出绑定咒语**：在你想连通飞书的工作项目里，对 Antigravity 甩出这句话：`绑定飞书 app-id=XXX app-secret=XXX`。
5. **激活双向链接**：照着它的提示，去飞书手机/电脑端里搜到你的机器人，发一句“你好”，全自动工作流就此双向打通！

> *如果你遇到任何奇奇怪怪的卡壳，直接把报错甩回给 Antigravity 让它给你修。想了解硬核细节，请看下方长文：*

---

## ⚠️ 使用须知（请先阅读）

> **1. 一个项目对应一个飞书机器人**
>
> 推荐为每个项目创建一个独立的飞书应用（App ID），这样消息上下文干净清晰、互不干扰。虽然技术上可以多个项目共用一个 App ID，但同一时间只有一个监听器能收到消息（后启动的会顶掉先启动的），容易造成混乱。

> **2. 同时只能开一个 Antigravity 窗口**
>
> watcher 通过 AppleScript 激活 Antigravity 窗口并输入触发文本。如果同时打开了多个 Antigravity 窗口（即多个项目），触发的消息可能发送到错误的窗口，导致任务"串台"。处理飞书任务时，请确保只运行一个 Antigravity 实例。

---

## 目录

- [工作原理](#工作原理)
- [飞书开放平台配置](#飞书开放平台配置one-time-与机器无关)
- [插件安装](#插件安装one-time-每台机器执行一次)
- [项目绑定](#项目绑定每个项目执行一次)
- [自动触发设置](#自动触发设置可选但推荐)
- [功能说明](#功能说明)
- [手动命令参考](#手动命令参考)
- [文件结构](#文件结构)
- [常见问题](#常见问题)
- [安全说明](#安全说明)

---

## 工作原理

```
飞书消息
    ↓
feishu_listener.py      ← WebSocket 长连接（无需公网地址）
（接收消息 → 写入队列）
    ↓
feishu_messages.json    ← 本地队列文件（项目内）
    ↓
feishu_watcher.py       ← 后台轮询（2 秒一次）
（检测到新消息 → 激活 Antigravity → 触发 Agent）
    ↓
Antigravity Agent       ← 读 SKILL.md 规则 → 读队列 → 处理任务 → 推送结果
    ↓
feishu.py send_result   ← 结果发回飞书
```

---

## 飞书开放平台配置（one-time，与机器无关）

在飞书开放平台完成以下步骤。**此配置只需做一次，与使用哪台电脑无关。**

> 💡 **快速创建入口（强烈推荐）**：你可以直接点击此链接快速创建飞书机器人，省去大部分繁琐配置：
> [https://open.feishu.cn/page/openclaw?form=multiAgent](https://open.feishu.cn/page/openclaw?form=multiAgent)
> 创建好应用后，只需去后台复制出 `App ID` 和 `App Secret`，然后确认开启「长连接」事件即可。

如果你选择手动从零开始配置：

### 第 1 步：创建应用

1. 点击「创建企业自建应用」，填写名称（如 `Antigravity 助手`）
2. 进入应用详情页 → **功能 → 机器人 → 启用**

### 第 2 步：配置权限

进入 **权限管理** → 添加以下权限：

| 权限标识 | 说明 |
|---------|------|
| `im:message` | 接收和发送私聊、群聊消息 |
| `im:chat:readonly` | 读取群信息（加群时需要）|

### 第 3 步：配置事件订阅（⚠️ 关键步骤，缺少此步监听器收不到任何消息）

进入 **事件订阅** → 按以下配置：

1. **接收事件方式** → 选择「**使用长连接接收事件（WebSocket）**」  
   *(不需要填写服务器 URL，无需公网地址)*

2. 点击「**添加事件**」→ 搜索并添加：
   - `im.message.receive_v1`（接收消息）

### 第 4 步：发布应用

**版本管理与发布 → 创建版本 → 申请发布**

> 如果是个人测试，可先使用「测试版本」（选择测试用户），无需等待审批。

### 第 5 步：保存凭证

**凭证与基础信息** → 复制并妥善保存：
- `App ID`（格式：`cli_xxxxxxxxxxxxxxxxx`）
- `App Secret`（32 位字符串）

> ⚠️ 不要将 App Secret 提交到 Git 或贴在任何公开位置

---

## 插件安装（one-time，每台机器执行一次）

### 第 1 步：克隆到 Antigravity 插件目录

```bash
git clone https://github.com/stoneaeon/antigravity-feishu-bot.git \
  ~/Antigravity/AFPlugin/feishu-bot
```

**为什么必须克隆到这个目录？**  
Antigravity 扫描 `~/Antigravity/AFPlugin/` 下的子目录，加载 `SKILL.md` 技能文件。克隆到这里后，Antigravity 重启时会识别本插件定义的魔法指令和自动行为规则。如果你的 Antigravity 在其他路径，请替换为实际的 AFPlugin 目录。

验证：
```bash
ls ~/Antigravity/AFPlugin/feishu-bot/
# 应看到：feishu.py  feishu_listener.py  feishu_watcher.py  SKILL.md  requirements.txt  README.md
```

### 第 2 步：安装 Python 依赖

```bash
pip3 install requests lark-oapi
```

验证：
```bash
python3 -c "import requests, lark_oapi; print('✅ 依赖安装成功')"
```

### 第 3 步：设置环境变量

```bash
echo 'export FEISHU_PLUGIN_PATH="$HOME/Antigravity/AFPlugin/feishu-bot"' >> ~/.zshrc
source ~/.zshrc
```

验证：
```bash
echo $FEISHU_PLUGIN_PATH
# 应输出包含你的实际完整路径，如：/Users/xxx/Antigravity/AFPlugin/feishu-bot
```

### 第 4 步：重启 Antigravity

完全退出并重新打开 Antigravity。

> ⚠️ **这是关键步骤**：SKILL.md 必须被 Antigravity 加载后，后续的魔法指令才能生效。克隆插件后必须重启一次。

---

安装完成。接下来为每个项目执行一次绑定。

> 💡 **专家提示**：既然你已经在使用 Antigravity，如果安装或后续使用中碰到任何问题、报错，请**直接把问题抛给 Antigravity**！作为本项目的原生定制环境，Agent 可以瞬间帮你分析日志和排查故障，完全不用自己动手！

---

## 项目绑定（每个项目执行一次）

在 Antigravity 中打开你的项目，在对话框输入：

```
绑定飞书 app-id=cli_你的AppID app-secret=你的AppSecret
```

Agent 会自动执行：
1. 验证 App ID / App Secret 是否有效
2. 在项目目录创建 `.antigravity/feishu_config.json`
3. 后台启动消息监听器（`feishu_listener.py`）
4. 后台启动自动触发器（`feishu_watcher.py`）

绑定成功后，**在飞书中搜索你的机器人，发送任意消息（如「你好」）**。  
机器人会自动回复确认，双向通信激活。此后：
- 你在飞书发消息 → 监听器接收 → 写入队列 → watcher 检测到 → 自动激活 Antigravity → Agent 处理任务 → 结果推回飞书

---

## 自动触发设置（可选但推荐）

`feishu_watcher.py` 在检测到飞书新消息后，通过 macOS AppleScript 自动激活 Antigravity 并触发任务处理。这需要两个额外设置：

### 授予辅助功能权限（一次性）

第一次运行 watcher 时，macOS 会弹出权限请求。如果没有弹出，手动添加：

```
系统设置 → 隐私与安全性 → 辅助功能 → 点击「+」→ 添加 Terminal.app（或你使用的终端）
```

没有这个权限，watcher 仍然会发 macOS 通知提醒你，但无法自动输入触发文字。

### 设置「息屏不锁屏」（推荐）

这样 Mac 在你不在时能自动处理飞书任务：

```
系统设置 → 锁定屏幕 → 「需要密码以关闭屏幕保护程序或屏幕将进入睡眠后」→ 改为「永不」
系统设置 → 显示器（或电池）→ 关闭显示器 → 设置合适时间（如 10 分钟）
```

**效果**：显示器自动关闭（省电），但屏幕不锁定。飞书消息到达时，watcher 自动激活 Antigravity，macOS 同时唤醒显示器。

---

## 功能说明

| 功能 | 触发时机 | 实现方式 |
|------|----------|---------|
| **项目启动通知** | 打开 Antigravity 时 | SKILL.md 规则1 → `send_open_message` → 飞书收到「🚀 项目名 · 准备就绪」|
| **飞书消息确认(已读)** | 接收到飞书消息时 | 监听器调用 `send_reaction` → 原消息自动添加 `✅(OK)` 表情 |
| **飞书消息触发任务** | 飞书消息到达时 | 监听器写入队列 → watcher 激活 Antigravity → Agent 读队列处理 |
| **对话结果推送** | Agent 完成回复后 | SKILL.md 规则3 → `send_result` → 飞书收到「✅ 任务完成」卡片 |

> **关于自动触发的准确说明**：watcher 通过 AppleScript 将 Antigravity 置为前台并触发新一轮对话，Agent 按 SKILL.md 规则读取飞书消息队列并处理。若 watcher 未授权辅助功能，则仅发系统通知，需手动切换到 Antigravity。

**支持 P2P 私聊（无需建群）**：直接在飞书搜索机器人私信即可。
**支持群聊**：将机器人邀请进群，在群内首次发消息后自动激活。

---

## 验证安装

```bash
# 查看项目绑定状态
python3 $FEISHU_PLUGIN_PATH/feishu.py status

# 发送测试消息（需已完成绑定 + 飞书发消息激活）
python3 $FEISHU_PLUGIN_PATH/feishu.py test

# 查看监听器状态
python3 $FEISHU_PLUGIN_PATH/feishu_listener.py --status

# 查看 watcher 状态
python3 $FEISHU_PLUGIN_PATH/feishu_watcher.py --status
```

---

## 手动命令参考

所有命令需在项目目录下执行，或加 `--workspace /你的项目路径`。

```bash
P=$FEISHU_PLUGIN_PATH

# ── feishu.py ────────────────────────────────────────────────────────────
# 绑定项目（等同于魔法词「绑定飞书」）
python3 $P/feishu.py setup --app-id=cli_xxx --app-secret=yyy

# 查看当前项目飞书绑定状态
python3 $P/feishu.py status

# 测试连接（验证凭证 + 发测试消息）
python3 $P/feishu.py test

# 手动发送项目启动通知
python3 $P/feishu.py send_open_message

# 手动推送对话结果
python3 $P/feishu.py send_result "完成了登录功能" "新增 auth.py，修改 routes.py"

# 手动发送纯文本消息
python3 $P/feishu.py send_text "这是一条纯文本消息测试"

# 增加表情回复（给指定消息打卡，如已读）
python3 $P/feishu.py send_reaction <message_id> OK

# 读取飞书消息队列（读后自动清空）
python3 $P/feishu.py read_messages

# 列出机器人所在的所有群
python3 $P/feishu.py get_chats

# ── feishu_listener.py ───────────────────────────────────────────────────
python3 $P/feishu_listener.py --daemon    # 后台启动监听器
python3 $P/feishu_listener.py --status    # 查看监听器状态
python3 $P/feishu_listener.py --stop      # 停止监听器
python3 $P/feishu_listener.py             # 前台运行（调试用）

# ── feishu_watcher.py ────────────────────────────────────────────────────
python3 $P/feishu_watcher.py --daemon     # 后台启动 watcher
python3 $P/feishu_watcher.py --status     # 查看 watcher 状态
python3 $P/feishu_watcher.py --stop       # 停止 watcher
python3 $P/feishu_watcher.py              # 前台运行（调试用）
```

---

## 文件结构

```
~/Antigravity/AFPlugin/feishu-bot/        ← 插件目录（安装一次）
├── SKILL.md                               # Antigravity 自动加载的技能定义
├── feishu.py                              # 核心脚本：配置/发消息/读队列
├── feishu_listener.py                     # 飞书 WebSocket 消息监听守护进程
├── feishu_watcher.py                      # 队列监控 + AppleScript 自动触发器
├── requirements.txt                       # Python 依赖
└── README.md                              # 本文件

{你的项目}/                               ← 每个项目独立，互不干扰
└── .antigravity/                          ← 加入 .gitignore！包含密钥
    ├── feishu_config.json                 # 项目配置（app_id、app_secret、target_id）
    ├── feishu_messages.json               # 飞书消息队列（自动管理）
    ├── .feishu_token_cache.json           # Token 缓存（自动管理，2小时刷新）
    ├── feishu_listener.pid / .log         # 监听器进程信息（运行时生成）
    └── feishu_watcher.pid / .log          # watcher 进程信息（运行时生成）
```

将 `.antigravity/` 加入你的项目 `.gitignore`：

```bash
echo '.antigravity/' >> .gitignore
```

---

## 常见问题

**Q：输入「绑定飞书」没有反应？**  
A：两个检查点：① Antigravity 是否已在克隆插件后**重启过**；② `echo $FEISHU_PLUGIN_PATH` 是否有输出。

**Q：监听器启动正常，但飞书发消息收不到？**  
A：这是最常见的配置遗漏。检查飞书开放平台：
1. **事件订阅 → 接收方式 → 是否选了「长连接」**（不是 Webhook URL）
2. **事件列表 → 是否添加了 `im.message.receive_v1`**
3. 应用是否已发布（或在测试用户名单中）

**Q：feishu.py test 提示 Token 获取失败？**  
A：检查 App ID / App Secret 是否正确，以及应用是否已发布。

**Q：watcher 启动了，但飞书消息来了 Antigravity 没自动弹出？**  
A：按以下顺序排查：
1. `系统设置 → 隐私与安全性 → 辅助功能` 中是否有 Terminal（或启动 watcher 的程序）  
2. Antigravity 是否在运行（watcher 不会自动启动 Antigravity，只是把它置为前台）
3. 查看 watcher 日志：`tail -f {项目}/.antigravity/feishu_watcher.log`

**Q：Cmd+Shift+L 没有聚焦到 Antigravity 的 Chat 输入框？**  
A：watcher 默认使用 `Cmd+1`（焦点到编辑区）→ `Cmd+L`（跳转到 Chat）。如果快捷键不同，修改 `feishu_watcher.py` 顶部的 `CHAT_KEYCODE` 变量。

**Q：多个项目共用同一个飞书 App，消息只有一个项目收到？**  
A：这是正常的。飞书 WebSocket 每个 App ID 只允许有效建立一个连接，后建立的会替代先建立的。同一时间只运行一个项目的监听器即可，切换项目时先 stop 旧的，绑定新项目会自动 start 新的。

**Q：重启电脑后监听器和 watcher 都停了？**  
A：重新打开 Antigravity 并打开已绑定的项目，SKILL.md 规则1 会自动重启两个守护进程，无需手动操作。

**Q：在其他机器上如何使用？**  
A：重复「插件安装」章节的第 1-4 步（约 2 分钟）。飞书 App 无需重新创建，每个项目目录使用「绑定飞书」魔法词重新绑定即可。

---

## 安全说明

- **App Secret 存储位置**：仅存在于本地 `{项目}/.antigravity/feishu_config.json`，不上传。
- **Token 安全**：访问 token 本地缓存于 `.feishu_token_cache.json`（2小时自动刷新），缓存文件以 `.` 开头（隐藏文件）。
- **必须 gitignore**：`.antigravity/` 目录包含密钥，务必加入 `.gitignore`，一旦泄露请立即前往飞书开放平台重置 App Secret。
- **对话安全**：SKILL.md 规则明确要求 Agent 不得在对话消息中输出 App Secret。

---

## ⚠️ 风险声明与免责

> **本项目为非官方社区插件，仅供开发和探索自动化工作流使用。**

**对于用户的保护建议：**
1. **防泄漏风险**：飞书 API 权限（特别是收发消息的机器人权限）非常敏感，请**绝对确保**你的项目 `.gitignore` 已包含 `.antigravity/`。切勿将飞书 App_ID 和 App_Secret 发布到公共代码库中。
2. **系统焦点抢占的干扰风险**：本插件包含基于 AppleScript 和 Mac 辅助功能的自动唤醒功能（watcher）。这意味着在你不在电脑前，或者正在用电脑办公时，一旦飞书收到消息，由于需要保证 AI 能够被激活，系统焦点可能会**瞬间转移**到底层的 Antigravity 窗口中。偶尔可能会打断你正常的键盘输入行为，敬请知悉。

**免责说明（保护开发者的共同约定）：**
1. 由于 Mac 系统的辅助功能劫持（如键盘 `keystroke` 映射模拟），本插件尝试做最大程度的对齐（Cmd+1 至 Cmd+L），但在不同系统版本、第三方快捷键冲突下，可能会出现触发错误指令的意外情况。
2. 任何因配置泄露造成的隐私数据丢失、或是因焦点转移导致在其他软件按下意外快捷键造成的故障甚至财产损失，**插件提供者与作者概不负责**。
3. 如果您对自动化可能带来的“幽灵击键”感到顾虑，您可以随时在命令行输入 `python3 feishu_watcher.py --stop` 来禁用自动唤醒屏幕的功能，仅当手动点开时处理队列。

---

## ☕️ 赞赏与支持

如果这个基于大模型的全自动飞书联动小插件，为你切切实实省下了开发或者摸鱼的时间，欢迎请作者喝杯咖啡！你的支持是我持续维护和优化扩展的最大动力。

<div align="center">
  <div style="display: inline-block; margin-right: 20px; text-align: center;">
    <img src="https://github.com/user-attachments/assets/1fb9b892-f993-49c5-8821-dfc920070180" width="250px" alt="微信赞赏码"><br>
    <b>Wechat Pay</b>
  </div>
  <div style="display: inline-block; text-align: center;">
    <img src="https://github.com/user-attachments/assets/784dc0c0-3413-4eff-8a71-19e8bd8880b1" width="250px" alt="支付宝收款码"><br>
    <b>Alipay</b>
  </div>
</div>

