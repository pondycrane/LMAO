"""Dependency-injection wrapper for Reticulum and LXMF modules.

Provides module-level access to ``RNS`` and ``LXMF`` with defaults set
to the real libraries.  Tests can monkeypatch these attributes without
touching ``sys.modules``, eliminating fragile module-level mocking::

    from lma_core import rns_di
    rns_di.RNS = mock_rns
    rns_di.LXMF = mock_lxmf
"""

try:
    import RNS as _RNS
except ImportError:
    _RNS = None

try:
    import LXMF as _LXMF
except ImportError:
    _LXMF = None

RNS = _RNS
LXMF = _LXMF
