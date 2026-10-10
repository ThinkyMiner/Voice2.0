"""Telephony surfaces for the loop's live and dogfood calls (PLAN_LOOP.md §L2.4, §L9 item 6).

`get_surface(cfg)` is the one place a loop config turns into an adapter. S1 imports httpx only;
its optional SDK would be imported lazily inside s1_sarvam.py, never here, so `make test` and the
fake surface never need it.
"""

from abtest.loop.contracts import LoopConfig
from abtest.loop.telephony.base import (CallRef, CallRequest, CallResult, LiveCallsDisabled, Surface)
from abtest.loop.telephony.fake import FakeSurface

__all__ = ["CallRef", "CallRequest", "CallResult", "FakeSurface", "LiveCallsDisabled", "Surface",
           "get_surface"]


def get_surface(cfg: LoopConfig) -> Surface:
    """The adapter for `cfg.live.surface`. S1 refuses (LiveCallsDisabled) unless the caps and the
    dogfood file allow it; S2 is not built in this version."""
    if cfg.live.surface == "fake":
        return FakeSurface()
    if cfg.live.surface == "s1":
        from abtest.loop.telephony.s1_sarvam import S1Surface
        return S1Surface(cfg)
    raise LiveCallsDisabled(f"live surface {cfg.live.surface!r} is not built in this version")
