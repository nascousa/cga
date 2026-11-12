---
description: "Use before starting, previewing, testing, restarting or deploying project services."
applyTo: "**"
---

# Fixed Project Ports

Follow ADC's Fixed Project Ports and Conflict Cleanup policy. CGA's configured host port is **18001**: reuse `http://127.0.0.1:18001/admin`, including for previews and UI validation. Do not auto-increment ports, choose another free port or create alternate preview endpoints; changing a port requires explicit user approval.

If the intended instance is healthy, reuse it. On a conflict, identify the exact listener, project ownership and startup source first; stop/remove only a confirmed obsolete same-project instance by its specific PID, service or container ID, removing obsolete auto-start registrations when necessary while preserving data, configuration and recovery. Never kill a shared Docker host process or unrelated service. If ownership or permissions block resolution, report that blocker instead of changing ports; verify the original URL and health after resolution.
