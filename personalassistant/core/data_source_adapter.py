"""
data_source_adapter.py — Thin abstraction layer between monitors and external
data sources.

Defines:
  DataSourceAdapter (ABC)
      Abstract base class with a single required method:
          fetch(params: dict) -> dict
      All monitors must call data through an adapter, never inline to
      the Yahoo quote API, the Google Calendar client, or any other external library.
      This is the seam that allows a backend to be swapped (e.g. to an MCP
      server or a different market data provider) without touching monitor
      logic.

  DirectAPIAdapter(DataSourceAdapter)
      Concrete implementation that wraps a plain Python callable.
      The callable is injected at construction time; DirectAPIAdapter.fetch()
      calls it with the supplied params dict and returns whatever it returns.
      Used in Phase 1 to wrap the stock-quote fetch function and the Google
      Calendar API call.

No business logic lives here — this file is purely interface + one minimal
concrete implementation. Additional adapters (e.g. MCPAdapter) can be added
later without modifying this file's existing classes.
"""

from abc import ABC, abstractmethod
from typing import Any, Callable


class DataSourceAdapter(ABC):
    """
    Abstract base for all data source adapters.

    Every monitor fetches data exclusively through this interface.
    The concrete implementation (and therefore the external library it
    calls) is injected at construction time and invisible to the monitor.

    Contract
    ────────
    fetch(params) must:
    - Accept a plain dict of source-specific parameters.
    - Return a plain dict of results (structure defined per adapter).
    - Raise an exception on unrecoverable fetch errors; the monitor is
      responsible for catching and logging those.
    """

    @abstractmethod
    def fetch(self, params: dict) -> dict:
        """
        Fetch data from the underlying source.

        Args:
            params: Source-specific parameters (e.g. {"ticker": "RELIANCE.NS"}
                    for stock data, or {"user_id": "...", "token_path": "..."}
                    for calendar data).

        Returns:
            A plain dict whose structure is defined by the concrete adapter
            and consumed by the calling monitor.
        """


class DirectAPIAdapter(DataSourceAdapter):
    """
    Wraps any plain Python callable as a DataSourceAdapter.

    The callable receives the params dict exactly as passed to fetch() and
    must return a dict. This is the Phase 1 implementation: monitors call
    the Yahoo quote API / the Google Calendar client through a DirectAPIAdapter so
    the external library is never referenced inline in monitor code.

    Example::

        def _fetch_stock(params: dict) -> dict:
            ticker = params["ticker"]
            ...

        adapter = DirectAPIAdapter(_fetch_stock)
        result  = adapter.fetch({"ticker": "RELIANCE.NS"})

    Swapping to an MCP backend later means replacing DirectAPIAdapter with
    a new class that implements DataSourceAdapter — the monitor is unchanged.
    """

    def __init__(self, callable_fn: Callable[[dict], Any]) -> None:
        """
        Args:
            callable_fn: Any callable that accepts a single dict argument and
                         returns a dict (or raises on failure).
        """
        if not callable(callable_fn):
            raise TypeError(
                f"callable_fn must be callable, got {type(callable_fn).__name__!r}"
            )
        self._fn = callable_fn

    def fetch(self, params: dict) -> dict:
        """
        Delegate to the wrapped callable.

        Args:
            params: Passed through verbatim to the underlying callable.

        Returns:
            Whatever the callable returns.
        """
        return self._fn(params)
