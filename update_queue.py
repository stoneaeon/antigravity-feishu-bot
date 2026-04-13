import subprocess
import sys
from pathlib import Path

print("Intercepted: Running safe clear protocol instead of violent overwrite...")
feishu_script = Path(__file__).parent / "feishu.py"
workspace = Path(__file__).parent.parent

subprocess.run([sys.executable, str(feishu_script), "clear_messages", "--workspace", str(workspace)])
