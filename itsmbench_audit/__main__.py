from __future__ import annotations

import argparse
import ast
import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path


STOPWORDS = {
    "a", "an", "and", "as", "at", "be", "by", "do", "for", "from", "in",
    "into", "is", "it", "of", "on", "or", "the", "their", "this", "to",
    "with", "your",
}


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


def documentation(task: Path) -> list[tuple[str, str]]:
    docs = []
    for filename in ("instruction.md", "README.md"):
        path = task / filename
        if path.exists():
            docs.append((filename, read(path)))
    return docs


def verifier_files(task: Path) -> list[Path]:
    candidates = [
        task / "tests" / "test_outputs.py",
        task / "tests" / "grade.js",
        task / "tests" / "assertions.json",
        task / "tests" / "expected_state.json",
    ]
    return [path for path in candidates if path.exists()]


def normalize_tokens(text: str) -> set[str]:
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    result = set()
    for token in tokens:
        if token in STOPWORDS:
            continue
        if len(token) <= 2:
            continue
        if token.endswith("ies") and len(token) > 4:
            token = token[:-3] + "y"
        elif token.endswith("s") and not token.endswith("ss") and len(token) > 4:
            token = token[:-1]
        result.add(token)
    return result



# ---------------------------------------------------------------------------
# Verifier intermediate representation
# ---------------------------------------------------------------------------

@dataclass
class VerifierAssertion:
    """A conservative, source-aware representation of one verifier check.

    ``actions`` are things the verifier proves were done (or, with negative
    polarity, were NOT done); ``states`` are end states it checks. Both are
    derived from predicate semantics first; the test label is secondary
    evidence only (see ``action_source``).
    """

    label: str
    source_file: str
    source_kind: str

    action: str | None = None
    actions: set[str] = field(default_factory=set)
    action_polarity: dict[str, str] = field(default_factory=dict)
    # Actions only *implied* by an end-state (e.g. status == "suspended").
    # They never produce an OK mapping on their own.
    implied_actions: set[str] = field(default_factory=set)
    implied_polarity: dict[str, str] = field(default_factory=dict)
    # state | membership | preservation | relation | action
    kind: str = ""
    # Control-flow / disjunction ambiguity found while resolving the check.
    ambiguity: list[str] = field(default_factory=list)
    # Where each semantic dimension came from (predicate/structured/body/label...).
    provenance: dict[str, str] = field(default_factory=dict)
    line: int = 0
    objects: set[str] = field(default_factory=set)
    entities: set[str] = field(default_factory=set)
    qualifiers: set[str] = field(default_factory=set)
    states: set[str] = field(default_factory=set)

    # Structured predicate dimensions.
    fields: set[str] = field(default_factory=set)
    expected_values: set[str] = field(default_factory=set)
    forbidden_values: set[str] = field(default_factory=set)
    relations: set[str] = field(default_factory=set)
    predicates: list[str] = field(default_factory=list)

    # Summary polarity: "negative" only when every action-bearing predicate
    # asserts the action did NOT happen.
    polarity: str = "positive"
    # predicate | structured | predicate+label | label | none
    action_source: str = "none"
    structured_predicates: int = 0

    # Primary evidence: assertion expressions as written in the verifier.
    raw_evidence: list[str] = field(default_factory=list)
    # Secondary evidence (literals, helper names). Object inference only.
    secondary_evidence: list[str] = field(default_factory=list)
    # Structured-data text whose words may carry action semantics (JSON keys).
    structured_text: str = ""
    # Label-priority hint text (label-like descriptions).
    hint_text: str = ""

    diagnostics: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    # extracted   = structured predicate + action/state + object recovered
    # partial     = some semantics found, important dimensions unresolved
    # unextracted = verifier found, no useful semantics recovered
    extraction_status: str = "unextracted"

    def predicate_text(self) -> str:
        parts: list[str] = []
        parts.extend(self.predicates)
        parts.extend(self.raw_evidence)
        parts.extend(self.fields)
        parts.extend(self.expected_values)
        parts.extend(self.forbidden_values)
        parts.extend(self.entities)
        return " ".join(p for p in parts if p)

    def semantic_text(self) -> str:
        parts = [self.label, self.predicate_text(), self.structured_text, self.hint_text]
        parts.extend(self.secondary_evidence)
        parts.extend(self.qualifiers)
        parts.extend(self.states)
        return " ".join(p for p in parts if p)

    def actions_with(self, polarity: str) -> set[str]:
        return {a for a in self.actions if self.action_polarity.get(a, "positive") == polarity}

    def implied_with(self, polarity: str) -> set[str]:
        return {a for a in self.implied_actions if self.implied_polarity.get(a, "positive") == polarity}


@dataclass
class Pred:
    """One recovered atomic predicate (language independent)."""

    text: str
    # eq | is | in | in_literals | cmp | empty | truthy | opaque
    relation: str
    holds: bool = True  # does the stated relation hold, per the verifier?
    fields: set[str] = field(default_factory=set)
    literals: list[str] = field(default_factory=list)
    values: list[str] = field(default_factory=list)  # True / False / None tokens
    names: list[str] = field(default_factory=list)  # symbolic entity candidates
    helper_strings: list[str] = field(default_factory=list)
    via: str = ""

    @property
    def structured(self) -> bool:
        return self.relation != "opaque"


GENERIC_ENTITY_WORDS = {
    "active", "inactive", "removed", "valid", "invalid", "license", "licenses",
    "server", "servers", "true", "false", "none", "null", "undefined", "enabled",
    "disabled", "present", "absent", "status", "state", "user", "users", "name",
    "id", "type", "value", "yes", "no", "ok", "open", "closed", "suspended",
    "deactivated", "deleted", "revoked", "blocked", "on_hold", "resolved",
    "account", "accounts", "group", "groups", "ticket", "token", "tokens",
    "device", "devices", "role", "roles", "app", "apps", "mailbox", "employee",
    "contractor", "admin", "certificate", "certificates", "incident",
}

NOISE_NAMES = {
    "x", "u", "i", "e", "r", "d", "a", "b", "s", "fe", "row", "rec", "item",
    "self", "cls", "state", "result", "data", "res", "resp", "user", "users",
    "server", "servers", "ticket", "account", "accounts", "response", "payload",
    "body", "record", "records", "rows", "items", "entry", "entries", "obj",
}


def _split_words(text: str) -> str:
    """Split camelCase / snake_case so keyword detection sees real words."""
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return re.sub(r"[_\-.]+", " ", text).lower()


def _dedupe_assertions(assertions: list[VerifierAssertion]) -> list[VerifierAssertion]:
    seen: set[tuple[str, str, str]] = set()
    result = []
    for assertion in assertions:
        key = (assertion.source_file, assertion.source_kind, assertion.label)
        if key in seen:
            continue
        seen.add(key)
        result.append(assertion)
    return result


# ---------------------------------------------------------------------------
# Predicate semantics (shared by Python / JavaScript / JSON extractors)
# ---------------------------------------------------------------------------

# value token -> (state, [actions]); actions are what a check proving that
# state is evidence of.
STATE_VALUES: dict[str, tuple[str, list[str]]] = {
    "suspended": ("suspended", ["suspend"]),
    "deactivated": ("deactivated", ["deactivate"]),
    "disabled": ("disabled", ["deactivate"]),
    "inactive": ("inactive", ["deactivate"]),
    "deprovisioned": ("deprovisioned", ["deprovision"]),
    "offboarded": ("deprovisioned", ["deprovision"]),
    "removed": ("removed", ["remove"]),
    "deleted": ("deleted", ["delete"]),
    "revoked": ("revoked", ["revoke"]),
    "blocked": ("blocked", ["block"]),
    "closed": ("closed", ["close"]),
    "escalated": ("escalated", ["escalate"]),
    "archived": ("archived", ["archive"]),
    "contained": ("contained", ["contain"]),
    "locked": ("locked", ["suspend"]),
    "active": ("active", ["remain"]),
    "enabled": ("enabled", ["remain"]),
    "valid": ("valid", []),
    "present": ("present", []),
}

ID_FIELD_WORDS = {
    "name", "id", "email", "upn", "username", "login", "owner", "displayname",
    "hostname", "host", "serial", "mail", "principal",
}
BAD_BOOL_FIELD_WORDS = {
    "suspended": "suspended", "disabled": "disabled", "deleted": "deleted",
    "locked": "locked", "blocked": "blocked", "revoked": "revoked",
    "archived": "archived", "closed": "closed", "deactivated": "deactivated",
}
GOOD_BOOL_FIELD_WORDS = {"enabled", "active", "valid", "approved"}
FIELD_ACTIONS = [
    ({"assigned", "assignee", "assignment", "responder", "owner"}, "assign"),
    ({"reason", "note", "notes", "comment", "comments", "worknote", "justification"}, "record"),
    ({"hold"}, "hold"),
    ({"escalated", "escalation"}, "escalate"),
]


def _state_from_value(lit: str) -> tuple[str, list[str]] | None:
    low = lit.lower().strip()
    if re.search(r"\bon[ _-]?hold\b", low):
        return ("on_hold", ["hold"])
    for token in re.split(r"[^a-z0-9]+", low):
        if token in STATE_VALUES:
            return STATE_VALUES[token]
    return None


def _non_generic(value: str) -> bool:
    v = value.strip()
    return bool(v) and len(v) >= 2 and len(v) <= 80 and v.lower() not in GENERIC_ENTITY_WORDS \
        and v.lower() not in STATE_VALUES and v.lower() not in NOISE_NAMES


def _pred_semantics(p: Pred) -> dict:
    """Derive semantics from one predicate.

    ``est`` actions are established by the predicate itself (membership removal,
    preservation). ``imp`` actions are merely implied by an end-state and are
    never treated as proof that the action was performed.
    """
    est: list[tuple[str, str]] = []
    imp: list[tuple[str, str]] = []
    states: set[str] = set()
    expected: set[str] = set()
    forbidden: set[str] = set()
    entities: set[str] = set()
    relations: set[str] = set()
    kind = "state"
    field_words = set(_split_words(" ".join(p.fields)).split())
    rel = p.relation

    if rel in ("in", "empty", "preserve") or (field_words & ID_FIELD_WORDS):
        for n in p.names:
            if _non_generic(n):
                entities.add(n)

    if rel == "preserve":
        est += [("preserve", "positive"), ("remain", "positive")]
        states.add("unchanged")
        kind = "preservation"
    elif rel in ("eq", "is", "in_literals", "truthy"):
        for lit in p.literals:
            (expected if p.holds else forbidden).add(lit)
            info = _state_from_value(lit)
            if info:
                state, acts = info
                if p.holds:
                    states.add(state)
                for a in acts:
                    imp.append((a, "positive" if p.holds else "negative"))
            elif p.holds and rel in ("eq", "in_literals") and field_words & ID_FIELD_WORDS:
                if _non_generic(lit):
                    entities.add(lit)
            elif p.holds and rel in ("eq", "in_literals") and p.fields:
                relations.add(f"{sorted(p.fields)[0]}={lit}")
                kind = "relation"
                for words, act in FIELD_ACTIONS:
                    if field_words & words:
                        imp.append((act, "positive"))
                        break
        for token in p.values:
            if token in ("True", "False"):
                truth = (token == "True") == p.holds  # effective truth of the field
                bad = field_words & set(BAD_BOOL_FIELD_WORDS)
                good = field_words & GOOD_BOOL_FIELD_WORDS
                if bad:
                    state, acts = STATE_VALUES[BAD_BOOL_FIELD_WORDS[sorted(bad)[0]]]
                    if truth:
                        states.add(state)
                    for a in acts:
                        imp.append((a, "positive" if truth else "negative"))
                elif good:
                    if truth:
                        states.add("enabled" if "enabled" in good else "active")
                        imp.append(("remain", "positive"))
                    else:
                        states.add("disabled")
                        imp.append(("deactivate", "positive"))
                (expected if p.holds == (token == "True") else forbidden).add(token.lower())
            elif token == "None":
                if p.holds:
                    states.add("absent")
                    imp.append(("remove", "positive"))
                    forbidden.add("none")
                else:
                    states.add("present")
    elif rel == "in":
        kind = "membership"
        for lit in p.literals:
            (expected if p.holds else forbidden).add(lit)
            if _non_generic(lit):
                entities.add(lit)
        if p.literals or p.names:
            if p.holds:
                states.add("present")
                imp.append(("add", "positive"))
            else:
                states.add("removed")
                est.append(("remove", "positive"))  # NOT_CONTAINS establishes removal
    elif rel == "empty":
        states.add("empty" if p.holds else "non_empty")
        for lit in p.literals:
            if _non_generic(lit):
                entities.add(lit)

    return {
        "est": est, "imp": imp, "states": states, "expected": expected,
        "forbidden": forbidden, "entities": entities, "relations": relations, "kind": kind,
    }


_KIND_RANK = {"": 0, "state": 1, "relation": 2, "membership": 3, "preservation": 4}


def _apply_preds(a: VerifierAssertion, preds: list[Pred]) -> None:
    pos: set[str] = set()
    neg: set[str] = set()
    ipos: set[str] = set()
    ineg: set[str] = set()
    for p in preds:
        shown = p.text if not p.via else f"{p.text}  [via {p.via}]"
        if shown not in a.predicates:
            a.predicates.append(shown)
        if not p.structured:
            continue
        sem = _pred_semantics(p)
        has_content = bool(p.fields or p.literals or p.values or p.names or sem["est"] or sem["imp"])
        if not has_content:
            continue
        a.structured_predicates += 1
        rel_name = {"in": "contains", "preserve": "unchanged_from_original"}.get(p.relation, p.relation)
        a.relations.add(rel_name if p.holds else f"not_{rel_name}")
        a.relations |= sem["relations"]
        a.fields |= p.fields
        a.states |= sem["states"]
        a.expected_values |= sem["expected"]
        a.forbidden_values |= sem["forbidden"]
        a.entities |= sem["entities"]
        a.secondary_evidence.extend(p.helper_strings)
        if _KIND_RANK[sem["kind"]] > _KIND_RANK[a.kind]:
            a.kind = sem["kind"]
        for act, pol in sem["est"]:
            (pos if pol == "positive" else neg).add(act)
        for act, pol in sem["imp"]:
            (ipos if pol == "positive" else ineg).add(act)
    for act in neg - pos:
        a.action_polarity[act] = "negative"
    for act in pos:
        a.action_polarity[act] = "positive"
    a.actions |= pos | neg
    for act in ineg - ipos:
        a.implied_polarity[act] = "negative"
    for act in ipos:
        a.implied_polarity[act] = "positive"
    a.implied_actions |= ipos | ineg


# ---------------------------------------------------------------------------
# Python verifier extraction: bounded intra-file interprocedural analysis
# ---------------------------------------------------------------------------

MAX_DEPTH = 5

ORIGINAL_RE = re.compile(r"(?:^|_)(?:original|orig|before|baseline|initial|snapshot|pristine|seed)(?:_|$)", re.I)
AMBIG_PREFIX = "<ambiguous:"

BUILTIN_CALLS = {
    "len", "all", "any", "set", "list", "tuple", "dict", "sorted", "str", "int",
    "float", "bool", "isinstance", "sum", "min", "max", "range", "enumerate",
    "zip", "map", "filter", "next", "iter", "print", "repr", "open", "getattr",
    "hasattr", "reversed", "type", "abs", "round", "frozenset", "json", "lower",
    "upper", "strip", "get", "items", "keys", "values", "append", "add",
    "startswith", "endswith", "format", "join", "split", "loads", "load",
}

ASSERT_FUNCS = {
    "assertequal": "eq", "assertequals": "eq", "checkequal": "eq", "asserteq": "eq",
    "assertdictequal": "eq", "assertlistequal": "eq", "assertcountequal": "eq",
    "assertnotequal": "ne", "assertin": "in", "assertnotin": "notin",
    "asserttrue": "true", "assert_true": "true", "assertfalse": "false",
    "assertisnone": "none", "assertisnotnone": "notnone",
    "check": "true", "verify": "true", "validate": "true", "expect": "true",
    "checkstate": "true", "assertstate": "true",
}


@dataclass
class _Func:
    name: str
    params: list[str]
    defaults: dict[str, ast.expr]
    node: ast.FunctionDef | ast.AsyncFunctionDef


class _Ctx:
    def __init__(self, source: str, funcs: dict[str, _Func], consts: dict[str, ast.expr]):
        self.source = source
        self.funcs = funcs
        self.consts = consts
        self.secondary: list[str] = []
        self.unresolved: list[str] = []
        self.evidence: list[str] = []
        self.hints: list[str] = []
        self.stack: list[str] = []
        self.fn_stack: list[ast.AST] = []
        self.assert_count = 0
        self.ambiguous: list[str] = []


class _Subst(ast.NodeTransformer):
    def __init__(self, env: dict[str, ast.expr]):
        self.env = env

    def visit_Name(self, node: ast.Name):
        if isinstance(node.ctx, ast.Load) and node.id in self.env:
            return copy.deepcopy(self.env[node.id])
        return node


def _subst(node: ast.AST, env: dict[str, ast.expr]):
    if not env:
        return node
    return _Subst(env).visit(copy.deepcopy(node))


def _unparse(node: ast.AST, limit: int = 300) -> str:
    try:
        text = ast.unparse(node)
    except Exception:
        text = ""
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _build_func_index(tree: ast.AST) -> dict[str, _Func]:
    funcs: dict[str, _Func] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            positional = [a.arg for a in args.posonlyargs + args.args]
            if positional and positional[0] in ("self", "cls"):
                positional = positional[1:]
            names = positional + [a.arg for a in args.kwonlyargs]
            defaults: dict[str, ast.expr] = {}
            pos_all = [a.arg for a in args.posonlyargs + args.args]
            for name, dflt in zip(reversed(pos_all), reversed(args.defaults)):
                defaults[name] = dflt
            for name, dflt in zip([a.arg for a in args.kwonlyargs], args.kw_defaults):
                if dflt is not None:
                    defaults[name] = dflt
            funcs.setdefault(node.name, _Func(node.name, names, defaults, node))
    return funcs


def _module_consts(tree: ast.AST) -> dict[str, ast.expr]:
    consts: dict[str, ast.expr] = {}
    body = tree.body if isinstance(tree, ast.Module) else []
    for st in body:
        if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
            consts[st.targets[0].id] = st.value
        elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and st.value is not None:
            consts[st.target.id] = st.value
    return consts


def _const_strings(node: ast.AST, consts: dict[str, ast.expr], depth: int = 0) -> list[str]:
    if depth > 4:
        return []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value] if node.value.strip() else []
    if isinstance(node, ast.Name) and node.id in consts:
        return _const_strings(consts[node.id], consts, depth + 1)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        out: list[str] = []
        for elt in node.elts:
            out.extend(_const_strings(elt, consts, depth + 1))
        return out
    if isinstance(node, ast.Dict):
        out = []
        for key in node.keys:
            if key is not None:
                out.extend(_const_strings(key, consts, depth + 1))
        return out
    return []


@dataclass
class _Facts:
    fields: set[str] = field(default_factory=set)
    literals: list[str] = field(default_factory=list)
    values: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    helper_strings: list[str] = field(default_factory=list)


def _facts(expr: ast.AST, ctx: _Ctx, env: dict[str, ast.expr], depth: int = 0) -> _Facts:
    f = _Facts()
    node = _subst(expr, env)
    skip: set[int] = set()
    func_pos: set[int] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            func_pos.add(id(n.func))
    for n in ast.walk(node):
        if isinstance(n, ast.Subscript):
            sl = n.slice
            if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                f.fields.add(sl.value)
                skip.add(id(sl))
        elif isinstance(n, ast.Call):
            if isinstance(n.func, ast.Attribute) and n.func.attr in ("get", "pop", "setdefault") and n.args:
                a0 = n.args[0]
                if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                    f.fields.add(a0.value)
                    skip.add(id(a0))
            callee = n.func.id if isinstance(n.func, ast.Name) else None
            if callee and callee in ctx.funcs and depth < 4:
                if _returns(ctx.funcs[callee].node)[1]:
                    ctx.ambiguous.append(f"{callee}() returns on multiple or conditional paths")
                hf = _helper_facts(ctx.funcs[callee], ctx, depth + 1, set())
                f.fields |= hf.fields
                f.helper_strings.extend(hf.literals[:20])
                f.helper_strings.append(f"{callee}()")
        elif isinstance(n, ast.Attribute):
            if id(n) not in func_pos and not n.attr.startswith("__") and n.attr not in (
                "length", "size", "value", "values", "keys", "items",
            ):
                f.fields.add(n.attr)
        elif isinstance(n, ast.Constant):
            if id(n) in skip:
                continue
            if isinstance(n.value, str):
                if n.value.strip():
                    f.literals.append(n.value.strip())
            elif n.value is True or n.value is False or n.value is None:
                f.values.append(repr(n.value))
        elif isinstance(n, ast.IfExp):
            ctx.ambiguous.append("conditional expression selects between values")
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            if n.id.startswith(AMBIG_PREFIX):
                ctx.ambiguous.append(f"value of {n.id[len(AMBIG_PREFIX):-1]} depends on control flow")
            elif n.id in ctx.consts:
                strings = _const_strings(n, ctx.consts)
                f.literals.extend(s.strip() for s in strings)
                f.names.append(n.id)
    return f


def _helper_facts(fn: _Func, ctx: _Ctx, depth: int, seen: set[str]) -> _Facts:
    out = _Facts()
    if fn.name in seen or depth > 4:
        return out
    seen = seen | {fn.name}
    body = list(fn.node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
        body = body[1:]
    holder = ast.Module(body=body, type_ignores=[])
    sub = _facts(holder, _Ctx(ctx.source, {}, ctx.consts), {}, depth)
    out.fields |= sub.fields
    out.literals.extend(sub.literals)
    for n in ast.walk(holder):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ctx.funcs:
            inner = _helper_facts(ctx.funcs[n.func.id], ctx, depth + 1, seen)
            out.fields |= inner.fields
            out.literals.extend(inner.literals)
    return out


def _opaque(expr: ast.AST, holds: bool, why: str, ctx: _Ctx, unresolved: str | None = None) -> Pred:
    if unresolved and unresolved not in ctx.unresolved:
        ctx.unresolved.append(unresolved)
    return Pred(text=_unparse(expr), relation="opaque", holds=holds, via=why)


def _entity_hint(arg: ast.AST, ctx: _Ctx) -> None:
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        if _non_generic(arg.value):
            ctx.hints.append(arg.value)
    elif isinstance(arg, ast.Name):
        if arg.id in ctx.consts:
            ctx.hints.append(arg.id)
            ctx.hints.extend(s for s in _const_strings(arg, ctx.consts) if _non_generic(s))
        elif _non_generic(arg.id) and len(arg.id) > 2:
            ctx.hints.append(arg.id)


def _bind(fn: _Func, call: ast.Call, env: dict[str, ast.expr], ctx: _Ctx) -> dict[str, ast.expr]:
    new_env: dict[str, ast.expr] = {}
    for i, arg in enumerate(call.args):
        if isinstance(arg, ast.Starred):
            continue
        if i < len(fn.params):
            new_env[fn.params[i]] = _subst(arg, env)
            _entity_hint(arg, ctx)
    for kw in call.keywords:
        if kw.arg and kw.arg in fn.params:
            new_env[kw.arg] = _subst(kw.value, env)
            _entity_hint(kw.value, ctx)
    for name, dflt in fn.defaults.items():
        new_env.setdefault(name, dflt)
    return new_env


def _ambig(name: str) -> ast.Name:
    return ast.Name(id=f"{AMBIG_PREFIX}{name}>", ctx=ast.Load())


def _assigned_names(stmts: list[ast.stmt]) -> set[str]:
    names: set[str] = set()
    for st in stmts:
        for n in ast.walk(st):
            targets: list[ast.AST] = []
            if isinstance(n, ast.Assign):
                targets = list(n.targets)
            elif isinstance(n, (ast.AugAssign, ast.AnnAssign)):
                targets = [n.target]
            for t in targets:
                for sub in ast.walk(t):
                    if isinstance(sub, ast.Name):
                        names.add(sub.id)
    return names


def _record_assigns(stmts: list[ast.stmt], env: dict[str, ast.expr]) -> None:
    """Order-insensitive pre-pass: only unconditional single assignments are
    recorded as values. Anything assigned conditionally, repeatedly or
    augmented is marked ambiguous instead of guessed."""
    counts: dict[str, int] = {}
    conditional: set[str] = set()
    for st in stmts:
        if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
            name = st.targets[0].id
            if not any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(st.value)):
                counts[name] = counts.get(name, 0) + 1
        elif isinstance(st, (ast.With, ast.AsyncWith)):
            _record_assigns(st.body, env)
        elif isinstance(st, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.AugAssign)):
            conditional |= _assigned_names([st])
    for st in stmts:
        if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
            name = st.targets[0].id
            if any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(st.value)):
                continue
            if name in conditional or counts.get(name, 0) > 1:
                env[name] = _ambig(name)
            else:
                env[name] = _subst(st.value, env)
    for name in conditional:
        env[name] = _ambig(name)


def _callee_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _local_func(call: ast.Call, ctx: _Ctx) -> _Func | None:
    if isinstance(call.func, ast.Name):
        return ctx.funcs.get(call.func.id)
    if (
        isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id in ("self", "cls")
    ):
        return ctx.funcs.get(call.func.attr)
    return None


def _is_empty_container(node: ast.AST) -> bool:
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)) and not node.elts:
        return True
    if isinstance(node, ast.Dict) and not node.keys:
        return True
    return (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id in ("set", "list", "tuple", "dict") and not node.args
    )


def _append_conditions(var: str, fn_node: ast.AST) -> list[ast.expr]:
    conds: list[ast.expr] = []
    for node in ast.walk(fn_node):
        if isinstance(node, ast.If):
            for sub in node.body:
                hit = False
                for n in ast.walk(sub):
                    if (
                        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                        and n.func.attr in ("append", "add", "extend", "update")
                        and isinstance(n.func.value, ast.Name) and n.func.value.id == var
                    ):
                        hit = True
                if hit:
                    conds.append(node.test)
                    break
    return conds


def _empty_compare(left: ast.AST, op: ast.cmpop, right: ast.AST):
    """Recognise `E == []`, `len(E) == 0`, `E != []`, `len(E) > 0` ..."""
    def is_zero(n):
        return isinstance(n, ast.Constant) and n.value == 0 and not isinstance(n.value, bool)

    def is_one(n):
        return isinstance(n, ast.Constant) and n.value == 1 and not isinstance(n.value, bool)

    def len_arg(n):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "len" and len(n.args) == 1:
            return n.args[0]
        return None

    for a, b, flipped in ((left, right, False), (right, left, True)):
        if _is_empty_container(b):
            if isinstance(op, (ast.Eq, ast.Is)):
                return a, True
            if isinstance(op, (ast.NotEq, ast.IsNot)):
                return a, False
        inner = len_arg(a)
        if inner is not None:
            if flipped:
                continue
            if isinstance(op, ast.Eq) and is_zero(b):
                return inner, True
            if isinstance(op, ast.NotEq) and is_zero(b):
                return inner, False
            if isinstance(op, ast.Gt) and is_zero(b):
                return inner, False
            if isinstance(op, ast.GtE) and is_one(b):
                return inner, False
            if isinstance(op, ast.Lt) and is_one(b):
                return inner, True
            if isinstance(op, ast.LtE) and is_zero(b):
                return inner, True
    return None


def _preds(expr: ast.AST, env: dict[str, ast.expr], holds: bool, ctx: _Ctx, depth: int) -> list[Pred]:
    if depth > MAX_DEPTH:
        return [_opaque(expr, holds, "depth limit reached", ctx)]
    e = expr
    if isinstance(e, ast.UnaryOp) and isinstance(e.op, ast.Not):
        return _preds(e.operand, env, not holds, ctx, depth)
    if isinstance(e, ast.BoolOp):
        if (isinstance(e.op, ast.Or) and holds) or (isinstance(e.op, ast.And) and not holds):
            ctx.ambiguous.append("disjunctive predicate (or / negated and)")
        out: list[Pred] = []
        for v in e.values:
            out.extend(_preds(v, env, holds, ctx, depth))
        return out
    if isinstance(e, ast.Compare):
        return _compare_preds(e, env, holds, ctx, depth)
    if isinstance(e, (ast.ListComp, ast.GeneratorExp, ast.SetComp)):
        return _empty_preds(e, env, not holds, ctx, depth + 1)
    if isinstance(e, ast.Call):
        return _call_preds(e, env, holds, ctx, depth)
    if isinstance(e, ast.Name) and e.id in env:
        val = env[e.id]
        if _is_empty_container(val):
            return _empty_preds(e, env, not holds, ctx, depth + 1)
        if not (isinstance(val, ast.Name) and val.id == e.id):
            return _preds(val, env, holds, ctx, depth + 1)
    if isinstance(e, ast.Constant):
        return []
    f = _facts(e, ctx, env)
    if f.fields or f.literals:
        return [Pred(_unparse(e), "truthy", holds, f.fields, f.literals, f.values, f.names, f.helper_strings)]
    return [_opaque(e, holds, "truthiness of unresolved value", ctx)]


def _compare_preds(node: ast.Compare, env, holds: bool, ctx: _Ctx, depth: int) -> list[Pred]:
    out: list[Pred] = []
    left = node.left
    for op, right in zip(node.ops, node.comparators):
        out.extend(_pair_preds(left, op, right, env, holds, ctx, depth))
        left = right
    return out


def _pair_preds(left, op, right, env, holds: bool, ctx: _Ctx, depth: int) -> list[Pred]:
    sub_cmp = ast.Compare(left=left, ops=[op], comparators=[right])
    empt = _empty_compare(left, op, right)
    if empt is not None:
        target, want_empty = empt
        return _empty_preds(target, env, want_empty if holds else not want_empty, ctx, depth + 1)

    positive_op = isinstance(op, (ast.Eq, ast.Is, ast.In))
    if isinstance(op, ast.Eq) and holds:
        for side, other in ((left, right), (right, left)):
            if (
                isinstance(side, ast.Name) and ORIGINAL_RE.search(side.id)
                and not isinstance(other, ast.Constant)
            ):
                f = _facts(other, ctx, env)
                return [Pred(_unparse(_subst(sub_cmp, env)), "preserve", True, f.fields, [], [], f.names, f.helper_strings)]
    constants = [
        c for c in (left, right)
        if isinstance(c, ast.Constant) and (c.value is True or c.value is False or c.value is None)
    ]
    if isinstance(op, (ast.Eq, ast.NotEq, ast.Is, ast.IsNot)) and constants:
        const = constants[0]
        other = right if const is left else left
        if (
            isinstance(other, ast.Call) and _local_func(other, ctx)
            and const.value in (True, False)
        ):
            wanted = bool(const.value) if positive_op else not bool(const.value)
            return _preds(other, env, wanted if holds else not wanted, ctx, depth + 1)
        f = _facts(other, ctx, env)
        return [Pred(
            _unparse(_subst(sub_cmp, env)), "is", positive_op == holds, f.fields,
            f.literals, [repr(const.value)], f.names, f.helper_strings,
        )]

    if isinstance(op, (ast.In, ast.NotIn)):
        stated = isinstance(op, ast.In) == holds
        if stated and isinstance(_subst(right, env), (ast.List, ast.Tuple, ast.Set)) and len(
            getattr(_subst(right, env), "elts", [])
        ) > 1:
            ctx.ambiguous.append("membership in several alternative literals")
        lf = _facts(left, ctx, env)
        rf = _facts(right, ctx, env)
        rsub = _subst(right, env)
        if isinstance(rsub, (ast.List, ast.Tuple, ast.Set)) and not isinstance(left, ast.Constant):
            return [Pred(
                _unparse(_subst(sub_cmp, env)), "in_literals", stated, lf.fields | rf.fields,
                rf.literals, lf.values, lf.names, lf.helper_strings,
            )]
        names = list(lf.names)
        if isinstance(left, ast.Name) and left.id not in env and _non_generic(left.id) and left.id not in ctx.consts:
            names.append(left.id)
        return [Pred(
            _unparse(_subst(sub_cmp, env)), "in", stated, rf.fields | lf.fields, lf.literals,
            [], names, rf.helper_strings + lf.helper_strings,
        )]

    lf = _facts(left, ctx, env)
    rf = _facts(right, ctx, env)
    fields = lf.fields | rf.fields
    literals = lf.literals + rf.literals
    helper = lf.helper_strings + rf.helper_strings
    names = lf.names + rf.names
    for side in (left, right):
        if isinstance(side, ast.Name) and side.id not in env and side.id not in ctx.consts \
                and _non_generic(side.id) and len(side.id) > 2:
            names.append(side.id)
    text = _unparse(_subst(sub_cmp, env))
    if isinstance(op, (ast.Eq, ast.NotEq, ast.Is, ast.IsNot)):
        stated = isinstance(op, (ast.Eq, ast.Is)) == holds
        return [Pred(text, "eq", stated, fields, literals, lf.values + rf.values, names, helper)]
    return [Pred(text, "cmp", holds, fields, literals, [], names, helper)]


def _call_preds(call: ast.Call, env, holds: bool, ctx: _Ctx, depth: int) -> list[Pred]:
    name = _callee_name(call)
    local = _local_func(call, ctx)
    if local is not None:
        return _helper_return_preds(local, call, env, holds, ctx, depth)
    if name in ("all", "any") and call.args:
        arg = call.args[0]
        if isinstance(arg, (ast.GeneratorExp, ast.ListComp, ast.SetComp)):
            return _preds(arg.elt, env, holds, ctx, depth + 1)
        return _preds(arg, env, holds, ctx, depth + 1)
    if name in ("bool",) and call.args:
        return _preds(call.args[0], env, holds, ctx, depth + 1)
    if name == "len" and call.args:
        return _empty_preds(call.args[0], env, not holds, ctx, depth + 1)
    if name and name.lower().replace("_", "") in ASSERT_FUNCS and call.args:
        return _assert_call_preds(name, call, env, holds, ctx, depth)
    f = _facts(call, ctx, env)
    if f.fields or f.literals:
        return [Pred(_unparse(call), "truthy", holds, f.fields, f.literals, f.values, f.names, f.helper_strings)]
    unresolved = f"{name}()" if name and name not in BUILTIN_CALLS else None
    return [_opaque(call, holds, "call to non-local function", ctx, unresolved)]


def _assert_call_preds(name: str, call: ast.Call, env, holds: bool, ctx: _Ctx, depth: int) -> list[Pred]:
    kind = ASSERT_FUNCS[name.lower().replace("_", "")]
    args = call.args
    if kind in ("eq", "ne") and len(args) >= 2:
        op = ast.Eq() if kind == "eq" else ast.NotEq()
        return _pair_preds(args[0], op, args[1], env, holds, ctx, depth)
    if kind in ("in", "notin") and len(args) >= 2:
        op = ast.In() if kind == "in" else ast.NotIn()
        return _pair_preds(args[0], op, args[1], env, holds, ctx, depth)
    if kind == "true":
        return _preds(args[0], env, holds, ctx, depth + 1)
    if kind == "false":
        return _preds(args[0], env, not holds, ctx, depth + 1)
    if kind in ("none", "notnone"):
        op = ast.Is() if kind == "none" else ast.IsNot()
        return _pair_preds(args[0], op, ast.Constant(value=None), env, holds, ctx, depth)
    return [_opaque(call, holds, "unsupported assertion helper", ctx)]


def _helper_return_preds(fn: _Func, call: ast.Call, env, holds: bool, ctx: _Ctx, depth: int) -> list[Pred]:
    if fn.name in ctx.stack:
        return [_opaque(call, holds, "recursive helper", ctx, f"{fn.name}()")]
    new_env = _bind(fn, call, env, ctx)
    _record_assigns(fn.node.body, new_env)
    ctx.stack.append(fn.name)
    ctx.fn_stack.append(fn.node)
    out: list[Pred] = []
    try:
        rets, ambiguous = _returns(fn.node)
        if ambiguous:
            ctx.ambiguous.append(f"{fn.name}() returns on multiple or conditional paths")
            out.append(_opaque(call, holds, "helper has multiple/conditional return paths", ctx, f"{fn.name}()"))
        elif rets:
            out.extend(_preds(rets[0].value, new_env, holds, ctx, depth + 1))
        else:
            out.extend(_walk_stmts(fn.node.body, new_env, ctx, depth + 1))
    finally:
        ctx.stack.pop()
        ctx.fn_stack.pop()
    for p in out:
        p.via = p.via or f"{fn.name}()"
    if not out:
        return [_opaque(call, holds, "helper returns no recoverable predicate", ctx, f"{fn.name}()")]
    if all(not p.structured for p in out):
        ctx.unresolved.append(f"{fn.name}()") if f"{fn.name}()" not in ctx.unresolved else None
    return out


def _returns(fn_node: ast.AST):
    rets = [n for n in _walk_no_nested(fn_node) if isinstance(n, ast.Return) and n.value is not None]
    top = {id(s) for s in fn_node.body}
    ambiguous = len(rets) > 1 or (len(rets) == 1 and id(rets[0]) not in top)
    return rets, ambiguous


def _walk_no_nested(fn_node: ast.AST):
    stack = list(ast.iter_child_nodes(fn_node))
    while stack:
        n = stack.pop(0)
        yield n
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(n))


def _empty_preds(target: ast.AST, env, want_empty: bool, ctx: _Ctx, depth: int) -> list[Pred]:
    """Predicates implied by `target` being (non-)empty."""
    if depth > MAX_DEPTH + 1:
        return [_opaque(target, want_empty, "depth limit reached", ctx)]
    var = None
    node = target
    if isinstance(node, ast.Name) and node.id in env:
        var = node.id
        node = env[node.id]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in (
        "len", "set", "list", "tuple", "sorted", "frozenset",
    ) and len(node.args) == 1:
        return _empty_preds(node.args[0], env, want_empty, ctx, depth + 1)
    if isinstance(node, (ast.ListComp, ast.GeneratorExp, ast.SetComp)):
        conds = [c for g in node.generators for c in g.ifs]
        out: list[Pred] = []
        for cond in conds:
            out.extend(_preds(cond, env, not want_empty, ctx, depth + 1))
        if out:
            return out
        f = _facts(node, ctx, env)
        return [Pred(_unparse(node), "empty", want_empty, f.fields, f.literals, f.values, f.names, f.helper_strings)]
    if _is_empty_container(node) and var is not None and ctx.fn_stack:
        conds = _append_conditions(var, ctx.fn_stack[-1])
        out = []
        for cond in conds:
            out.extend(_preds(cond, env, not want_empty, ctx, depth + 1))
        if out:
            return out
        return [_opaque(target, want_empty, "accumulator pattern not recognised", ctx)]
    if isinstance(node, ast.Call):
        fn = _local_func(node, ctx)
        if fn is not None:
            if fn.name in ctx.stack:
                return [_opaque(node, want_empty, "recursive helper", ctx, f"{fn.name}()")]
            new_env = _bind(fn, node, env, ctx)
            _record_assigns(fn.node.body, new_env)
            ctx.stack.append(fn.name)
            ctx.fn_stack.append(fn.node)
            out = []
            try:
                rets, ambiguous = _returns(fn.node)
                if ambiguous:
                    ctx.ambiguous.append(f"{fn.name}() returns on multiple or conditional paths")
                    out.append(_opaque(node, want_empty, "helper has multiple/conditional return paths", ctx, f"{fn.name}()"))
                elif rets:
                    out.extend(_empty_preds(rets[0].value, new_env, want_empty, ctx, depth + 1))
            finally:
                ctx.stack.pop()
                ctx.fn_stack.pop()
            for p in out:
                p.via = p.via or f"{fn.name}()"
            if out:
                return out
            return [_opaque(node, want_empty, "helper returns no recoverable collection", ctx, f"{fn.name}()")]
    f = _facts(node, ctx, env)
    if f.fields or f.literals or f.helper_strings:
        return [Pred(_unparse(target), "empty", want_empty, f.fields, f.literals, f.values, f.names, f.helper_strings)]
    return [_opaque(target, want_empty, "emptiness of unresolved value", ctx)]


def _raises_assertion(stmts: list[ast.stmt]) -> bool:
    for st in stmts:
        for n in ast.walk(st):
            if isinstance(n, ast.Raise) and n.exc is not None:
                exc = n.exc.func if isinstance(n.exc, ast.Call) else n.exc
                nm = exc.id if isinstance(exc, ast.Name) else getattr(exc, "attr", "")
                if nm == "AssertionError":
                    return True
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "fail":
                return True
    return False


def _walk_stmts(stmts: list[ast.stmt], env, ctx: _Ctx, depth: int) -> list[Pred]:
    out: list[Pred] = []
    for st in stmts:
        if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
            name = st.targets[0].id
            if not any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(st.value)):
                env[name] = _subst(st.value, env)
        elif isinstance(st, ast.Assert):
            ctx.assert_count += 1
            shown = _unparse(st.test if depth == 0 else _subst(st.test, env))
            ctx.evidence.append(shown)
            if st.msg is not None:
                ctx.secondary.extend(_const_strings(st.msg, {}) or [])
            out.extend(_preds(st.test, env, True, ctx, depth))
        elif isinstance(st, ast.If):
            if _raises_assertion(st.body):
                ctx.assert_count += 1
                ctx.evidence.append(f"not ({_unparse(st.test if depth == 0 else _subst(st.test, env))})")
                out.extend(_preds(st.test, env, False, ctx, depth))
            else:
                names = _assigned_names(st.body + st.orelse)
                branch = _walk_stmts(st.body, dict(env), ctx, depth) + _walk_stmts(st.orelse, dict(env), ctx, depth)
                if branch:
                    ctx.ambiguous.append("assertion inside a conditional branch")
                out.extend(branch)
                for n in names:
                    env[n] = _ambig(n)
        elif isinstance(st, ast.Expr) and isinstance(st.value, ast.Call):
            call = st.value
            local = _local_func(call, ctx)
            name = _callee_name(call)
            if local is not None:
                if local.name in ctx.stack or depth > MAX_DEPTH:
                    out.append(_opaque(call, True, "recursive or too deep helper", ctx, f"{local.name}()"))
                    continue
                new_env = _bind(local, call, env, ctx)
                _record_assigns(local.node.body, new_env)
                ctx.stack.append(local.name)
                ctx.fn_stack.append(local.node)
                try:
                    out.extend(_walk_stmts(local.node.body, new_env, ctx, depth + 1))
                finally:
                    ctx.stack.pop()
                    ctx.fn_stack.pop()
            elif name and name.lower().replace("_", "") in ASSERT_FUNCS and call.args:
                ctx.assert_count += 1
                ctx.evidence.append(_unparse(call))
                out.extend(_assert_call_preds(name, call, env, True, ctx, depth))
            elif name and name.lower().startswith(("assert", "expect")):
                ctx.assert_count += 1
                ctx.evidence.append(_unparse(call))
                out.append(_opaque(call, True, "unsupported assertion helper", ctx))
        elif isinstance(st, ast.Raise):
            exc = st.exc.func if isinstance(st.exc, ast.Call) else st.exc
            nm = exc.id if isinstance(exc, ast.Name) else getattr(exc, "attr", "")
            if nm == "AssertionError":
                ctx.assert_count += 1
                ctx.evidence.append(_unparse(st))
                out.append(_opaque(st.exc, True, "unconditional AssertionError", ctx))
        elif isinstance(st, (ast.With, ast.AsyncWith)):
            out.extend(_walk_stmts(st.body, env, ctx, depth))
        elif isinstance(st, (ast.For, ast.AsyncFor, ast.While, ast.Try)):
            names = _assigned_names([st])
            for attr in ("body", "orelse", "finalbody"):
                out.extend(_walk_stmts(getattr(st, attr, []) or [], dict(env), ctx, depth))
            for handler in getattr(st, "handlers", []) or []:
                out.extend(_walk_stmts(handler.body, dict(env), ctx, depth))
            for n in names:
                env[n] = _ambig(n)
    return out


def _python_test_functions(source: str):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test_") or node.name.lower().startswith(("test", "check_", "verify_")):
                found.append((node.name, node))
    return found


def _dedupe_strs(items: list[str], limit: int) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        item = re.sub(r"\s+", " ", item).strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out[:limit]


def analyze_python_source(source: str, path: str = "<memory>") -> list[VerifierAssertion]:
    """Static, bounded, intra-file analysis. Never executes verifier code."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [VerifierAssertion(
            label=f"{Path(path).name}:<unparsed>", source_file=path, source_kind="python",
            raw_evidence=[f"Python AST parse failed: {exc}"],
            diagnostics=["reason=Python AST parse failed"],
        )]
    funcs = _build_func_index(tree)
    consts = _module_consts(tree)
    results: list[VerifierAssertion] = []
    for label, fn in _python_test_functions(source):
        ctx = _Ctx(source, funcs, consts)
        ctx.stack.append(fn.name)
        ctx.fn_stack.append(fn)
        env: dict[str, ast.expr] = {}
        try:
            preds = _walk_stmts(fn.body, env, ctx, 0)
        except RecursionError:
            preds = []
            ctx.unresolved.append("<recursion>")
        a = VerifierAssertion(label=label, source_file=path, source_kind="python")
        a.raw_evidence = _dedupe_strs(ctx.evidence, 40)
        a.secondary_evidence = _dedupe_strs(ctx.secondary + ctx.hints, 60)
        a.entities |= {h for h in ctx.hints if _non_generic(h)}
        a.unresolved = _dedupe_strs(ctx.unresolved, 10)
        a.ambiguity = _dedupe_strs(ctx.ambiguous, 6)
        a.line = getattr(fn, "lineno", 0)
        _apply_preds(a, preds)
        a.secondary_evidence = _dedupe_strs(a.secondary_evidence, 60)
        if ctx.assert_count == 0:
            a.diagnostics.append("reason=no assertion found in test body")
        results.append(a)
    return results


def extract_python_assertions(path: Path) -> list[VerifierAssertion]:
    return analyze_python_source(read(path), str(path))


# ---------------------------------------------------------------------------
# JavaScript extraction (static text analysis, bounded helper resolution)
# ---------------------------------------------------------------------------

JS_IGNORED_CALLS = {
    "if", "for", "while", "switch", "catch", "function", "return", "typeof",
    "expect", "assert", "test", "it", "describe", "require", "console", "JSON",
    "Object", "Array", "String", "Number", "Boolean", "Math", "Date", "Set",
    "Map", "Promise", "parseInt", "parseFloat", "isNaN", "async", "await",
    "beforeAll", "beforeEach", "afterAll", "afterEach", "new", "Error",
}


def _js_balanced(source: str, open_idx: int, open_ch: str = "{", close_ch: str = "}") -> int:
    """Index just past the matching close, honouring strings. -1 if unbalanced."""
    depth = 0
    in_string = None
    escape = False
    for i in range(open_idx, len(source)):
        ch = source[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == in_string:
                in_string = None
            continue
        if ch in "'\"`":
            in_string = ch
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def _js_split_args(text: str) -> list[str]:
    args: list[str] = []
    depth = 0
    in_string = None
    escape = False
    cur: list[str] = []
    for ch in text:
        if in_string:
            cur.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == in_string:
                in_string = None
            continue
        if ch in "'\"`":
            in_string = ch
            cur.append(ch)
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            args.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        args.append(tail)
    return args


def _js_test_blocks(source: str) -> list[tuple[str, str]]:
    """Explicit test()/it() blocks only; helper functions are never verifiers."""
    pattern = re.compile(r"\b(?:test|it)\s*\(\s*([\"'`])(.+?)\1\s*,", flags=re.IGNORECASE | re.DOTALL)
    matches = []
    for match in pattern.finditer(source):
        label = match.group(2).strip()
        start = match.end()
        brace = source.find("{", start)
        if brace < 0:
            matches.append((label, source[start:start + 2000]))
            continue
        end = _js_balanced(source, brace)
        matches.append((label, source[brace:end if end > 0 else len(source)]))
    return matches


def _js_function_index(source: str) -> dict[str, tuple[list[str], str]]:
    funcs: dict[str, tuple[list[str], str]] = {}
    patterns = [
        r"\bfunction\s+(\w+)\s*\(([^)]*)\)\s*\{",
        r"\b(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?function\s*\w*\s*\(([^)]*)\)\s*\{",
        r"\b(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(([^)]*)\)\s*=>\s*\{",
    ]
    for pat in patterns:
        for m in re.finditer(pat, source):
            name = m.group(1)
            if name in ("test", "it", "describe") or name in funcs:
                continue
            brace = m.end() - 1
            end = _js_balanced(source, brace)
            if end < 0:
                continue
            params = [p.strip().split("=")[0].strip() for p in m.group(2).split(",") if p.strip()]
            funcs[name] = (params, source[brace:end])
    return funcs


def _js_expand(body: str, funcs, depth: int, visited: frozenset, hints: list[str]) -> list[str]:
    """Return [body] plus helper bodies with parameters substituted by arguments."""
    blocks = [body]
    if depth >= 4:
        return blocks
    for name, (params, hbody) in funcs.items():
        if name in visited:
            continue
        for m in re.finditer(rf"(?<![\w.]){re.escape(name)}\s*\(", body):
            open_idx = m.end() - 1
            end = _js_balanced(body, open_idx, "(", ")")
            if end < 0:
                continue
            args = _js_split_args(body[open_idx + 1:end - 1])
            expanded = hbody
            for i, p in enumerate(params):
                if i < len(args) and p:
                    arg = args[i]
                    simple = re.fullmatch(r"[\w.$\[\]\"'`]+", arg) is not None
                    expanded = re.sub(rf"(?<![\w.]){re.escape(p)}\b", arg if simple else f"({arg})", expanded)
                    if re.fullmatch(r"[A-Za-z_$][\w$]*", arg) and _non_generic(arg) and len(arg) > 2:
                        hints.append(arg)
                    elif re.fullmatch(r"[\"'`][^\"'`]+[\"'`]", arg) and _non_generic(arg[1:-1]):
                        hints.append(arg[1:-1])
            blocks.extend(_js_expand(expanded, funcs, depth + 1, visited | {name}, hints))
    return blocks


def _js_facts(expr: str) -> _Facts:
    f = _Facts()
    for m in re.finditer(r"\[\s*([\"'`])([^\"'`]+)\1\s*\]", expr):
        f.fields.add(m.group(2))
    stripped = re.sub(r"\[\s*([\"'`])([^\"'`]+)\1\s*\]", " ", expr)
    for m in re.finditer(r"\.\s*([A-Za-z_$][\w$]*)\b(?!\s*\()", stripped):
        if m.group(1) not in ("length", "not", "to", "be", "have", "size"):
            f.fields.add(m.group(1))
    for m in re.finditer(r"([\"'`])([^\"'`]*)\1", stripped):
        if m.group(2).strip():
            f.literals.append(m.group(2).strip())
    for tok, val in (("true", "True"), ("false", "False"), ("null", "None"), ("undefined", "None")):
        if re.search(rf"\b{tok}\b", stripped):
            f.values.append(val)
    root = re.match(r"\s*\(*\s*([A-Za-z_$][\w$]*)", stripped)
    if root and _non_generic(root.group(1)) and len(root.group(1)) > 2:
        f.names.append(root.group(1))
    return f


def _js_pred(text: str, relation: str, holds: bool, *exprs: str, values_extra: list[str] | None = None) -> Pred:
    fields: set[str] = set()
    literals: list[str] = []
    values: list[str] = list(values_extra or [])
    names: list[str] = []
    for e in exprs:
        f = _js_facts(e)
        fields |= f.fields
        literals += f.literals
        values += f.values
        names += f.names
    if relation == "is" and values_extra:
        values = list(values_extra)
    return Pred(re.sub(r"\s+", " ", text).strip()[:300], relation, holds, fields, literals, values, names)


MATCHER_RE = re.compile(
    r"^\s*((?:\.\s*(?:not|to|be|been|is|have|deep|that|resolves|rejects|with|a|an)\b)*)"
    r"\s*\.?\s*(toBe|toEqual|toStrictEqual|toContain|toContainEqual|toInclude|toHaveProperty|"
    r"toBeNull|toBeUndefined|toBeDefined|toBeTruthy|toBeFalsy|toMatch|toHaveLength|"
    r"equal|equals|eql|include|includes|contain|property|members|lengthOf|true|false|null|undefined|exist|ok|empty)\b"
    r"\s*(\()?",
)


def _js_assertion_preds(text: str) -> tuple[list[Pred], int]:
    preds: list[Pred] = []
    count = 0
    for m in re.finditer(r"(?<![\w.])expect\s*\(", text):
        end = _js_balanced(text, m.end() - 1, "(", ")")
        if end < 0:
            continue
        subject = text[m.end():end - 1]
        rest = text[end:end + 400]
        mm = MATCHER_RE.match(rest)
        if not mm:
            continue
        count += 1
        chain, matcher = mm.group(1), mm.group(2)
        negated = bool(re.search(r"\bnot\b", chain))
        args: list[str] = []
        if mm.group(3):
            aend = _js_balanced(rest, mm.end() - 1, "(", ")")
            if aend > 0:
                args = _js_split_args(rest[mm.end():aend - 1])
        snippet = f"expect({subject}){'.not' if negated else ''}.{matcher}({', '.join(args)})"
        holds = not negated
        low = matcher.lower()
        if low in ("tobe", "toequal", "tostrictequal", "equal", "equals", "eql"):
            if args and re.fullmatch(r"true|false|null|undefined", args[0].strip()):
                tok = {"true": "True", "false": "False"}.get(args[0].strip(), "None")
                preds.append(_js_pred(snippet, "is", holds, subject, values_extra=[tok]))
            else:
                preds.append(_js_pred(snippet, "eq", holds, subject, *args))
        elif low in ("tocontain", "tocontainequal", "toinclude", "include", "includes", "contain", "members"):
            p = _js_pred(snippet, "in", holds, subject, *args)
            p.names = [n for n in _js_facts(args[0]).names] if args else []
            preds.append(p)
        elif low in ("tohaveproperty", "property"):
            p = _js_pred(snippet, "eq" if len(args) > 1 else "truthy", holds, *(args[1:] if len(args) > 1 else []))
            if args:
                key = args[0].strip().strip("\"'`")
                p.fields.add(key)
            p.fields |= _js_facts(subject).fields
            preds.append(p)
        elif low in ("tobenull", "tobeundefined", "null", "undefined"):
            preds.append(_js_pred(snippet, "is", holds, subject, values_extra=["None"]))
        elif low in ("tobetruthy", "true", "ok", "exist", "tobedefined"):
            preds.append(_js_pred(snippet, "is", holds, subject, values_extra=["True"]))
        elif low in ("tobefalsy", "false"):
            preds.append(_js_pred(snippet, "is", holds, subject, values_extra=["False"]))
        elif low in ("tohavelength", "lengthof", "empty"):
            is_zero = low == "empty" or (args and args[0].strip() == "0")
            preds.append(_js_pred(snippet, "empty" if is_zero else "cmp", holds, subject))
        else:
            preds.append(_js_pred(snippet, "eq", holds, subject, *args))

    for m in re.finditer(r"(?<![\w.])assert\s*\.\s*(\w+)\s*\(", text):
        end = _js_balanced(text, m.end() - 1, "(", ")")
        if end < 0:
            continue
        kind = m.group(1)
        args = _js_split_args(text[m.end():end - 1])
        count += 1
        snippet = f"assert.{kind}({', '.join(args)})"
        negated = kind.lower().startswith("not") or kind.lower() in ("doesnotmatch", "doesnotinclude")
        if kind.lower() in ("equal", "strictequal", "deepequal", "deepstrictequal", "notequal",
                            "notstrictequal", "notdeepequal") and len(args) >= 2:
            if re.fullmatch(r"true|false|null|undefined", args[1].strip()):
                tok = {"true": "True", "false": "False"}.get(args[1].strip(), "None")
                preds.append(_js_pred(snippet, "is", not negated, args[0], values_extra=[tok]))
            else:
                preds.append(_js_pred(snippet, "eq", not negated, args[0], args[1]))
        elif kind.lower() in ("ok", "istrue") and args:
            preds.append(_js_pred(snippet, "is", True, args[0], values_extra=["True"]))
        elif kind.lower() in ("includes", "include") and len(args) >= 2:
            preds.append(_js_pred(snippet, "in", True, args[0], args[1]))
        else:
            preds.append(Pred(snippet[:300], "opaque", True, via="unsupported assert method"))

    for m in re.finditer(r"(?<![\w.])assert\s*\(", text):
        end = _js_balanced(text, m.end() - 1, "(", ")")
        if end < 0:
            continue
        args = _js_split_args(text[m.end():end - 1])
        if not args:
            continue
        count += 1
        preds.extend(_js_cond_preds(args[0], True))
    return preds, count


def _js_cond_preds(cond: str, holds: bool) -> list[Pred]:
    cond = cond.strip()
    while cond.startswith("(") and cond.endswith(")") and _js_balanced(cond, 0, "(", ")") == len(cond):
        cond = cond[1:-1].strip()
    if cond.startswith("!") and not cond.startswith("!="):
        return _js_cond_preds(cond[1:], not holds)
    parts = re.split(r"\s*(?:&&|\|\|)\s*", cond)
    if len(parts) > 1:
        out: list[Pred] = []
        for part in parts:
            out.extend(_js_cond_preds(part, holds))
        return out
    m = re.match(r"^(.*?)\s*(===|!==|==|!=)\s*(.*)$", cond)
    if m:
        left, op, right = m.groups()
        stated = (op in ("===", "==")) == holds
        if re.fullmatch(r"true|false|null|undefined", right.strip()):
            tok = {"true": "True", "false": "False"}.get(right.strip(), "None")
            return [_js_pred(cond, "is", stated, left, values_extra=[tok])]
        return [_js_pred(cond, "eq", stated, left, right)]
    m = re.match(r"^(.*?)\.(?:includes|has)\((.*)\)$", cond)
    if m:
        return [_js_pred(cond, "in", holds, m.group(1), m.group(2))]
    f = _js_facts(cond)
    if f.fields or f.literals:
        return [_js_pred(cond, "truthy", holds, cond)]
    return [Pred(cond[:300], "opaque", holds, via="unsupported JS condition")]


def extract_js_assertions(path: Path) -> list[VerifierAssertion]:
    source = read(path)
    funcs = _js_function_index(source)
    assertions: list[VerifierAssertion] = []
    for label, body in _js_test_blocks(source):
        hints: list[str] = []
        blocks = _js_expand(body, funcs, 0, frozenset(), hints)
        preds: list[Pred] = []
        count = 0
        evidence: list[str] = []
        for blk in blocks:
            p, c = _js_assertion_preds(blk)
            preds.extend(p)
            count += c
            evidence.extend(x.text for x in p)
        a = VerifierAssertion(label=label, source_file=str(path), source_kind="javascript")
        a.raw_evidence = _dedupe_strs(evidence, 40)
        a.secondary_evidence = _dedupe_strs(
            re.findall(r"""["'`]([^"'`]{2,120})["'`]""", body)[:30] + hints, 60,
        )
        a.entities |= {h for h in hints if _non_generic(h)}
        _apply_preds(a, preds)
        if count == 0:
            calls = {
                c for c in re.findall(r"(?<![\w.])([A-Za-z_$][\w$]*)\s*\(", body)
                if c not in JS_IGNORED_CALLS and c not in funcs
            }
            a.unresolved = sorted(f"{c}()" for c in calls)[:8]
            a.diagnostics.append("reason=no recognised assertion idiom in test body")
        results = a
        assertions.append(results)
    return assertions


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------

JSON_ACTION_KEYS = {"action", "operation", "op"}
JSON_OBJECT_KEYS = {"target", "object", "type", "resource", "kind"}
JSON_ENTITY_KEYS = {"entity", "subject", "user", "account", "name_of", "principal"}
JSON_EXPECTED_KEYS = {"expected", "state", "value", "expected_value", "expected_state", "equals"}
JSON_FIELD_KEYS = {"field", "path", "key", "attribute", "property"}
JSON_TEXT_KEYS = {"condition", "description", "message", "reason", "actual"}


def _json_leaves(value, prefix: str = "") -> list[tuple[str, object]]:
    out: list[tuple[str, object]] = []
    if isinstance(value, dict):
        for k, v in value.items():
            out.extend(_json_leaves(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            out.extend(_json_leaves(v, f"{prefix}[{i}]"))
    else:
        out.append((prefix, value))
    return out


def _json_assertion_from_dict(label: str, value: dict, path: str, kind: str) -> VerifierAssertion:
    a = VerifierAssertion(label=label, source_file=path, source_kind=kind)
    a.raw_evidence = [json.dumps(value, ensure_ascii=False, sort_keys=True)[:600]]
    structured: list[str] = []
    hints: list[str] = []
    preds: list[Pred] = []
    explicit_actions: set[str] = set()

    def handle(key: str, val, prefix: str = "") -> None:
        k = key.lower()
        if isinstance(val, dict):
            if k in JSON_EXPECTED_KEYS or k in JSON_FIELD_KEYS or k == "actual":
                for leaf_path, leaf in _json_leaves(val):
                    fld = re.split(r"[.\[]", leaf_path)[-1].rstrip("]") or leaf_path
                    emit_eq(fld, leaf, leaf_path)
            else:
                for sub_k, sub_v in val.items():
                    handle(str(sub_k), sub_v, f"{prefix}{key}.")
            return
        if isinstance(val, list):
            for item in val:
                handle(key, item, prefix)
            return
        text = "" if val is None else str(val)
        if k in JSON_ACTION_KEYS:
            explicit_actions.update(verifier_actions(_split_words(text)) | verifier_actions(text.lower()))
            structured.append(text)
        elif k in JSON_OBJECT_KEYS:
            a.secondary_evidence.append(text)
            structured.append(text) if k != "type" else None
        elif k in JSON_ENTITY_KEYS:
            if _non_generic(text):
                a.entities.add(text)
            a.secondary_evidence.append(text)
        elif k in JSON_FIELD_KEYS:
            a.fields.add(text)
            structured.append(text)
        elif k in JSON_EXPECTED_KEYS:
            emit_eq("", val, key)
        elif k in JSON_TEXT_KEYS:
            a.hint_text += " " + text
        elif k == "name":
            pass
        else:
            a.secondary_evidence.append(text) if isinstance(val, str) else None

    def emit_eq(fld: str, leaf, leaf_path: str) -> None:
        values = []
        literals = []
        if leaf is True:
            values = ["True"]
        elif leaf is False:
            values = ["False"]
        elif leaf is None:
            values = ["None"]
        else:
            literals = [str(leaf)]
        f = {fld} if fld else set()
        structured.append(_split_words(str(leaf_path) + " " + (str(leaf) if literals else "")))
        preds.append(Pred(f"{leaf_path} == {leaf}", "eq" if literals else "is", True, f, literals, values))

    for k, v in value.items():
        handle(str(k), v)

    action_words = " ".join(structured)
    a.structured_text = _split_words(action_words)
    # Fields given separately plus expected values -> one structured predicate.
    if a.fields and not preds:
        expected_vals = [
            v for kk, v in value.items()
            if str(kk).lower() in JSON_EXPECTED_KEYS and not isinstance(v, (dict, list))
        ]
        for ev in expected_vals:
            if ev is True or ev is False or ev is None:
                preds.append(Pred(f"{sorted(a.fields)} == {ev}", "is", True, set(a.fields), [], [repr(ev)]))
            else:
                preds.append(Pred(f"{sorted(a.fields)} == {ev}", "eq", True, set(a.fields), [str(ev)], []))
    _apply_preds(a, preds)
    for act in explicit_actions:
        a.actions.add(act)
        a.action_polarity[act] = "positive"
    if not (preds or structured or a.entities or a.hint_text.strip()):
        a.diagnostics.append("reason=JSON assertion without recognisable semantic keys")
    return a


def _walk_json_assertions(value, prefix: str = "", inside_named: bool = False) -> list[tuple[str, dict]]:
    found: list[tuple[str, dict]] = []
    semantic_keys = JSON_ACTION_KEYS | JSON_EXPECTED_KEYS | JSON_FIELD_KEYS | {"condition", "target", "object", "entity"}
    if isinstance(value, dict):
        name = value.get("name")
        named = isinstance(name, str) and bool(name.strip())
        has_sem = any(str(k).lower() in semantic_keys for k in value)
        if named:
            found.append((name.strip(), value))
        elif has_sem and not inside_named:
            label = value.get("description") if isinstance(value.get("description"), str) else None
            label = label or ":".join(str(value[k]) for k in ("action", "target", "object") if k in value) or prefix or "assertion"
            found.append((str(label)[:120], value))
        for key, child in value.items():
            found.extend(_walk_json_assertions(child, f"{prefix}.{key}" if prefix else str(key), inside_named or named))
    elif isinstance(value, list):
        for i, child in enumerate(value):
            found.extend(_walk_json_assertions(child, f"{prefix}[{i}]", inside_named))
    return found


def extract_json_assertions(path: Path) -> list[VerifierAssertion]:
    try:
        data = json.loads(read(path))
    except json.JSONDecodeError:
        return []
    return [_json_assertion_from_dict(label, value, str(path), "json") for label, value in _walk_json_assertions(data)]


def _flatten_expected_state(value, prefix: str = "") -> list[tuple[str, str]]:
    result = []
    if isinstance(value, dict):
        for key, child in value.items():
            result.extend(_flatten_expected_state(child, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(value, list):
        for i, child in enumerate(value):
            result.extend(_flatten_expected_state(child, f"{prefix}[{i}]"))
    else:
        result.append((prefix, json.dumps(value, ensure_ascii=False)))
    return result


def extract_expected_state_assertions(path: Path) -> list[VerifierAssertion]:
    try:
        data = json.loads(read(path))
    except json.JSONDecodeError:
        return []
    assertions = []
    for label, raw_value in _flatten_expected_state(data):
        a = VerifierAssertion(label=label, source_file=str(path), source_kind="expected_state")
        a.raw_evidence = [f"{label} == {raw_value}"]
        try:
            val = json.loads(raw_value)
        except json.JSONDecodeError:
            val = raw_value
        leaf_field = re.split(r"[.\[]", label)[-1].rstrip("]") or label
        a.structured_text = _split_words(label + (f" {val}" if isinstance(val, str) else ""))
        a.secondary_evidence = [_split_words(label)]
        if val is True or val is False or val is None:
            pred = Pred(a.raw_evidence[0], "is", True, {leaf_field, label}, [], [repr(val)])
        else:
            pred = Pred(a.raw_evidence[0], "eq", True, {leaf_field, label}, [str(val)], [])
        _apply_preds(a, [pred])
        assertions.append(a)
    return assertions


def extract_verifier_assertions(path: Path) -> list[VerifierAssertion]:
    if path.name == "assertions.json":
        return extract_json_assertions(path)
    if path.name == "expected_state.json":
        return extract_expected_state_assertions(path)
    if path.suffix == ".py":
        return extract_python_assertions(path)
    if path.suffix == ".js":
        return extract_js_assertions(path)
    return []


# ---------------------------------------------------------------------------
# Semantic normalization
# ---------------------------------------------------------------------------


ACTION_GROUPS = {
    "suspend": {"suspend", "suspended", "suspension", "suspending"},
    "deactivate": {"deactivate", "deactivated", "deactivation", "deactivating", "disable", "disabling"},
    "revoke": {"revoke", "revoked", "revocation", "revoking"},
    "remove": {"remove", "removed", "removal", "removing"},
    "delete": {"delete", "deleted", "deletion", "deleting"},
    "reset": {"reset", "resetting", "credential", "credentials"},
    "move": {"move", "moved", "moving", "place", "placed"},
    "preserve": {"preserve", "preserved", "preserving", "retain", "retained", "keep", "kept"},
    "remain": {"remain", "remains", "remaining", "unchanged", "stay", "stays"},
    "assign": {"assign", "assigned", "assignment", "reassign", "reassigned"},
    "record": {"record", "recorded", "reason", "document", "documented"},
    "hold": {"hold", "on-hold", "onhold"},
    "escalate": {"escalate", "escalated", "escalation"},
    "restore": {
        "restore", "restored", "restoration", "reenable", "re-enabled",
        "reactivate", "reactivated", "re-enabling", "reactivating",
    },
    "deprovision": {
        "deprovision", "deprovisioned", "deprovisioning",
        "offboard", "offboarded", "offboarding",
    },
    "report": {"report", "reported", "reporting"},
    # Generic verifier actions. These describe state transitions commonly
    # represented by assertion keys/expected values; they are not task-specific.
    "block": {"block", "blocked", "blocking"},
    "close": {"close", "closed", "closing"},
    "create": {"create", "created", "creation", "creating"},
    "scan": {"scan", "scanned", "scanning"},
    "sync": {"sync", "synced", "synchronized", "synchronised", "resync", "resynced"},
    "audit": {"audit", "audited", "auditing"},
    "contain": {"contain", "contained", "containment"},
    "lift": {"lift", "lifted"},
    "change": {"change", "changed", "update", "updated", "modify", "modified"},
    "route": {"route", "routed", "routing"},
    "add": {"add", "added"},
    "complete": {"complete", "completed", "completion"},
    "archive": {"archive", "archived"},
}

ACTION_SYNONYMS = {
    variant: canonical
    for canonical, variants in ACTION_GROUPS.items()
    for variant in variants
}

ACTION_CONFLICTS = {
    "suspend": {"restore", "deactivate"},
    "deactivate": {"restore"},
    "revoke": {"restore"},
    "remove": {"preserve"},
    "delete": {"preserve"},
    "reset": {"preserve", "remain"},
    "restore": {"suspend", "deactivate", "revoke"},
}


OBJECT_GROUPS = {
    "google_account": {"google", "gmail"},
    "slack_account": {"slack"},
    "okta": {"okta"},
    "ticket": {"ticket", "incident", "case", "service desk"},
    "token": {"token", "tokens", "api token", "api-token"},
    "third_party_app": {"third-party app", "third party app", "connector"},
    "legal_hold": {"legal hold", "legal-hold", "litigation hold"},
    "organizational_unit": {"organizational unit", "ou"},
    "group": {"group", "groups"},
    "mfa_factor": {"mfa factor", "mfa factors", "sign-in factor", "sign in factor"},
    "mailbox": {"mailbox", "delegate", "forwarding filter"},
    "service_account": {"service account", "service-account"},
    "oauth_app": {"oauth app", "oauth connector", "oauth application"},
    "app": {"app", "application"},
    "privileged_role": {
        "super administrator", "superadmin", "super admin",
        "privileged admin", "privileged administrator",
    },
    "contractor": {"contractor", "contractors"},
    "security": {"security"},
    "access_sync": {"access-sync", "access sync"},
    "user_account": {"account", "accounts", "user account", "user accounts"},
    # Generic enterprise/ITSM categories useful when they appear directly in
    # verifier assertions or expected-state keys.
    "malware_hash": {"malware hash", "file hash", "hash blocked", "hash_blocked", "sha256", "sha-256"},
    "device": {"device", "laptop", "endpoint", "asset"},
    "detection": {"detection", "alert"},
    "problem_record": {"problem record", "problem_record", "root cause"},
    "responder": {"responder", "assignee", "assigned to"},
    "device_management": {"device management", "intune", "management"},
    "asset": {"asset", "inventory"},
    "scan": {"scan", "defender scan"},
    "certificate": {"certificate", "cert", "certificates"},
    "license": {"license", "licenses", "seat", "seats"},
    "firewall": {"firewall", "firewall rule", "firewall_rule"},
    "server": {"server", "host", "hosts"},
    "route": {"route", "routing"},
    "approval": {"approval", "approvals"},
    "change_record": {"change record", "change_record"},
    "ci": {"configuration item", "cmdb", "ci"},
    "ownership": {"owner", "ownership"},
}


def canonical_actions(text: str) -> set[str]:
    lowered = text.lower()
    relevant_parts = re.split(
        r"\b(?:because|although|though|while|whereas|after|before|once|when|"
        r"that|which|who|whom|whose|instead of|rather than)\b",
        lowered,
        maxsplit=1,
    )
    primary_text = relevant_parts[0]
    tokens = normalize_tokens(primary_text)
    actions = {ACTION_SYNONYMS[token] for token in tokens if token in ACTION_SYNONYMS}

    # Multi-token/state-key forms.
    patterns = {
        "hold": r"\bon[- ]?hold\b|\bput .* on hold\b",
        "assign": r"\breassign(?:ed|ing)?\b",
        "remain": r"\bleave .* unchanged\b|\bstatus_remains_\w+\b|\bremains_\w+\b",
        "deprovision": r"\bdeprovision(?:ed|ing)?\b|\boffboard(?:ed|ing)?\b",
        "restore": r"\bre[- ]?enable\b|\breactivat(?:e|ed|ing)\b",
        "block": r"\b(?:hash|token|account|file)_?block(?:ed|ing)?\b",
        "scan": r"\bscan_?(?:ran|run|completed|complete|executed)\b",
        "sync": r"\b(?:re)?sync(?:ed|hronized|hronised)?\b",
        "contain": r"\bcontain(?:ed|ment)?\b",
        "lift": r"\blifted\b",
        "close": r"\bclosed\b",
        "create": r"\bcreated\b|\bcreation\b",
        "audit": r"\baudited\b",
        "route": r"\brouted\b",
    }
    for action, pattern in patterns.items():
        if re.search(pattern, primary_text):
            actions.add(action)

    return actions


def object_groups(text: str) -> set[str]:
    lowered = text.lower()
    groups: set[str] = set()

    if re.search(r"\bservice[- ]account\b", lowered):
        groups.add("service_account")
    elif re.search(r"\b(?:user )?accounts?\b", lowered):
        groups.add("user_account")

    if "google" in lowered and ("account" in lowered or "gmail" in lowered):
        groups.add("google_account")
    if "slack account" in lowered:
        groups.add("slack_account")
    if "okta" in lowered:
        groups.add("okta")
    if re.search(r"\bmfa (?:factor|factors)\b", lowered) or "sign-in factor" in lowered or "sign in factor" in lowered:
        groups.add("mfa_factor")
    if "third-party app" in lowered or "third party app" in lowered:
        groups.add("third_party_app")
    if "token" in lowered or "api token" in lowered or "api-token" in lowered:
        groups.add("token")
    if "legal-hold" in lowered or "legal hold" in lowered or "litigation hold" in lowered:
        groups.add("legal_hold")
    if "organizational unit" in lowered or re.search(r"\blegal[- ]hold\s+ou\b", lowered):
        groups.add("organizational_unit")
    if re.search(r"\blegal[- ]hold group\b", lowered):
        groups.add("group")
    if "ticket" in lowered or "incident" in lowered or "service desk" in lowered:
        groups.add("ticket")
    if "group" in lowered and ("reassign" in lowered or "route" in lowered):
        groups.add("group")
    if "mailbox" in lowered or "delegate" in lowered or "forwarding filter" in lowered:
        groups.add("mailbox")
    if "oauth app" in lowered or "oauth connector" in lowered or "oauth application" in lowered:
        groups.add("oauth_app")
    elif re.search(r"\b(?:app|application)\b", lowered):
        groups.add("app")
    if any(p in lowered for p in ("super administrator", "superadmin", "super admin", "privileged admin", "privileged administrator")):
        groups.add("privileged_role")
    if "contractor" in lowered:
        groups.add("contractor")
    if "security" in lowered:
        groups.add("security")
    if "access-sync" in lowered or "access sync" in lowered:
        groups.add("access_sync")

    # Generic technical objects.
    if any(p in lowered for p in ("malware hash", "file hash", "hash_blocked", "hash blocked", "sha256", "sha-256")):
        groups.add("malware_hash")
    if any(p in lowered for p in ("device", "laptop", "endpoint")):
        groups.add("device")
    if "asset" in lowered:
        groups.add("asset")
    if "detection" in lowered or "alert" in lowered:
        groups.add("detection")
    if "problem record" in lowered or "problem_record" in lowered or "root cause" in lowered:
        groups.add("problem_record")
    if "responder" in lowered or "assignee" in lowered or "assigned_to" in lowered:
        groups.add("responder")
    if "intune" in lowered or "device management" in lowered:
        groups.add("device_management")

    return groups


def has_broad_scope(text: str) -> bool:
    lowered = text.lower()
    return bool(
        re.search(
            r"\b(?:all|every|each|both|five|eight|three|two|those|these|the accounts|"
            r"those accounts|these accounts)\b",
            lowered,
        )
    )


def is_negated_action(text: str, action: str) -> bool:
    lowered = text.lower()
    patterns = {
        "reset": [r"\bdo not reset\b", r"\bdon't reset\b", r"\bwithout .*reset\b", r"\bnever .*reset\b"],
        "delete": [r"\bdo not delete\b", r"\bdon't delete\b", r"\bwithout .*delete\b", r"\bnever .*delete\b"],
        "remove": [r"\bdo not remove\b", r"\bdon't remove\b", r"\bwithout .*remove\b", r"\bnever .*remove\b"],
        "restore": [r"\bdo not restore\b", r"\bdon't restore\b", r"\bwithout .*restore\b", r"\bnever .*restore\b"],
        "deactivate": [r"\bdo not deactivate\b", r"\bdon't deactivate\b"],
        "revoke": [r"\bdo not revoke\b", r"\bdon't revoke\b"],
        "suspend": [r"\bdo not suspend\b", r"\bdon't suspend\b"],
    }
    return any(re.search(pattern, lowered) for pattern in patterns.get(action, []))




def verifier_actions(text: str) -> set[str]:
    """Generic action vocabulary only. No test-name or task-specific tables."""
    return canonical_actions(text.lower())


def verifier_objects(text: str) -> set[str]:
    """Generic object vocabulary detected in already word-split text."""
    lowered = text.lower()
    objects = object_groups(lowered)
    if "access-sync" in lowered or "access_sync" in lowered or "access sync" in lowered:
        objects.add("access_sync")
    if re.search(r"\btokens?\b|\bapitoken\b", lowered):
        objects.add("token")
    if re.search(r"service[- _]accounts?", lowered):
        objects.add("service_account")
    if "contractor" in lowered:
        objects.add("contractor")
    if re.search(r"privileged[- _]admin|super[- _]?admin", lowered):
        objects.add("privileged_role")
    if "oauth" in lowered and any(p in lowered for p in ("app", "connector", "application")):
        objects.add("oauth_app")
    if re.search(r"\bmailbox|\bdelegate|\bforwarding\b", lowered):
        objects.add("mailbox")
    if "incident" in lowered:
        objects.add("ticket")
    return objects


PRIORITY = [
    "suspend", "deactivate", "revoke", "remove", "delete", "reset",
    "restore", "deprovision", "assign", "record", "hold", "escalate",
    "report", "block", "close", "create", "scan", "sync", "audit",
    "contain", "lift", "route", "add", "complete", "archive", "change",
    "remain", "preserve",
]


def _extra_objects(lowered: str) -> set[str]:
    objects: set[str] = set()
    if re.search(r"\b(?:assigned(?!\s+licen)|assignee|responder)\b", lowered):
        objects.add("responder")
    if "root cause" in lowered:
        objects.add("problem_record")
    if "file hash" in lowered or "hash blocked" in lowered:
        objects.add("malware_hash")
    padded = f" {lowered} "
    patterns = {
        "certificate": ("certificate",),
        "license": ("license", "licenses", "seat", "seats"),
        "firewall": ("firewall",),
        "server": ("server", "host"),
        "route": ("route", "routing"),
        "approval": ("approval",),
        "change_record": ("change record",),
        "ci": ("configuration item", " ci ", "cmdb"),
        "asset": ("asset",),
        "ownership": ("owner", "ownership"),
    }
    for group, terms in patterns.items():
        if any(term in padded for term in terms):
            objects.add(group)
    return objects


def normalize_verifier_assertion(a: VerifierAssertion) -> VerifierAssertion:
    """Evidence hierarchy: predicate > structured metadata > messages/description
    > helper bodies > test/function name (label only as a last, non-matchable resort)."""
    body_text = _split_words(
        a.predicate_text() + " " + a.structured_text + " " + a.hint_text + " " + " ".join(a.secondary_evidence)
    )
    label_text = _split_words(a.label)
    has_struct = a.structured_predicates > 0 and bool(
        a.fields or a.expected_values or a.forbidden_values or a.states or a.relations
    )

    # ---- objects: body evidence only; label only when there is no body evidence ----
    objects = set(a.objects) | verifier_objects(body_text) | _extra_objects(body_text)
    if re.search(r"\b(?:users?|employees?|staff|accounts?)\b", body_text):
        objects.add("user_account")
    object_source = "body" if objects else "none"
    if not objects and not has_struct:
        label_objs = verifier_objects(label_text) | _extra_objects(label_text)
        if label_objs:
            objects = label_objs
            object_source = "label"
    a.objects = objects

    # ---- actions ----
    established = set(a.actions)
    implied = set(a.implied_actions)
    if a.structured_text:
        implied |= verifier_actions(a.structured_text) - established
        for act in implied:
            a.implied_polarity.setdefault(act, "positive")
    if a.ambiguity:
        # Ambiguous control flow: nothing is established, everything is only implied.
        for act in established:
            implied.add(act)
            a.implied_polarity[act] = a.action_polarity.get(act, "positive")
        established = set()
        a.action_polarity = {}
    label_actions: set[str] = set()
    if not established and not implied and not has_struct:
        label_actions = verifier_actions(label_text)

    a.actions = established
    a.implied_actions = implied - established
    for act in a.actions:
        a.action_polarity.setdefault(act, "positive")

    if established:
        a.action_source = "structured" if a.source_kind == "json" and not has_struct else "predicate"
    elif implied:
        a.action_source = "implied-state"
    elif label_actions:
        a.action_source = "label"
        a.actions = label_actions
        for act in label_actions:
            a.action_polarity.setdefault(act, "positive")
    else:
        a.action_source = "none"

    pos = a.actions_with("positive")
    neg = a.actions_with("negative")
    ordered = [x for x in PRIORITY if x in pos] + sorted(pos - set(PRIORITY))
    ordered_neg = [x for x in PRIORITY if x in neg] + sorted(neg - set(PRIORITY))
    a.action = (ordered or ordered_neg or [None])[0] if a.action_source in ("predicate", "structured") else None
    a.polarity = "negative" if (neg and not pos) else "positive"
    if not a.kind:
        a.kind = "action" if established else ("state" if has_struct else "")

    a.provenance = {
        "action": a.action_source,
        "objects": object_source,
        "entities": "predicate/literal" if a.entities else "none",
    }

    # ---- status ----
    reason = None
    structured_ok = has_struct or (a.source_kind == "json" and bool(established))
    if a.ambiguity:
        a.extraction_status = "partial"
        reason = "ambiguous semantics: " + "; ".join(a.ambiguity[:3])
    elif a.action_source in ("predicate", "structured") and a.objects and structured_ok:
        a.extraction_status = "extracted"
    elif a.action_source == "label":
        a.extraction_status = "partial"
        reason = "action inferred from test label only; no structured predicate recovered"
    elif structured_ok and (implied or a.states or a.relations):
        a.extraction_status = "partial"
        if a.unresolved:
            reason = "helper predicate partially resolved"
        elif not a.objects:
            reason = "state predicate recovered but no object could be identified"
        else:
            reason = "state/relation assertion; action not independently established"
    elif structured_ok or a.actions or a.objects:
        a.extraction_status = "partial"
        reason = "helper predicate partially resolved" if a.unresolved else "predicate semantics incomplete"
    else:
        a.extraction_status = "unextracted"
        if not any(d.startswith("reason=") for d in a.diagnostics):
            reason = "unsupported verifier construct" if (a.predicates or a.raw_evidence) else "no recoverable semantics"
    if reason and not any(d.startswith("reason=") for d in a.diagnostics):
        a.diagnostics.insert(0, f"reason={reason}")
    elif a.extraction_status == "extracted":
        a.diagnostics = [d for d in a.diagnostics if not d.startswith("reason=")]
    return a


def extract_task_verifiers(task: Path) -> list[VerifierAssertion]:
    assertions: list[VerifierAssertion] = []
    for path in verifier_files(task):
        assertions.extend(extract_verifier_assertions(path))
    return _dedupe_assertions([normalize_verifier_assertion(item) for item in assertions])


# ---------------------------------------------------------------------------
# Requirement/assertion coverage engine
# ---------------------------------------------------------------------------


def action_compatible(doc_actions: set[str], verifier_actions_set: set[str]) -> bool:
    if not doc_actions or not verifier_actions_set:
        return False
    if doc_actions.intersection(verifier_actions_set):
        return True

    equivalences = {
        ("suspend", "deactivate"),
        ("deactivate", "suspend"),
        ("remove", "revoke"),
        ("revoke", "remove"),
        ("delete", "remove"),
        ("remove", "delete"),
        ("report", "escalate"),
        ("escalate", "report"),
        ("preserve", "remain"),
        ("remain", "preserve"),
        ("restore", "active"),
        # These represent the same concrete end state when a verifier uses
        # either wording.
        ("block", "contain"),
        ("contain", "block"),
    }
    return any(
        (doc, check) in equivalences
        for doc in doc_actions
        for check in verifier_actions_set
    )


def _specificity_compatible(doc_objects: set[str], verifier_objects_set: set[str]) -> bool:
    if not doc_objects or not verifier_objects_set:
        return False

    distinguishing = {
        "service_account", "contractor", "oauth_app", "access_sync",
        "mailbox", "privileged_role", "mfa_factor", "legal_hold",
        "organizational_unit", "malware_hash", "problem_record",
        "certificate", "license", "firewall", "server", "approval",
        "change_record", "ci", "ownership",
    }
    verifier_specific = verifier_objects_set.intersection(distinguishing)
    doc_specific = doc_objects.intersection(distinguishing)

    if verifier_specific:
        if verifier_specific == {"oauth_app"} and ({"app", "access_sync"} & doc_objects):
            pass
        elif not verifier_specific.issubset(doc_specific):
            return False

    if doc_specific and verifier_specific and not doc_specific.intersection(verifier_specific):
        if not (verifier_specific == {"oauth_app"} and {"app", "access_sync"} & doc_specific):
            return False

    if doc_specific and not doc_specific.intersection(verifier_objects_set):
        if not ("app" in doc_objects and "oauth_app" in verifier_objects_set):
            return False

    return True


def object_compatible(
    doc_objects: set[str],
    verifier_objects_set: set[str],
    broad: bool = False,
) -> bool:
    if not doc_objects or not verifier_objects_set:
        return False
    if not _specificity_compatible(doc_objects, verifier_objects_set):
        return False
    if doc_objects.intersection(verifier_objects_set):
        return True
    if "app" in doc_objects and "oauth_app" in verifier_objects_set:
        return True
    if {"legal_hold", "organizational_unit"}.issubset(doc_objects) and {
        "legal_hold", "organizational_unit"
    }.issubset(verifier_objects_set):
        return True
    if broad and "user_account" in doc_objects and "user_account" in verifier_objects_set:
        return True
    if broad and "token" in doc_objects and "token" in verifier_objects_set:
        return True
    return False



@dataclass
class Requirement:
    """Documentation-side IR mirroring the VerifierAssertion dimensions."""

    text: str
    actions: set[str] = field(default_factory=set)
    objects: set[str] = field(default_factory=set)
    entities: set[str] = field(default_factory=set)
    qualifiers: set[str] = field(default_factory=set)
    states: set[str] = field(default_factory=set)
    broad: bool = False
    # positive | negative ("do not X") | preserve ("leave X unchanged")
    polarity: str = "positive"


NEGATORS = r"(?:do not|don't|dont|never|must not|should not|shall not|cannot|can't|without)"
PRESERVE_RE = re.compile(
    r"\b(?:leave|keep|preserve|retain|maintain)\b.*\b(?:unchanged|as[- ]is|intact|untouched|active|enabled|in place)\b"
    r"|\b(?:preserve|retain|remain|remains|unchanged|untouched)\b",
    re.IGNORECASE,
)
CONTEXT_START = re.compile(
    r"^(?:first,?\s+|then,?\s+|next,?\s+|finally,?\s+)?"
    r"(?:read|review|compare|inspect|examine|investigate|look|identify|determine|find|list|"
    r"consult|open|check|note|understand|analy[sz]e|the .{1,60}? (?:is|are|was|were|has|have) (?:currently|already)\b)",
    re.IGNORECASE,
)


def _negated_actions(lowered: str) -> set[str]:
    actions: set[str] = set()
    for m in re.finditer(NEGATORS + r"\s+((?:[\w'-]+\s+){0,3}?)([\w-]+)", lowered):
        window = (m.group(1) + m.group(2)).split()
        for word in window:
            word = word.strip(",.;:")
            if word in ACTION_SYNONYMS:
                actions.add(ACTION_SYNONYMS[word])
                break
    return actions


def parse_requirement(clause: str) -> Requirement:
    text = re.sub(r"\s+", " ", clause).strip()
    lowered = text.lower()
    req = Requirement(text=text, broad=has_broad_scope(text))
    req.objects = object_groups(text) | _extra_objects(_split_words(text))
    if re.search(r"\b(?:employees?|staff|users?|people|person|member|members)\b", lowered):
        req.objects.add("user_account")
    req.entities = {m for m in re.findall(r"\b[A-Z][a-z]{2,}\b", text)[1:] if m.lower() not in GENERIC_ENTITY_WORDS}
    if re.search(NEGATORS, lowered):
        negated = _negated_actions(lowered)
        if negated:
            req.polarity = "negative"
            req.actions = negated
            return req
    if PRESERVE_RE.search(lowered):
        req.polarity = "preserve"
        req.actions = {"preserve", "remain"}
        return req
    req.actions = canonical_actions(text)
    return req


def _is_action_bearing_requirement(text: str) -> bool:
    lowered = text.lower().strip()
    if CONTEXT_START.match(lowered):
        return False
    req = parse_requirement(text)
    if not req.actions:
        return False
    if re.match(r"^(?:two|three|four|five|several|multiple) actions? (?:are|is) required", lowered):
        return False
    if lowered.startswith((
        "first, contain", "second, list", "use the ", "these accounts ",
        "they have ", "they only have ",
    )):
        return False
    return True


def _requirement_clauses(requirement: str) -> list[str]:
    text = re.sub(r"\s+", " ", requirement).strip()
    if not text:
        return []
    verbs = (
        r"suspend|move|force|revoke|deactivate|preserve|leave|reassign|record|put|delete|remove|"
        r"restore|re-enable|reactivate|escalate|deprovision|offboard|create|archive|close|change|"
        r"add|confirm|report|complete|block|scan|sync|audit|contain|lift|route|keep|retain|do not|don't|never"
    )
    clauses: list[str] = []
    for sentence in split_sentences(text):
        sentence = re.sub(r"^(?:also|additionally|then|finally),?\s+", "", sentence, flags=re.IGNORECASE)
        # "A, while leaving C unchanged" -> "A" + "Leave C unchanged"
        m = re.match(r"^(.*?),?\s+while\s+(leaving|keeping|preserving|retaining)\s+(.*)$", sentence, flags=re.IGNORECASE)
        extra = None
        if m:
            sentence = m.group(1)
            verb = {"leaving": "Leave", "keeping": "Keep", "preserving": "Preserve", "retaining": "Retain"}[m.group(2).lower()]
            extra = f"{verb} {m.group(3)}"
        for semi in re.split(r";\s+", sentence):
            parts = re.split(
                rf"(?:,\s*(?:and\s+)?then\s+|\s+and then\s+|\s+and also\s+|,\s+and\s+|\s+and\s+)(?=(?:{verbs})\b)",
                semi, flags=re.IGNORECASE,
            )
            clauses.extend(p.strip(" ,") for p in parts if p.strip(" ,"))
        if extra:
            clauses.append(extra)
    return clauses


EXACT, STRONG, WEAK, NONE = "EXACT", "STRONG", "WEAK", "NONE"
_RANK = {EXACT: 3, STRONG: 2, WEAK: 1, NONE: 0}

# Related-but-not-equivalent actions: only ever WEAK (-> WARN, never OK).
WEAK_EQUIVALENCES = {
    ("suspend", "deactivate"), ("deactivate", "suspend"),
    ("remove", "revoke"), ("revoke", "remove"),
    ("delete", "remove"), ("remove", "delete"),
    ("report", "escalate"), ("escalate", "report"),
    ("block", "contain"), ("contain", "block"),
}
CHANGING = {"remove", "delete", "reset", "suspend", "deactivate", "revoke", "block", "close"}
SYSTEM_WORDS = {
    "slack", "okta", "google", "jira", "servicenow", "microsoft", "azure", "intune",
    "defender", "gmail", "oauth", "mfa", "api", "workspace", "admin", "super",
}


def _action_relation(req: Requirement, a: VerifierAssertion) -> tuple[str, str]:
    P, N = a.actions_with("positive"), a.actions_with("negative")
    IP, IN = a.implied_with("positive"), a.implied_with("negative")
    if a.action_source == "label":
        return NONE, ""
    preserve_set = {"preserve", "remain"}
    if req.polarity == "positive":
        if req.actions & P:
            return EXACT, "action established by predicate"
        if (P | IP) and (P | IP) <= preserve_set and req.actions & CHANGING:
            return NONE, ""
        if req.actions & IP:
            return WEAK, "state agrees; action not independently established"
        if any((d, c) in WEAK_EQUIVALENCES for d in req.actions for c in (P | IP)):
            return WEAK, "related but non-equivalent action"
        return NONE, ""
    if req.polarity == "negative":
        if req.actions & N:
            return EXACT, "negative assertion on the same action"
        if P & preserve_set:
            return STRONG, "preservation assertion"
        if req.actions & IN or IP & preserve_set:
            return WEAK, "state agrees; action not independently established"
        return NONE, ""
    # preserve ("leave X unchanged")
    if P & preserve_set:
        return EXACT, "preservation assertion"
    if N:
        return WEAK, "negative assertion; preservation not established"
    if IP & preserve_set:
        return WEAK, "state agrees; preservation not independently established"
    return NONE, ""


def _assertion_usable(a: VerifierAssertion) -> bool:
    if a.extraction_status == "unextracted" or a.action_source in ("label", "none"):
        return False
    return bool(a.objects and (a.actions or a.implied_actions))


def match_detail(requirement: str, a: VerifierAssertion) -> tuple[str, list[str]]:
    """Best confidence over the requirement's clauses, with human-readable reasons."""
    if not _assertion_usable(a):
        return NONE, []
    best, best_reasons = NONE, []
    haystack = " ".join(
        [a.label, a.predicate_text(), " ".join(a.entities), " ".join(a.secondary_evidence)]
    ).lower()
    for clause in _requirement_clauses(requirement):
        if not _is_action_bearing_requirement(clause):
            continue
        req = parse_requirement(clause)
        if not req.actions:
            continue
        conf, why = _action_relation(req, a)
        if conf == NONE:
            continue
        broad_ok = (
            (req.broad and "user_account" in req.objects and "user_account" in a.objects)
            or (req.broad and "revoke" in req.actions and "token" in req.objects and "token" in a.objects)
        )
        if not (object_compatible(req.objects, a.objects, broad=req.broad) or broad_ok):
            continue
        reasons = [f"action: {why}", f"object: {', '.join(sorted(req.objects & a.objects)) or 'compatible'}"]
        names = {e for e in req.entities if e.lower() not in SYSTEM_WORDS}
        if names:
            if any(n.lower() in haystack for n in names):
                reasons.append(f"entity: {', '.join(sorted(n for n in names if n.lower() in haystack))}")
            elif a.entities:
                conf = WEAK  # named entity in docs does not appear in the verifier
                reasons.append("entity: documentation names an entity the verifier does not mention")
        if a.kind:
            reasons.append(f"predicate: {a.kind}")
        if _RANK[conf] > _RANK[best]:
            best, best_reasons = conf, reasons
    return best, best_reasons


def match_requirement(requirement: str, a: VerifierAssertion) -> bool:
    """True only for EXACT/STRONG matches; WEAK candidates are warnings."""
    return match_detail(requirement, a)[0] in (EXACT, STRONG)


# ---------------------------------------------------------------------------
# Documentation extraction
# ---------------------------------------------------------------------------


TARGET_HEADINGS = {
    "your goal",
    "what we expect the agent to do",
    "requirements",
    "expected behavior",
    "expected actions",
    "agent requirements",
    "ideal solution",
}


def clean_heading(text: str) -> str:
    text = re.sub(r"^#+\s*", "", text)
    return re.sub(r"\s+", " ", text.strip().lower())


def split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text.strip())
    if not text:
        return []
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", text)
    return [part.strip(" -\t") for part in parts if part.strip(" -\t")]


def documentation_requirements(
    docs: list[tuple[str, str]]
) -> list[tuple[str, str]]:
    requirements = []

    for filename, text in docs:
        lines = text.splitlines()
        current_heading = None
        section_lines: list[str] = []

        def flush_section():
            nonlocal section_lines
            if not current_heading:
                section_lines = []
                return

            paragraphs = []
            current_paragraph = []
            for raw in section_lines:
                line = raw.strip()
                if not line:
                    if current_paragraph:
                        paragraphs.append(" ".join(current_paragraph))
                        current_paragraph = []
                    continue
                current_paragraph.append(line)

            if current_paragraph:
                paragraphs.append(" ".join(current_paragraph))

            # Parse list items from the section as a whole so wrapped Markdown
            # remains one requirement.
            items = []
            collecting = False
            current_item = None

            for raw in section_lines:
                line = raw.strip()
                if not line:
                    continue

                marker = re.match(r"^(?:[-*+]|\d+[.)])\s+(.*)$", line)
                if marker:
                    if current_item:
                        items.append(current_item.strip())
                    current_item = marker.group(1).strip()
                    collecting = True
                elif collecting and current_item:
                    current_item += " " + line

            if current_item:
                items.append(current_item.strip())

            if items:
                for item in items:
                    requirements.append((filename, item))
            else:
                for paragraph in paragraphs:
                    for sentence in split_sentences(paragraph):
                        requirements.append((filename, sentence))

            section_lines = []

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#"):
                heading = clean_heading(stripped)
                if current_heading:
                    flush_section()
                current_heading = heading if heading in TARGET_HEADINGS else None
                continue
            if current_heading:
                section_lines.append(line)

        if current_heading:
            flush_section()

    seen = set()
    unique = []
    for item in requirements:
        key = (item[0], item[1])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique




# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


@dataclass
class TaskStats:
    failed: bool = False
    checks: int = 0
    extracted: int = 0
    partial: int = 0
    unextracted: int = 0
    requirements: int = 0
    matched: int = 0
    unmatched: int = 0
    weak_req: int = 0
    documented: int = 0
    unmapped: int = 0
    weak_check: int = 0
    review: int = 0
    reasons: list[str] = field(default_factory=list)


def _assertion_display(assertion: VerifierAssertion) -> str:
    return assertion.label


def _assertion_evidence_display(a: VerifierAssertion) -> str:
    parts = []
    if a.action:
        sign = "" if a.polarity == "positive" else " (negative)"
        parts.append(f"action={a.action}{sign}")
    if a.implied_actions:
        parts.append(f"implied_actions={','.join(sorted(a.implied_actions))}")
    if a.kind:
        parts.append(f"kind={a.kind}")
    if a.objects:
        parts.append(f"objects={','.join(sorted(a.objects))}")
    if a.entities:
        parts.append(f"entity={','.join(sorted(a.entities)[:4])}")
    if a.fields:
        parts.append(f"fields={','.join(sorted(a.fields)[:4])}")
    if a.states:
        parts.append(f"states={','.join(sorted(a.states))}")
    if a.forbidden_values:
        parts.append(f"forbidden={','.join(sorted(a.forbidden_values)[:4])}")
    if a.expected_values:
        parts.append(f"expected={','.join(sorted(a.expected_values)[:4])}")
    if a.relations:
        parts.append(f"relation={','.join(sorted(a.relations)[:4])}")
    if a.action_source != "none":
        parts.append(f"action_source={a.action_source}")
    if a.provenance.get("objects") not in (None, "none"):
        parts.append(f"object_source={a.provenance['objects']}")
    evidence = (a.raw_evidence or a.predicates or [""])[0]
    if evidence:
        parts.append(f"evidence={evidence[:237] + '...' if len(evidence) > 240 else evidence}")
    return " | ".join(parts)


def _location(a: VerifierAssertion) -> str:
    name = Path(a.source_file).name
    return f"{name}:{a.line}" if a.line else name


def _print_diagnostics(a: VerifierAssertion) -> None:
    reason = next((d[7:] for d in a.diagnostics if d.startswith("reason=")), None)
    if reason:
        print(f"       reason={reason}")
    if a.unresolved:
        print(f"       unresolved={', '.join(a.unresolved[:5])}")
    if a.ambiguity:
        print(f"       ambiguity={'; '.join(a.ambiguity[:3])}")
    evidence = (a.raw_evidence or a.predicates or [""])[0]
    if evidence:
        print(f"       evidence={evidence[:200]}")


def audit(task: Path, stats: TaskStats | None = None) -> int:
    stats = stats if stats is not None else TaskStats()
    print("ITSMBench Evaluation Audit")
    print("==========================")
    print(f"Task: {task.name}")
    print()
    print("Package")
    print("-------")
    if (task / "task.toml").exists():
        print("  [OK] task.toml")
    else:
        print("  [FAIL] task.toml missing")
        stats.failed = True
    verifiers = verifier_files(task)
    if verifiers:
        print(f"  [OK] {len(verifiers)} verifier file(s) found")
    else:
        print("  [FAIL] no known verifier files found")
        stats.failed = True
    print()

    print("Verifier inventory")
    print("------------------")
    assertions = extract_task_verifiers(task)
    stats.checks = len(assertions)
    if assertions:
        print(f"  {len(assertions)} semantic verifier check(s) detected")
        for a in assertions:
            status = a.extraction_status.upper()
            print(f"  [CHECK:{status}] {_assertion_display(a)}  ({_location(a)})")
            if a.extraction_status == "extracted":
                summary = _assertion_evidence_display(a)
                if summary:
                    print(f"       {summary}")
            else:
                summary = _assertion_evidence_display(a)
                if summary and a.extraction_status == "partial":
                    print(f"       {summary}")
                _print_diagnostics(a)
                reason = next((d[7:] for d in a.diagnostics if d.startswith("reason=")), "unknown")
                stats.reasons.append(reason)
    else:
        print("  [WARN] no semantic verifier checks detected")
    print()

    docs = documentation(task)
    requirements = documentation_requirements(docs)
    print("Explicit documentation requirements")
    print("-----------------------------------")
    if requirements:
        for filename, requirement in requirements:
            if not _is_action_bearing_requirement(requirement):
                print(f"  [INFO] [{filename}] {requirement}")
                print("       context/investigation prose; not treated as a required verifier action")
                continue
            stats.requirements += 1
            results = [(m, *match_detail(requirement, m)) for m in assertions]
            strong = [(m, c, why) for m, c, why in results if c in (EXACT, STRONG)]
            weak = [(m, c, why) for m, c, why in results if c == WEAK]
            if strong:
                stats.matched += 1
                print(f"  [OK] [{filename}] {requirement}")
                labels = [_assertion_display(m) for m, _, _ in strong]
                extra = f" (+{len(labels) - 5} more)" if len(labels) > 5 else ""
                print(f"       verifier: {', '.join(labels[:5])}{extra}")
                m0, c0, why0 = strong[0]
                print(f"       confidence={c0} | " + " | ".join(why0))
            elif weak:
                stats.weak_req += 1
                print(f"  [WARN] [{filename}] {requirement}")
                m0, c0, why0 = weak[0]
                print(f"       weak candidate (not counted as covered): {m0.label}")
                print("       " + " | ".join(why0))
            else:
                stats.unmatched += 1
                print(f"  [WARN] [{filename}] {requirement}")
                print("       no obvious matching extracted verifier semantics")
    else:
        print("  [WARN] no explicit documentation requirements detected")
    print()

    print("Verifier -> documentation review")
    print("--------------------------------")
    for a in assertions:
        if a.extraction_status == "extracted":
            stats.extracted += 1
        elif a.extraction_status == "partial":
            stats.partial += 1
        else:
            stats.unextracted += 1
        if not _assertion_usable(a):
            stats.review += 1
            print(f"  [REVIEW] {a.label}")
            print("       verifier detected, but assertion semantics could not be safely extracted")
            continue
        scored = [
            (fn, rq, *match_detail(rq, a)) for fn, rq in requirements
            if _is_action_bearing_requirement(rq)
        ]
        strong = [x for x in scored if x[2] in (EXACT, STRONG)]
        weak = [x for x in scored if x[2] == WEAK]
        if strong:
            stats.documented += 1
            fn, rq, conf, why = strong[0]
            print(f"  [OK] {a.label}")
            print(f"       documentation evidence: [{fn}] {rq}")
            print(f"       confidence={conf} | " + " | ".join(why))
        elif weak:
            stats.weak_check += 1
            fn, rq, conf, why = weak[0]
            print(f"  [WARN] {a.label}")
            print(f"       weak candidate (not counted as documented): [{fn}] {rq}")
            print("       " + " | ".join(why))
        else:
            stats.unmapped += 1
            print(f"  [UNMAPPED] {a.label}")
            print("       semantics extracted, but no documentation requirement matched")
    print()

    print("Documentation sections")
    print("----------------------")
    found_sections = set()
    for filename, text in docs:
        for line in text.splitlines():
            if line.startswith("#") and clean_heading(line) in TARGET_HEADINGS:
                found_sections.add((filename, clean_heading(line)))
    if found_sections:
        for filename, heading in sorted(found_sections):
            print(f"  [FOUND] {filename} :: {heading}")
    else:
        print("  [WARN] no recognized task-requirement section found")
    print()

    print("Notes")
    print("-----")
    print("This is a static heuristic audit for benchmark authoring.")
    print("WARN means 'review this', not 'the benchmark is definitely wrong'.")
    print("REVIEW means the verifier was found but its semantics were not safely recoverable.")
    print()
    print("Summary")
    print("-------")
    print(f"  Documentation requirements unmatched: {stats.unmatched} (weak candidates only: {stats.weak_req})")
    print(f"  Verifier checks UNMAPPED: {stats.unmapped} (weak candidates only: {stats.weak_check})")
    print(f"  Verifier checks needing semantic extraction review: {stats.review}")
    print(f"  Verifier checks with extracted semantics: {stats.extracted}")
    print(f"  Verifier checks with partial semantics: {stats.partial}")
    print(f"  Verifier checks unextracted: {stats.unextracted}")
    return 1 if stats.failed else 0


def audit_all(root: Path) -> int:
    tasks = sorted(p for p in root.iterdir() if p.is_dir() and (p / "task.toml").exists())
    if not tasks:
        print(f"No ITSMBench tasks found under: {root}")
        return 1
    failures = 0
    all_stats: list[tuple[str, TaskStats]] = []
    for index, task in enumerate(tasks, start=1):
        print()
        print("=" * 72)
        print(f"TASK {index}/{len(tasks)}: {task.name}")
        print("=" * 72)
        print()
        stats = TaskStats()
        if audit(task, stats) != 0:
            failures += 1
        all_stats.append((task.name, stats))

    total = lambda attr: sum(getattr(s, attr) for _, s in all_stats)  # noqa: E731
    reasons: dict[str, int] = {}
    for _, s in all_stats:
        for r in s.reasons:
            reasons[r] = reasons.get(r, 0) + 1
    print()
    print("=" * 72)
    print("CORPUS SUMMARY")
    print("=" * 72)
    print(f"Tasks scanned: {len(tasks)}")
    print(f"Audit failures: {failures}")
    print()
    print(f"Total verifier checks: {total('checks')}")
    print(f"  EXTRACTED:   {total('extracted')}")
    print(f"  PARTIAL:     {total('partial')}")
    print(f"  UNEXTRACTED: {total('unextracted')}")
    print()
    print(f"Documentation requirements (action-bearing): {total('requirements')}")
    print(f"  Matched (EXACT/STRONG): {total('matched')}")
    print(f"  Weak candidate only:    {total('weak_req')}")
    print(f"  Unmatched:              {total('unmatched')}")
    print()
    print("Verifier checks -> documentation")
    print(f"  Mapped (EXACT/STRONG): {total('documented')}")
    print(f"  Weak candidate only:   {total('weak_check')}")
    print(f"  UNMAPPED:              {total('unmapped')}")
    print(f"  Needing semantic review (not usable for matching): {total('review')}")
    print()
    zero_checks = [n for n, s in all_stats if s.checks == 0]
    zero_map = [n for n, s in all_stats if s.matched == 0 and s.documented == 0]
    print(f"Tasks with zero verifier checks: {len(zero_checks)}")
    for n in zero_checks:
        print(f"  - {n}")
    print(f"Tasks with zero documentation mappings: {len(zero_map)}")
    for n in zero_map:
        print(f"  - {n}")
    print()
    print("Top reasons for PARTIAL/UNEXTRACTED")
    for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1])[:10]:
        print(f"  {count:4d}  {reason}")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Static authoring-time audit for ITSMBench evaluation tasks.")
    parser.add_argument("task", nargs="?", help="Path to a single ITSMBench task directory.")
    parser.add_argument("--all", dest="all_tasks", metavar="TASKS_DIR",
                        help="Audit every task directory containing task.toml.")
    args = parser.parse_args()
    if args.all_tasks:
        return audit_all(Path(args.all_tasks))
    if not args.task:
        parser.error("provide a task directory or use --all TASKS_DIR")
    task = Path(args.task)
    if not task.exists():
        print(f"Task directory does not exist: {task}")
        return 1
    if not task.is_dir():
        print(f"Not a task directory: {task}")
        return 1
    return audit(task)


if __name__ == "__main__":
    raise SystemExit(main())