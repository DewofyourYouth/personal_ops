"""Capability protocols — small, structural contracts that plugins may satisfy.

These are intentionally narrow (interface segregation). A plugin satisfies one
just by having the right method — it does NOT inherit from it — so plugins stay
independent classes. `@runtime_checkable` lets the digest and the eval harness
*discover* which plugins have a capability without a hardcoded list.

Add a new protocol only when a second real shared need appears; never widen one
to fit a single domain.
"""
from typing import Protocol, runtime_checkable


@runtime_checkable
class Trackable(Protocol):
    """A tracking domain that can summarize its data over a recent window — used
    by the daily/weekly digest and (later) the eval harness as a signal source."""

    def summary(self, days: int) -> str: ...
