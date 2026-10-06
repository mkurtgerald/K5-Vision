"""Thread-affine ownership for bounded Win32 shell message polling.

This registry describes only shells created by the operator boundary. It does not
own a thread's foreign windows or intercept nonqueued Win32 sent messages.
"""

from __future__ import annotations

import threading

_MAX_OWNED_SHELLS = 64


class WindowsOperatorMessageRoutingError(RuntimeError):
    """Sanitized failure of a private shell registration or thread boundary."""

    def __init__(self) -> None:
        super().__init__("operator shell message ownership is invalid")


class _ThreadRoutes(threading.local):
    def __init__(self) -> None:
        self.active: dict[int, OwnedShellMessageRoute] = {}


_ROUTES = _ThreadRoutes()


class OwnedShellMessageRoute:
    """One creation-thread registration; quit applies to current owned peers only."""

    def __init__(self) -> None:
        # Thread object identity remains distinct if the OS later reuses a thread ID.
        self._thread = threading.current_thread()
        self._shell: int | None = None
        self._registration: object | None = None
        self._quit_requested = False

    def _require_thread(self) -> None:
        if threading.current_thread() is not self._thread:
            raise WindowsOperatorMessageRoutingError()

    def require_available(self) -> None:
        """Reject wrong-thread, duplicate, over-bound or quit-draining creation."""
        self._require_thread()
        if (
            self._shell is not None
            or len(_ROUTES.active) >= _MAX_OWNED_SHELLS
            or any(route._quit_requested for route in _ROUTES.active.values())
        ):
            raise WindowsOperatorMessageRoutingError()

    def register(self, shell: int) -> None:
        self.require_available()
        if type(shell) is not int or shell <= 0 or shell in _ROUTES.active:
            raise WindowsOperatorMessageRoutingError()
        self._shell = shell
        self._registration = object()
        self._quit_requested = False
        _ROUTES.active[shell] = self

    def require(self, shell: int, *, registration: object | None = None) -> None:
        """Validate the exact registration before any owner-scoped native call."""
        self._require_thread()
        if (
            type(shell) is not int
            or self._shell != shell
            or _ROUTES.active.get(shell) is not self
            or (registration is not None and registration is not self._registration)
        ):
            raise WindowsOperatorMessageRoutingError()

    def registration(self, shell: int) -> object:
        self.require(shell)
        assert self._registration is not None
        return self._registration

    def quit_requested(self, shell: int) -> bool:
        self.require(shell)
        return self._quit_requested

    def observe_quit(self, shell: int) -> None:
        """A removed thread quit closes all currently registered owned shells."""
        self.require(shell)
        self.observe_thread_quit()

    def observe_thread_quit(self) -> None:
        """Fan out a removed quit even if sent dispatch retired the polling shell.

        The caller must validate its registration before polling. A native poll
        can dispatch synchronous lifecycle callbacks and still remove WM_QUIT.
        """
        self._require_thread()
        for route in _ROUTES.active.values():
            route._quit_requested = True

    def unregister(self, shell: int, *, registration: object | None = None) -> None:
        self.require(shell, registration=registration)
        del _ROUTES.active[shell]
        self._shell = None
        self._registration = None
        self._quit_requested = False
