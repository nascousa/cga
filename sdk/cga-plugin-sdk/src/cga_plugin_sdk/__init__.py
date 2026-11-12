"""Public, host-independent CGA first-party plugin development contracts."""
from .contracts import ExecutionResult, HostResult, PluginManifest, ScheduledTask

__version__ = "1.0.0"
__all__ = ["ExecutionResult", "HostResult", "PluginManifest", "ScheduledTask"]
