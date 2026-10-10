import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", str(BASE / "requirements.txt")])
subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
print("安装完成。下一步运行: python login.py")
