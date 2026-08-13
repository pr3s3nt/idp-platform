"""Public Python API for the IDP engine.

The package facade keeps one namespace for tests while implementation is split by
responsibility. Attribute patches are propagated to every owning module so a test cannot
patch a facade that the real command never reads.
"""
from __future__ import annotations

import sys
from types import ModuleType

from . import context, resources, values, catalog, render, delivery, onboarding, audit, cli

_MODULES = (context, resources, values, catalog, render, delivery, onboarding, audit, cli)


def __getattr__(name: str):
    if name == "CONFIG":
        return context.CONFIG.current
    for module in reversed(_MODULES):
        if hasattr(module, name):
            return getattr(module, name)
    raise AttributeError(name)


def __dir__():
    return sorted(set(globals()) | {name for module in _MODULES for name in dir(module)})


class _EngineFacade(ModuleType):
    def __setattr__(self, name: str, value) -> None:
        if name == "CONFIG":
            context.set_config(value)
            return
        found = False
        for module in _MODULES:
            if hasattr(module, name):
                setattr(module, name, value)
                found = True
        if not found:
            super().__setattr__(name, value)


sys.modules[__name__].__class__ = _EngineFacade
