"""Read-only workspace inspection plugin."""


def register(registry):
    async def workspace_inspect(path='.', max_entries=200):
        target = registry._resolve(path)
        limit = max(1, min(int(max_entries), 1000))
        items = [target] if target.is_file() else target.rglob('*')
        entries = []
        for item in items:
            if len(entries) >= limit:
                break
            stat = item.stat()
            entries.append({'path': str(item.relative_to(registry.root)), 'type': 'directory' if item.is_dir() else 'file', 'size': stat.st_size if item.is_file() else None})
        return {'path': path, 'entries': entries, 'truncated': len(entries) >= limit}

    registry.register('workspace_inspect', workspace_inspect, '只读检查工作区文件类型和大小。', {'path': '相对工作区路径，默认 .', 'max_entries': '最多返回项数，默认 200'})
