"""Branch-scoped network control-plane helpers."""

from importlib import import_module

__version__ = "0.8.0"

# Keep the existing Python API lazy: importing the standalone client must not
# import the Compose planner or other server runtime modules.
_EXPORTS = {
    "EndpointDeclaration": ".model",
    "EndpointKind": ".model",
    "GenerateRequest": ".model",
    "GeneratedPlan": ".planner",
    "generate_plan": ".planner",
}

__all__ = [
    "EndpointDeclaration",
    "EndpointKind",
    "GenerateRequest",
    "GeneratedPlan",
    "generate_plan",
]


def __getattr__(name: str):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value
