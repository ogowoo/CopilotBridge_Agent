from types import SimpleNamespace
from gantry.tool_plugins import batch_analyze, capability_probe, workspace_inspect

def setup(ctx):
    base = ctx.host.registry
    adapter = SimpleNamespace(root=base.root, _resolve=base._path, register=lambda n,f,d,a: ctx.register_tool(n,f,d,a))
    workspace_inspect.register(adapter)
    capability_probe.register(adapter)
    batch_analyze.register(adapter)
