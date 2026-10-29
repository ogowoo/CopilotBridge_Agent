"""Read-only command capability probe plugin."""
import platform
import shutil
import sys


async def command_capabilities(commands=None):
    names = commands or ['git', 'node', 'npm', 'python', 'pwsh']
    return {
        'platform': platform.platform(),
        'python': sys.version.split()[0],
        'commands': {name: shutil.which(name) for name in names},
    }


def register(registry):
    registry.register(
        'command_capabilities',
        command_capabilities,
        '只读探测常用开发命令是否可用及其可执行文件路径。',
        {
            'type': 'object',
            'properties': {
                'commands': {
                    'type': 'array',
                    'items': {'type': 'string'},
                    'description': '要探测的命令名；默认 git、node、npm、python、pwsh。',
                }
            },
        },
    )

