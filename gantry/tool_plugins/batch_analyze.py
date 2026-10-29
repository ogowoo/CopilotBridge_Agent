"""Structured read-only batch file analysis plugin."""


def register(registry):
    async def batch_analyze(paths, max_bytes=1048576):
        limit = max(1, min(int(max_bytes), 10485760))
        results = []
        for path in list(paths)[:200]:
            target = registry._resolve(path)
            if not target.is_file():
                results.append({'path': path, 'error': 'not_a_file'})
                continue
            stat = target.stat()
            data = target.read_bytes()[:limit]
            results.append({'path': path, 'size': stat.st_size, 'sampled_bytes': len(data), 'lines': data.count(bytes([10])) + bool(data), 'binary': bytes([0]) in data})
        return {'files': results}

    registry.register('batch_analyze', batch_analyze, '结构化批量分析工作区文件元数据和样本，不修改文件。', {'paths': '相对工作区文件路径数组，最多 200 项', 'max_bytes': '每个文件最多采样字节数，默认 1 MiB，最大 10 MiB'})
