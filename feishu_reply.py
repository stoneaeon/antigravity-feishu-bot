import sys
import json
from pathlib import Path
sys.path.append("/Users/leona/Antigravity/AFPlugin/feishu-bot")
import feishu

def main():
    ws = Path("/Users/leona/Antigravity/AFPlugin")
    cfg = feishu.load_config(ws)
    token = feishu.get_token(cfg, ws)
    title = "🕵️ 前述 3 条信息复盘与问题分析"
    body = (
        "我查看了飞书消息记录和底层的监控日志。针对你连发的『修正 Bug 声明』、『重试』以及『疑问是否自动点击了 dismiss』这三条信息，结论非常出乎意料，问题链条如下：\n\n"
        "**真相：插件根本没有碰到 Dismiss，而是完全「没看见」报错！**\n\n"
        "**底层逻辑断层分析：**\n"
        "1. **幽灵弹窗 (Toast 自动消失)**：Antigravity 界面里的某些报错（包括超载）采用了类似系统 Toast 的通知框，它存在几秒后就会**自然淡出消失**，而不是一直卡在那里。\n"
        "2. **监控刷新率错位**：我们当前由于考虑性能，设定的视觉 OCR 扫描频率是 **每 10 秒才截屏一次** (`error_check_count % 5 == 0`)。这就造成了一个时间差——当报错弹窗出现时，监控器在休眠；等到监控器去截图扫描时，弹窗已经自动消失了！所以日志里完全没有 `🚨 检测到异常` 的记录，它压根不知道发了什么。\n"
        "3. **窗口遮挡干扰**：因为视觉 OCR 截取的是物理全屏 (`screencapture -x`)，如果你当时电脑最上层显示的是「飞书」的对话框并遮住了 Antigravity 的按钮，截屏也无法透视过去看到后面的重试按钮。\n\n"
        "**所以，你看到的“弹窗不见了”，不是我点掉的，而是它自己消失或者被遮盖了。**\n\n"
        "**接下来的对策（由你决定）：**\n"
        "如果你需要，我可以马上实施第二轮外科手术，做这几件事：\n"
        "- **高频扫描**：在锁定状态下，把视觉巡检压缩到 **2秒~3秒一次**，专门捕获转瞬即逝的 Toast 报错。\n"
        "- **精准窗口锁定**：尝试利用 Mac 窗口句柄（WindowID），让截图指令只对准 Antigravity 强行透视截图，无视上方飞书遮挡。\n\n"
        "如果要开搞，在飞书回我一句确认！"
    )
    if feishu.send_card(token, cfg, title, body):
        print("Success sent")
    else:
        sys.exit(1)

    # Clean the queue
    queue_path = Path("/Users/leona/Antigravity/AFPlugin/.antigravity/feishu_messages.json")
    if queue_path.exists():
        data = json.loads(queue_path.read_text(encoding="utf-8"))
        data["messages"] = []
        data.pop("processing", None)
        data.pop("processing_since", None)
        queue_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print("Queue cleared.")

if __name__ == "__main__":
    main()
