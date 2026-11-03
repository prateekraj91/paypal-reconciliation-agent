"""Operational tools. Not part of the reconciliation core.

The `reconcile` package is read-only against PayPal: its adapter issues GETs
and nothing else. Anything that mutates a PayPal record lives here instead, so
that capability is impossible to reach by accident from the core.
"""
