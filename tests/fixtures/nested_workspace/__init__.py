"""Opt-in nested-workspace integration fixture for the portable runtime.

The package is deliberately inert: importing it creates nothing. The driver
(``python -m tests.fixtures.nested_workspace.driver``) is the bounded
prepare/exercise/cleanup interface the integration test and an external
operator execute explicitly; the default unit suite never touches it.
"""
