"""Interactive terminal dashboard.

A second presentation surface over the same services the commands use. It reads
through :class:`~app.cli.context.ApplicationContext` exactly as a command does,
so the layering rule still holds: the dashboard calls services, never a
repository or the API client directly.
"""
