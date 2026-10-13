# -*- coding: utf-8 -*-
"""工作区改动回滚 CLI：服务/WebUI 起不来时的 failback 通道。

用法:
    python scripts/rollback.py --list                     # 列出有快照的 Session
    python scripts/rollback.py --sid <session_id>         # 回滚指定 Session 的工作区改动
    python scripts/rollback.py --last                     # 回滚最近一次有改动的任务
    python scripts/rollback.py --scope self --last        # 回滚最近一次"服务自升级"
    python scripts/rollback.py --scope self --list         # 列出自升级快照

自我升级把代码改坏（导入报错、服务起不来）时，WebUI 往往也打不开，
这是不依赖服务的最后一道回滚通道。--scope self 回滚的是服务安装目录
（cbp/、webui/、scripts/ 等）自身的代码。
"""
import argparse
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from cbp.snapshots import SnapshotStore  # noqa: E402

SELF_PREFIX = "_self_"


def project_root() -> Path:
    cfg_path = BASE / "config.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    agent = cfg.get("agent", {}) if isinstance(cfg, dict) else {}
    # 工作区默认在项目目录下；快照记录的是工作区内的相对路径
    ws = agent.get("workspace", "workspace")
    ws_path = Path(ws)
    return (ws_path if ws_path.is_absolute() else BASE / ws_path).resolve()


def main():
    ap = argparse.ArgumentParser(description="回滚 Agent 任务对文件的改动")
    ap.add_argument("--list", action="store_true", help="列出所有含快照的 Session")
    ap.add_argument("--sid", help="要回滚的 Session ID（--scope self 时为自升级作用的会话）")
    ap.add_argument("--last", action="store_true", help="回滚最近一次有改动的 Session")
    ap.add_argument("--scope", choices=["workspace", "self"], default="workspace",
                    help="workspace（默认，工作区文件）或 self（服务自身代码）")
    ap.add_argument("--workspace", help="覆盖工作区路径（默认取 config.json 的 agent.workspace）")
    ap.add_argument("--yes", action="store_true", help="跳过确认")
    args = ap.parse_args()

    store = SnapshotStore(BASE / "data" / "snapshots")
    all_scopes = ([d.name for d in sorted(store.base.iterdir()) if d.is_dir()]
                  if store.base.is_dir() else [])
    if args.scope == "self":
        sessions = [s for s in all_scopes if s.startswith(SELF_PREFIX)]
        target_root = BASE
        label = "服务自身代码"
    else:
        sessions = [s for s in all_scopes if not s.startswith(SELF_PREFIX)]
        target_root = Path(args.workspace).resolve() if args.workspace else project_root()
        label = "工作区"

    if args.list or not (args.sid or args.last):
        if not sessions:
            print(f"没有可回滚的{label}快照记录。")
            return 0
        print(f"含快照的{label}作用域：")
        for sid in sessions:
            s = store.summary(sid)
            kinds = ", ".join(f"{k}×{v}" for k, v in s["counts"].items()) or "无"
            shown = sid[len(SELF_PREFIX):] if sid.startswith(SELF_PREFIX) else sid
            print(f"  {shown}  条目 {len(s['files'])}（{kinds}）")
        if not args.list:
            print(f"\n用 --sid <id>、--last{'、--scope self' if args.scope == 'workspace' else ''} 执行回滚。")
        return 0

    sid = args.sid
    if args.scope == "self" and sid and not sid.startswith(SELF_PREFIX):
        sid = SELF_PREFIX + sid
    if args.last:
        if not sessions:
            print(f"没有可回滚的{label}快照记录。")
            return 1
        sid = max(sessions, key=lambda s: store.manifest_path(s).stat().st_mtime)

    summary = store.summary(sid)
    if not summary["files"]:
        print(f"作用域 {sid} 没有快照记录。")
        return 1

    print(f"作用域  : {sid}（{label}）")
    print(f"目标根  : {target_root}")
    print(f"将回滚 {len(summary['files'])} 个路径（修改还原 / 任务新建的删除）：")
    for f in summary["files"][:40]:
        print(f"  - [{f['kind']}] {f['path']}")
    if len(summary["files"]) > 40:
        print(f"  … 另有 {len(summary['files']) - 40} 个")
    if args.scope == "self":
        print("注意：回滚服务自身代码后需要重启服务才生效。")

    if not args.yes:
        try:
            if input("确认回滚？输入 yes 继续: ").strip().lower() != "yes":
                print("已取消。")
                return 0
        except EOFError:
            print("非交互环境请加 --yes。")
            return 1

    result = store.restore(sid, target_root)
    print(f"\n完成：还原 {result['restored']}，删除 {result['deleted']}，失败 {result['failed']}")
    for r in result["results"]:
        if r.get("status") in {"failed", "refused"}:
            print(f"  ! {r['path']}: {r.get('error')}")
    return 0 if result["failed"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
