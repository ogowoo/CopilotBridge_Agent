import json
from pathlib import Path
import uvicorn

BASE = Path(__file__).resolve().parent
CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
SERVER = CFG.get("server", {})

if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host=SERVER.get("host", "127.0.0.1"),
        port=int(SERVER.get("port", 8765)),
        reload=False,
    )
