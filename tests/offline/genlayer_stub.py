"""genlayer_stub — a stdlib-only stand-in for the `genlayer` SDK.

Importing this module registers it under the name `genlayer` in sys.modules,
so the REAL contract files (which start with `from genlayer import *`) can be
imported and executed by ordinary CPython — no chain, no network, no GenVM.

It implements just enough surface for deterministic white-box testing:

  * storage decls: TreeMap / DynArray with get_or_insert_default semantics
  * scalar wrappers: Address, u8..u256, i32..i256
  * gl.Contract base that materialises annotated storage fields per instance
  * gl.message (set sender_address / value per call in your tests)
  * gl.transfer (recorded into TRANSFERS for assertions)
  * gl.vm.UserError
  * gl.eq_principle.* (runs the leader function once — leader decides)
  * gl.nondet.web / exec_prompt — routed through NONDET_HOOKS so tests can
    run the REAL _judge (prompt building, JSON cleanup, coherence) with
    scripted web/LLM responses, instead of patching whole functions
  * gl.contract_interface + gl.get_contract_at (proxy into REGISTRY, so
    contract-to-contract view calls work against a deployed instance)
  * load_contract(path, name) — load a contract module from a file path

What it deliberately does NOT emulate: consensus itself, leader rotation,
replay under other validators. Those belong to gltest/simulator integration
tests (see tests/integration/). Everything the contract can decide
deterministically is fair game here.
"""
import importlib.util as _importlib_util
import os as _os
import sys as _sys
import types as _types
from dataclasses import fields as _dc_fields, is_dataclass as _is_dataclass
import typing as _typing

__all__ = [
    "gl", "Address", "TreeMap", "DynArray",
    "u8", "u16", "u32", "u64", "u128", "u256", "i32", "i64", "i256",
    "allow_storage", "TRANSFERS", "REGISTRY", "NONDET_HOOKS",
    "reset_state", "load_contract",
]


# ── scalar wrappers ───────────────────────────────────────────────────────

class Address(str):
    pass


def _int_like(name):
    return type(name, (int,), {})


u8 = _int_like("u8")
u16 = _int_like("u16")
u32 = _int_like("u32")
u64 = _int_like("u64")
u128 = _int_like("u128")
u256 = _int_like("u256")
i32 = _int_like("i32")
i64 = _int_like("i64")
i256 = _int_like("i256")


def allow_storage(cls):
    return cls


# ── storage containers ─────────────────────────────────────────────────────

def _fabricate_dataclass(cls):
    """Construct a blank instance of a storage dataclass the way GenVM's
    storage layer does: every field zero-valued, overwritten by the contract
    before use."""
    inst = cls.__new__(cls)
    for f in _dc_fields(cls):
        t = f.type
        if isinstance(t, type) and issubclass(t, bool):
            setattr(inst, f.name, False)
        elif isinstance(t, type) and issubclass(t, int):
            setattr(inst, f.name, t(0))
        elif isinstance(t, type) and issubclass(t, str):
            setattr(inst, f.name, t(""))
        else:
            setattr(inst, f.name, None)
    return inst


def _elem_ctor(annotation):
    """Decide what get_or_insert_default must fabricate for a container
    whose value type is `annotation`."""
    origin = _typing.get_origin(annotation)
    if origin is DynArray:
        return DynArray
    if origin is not None and isinstance(origin, type) and issubclass(origin, DynArray):
        return DynArray
    if isinstance(annotation, type) and _is_dataclass(annotation):
        return lambda: _fabricate_dataclass(annotation)
    if isinstance(annotation, type) and issubclass(annotation, int):
        return lambda: annotation(0)
    if isinstance(annotation, type) and issubclass(annotation, str):
        return lambda: annotation("")
    return lambda: None


class TreeMap(dict):
    _elem_ctor = None

    def get_or_insert_default(self, key):
        if key not in self:
            self[key] = self._elem_ctor() if self._elem_ctor is not None else None
        return self[key]

    def contains(self, key):
        return key in self


class DynArray(list):
    pass


def _storage_ctor(annotation):
    """For a contract class annotation, build the per-instance default.
    Container annotations materialise; scalars are left for __init__."""
    origin = _typing.get_origin(annotation)
    if annotation is TreeMap or origin is TreeMap:
        def make_tree(args=_typing.get_args(annotation)):
            tm = TreeMap()
            if len(args) == 2:
                tm._elem_ctor = _elem_ctor(args[1])
            return tm
        return make_tree
    if annotation is DynArray or (origin is not None and isinstance(origin, type) and issubclass(origin, DynArray)):
        return DynArray
    return None


# ── the gl namespace ───────────────────────────────────────────────────────

TRANSFERS = []   # every gl.transfer lands here: {"to": str, "amount": int}
REGISTRY = {}    # address -> contract instance, for IC-to-IC calls

# Scripted nondeterminism. Assign callables to run the REAL judging code
# offline: contract._judge -> gl.nondet.web.render / gl.nondet.exec_prompt
# will call these hooks. Default: raise, so every test must opt in.
NONDET_HOOKS = {
    "web_render": None,    # (url, mode, wait_after_loaded) -> str
    "exec_prompt": None,   # (prompt) -> str
}


class _UserError(Exception):
    pass


class _VM:
    UserError = _UserError


class _Message:
    sender_address = Address("0x0000000000000000000000000000000000000000")
    value = 0


class _PublicView:
    def __call__(self, fn):
        return fn


class _PublicWrite:
    def __call__(self, fn):
        return fn

    def payable(self, fn):
        fn.__is_payable__ = True
        return fn


class _Public:
    view = _PublicView()
    write = _PublicWrite()


class _Contract:
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        annotations = {}
        for base in reversed(cls.__mro__):
            annotations.update(getattr(base, "__annotations__", {}))
        user_init = cls.__dict__.get("__init__")

        def __init__(self, *args, **kw):
            for name, ann in annotations.items():
                ctor = _storage_ctor(ann)
                if ctor is not None:
                    object.__setattr__(self, name, ctor())
            if user_init is not None:
                user_init(self, *args, **kw)
        cls.__init__ = __init__


def _transfer(to, amount):
    TRANSFERS.append({"to": str(to), "amount": int(amount)})


def _registry_target(address):
    key = str(address)
    if key not in REGISTRY:
        raise _UserError("stub: no contract registered at " + key)
    return REGISTRY[key]


class _IcViewProxy:
    def __init__(self, address):
        self._address = address

    def __getattr__(self, name):
        return getattr(_registry_target(self._address), name)


class _IcProxy:
    """What gl.get_contract_at / a contract_interface class returns in the
    stub: a callable proxy whose .view() dispatch hits REGISTRY."""
    def __init__(self, address):
        self._address = address

    def view(self):
        return _IcViewProxy(self._address)

    def emit(self, *_a, **_k):
        return _IcViewProxy(self._address)


def _get_contract_at(address):
    return _IcProxy(address)


def _contract_interface(cls):
    return _IcProxy


class _EqPrinciple:
    """Offline equivalence: the leader's function runs once and its result
    is taken as the consensus outcome. Disagreement scenarios belong to the
    integration suite."""

    @staticmethod
    def strict_eq(fn):
        return fn()

    @staticmethod
    def prompt_comparative(fn, principle):
        return fn()

    @staticmethod
    def prompt_non_comparative(fn, input, task, criteria):
        return fn()


class _Web:
    @staticmethod
    def render(url, mode="text", wait_after_loaded=None):
        hook = NONDET_HOOKS.get("web_render")
        if hook is None:
            raise RuntimeError(
                "gl.nondet.web.render: no NONDET_HOOKS['web_render'] installed")
        return hook(url, mode, wait_after_loaded)


class _Nondet:
    web = _Web()

    @staticmethod
    def exec_prompt(prompt):
        hook = NONDET_HOOKS.get("exec_prompt")
        if hook is None:
            raise RuntimeError(
                "gl.nondet.exec_prompt: no NONDET_HOOKS['exec_prompt'] installed")
        return hook(prompt)


gl = _types.SimpleNamespace(
    Contract=_Contract,
    public=_Public(),
    message=_Message(),
    vm=_VM(),
    transfer=_transfer,
    nondet=_Nondet(),
    eq_principle=_EqPrinciple(),
    get_contract_at=_get_contract_at,
    contract_interface=_contract_interface,
)


def reset_state(sender="0x0000000000000000000000000000000000000000"):
    """Back to a clean slate between tests."""
    TRANSFERS.clear()
    REGISTRY.clear()
    NONDET_HOOKS["web_render"] = None
    NONDET_HOOKS["exec_prompt"] = None
    gl.message.sender_address = Address(str(sender))
    gl.message.value = 0


def load_contract(path, module_name=None):
    """Load a contract source file as a module (the `from genlayer import *`
    line resolves to this stub, which is already registered)."""
    path = _os.fspath(path)
    module_name = module_name or _os.path.splitext(_os.path.basename(path))[0]
    spec = _importlib_util.spec_from_file_location(module_name, path)
    mod = _importlib_util.module_from_spec(spec)
    _sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


# Registering under the SDK's module name happens HERE, on import, before any
# contract file runs `from genlayer import *`.
_sys.modules.setdefault("genlayer", _sys.modules[__name__])
