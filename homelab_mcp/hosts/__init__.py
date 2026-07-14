"""Host backends: LocalDocker and RemoteSSH.

Both backends implement the same :class:`HostClient` protocol so
downstream tools (list_stacks, stack_status, scan_host, etc.) don't
have to special-case the two.
"""
