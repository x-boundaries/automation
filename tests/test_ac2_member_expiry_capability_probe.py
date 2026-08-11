"""Static, AST, library, and subprocess tests for the synthetic ExpiryDate probe.

No test executes the probe's AutoCount path or contacts AutoCount, SQL Server, n8n, Google
Sheets, or the host/VM. Subprocess runs of the probe are driven so they stop before any
AutoCount assembly load (refusal, an approval-reference rejection, or an untrusted canonical
claim root). The pure state/claim/publication/fingerprint helpers are exercised directly
against scripts/member_expiry_capability_probe_lib.ps1 without AutoCount.

Two deliberate boundaries:

* The probe's claim/result root is a FIXED code constant, so it is not redirectable from a
  test. Every behaviour that needs a *trusted* root (claim races, publication, authoritative
  validation) is therefore tested at pure-library level with an injected temporary root, and
  the script-level tests never run an active probe that could create an artefact inside the
  real canonical root. The few script tests that would otherwise do so are skipped, with a
  visible reason, on a machine where that root exists (i.e. the AutoCount VM).
* Repository dependencies are a CLOSED contract: ``REPO_DEPENDENCIES`` is the single
  immutable registry, ``repo_path``/``read_repo_text`` are the only sanctioned readers, and
  ``repository_read_violations`` is an independent AST guard that fails on any repository read
  or path derivation escaping them — including unresolvable ones, which are reported rather
  than silently dropped from the inventory. Every registered dependency must also be covered
  by every workflow ``paths`` filter.
"""

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# ---- A2-3: closed repository dependency contract ---- #
# The single explicit, immutable registry of every repository file this module reads or
# semantically asserts. Reads are routed through repo_path()/read_repo_text(), and an
# INDEPENDENT AST guard (repository_read_violations) fails the suite on any repository read or
# path derivation that escapes them. Discovery is therefore fail-closed: an unresolved or
# unrecognised read is an error, never a silently missing inventory entry.
REPO_DEPENDENCIES = types.MappingProxyType({
    "probe_script": "scripts/ac2_member_expiry_capability_probe.ps1",
    "probe_lib": "scripts/member_expiry_capability_probe_lib.ps1",
    "probe_runbook": "docs/autocount2-automation/member_expiry_capability_probe_runbook.md",
    "create_uat_runbook": "docs/autocount2-automation/member_create_uat_runbook.md",
    "readme": "README.md",
    "gitignore": ".gitignore",
    "workflow": ".github/workflows/member-create-uat-tests.yml",
    "focused_tests": "tests/test_ac2_member_expiry_capability_probe.py",
})


def repo_path(key):
    """Resolve a REGISTERED dependency key to its path. An unregistered key fails closed."""
    if key not in REPO_DEPENDENCIES:
        raise KeyError("unregistered repository dependency key: %r" % (key,))
    return ROOT / REPO_DEPENDENCIES[key]


def read_repo_text(key):
    """The only sanctioned repository text read; accepts a literal registered key only."""
    return repo_path(key).read_text(encoding="utf-8")


def read_scratch_text(path):
    """Read a NON-repository (temporary) file. Fails closed on any repository path."""
    resolved = Path(path).resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        raise AssertionError("the scratch reader refuses a repository path: %s" % resolved)
    return resolved.read_text(encoding="utf-8")


def registered_dependencies():
    """Every registered repository dependency, as repo-relative POSIX paths."""
    return set(REPO_DEPENDENCIES.values())


def materialise_probe_scratch(directory):
    """Copy the probe script and its helper library into a scratch directory.

    Content crosses the sanctioned registry reader first, so no repository path is ever handed
    to a subprocess or other external callable. The pair keeps its filenames and relative layout
    because the probe dot-sources the library from its own directory. The scratch copy is never
    activated: every test drives it into a refusal or a pre-AutoCount stop.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "ac2_member_expiry_capability_probe.ps1"
    library = directory / "member_expiry_capability_probe_lib.ps1"
    script.write_text(read_repo_text("probe_script"), encoding="utf-8")
    library.write_text(read_repo_text("probe_lib"), encoding="utf-8")
    return script, library

# Reads that must resolve to a registered dependency, and the helpers whose own bodies are the
# sanctioned implementations of those reads.
REPO_READ_METHODS = ("read_text", "read_bytes", "open")
SANCTIONED_READ_HELPERS = ("repo_path", "read_repo_text", "read_scratch_text")


def _mentions_root(node):
    return any(isinstance(sub, ast.Name) and sub.id == "ROOT" for sub in ast.walk(node))


# A4-2: the ONLY callables permitted to receive a repository-tainted value outside the
# sanctioned helper bodies. Deliberately minimal — a pure conversion that cannot read, open,
# copy, move, stat, hash, transmit or store file content, and whose result stays tainted so a
# later external use is still caught. Being an attribute call, an imported name or a
# module-level definition confers nothing.
PURE_TAINT_SAFE_CALLABLES = frozenset({"str"})
# Lexical operations that may be invoked ON a tainted value: they inspect the text of the path,
# never the file. Any other method call on a tainted receiver fails closed.
PURE_LEXICAL_METHODS = frozenset({
    "lower", "upper", "strip", "lstrip", "rstrip", "startswith", "endswith",
    "split", "rsplit", "replace", "format", "join", "as_posix", "count", "find",
})
DYNAMIC_ATTRIBUTE_CALLS = frozenset({"getattr", "attrgetter", "import_module", "__import__"})


# ---- A7-5: one type-comment-aware parser for every analysed source ---- #
# ast.parse silently DISCARDS function type comments unless type_comments=True, so a helper could
# otherwise declare a different signature without changing its contract. Every analysed source —
# live module, canonical helper snippets, canonical dependency snippet and every fixture — is
# parsed through this single entry point.
TYPE_COMMENT_SYNTAX = re.compile(r"#\s*type\s*:")


def _type_comments_supported():
    """Whether the running interpreter can capture type comments at all."""
    try:
        ast.parse("value = 1  # type: int", type_comments=True)
    except TypeError:
        return False
    return True


TYPE_COMMENTS_SUPPORTED = _type_comments_supported()


def parse_source(source, supported=None):
    """Parse with function type comments captured, or fail closed.

    ``supported`` is the explicit cross-version seam: an interpreter that cannot capture type
    comments must REFUSE a source carrying type-comment syntax rather than analyse it with the
    contract field silently dropped. A source with no such syntax is safe to parse without the
    option, because there is then nothing to discard.
    """
    if TYPE_COMMENTS_SUPPORTED if supported is None else supported:
        return ast.parse(source, type_comments=True)
    if TYPE_COMMENT_SYNTAX.search(source):
        raise AssertionError(
            "this interpreter cannot capture type comments and the source contains "
            "type-comment syntax; refusing to analyse it with the contract field discarded")
    return ast.parse(source)


# ---- A7-1: the independent canonical closed-dependency declarations ---- #
# Hand-maintained and deliberately NOT derived from the live module nodes, exactly like the
# canonical helper bodies. An exact helper body means nothing if the declarations it closes over
# can be replaced: a widened root or an altered registry silently changes what an unchanged body
# resolves and reads. The registry pairs are duplicated here on purpose so the comparison cannot
# be tautological.
CANONICAL_TYPES_IMPORT = "import types\n"
CANONICAL_PATH_IMPORT = "from pathlib import Path\n"
CANONICAL_ROOT_ANCHOR = "ROOT = Path(__file__).resolve().parents[1]\n"
CANONICAL_REGISTRY = (
    'REPO_DEPENDENCIES = types.MappingProxyType({\n'
    '    "probe_script": "scripts/ac2_member_expiry_capability_probe.ps1",\n'
    '    "probe_lib": "scripts/member_expiry_capability_probe_lib.ps1",\n'
    '    "probe_runbook": "docs/autocount2-automation/member_expiry_capability_probe_runbook.md",\n'
    '    "create_uat_runbook": "docs/autocount2-automation/member_create_uat_runbook.md",\n'
    '    "readme": "README.md",\n'
    '    "gitignore": ".gitignore",\n'
    '    "workflow": ".github/workflows/member-create-uat-tests.yml",\n'
    '    "focused_tests": "tests/test_ac2_member_expiry_capability_probe.py",\n'
    '})\n'
)
CANONICAL_DEPENDENCY_SOURCE = (CANONICAL_TYPES_IMPORT + CANONICAL_PATH_IMPORT
                               + CANONICAL_ROOT_ANCHOR + CANONICAL_REGISTRY)

# Every name the root anchor and the sanctioned helper bodies close over. `types` is included
# because the immutable registry constructor closes over it; `Path` because the anchor does.
CLOSED_DEPENDENCY_NAMES = ("types", "Path", "ROOT", "REPO_DEPENDENCIES")
# Declarations whose runtime semantics depend on order: the constructor needs `types`, the anchor
# needs `Path`.
CLOSED_DEPENDENCY_ORDER = (("types", "REPO_DEPENDENCIES"), ("Path", "ROOT"))
# The anchor also closes over `__file__`, which has no declaration of its own: any binding of it
# is a replacement of the checkout identity.
CLOSED_DEPENDENCY_IMPLICIT = ("__file__",)


def _target_names(target):
    """Names an assignment target binds. A subscript or attribute target binds NO name."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        names = []
        for element in target.elts:
            names.extend(_target_names(element))
        return names
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    return []


def _direct_bindings(node):
    """(name, kind) pairs this single node binds, ignoring bindings made by its children."""
    pairs = []
    if isinstance(node, ast.Assign):
        simple = len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
        for target in node.targets:
            for name in _target_names(target):
                pairs.append((name, "assign" if simple else "destructured"))
    elif isinstance(node, ast.AnnAssign):
        pairs.extend((name, "annassign") for name in _target_names(node.target))
    elif isinstance(node, ast.AugAssign):
        pairs.extend((name, "augassign") for name in _target_names(node.target))
    elif isinstance(node, ast.NamedExpr):
        pairs.extend((name, "namedexpr") for name in _target_names(node.target))
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        pairs.extend(((alias.asname or alias.name.split(".")[0]), "import")
                     for alias in node.names)
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        pairs.append((node.name, "def"))
    elif isinstance(node, ast.ClassDef):
        pairs.append((node.name, "class"))
    elif isinstance(node, ast.arg):
        pairs.append((node.arg, "param"))
    elif isinstance(node, ast.ExceptHandler) and node.name:
        pairs.append((node.name, "except"))
    elif isinstance(node, (ast.For, ast.AsyncFor)):
        pairs.extend((name, "for") for name in _target_names(node.target))
    elif isinstance(node, ast.comprehension):
        pairs.extend((name, "comprehension") for name in _target_names(node.target))
    elif isinstance(node, ast.withitem) and node.optional_vars is not None:
        pairs.extend((name, "with") for name in _target_names(node.optional_vars))
    elif isinstance(node, (ast.Global, ast.Nonlocal)):
        pairs.extend((name, "global") for name in node.names)
    elif hasattr(ast, "MatchAs") and isinstance(node, ast.MatchAs) and node.name:
        pairs.append((node.name, "match"))
    elif hasattr(ast, "MatchStar") and isinstance(node, ast.MatchStar) and node.name:
        pairs.append((node.name, "match"))
    elif hasattr(ast, "MatchMapping") and isinstance(node, ast.MatchMapping) and node.rest:
        pairs.append((node.rest, "match"))
    return pairs


def canonical_dependency_contract():
    """name -> (statement position, normalised AST dump) for the hand-maintained declarations."""
    contract = {}
    for index, statement in enumerate(parse_source(CANONICAL_DEPENDENCY_SOURCE).body):
        for name, _kind in _direct_bindings(statement):
            if name in CLOSED_DEPENDENCY_NAMES:
                contract[name] = (index, ast.dump(statement, include_attributes=False))
    return contract


CANONICAL_DEPENDENCY_CONTRACT = canonical_dependency_contract()


def _closed_dependency_findings(tree):
    """Exact contract for the declarations the sanctioned boundary closes over.

    Returns ``(problems, intact)``. ``intact`` is the ONLY basis on which the root anchor, the
    exact helper bodies and the literal registry calls may be exempted: a declaration is accepted
    because its complete normalised AST equals the independent canonical one, never because it is
    the first statement with the right name.
    """
    problems = []
    top_level = {}
    for index, statement in enumerate(tree.body):
        for name, _kind in _direct_bindings(statement):
            if name in CLOSED_DEPENDENCY_NAMES:
                top_level.setdefault(name, []).append((index, statement))

    canonical_ids = set()
    positions = {}
    for name in CLOSED_DEPENDENCY_NAMES:
        hits = top_level.get(name, [])
        if not hits:
            problems.append("%s:closed_dependency_missing" % name)
            continue
        expected = CANONICAL_DEPENDENCY_CONTRACT[name][1]
        dumps = [ast.dump(statement, include_attributes=False) for _index, statement in hits]
        if len(hits) > 1:
            # Two exact declarations are a duplicate; a competing different one is a rebinding.
            problems.append("%s:closed_dependency_%s"
                            % (name, "duplicate" if all(dump == expected for dump in dumps)
                               else "rebound"))
            continue
        if dumps[0] != expected:
            problems.append("%s:closed_dependency_body_mismatch" % name)
            continue
        positions[name] = hits[0][0]
        canonical_ids.add(id(hits[0][1]))

    for earlier, later in CLOSED_DEPENDENCY_ORDER:
        if earlier in positions and later in positions and positions[earlier] > positions[later]:
            problems.append("%s:closed_dependency_out_of_order" % later)

    top_level_ids = {id(statement) for statement in tree.body}
    for node in ast.walk(tree):
        if id(node) in canonical_ids:
            continue
        for name, _kind in _direct_bindings(node):
            if name in CLOSED_DEPENDENCY_IMPLICIT:
                problems.append("%s:closed_dependency_rebound" % name)
            elif name in CLOSED_DEPENDENCY_NAMES and id(node) not in top_level_ids:
                # A nested, conditional or function-local substitute declaration.
                problems.append("%s:closed_dependency_not_top_level" % name)

    problems = sorted(set(problems))
    return problems, not problems


def closed_dependency_violations(source):
    """Public closed-dependency contract check."""
    problems, _intact = _closed_dependency_findings(parse_source(source))
    return problems


# ---- A7-2: dynamic namespace and binding integrity ---- #
# The static binding pass covers ordinary AST binding forms. These routes replace, delete or
# expose a binding at RUNTIME, so a later direct literal call can reach something other than the
# reviewed helper.
NAMESPACE_PRODUCERS = frozenset({"globals", "locals", "vars"})
# Bare-name calls only: these are the builtin bindings. `re.compile` is an attribute call on an
# imported module and is not a namespace route, so `compile` is dangerous only as a bare name;
# `exec`/`eval` have no legitimate attribute use here.
DYNAMIC_EXECUTION_NAMES = frozenset({"exec", "eval", "compile"})
DYNAMIC_EXECUTION_ATTRS = frozenset({"exec", "eval"})
NAMESPACE_MUTATORS = frozenset({"update", "__setitem__", "__delitem__", "setdefault",
                                "pop", "popitem", "clear"})


def _namespace_mutation_keys(call, attribute):
    """Statically resolvable literal keys a namespace-mapping method touches, else ``None``."""
    if attribute in ("clear", "popitem"):
        return None                                  # touches everything
    if attribute == "update":
        if len(call.args) == 1 and not call.keywords and isinstance(call.args[0], ast.Dict):
            keys = []
            for key in call.args[0].keys:
                if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                    return None                      # unpacking or a non-literal key
                keys.append(key.value)
            return keys
        if not call.args and call.keywords and all(keyword.arg for keyword in call.keywords):
            return [keyword.arg for keyword in call.keywords]
        return None
    if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
        return [call.args[0].value]
    return None


def _dynamic_namespace_findings(tree, scope_index=None):
    """Statically identifiable dynamic-namespace routes into the sanctioned boundary.

    Returns ``(problems, compromised_helpers, dependencies_compromised)``. A literal mutation of
    one helper compromises that helper; a literal mutation of a closed dependency compromises the
    whole boundary; an unresolved key, a dynamically selected namespace, an escaping namespace
    object and any ``exec``/``eval``/``compile`` use compromise everything, because their binding
    effects are not statically controlled.

    A8-2: these routes are recognised by resolved BUILTIN AUTHORITY, not by the bare label. A
    shadowed name (a local ``def compile(...)``) is not the builtin and must not be treated as
    one; conversely an alias or capture of the real builtin is caught by
    _protected_authority_findings, which fails the whole boundary closed.
    """
    problems = []
    compromised = set()
    state = {"dependencies": False}
    tracked = (set(SANCTIONED_READ_HELPERS) | set(CLOSED_DEPENDENCY_NAMES)
               | set(CLOSED_DEPENDENCY_IMPLICIT))
    scope_of, scope_parent, bindings, _definitions = scope_index or _build_scope_index(tree)

    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent

    def is_builtin(node):
        """True when this bare Name still refers to the builtin of that name."""
        scope = scope_of.get(id(node), tree)
        while scope is not None:
            if node.id in bindings.get(id(scope), {}):
                return False
            scope = scope_parent.get(id(scope))
        return True

    def compromise_name(name):
        if name in SANCTIONED_READ_HELPERS:
            compromised.add(name)
            problems.append("%s:sanctioned_helper_dynamic_binding" % name)
        else:
            state["dependencies"] = True
            problems.append("%s:closed_dependency_dynamic_binding" % name)

    def compromise_everything(category):
        state["dependencies"] = True
        compromised.update(SANCTIONED_READ_HELPERS)
        problems.append("*:%s" % category)

    def record_keys(keys, category):
        if keys is None:
            compromise_everything(category)
            return
        for key in keys:
            if key in tracked:
                compromise_name(key)
            else:
                problems.append("*:dynamic_namespace_binding")

    def is_module_registry(node):
        """`sys.modules[...]`: a live module object, whose attributes ARE its namespace."""
        return (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute)
                and node.value.attr == "modules")

    def is_namespace_expression(node):
        """An expression that is, or directly exposes, a module/namespace mapping."""
        if isinstance(node, ast.Attribute):
            return node.attr == "__dict__"
        if isinstance(node, ast.Subscript):
            return is_module_registry(node)
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Name) and func.id in NAMESPACE_PRODUCERS
                    and is_builtin(func)):
                return True
            if isinstance(func, ast.Attribute) and func.attr in NAMESPACE_PRODUCERS:
                return True
            if (isinstance(func, ast.Name) and func.id == "getattr" and is_builtin(func)
                    and len(node.args) >= 2):
                attribute = node.args[1]
                # Dynamic attribute selection of __dict__, or an unresolvable selection.
                if not (isinstance(attribute, ast.Constant) and isinstance(attribute.value, str)):
                    return False                     # reported as dynamic attribute access
                return attribute.value == "__dict__"
        return False

    handled = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if ((isinstance(func, ast.Name) and func.id in DYNAMIC_EXECUTION_NAMES
                 and is_builtin(func))
                    or (isinstance(func, ast.Attribute)
                        and func.attr in DYNAMIC_EXECUTION_ATTRS)):
                compromise_everything("dynamic_namespace_binding")
                continue
            if (isinstance(func, ast.Name) and func.id in ("setattr", "delattr")
                    and is_builtin(func)):
                arguments = list(node.args)
                if arguments:
                    handled.add(id(arguments[0]))
                attribute = arguments[1] if len(arguments) > 1 else None
                if not (isinstance(attribute, ast.Constant)
                        and isinstance(attribute.value, str)):
                    compromise_everything("dynamic_namespace_binding")
                elif attribute.value in tracked:
                    compromise_name(attribute.value)
                elif arguments and (is_namespace_expression(arguments[0])
                                    or is_module_registry(arguments[0])):
                    # A module-like target: any attribute write mutates its namespace.
                    compromise_everything("dynamic_namespace_binding")
                continue
            if isinstance(func, ast.Attribute) and is_namespace_expression(func.value):
                handled.add(id(func.value))
                if func.attr in NAMESPACE_MUTATORS:
                    record_keys(_namespace_mutation_keys(node, func.attr),
                                "dynamic_namespace_binding")
                else:
                    compromise_everything("dynamic_namespace_escape")
                continue
        if isinstance(node, ast.Subscript) and is_namespace_expression(node.value):
            handled.add(id(node.value))
            key = node.slice
            store = isinstance(node.ctx, (ast.Store, ast.Del))
            category = "dynamic_namespace_binding" if store else "dynamic_namespace_escape"
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                record_keys([key.value], category)
            else:
                compromise_everything(category)

    for node in ast.walk(tree):
        if id(node) in handled or not is_namespace_expression(node):
            continue
        parent = parents.get(id(node))
        if parent is not None and is_namespace_expression(parent):
            continue                     # a namespace derived from a namespace; judged above
        # Returned, stored, passed to an unresolved callable, or otherwise escaping.
        compromise_everything("dynamic_namespace_escape")

    return sorted(set(problems)), compromised, state["dependencies"]


def dynamic_namespace_violations(source):
    """Public dynamic-namespace integrity check."""
    problems, _compromised, _dependencies = _dynamic_namespace_findings(parse_source(source))
    return problems


def _build_scope_index(tree):
    """Lexical scopes, their bindings and the definitions visible in each.

    Returns ``(scope_of, scope_parent, bindings, definitions)``.

    ``scope_of`` maps every node to the scope it is evaluated in — decorators, argument defaults
    and return annotations belong to the ENCLOSING scope, the body does not. ``bindings`` maps a
    scope to ``{name: [record]}`` where a record carries the binding kind, the bound value for a
    simple assignment, the source position and whether the binding sits directly in that scope's
    own body rather than inside a conditional, loop or handler. ``definitions`` maps a scope to
    the function and statically bound lambda definitions it declares, keyed by name, so a call
    resolves to a definition IDENTITY instead of a bare name.
    """
    scope_of = {}
    scope_parent = {}
    bindings = {}
    definitions = {}

    def defaults_of(node):
        return (list(node.args.defaults)
                + [default for default in node.args.kw_defaults if default is not None])

    def arguments_of(node):
        collected = list(getattr(node.args, "posonlyargs", [])) + list(node.args.args)
        collected.extend(node.args.kwonlyargs)
        for extra in (node.args.vararg, node.args.kwarg):
            if extra is not None:
                collected.append(extra)
        return collected

    def enter(node, parent):
        scope_parent[id(node)] = parent
        bindings[id(node)] = {}
        definitions[id(node)] = {}

    def declare(scope, node, direct):
        for name, kind in _direct_bindings(node):
            value = node.value if kind == "assign" and isinstance(node, ast.Assign) else None
            bindings[id(scope)].setdefault(name, []).append({
                "kind": kind,
                "value": value,
                "position": (getattr(node, "lineno", 0), getattr(node, "col_offset", 0)),
                "direct": direct,
            })
            definition = None
            if kind == "def":
                definition = node
            elif value is not None and isinstance(value, ast.Lambda):
                definition = value
            if definition is not None:
                definitions[id(scope)].setdefault(name, []).append(definition)

    def visit(node, scope, direct):
        scope_of[id(node)] = scope
        declare(scope, node, direct)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            for expression in defaults_of(node):
                visit(expression, scope, False)
            if not isinstance(node, ast.Lambda):
                for expression in list(node.decorator_list):
                    visit(expression, scope, False)
                if node.returns is not None:
                    visit(node.returns, scope, False)
            enter(node, scope)
            for argument in arguments_of(node):
                visit(argument, node, True)
            for statement in (node.body if isinstance(node.body, list) else [node.body]):
                visit(statement, node, True)
            return
        if isinstance(node, ast.ClassDef):
            for expression in (list(node.decorator_list) + list(node.bases)
                               + [keyword.value for keyword in node.keywords]):
                visit(expression, scope, False)
            enter(node, scope)
            for statement in node.body:
                visit(statement, node, True)
            return
        for child in ast.iter_child_nodes(node):
            visit(child, scope, False)

    enter(tree, None)
    scope_of[id(tree)] = tree
    for statement in tree.body:
        visit(statement, tree, True)
    return scope_of, scope_parent, bindings, definitions


def _default_resolver(definition, scope_of, scope_parent, bindings, tree):
    """Definition-time lexical lookup for the names a definition's defaults may use.

    ``resolve(name)`` returns the single value expression a default may rely on, and ``None``
    whenever the contract cannot prove it: a binding in a sibling, nested or unrelated scope, an
    import, a parameter, a loop, handler or comprehension target, an augmented or destructured
    assignment, a binding inside a conditional, more than one binding, a binding that is not
    proven to execute before the definition, or no binding at all. ``is_shadowed(name)`` reports
    whether a builtin container constructor has been rebound anywhere in the relevant chain.
    """
    enclosing = scope_of.get(id(definition), tree)
    before = (getattr(definition, "lineno", 0), getattr(definition, "col_offset", 0))

    def resolve(name):
        scope = enclosing
        immediate = True
        while scope is not None:
            records = bindings.get(id(scope), {}).get(name)
            if isinstance(scope, ast.ClassDef) and not immediate:
                # CPython skips ENCLOSING class scopes during lexical lookup; a name found there
                # cannot be proven to be the one the default will see.
                if records:
                    return None
            elif records:
                if len(records) != 1:
                    return None                        # multiply bound: ambiguous
                record = records[0]
                if record["kind"] != "assign" or record["value"] is None:
                    return None
                if not record["direct"] or record["position"] >= before:
                    return None
                return record["value"]
            scope = scope_parent.get(id(scope))
            immediate = False
        return None

    def is_shadowed(name):
        scope = enclosing
        while scope is not None:
            if name in bindings.get(id(scope), {}):
                return True
            scope = scope_parent.get(id(scope))
        return False

    return resolve, is_shadowed


def _resolve_call_definitions(node, scope_of, scope_parent, bindings, definitions, tree):
    """(definitions, ambiguous) for a direct Name call, resolved lexically.

    Only a unique statically visible definition in the innermost scope that binds the name is a
    candidate. A name with no visible definition (an import or an unknown) resolves to no
    candidate and keeps its existing unresolved treatment; a name that is ALSO bound another way,
    or bound to more than one definition, is ambiguous and must not inherit a trusted summary.
    """
    func = node.func
    if not isinstance(func, ast.Name):
        return [], False
    scope = scope_of.get(id(node), tree)
    immediate = True
    while scope is not None:
        records = bindings.get(id(scope), {}).get(func.id)
        if records and not (isinstance(scope, ast.ClassDef) and not immediate):
            candidates = definitions.get(id(scope), {}).get(func.id, [])
            if not candidates:
                return [], False
            return candidates, len(records) > len(candidates) or len(candidates) > 1
        scope = scope_parent.get(id(scope))
        immediate = False
    return [], False


# ---- A8-1: the closed dependency OBJECTS, not merely their names ---- #
# A7 pins the exact bindings of `types`, `Path`, `ROOT` and `REPO_DEPENDENCIES`. That is not
# enough: the bound `pathlib.Path` class and `types` module can be monkey-patched
# (`Path.resolve = replacement`, `types.MappingProxyType = replacement`) so the reviewed root
# anchor, the immutable registry and the exact helper bodies mean something different at runtime,
# while every tracked name and every declaration AST stays untouched.
# The semantics the reviewed declarations and helper bodies actually depend on. The RULE is
# broader than this set -- ANY attribute write, delete or unresolved mutation of the protected
# objects fails closed -- but these are the ones the reviewed code would silently inherit.
PROTECTED_PATH_SEMANTICS = frozenset({
    "__new__", "__init__", "__truediv__", "resolve", "parent", "parents",
    "read_text", "read_bytes", "open", "joinpath",
})
PROTECTED_TYPES_SEMANTICS = frozenset({"MappingProxyType"})

# ---- A8-2: dangerous callable AUTHORITY, not merely dangerous names ---- #
# The namespace producers and dynamic executors are only fail-closed while they are recognised.
# An alias (`g = globals`), an import alias (`from builtins import exec as run`) or a captured
# module attribute (`builtins.exec`) replaces a sanctioned helper without the literal-name
# detector ever firing, and a later direct `read_repo_text("probe_script")` would keep authority.
DANGEROUS_BUILTIN_NAMES = frozenset({
    "globals", "locals", "vars", "exec", "eval", "compile", "setattr", "delattr"})
BUILTINS_MODULE_NAMES = frozenset({"builtins", "__builtins__"})
# Calls that hand back the namespace or an attribute OF their first argument, so protected-object
# authority flows through them.
AUTHORITY_FORWARDING_CALLS = frozenset({"vars", "getattr"})
ATTRIBUTE_MUTATION_HELPERS = frozenset({"setattr", "delattr"})
# `object.__setattr__(Path, ...)` / `type.__setattr__(Path, ...)` reach the class regardless of
# any descriptor protection on it.
ATTRIBUTE_MUTATION_DUNDERS = frozenset({"__setattr__", "__delattr__"})

PATH_AUTHORITY = "path"
TYPES_AUTHORITY = "types"
BUILTINS_AUTHORITY = "builtins"
DANGEROUS_AUTHORITY = "dangerous"
PROTECTED_OBJECT_AUTHORITIES = frozenset({PATH_AUTHORITY, TYPES_AUTHORITY})
REPORTABLE_AUTHORITIES = frozenset({PATH_AUTHORITY, TYPES_AUTHORITY, DANGEROUS_AUTHORITY})
AUTHORITY_PREFIXES = {PATH_AUTHORITY: "Path", TYPES_AUTHORITY: "types",
                      DANGEROUS_AUTHORITY: "builtin"}


def _protected_authority_findings(tree, scope_index=None):
    """Protected closed-dependency objects and dangerous builtin callable authority.

    Authority is tracked as a PROVENANCE label rather than a name, so an alias, container,
    wrapper return, default or captured module attribute carries it, and so a merely similar name
    (a local `compile`, `re.compile`) does not. Returns ``(problems, broken)``; ``broken`` means
    the complete dependency/helper boundary is compromised, and the caller must then withhold the
    root anchor exemption, the exact helper-body exemptions, the registry-reader authority and the
    direct literal `repo_path`/`read_repo_text` allowance.
    """
    problems = []
    state = {"broken": False}
    scope_of, scope_parent, bindings, definitions = scope_index or _build_scope_index(tree)
    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent

    def report(labels, category):
        for authority in (PATH_AUTHORITY, TYPES_AUTHORITY, DANGEROUS_AUTHORITY):
            if authority in labels:
                problems.append("%s:%s" % (AUTHORITY_PREFIXES[authority], category))
                break
        state["broken"] = True

    name_authorities = {}
    definition_authorities = {}

    def add_name(name, labels):
        current = name_authorities.setdefault(name, set())
        if labels <= current:
            return False
        current |= labels
        return True

    # ---- provenance seeds ---- #
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "pathlib":
                for alias in node.names:
                    if alias.name == "Path":
                        add_name(alias.asname or "Path", {PATH_AUTHORITY})
            elif node.module == "builtins":
                for alias in node.names:
                    if alias.name in DANGEROUS_BUILTIN_NAMES:
                        add_name(alias.asname or alias.name, {DANGEROUS_AUTHORITY})
                        report({DANGEROUS_AUTHORITY}, "dangerous_builtin_alias")
                    elif alias.name == "*":
                        # A star import cannot be resolved to a safe subset.
                        report({DANGEROUS_AUTHORITY}, "dangerous_builtin_dynamic_selection")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root == "types":
                    add_name(alias.asname or root, {TYPES_AUTHORITY})
                elif root == "builtins":
                    add_name(alias.asname or root, {BUILTINS_AUTHORITY})

    def shadowed(node, name):
        scope = scope_of.get(id(node), tree)
        while scope is not None:
            if name in bindings.get(id(scope), {}):
                return True
            scope = scope_parent.get(id(scope))
        return False

    def labels_of(node):
        if node is None:
            return set()
        if isinstance(node, ast.Name):
            labels = set(name_authorities.get(node.id, ()))
            if node.id == "__builtins__":
                labels.add(BUILTINS_AUTHORITY)
            if node.id in DANGEROUS_BUILTIN_NAMES and not shadowed(node, node.id):
                labels.add(DANGEROUS_AUTHORITY)
            return labels
        if isinstance(node, ast.Attribute):
            base = labels_of(node.value)
            if BUILTINS_AUTHORITY in base:
                return {DANGEROUS_AUTHORITY} if node.attr in DANGEROUS_BUILTIN_NAMES else set()
            if node.attr == "__dict__":
                # The class or module namespace carries the same mutation authority.
                return base & PROTECTED_OBJECT_AUTHORITIES
            return set()
        if isinstance(node, ast.Subscript):
            base = labels_of(node.value)
            if BUILTINS_AUTHORITY in base:
                key = node.slice
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    return {DANGEROUS_AUTHORITY} if key.value in DANGEROUS_BUILTIN_NAMES else set()
                return {DANGEROUS_AUTHORITY}          # unresolved selection: fail closed
            return set(base)                          # recovered from a container
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in AUTHORITY_FORWARDING_CALLS:
                forwarded = labels_of(node.args[0]) if node.args else set()
                if func.id == "getattr" and BUILTINS_AUTHORITY in forwarded:
                    attribute = node.args[1] if len(node.args) > 1 else None
                    if isinstance(attribute, ast.Constant) and isinstance(attribute.value, str):
                        return ({DANGEROUS_AUTHORITY}
                                if attribute.value in DANGEROUS_BUILTIN_NAMES else set())
                    return {DANGEROUS_AUTHORITY}
                return set(forwarded)
            candidates, _ambiguous = _resolve_call_definitions(
                node, scope_of, scope_parent, bindings, definitions, tree)
            labels = set()
            for definition in candidates:
                labels |= definition_authorities.get(id(definition), set())
            return labels
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            labels = set()
            for element in node.elts:
                labels |= labels_of(element)
            return labels
        if isinstance(node, ast.Dict):
            labels = set()
            for key in node.keys:
                labels |= labels_of(key)
            for value in node.values:
                labels |= labels_of(value)
            return labels
        if isinstance(node, ast.Starred):
            return labels_of(node.value)
        if isinstance(node, ast.IfExp):
            return labels_of(node.body) | labels_of(node.orelse)
        if isinstance(node, ast.NamedExpr):
            return labels_of(node.value)
        return set()

    def scope_definitions_of(node):
        return [scope for scope in ast.walk(node)
                if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))]

    # Finite fixpoint: aliases, destructuring, containers, wrapper returns, lambda returns and
    # defaults all carry authority, so long chains resolve. Monotonic over finite label sets.
    convergence_bound = sum(1 for _ in ast.walk(tree)) + 2
    rounds = 0
    while True:
        rounds += 1
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                labels = labels_of(getattr(node, "value", None))
                if labels:
                    targets = (node.targets if isinstance(node, ast.Assign) else [node.target])
                    for target in targets:
                        for name in _target_names(target):
                            changed = add_name(name, labels) or changed
            elif isinstance(node, ast.Return):
                owner = scope_of.get(id(node))
                if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    labels = labels_of(node.value)
                    if labels:
                        current = definition_authorities.setdefault(id(owner), set())
                        if not labels <= current:
                            current |= labels
                            changed = True
            elif isinstance(node, ast.Lambda):
                labels = labels_of(node.body)
                if labels:
                    current = definition_authorities.setdefault(id(node), set())
                    if not labels <= current:
                        current |= labels
                        changed = True
        for scope in scope_definitions_of(tree):
            arguments = scope.args
            positional = list(getattr(arguments, "posonlyargs", [])) + list(arguments.args)
            defaults = list(arguments.defaults)
            pairs = []
            if defaults:
                pairs.extend(zip(positional[len(positional) - len(defaults):], defaults))
            pairs.extend((param, default)
                         for param, default in zip(arguments.kwonlyargs, arguments.kw_defaults)
                         if default is not None)
            for param, default in pairs:
                labels = labels_of(default)
                if labels:
                    changed = add_name(param.arg, labels) or changed
        if not changed or rounds >= convergence_bound:
            break

    handled = set()

    def mark(node):
        for sub in ast.walk(node):
            handled.add(id(sub))

    def literal_string(node):
        return isinstance(node, ast.Constant) and isinstance(node.value, str)

    # ---- protected-object mutation routes ---- #
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, (ast.Assign, ast.Delete)):
            targets = list(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        for target in targets:
            if not isinstance(target, (ast.Attribute, ast.Subscript)):
                continue
            labels = labels_of(target.value) & PROTECTED_OBJECT_AUTHORITIES
            if not labels:
                continue
            mark(target)
            if isinstance(target, ast.Attribute):
                report(labels, "closed_dependency_object_mutation")
            else:
                report(labels, "closed_dependency_object_mutation"
                       if literal_string(target.slice)
                       else "closed_dependency_object_dynamic_mutation")

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # `Path.__dict__.update(...)` / `vars(types).update(...)`
        if isinstance(func, ast.Attribute) and func.attr in NAMESPACE_MUTATORS:
            labels = labels_of(func.value) & PROTECTED_OBJECT_AUTHORITIES
            if labels:
                mark(func.value)
                report(labels, "closed_dependency_object_mutation")
                continue
        mutating = False
        if isinstance(func, ast.Name):
            mutating = (func.id in ATTRIBUTE_MUTATION_HELPERS
                        or DANGEROUS_AUTHORITY in labels_of(func))
        elif isinstance(func, ast.Attribute):
            mutating = func.attr in ATTRIBUTE_MUTATION_DUNDERS
        if not mutating or not node.args:
            continue
        labels = labels_of(node.args[0]) & PROTECTED_OBJECT_AUTHORITIES
        if not labels:
            continue
        mark(node.args[0])
        attribute = node.args[1] if len(node.args) > 1 else None
        report(labels, "closed_dependency_object_mutation" if literal_string(attribute)
               else "closed_dependency_object_dynamic_mutation")

    # ---- the canonical uses the reviewed declarations and helper bodies require ---- #
    # A call cannot mutate its callee, so construction and the registry constructor stay allowed.
    # Every OTHER appearance of protected or dangerous authority is an alias, an escape or a
    # mutation route, and fails closed.
    permitted = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            labels = labels_of(func)
            if PATH_AUTHORITY in labels:
                permitted.add(id(func))
            elif (DANGEROUS_AUTHORITY in labels and func.id in DANGEROUS_BUILTIN_NAMES
                  and func.id not in name_authorities):
                # The exact direct-callee form the dynamic-namespace checker already fails closed.
                permitted.add(id(func))
        elif (isinstance(func, ast.Attribute) and func.attr in PROTECTED_TYPES_SEMANTICS
              and TYPES_AUTHORITY in labels_of(func.value)):
            permitted.add(id(func))
            permitted.add(id(func.value))

    for node in ast.walk(tree):
        if id(node) in handled or id(node) in permitted:
            continue
        if not isinstance(node, (ast.Name, ast.Attribute, ast.Subscript, ast.Call)):
            continue
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            continue                     # a Store of a tracked name is A7's rebinding finding
        labels = labels_of(node) & REPORTABLE_AUTHORITIES
        if not labels:
            continue
        parent = parents.get(id(node))
        if (isinstance(parent, (ast.Attribute, ast.Subscript, ast.Call))
                and (labels_of(parent) & labels) and id(parent) not in handled):
            continue                     # an enclosing expression forwards it; report once there
        protected = bool(labels & PROTECTED_OBJECT_AUTHORITIES)
        # An unresolvable builtins selection cannot be narrowed to a safe name.
        dynamic = False
        if not protected:
            if isinstance(node, ast.Subscript) and BUILTINS_AUTHORITY in labels_of(node.value):
                dynamic = not literal_string(node.slice)
            elif (isinstance(node, ast.Call) and node.args
                  and BUILTINS_AUTHORITY in labels_of(node.args[0])):
                dynamic = not (len(node.args) > 1 and literal_string(node.args[1]))
        if dynamic:
            report(labels, "dangerous_builtin_dynamic_selection")
            continue
        binder = node
        current = parent
        while isinstance(current, (ast.Tuple, ast.List, ast.Set, ast.Dict, ast.Starred)):
            binder = current
            current = parents.get(id(current))
        bound = ((isinstance(current, (ast.Assign, ast.AnnAssign, ast.NamedExpr))
                  and getattr(current, "value", None) is binder)
                 or isinstance(current, ast.arguments))
        if bound:
            report(labels, "closed_dependency_object_alias" if protected
                   else "dangerous_builtin_alias")
        else:
            report(labels, "closed_dependency_object_escape" if protected
                   else "dangerous_builtin_escape")

    return sorted(set(problems)), state["broken"]


def protected_authority_violations(source):
    """Public protected-object and dangerous-builtin authority check."""
    problems, _broken = _protected_authority_findings(parse_source(source))
    return problems


# ---- A5-1: the independent canonical contract for the sanctioned helper bodies ---- #
# These snippets are maintained by hand and are the AUTHORITY the live helpers are compared
# against. They are deliberately NOT derived from the live definitions, so the comparison
# cannot be tautological. Docstrings, comments and source locations are normalised away; every
# executable statement, call, argument, constant, operator, exception type and control-flow
# construct is significant.
CANONICAL_HELPER_SOURCES = {
    "repo_path": (
        'def repo_path(key):\n'
        '    if key not in REPO_DEPENDENCIES:\n'
        '        raise KeyError(\"unregistered repository dependency key: %r\" % (key,))\n'
        '    return ROOT / REPO_DEPENDENCIES[key]\n'
    ),
    "read_repo_text": (
        'def read_repo_text(key):\n'
        '    return repo_path(key).read_text(encoding=\"utf-8\")\n'
    ),
    "read_scratch_text": (
        'def read_scratch_text(path):\n'
        '    resolved = Path(path).resolve()\n'
        '    if resolved == ROOT or ROOT in resolved.parents:\n'
        '        raise AssertionError(\"the scratch reader refuses a repository path: %s\" % resolved)\n'
        '    return resolved.read_text(encoding=\"utf-8\")\n'
    ),
}


def _helper_definition_contract(node):
    """The complete executable and binding-relevant contract of a helper definition.

    Source positions and comments are ignored, and exactly one harmless leading docstring may be
    removed. Everything else is significant: the node kind, the helper name, the full argument
    structure (positional-only, positional, keyword-only, defaults, keyword defaults, *args,
    **kwargs and annotations), the decorator list, the return annotation, the type comment, type
    parameters where the running interpreter supports them, and every executable statement in
    order.
    """
    body = list(node.body)
    if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]

    def dump(value):
        if value is None:
            return None
        if isinstance(value, list):
            return tuple(dump(item) for item in value)
        return ast.dump(value, include_attributes=False)

    return (
        type(node).__name__,
        node.name,
        dump(node.args),
        dump(list(node.decorator_list)),
        dump(node.returns),
        getattr(node, "type_comment", None),
        # getattr keeps the contract cross-version safe without weakening it: interpreters that
        # support PEP 695 type parameters contribute them, older ones contribute an empty tuple.
        dump(list(getattr(node, "type_params", []) or [])),
        tuple(dump(stmt) for stmt in body),
    )


def canonical_helper_contract(name):
    return _helper_definition_contract(parse_source(CANONICAL_HELPER_SOURCES[name]).body[0])


# Names the reviewed helper bodies close over. Their canonical declarations are accepted; any
# other binding would silently change the helpers' reviewed semantics.
SANCTIONED_HELPER_DEPENDENCIES = ("ROOT", "REPO_DEPENDENCIES", "Path")

# Calls that must never appear inside a sanctioned helper body: they could read, copy or
# transmit repository content while wearing the exemption. Defence in depth only — the exact
# definition contract above is the authority.
HELPER_FORBIDDEN_CALLS = frozenset({
    "copyfile", "copy", "copy2", "copytree", "move", "run", "Popen", "check_call",
    "check_output", "call", "system", "popen", "getattr", "attrgetter", "import_module",
    "__import__", "eval", "exec", "compile",
})


def _sanctioned_helper_findings(tree, require_all=True):
    """Exact full-definition contract for the sanctioned helpers.

    Returns ``(problems, qualified_names)``. With ``require_all`` the source must declare exactly
    one top-level ``FunctionDef`` for each helper — that is the real-module boundary. Generic
    fixture snippets may pass ``require_all=False`` to waive irrelevant presence noise; that
    waiver is never evidence that the real module satisfies the contract.
    """
    problems = []
    qualified = set()
    for name in SANCTIONED_READ_HELPERS:
        top_level = [stmt for stmt in tree.body
                     if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and stmt.name == name]
        nested = [node for node in ast.walk(tree)
                  if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and node.name == name and all(node is not top for top in top_level)]
        if not top_level:
            if nested:
                problems.append("%s:sanctioned_helper_not_top_level" % name)
            if require_all:
                problems.append("%s:sanctioned_helper_missing" % name)
            continue
        if len(top_level) > 1:
            problems.append("%s:sanctioned_helper_duplicate" % name)
            continue
        definition = top_level[0]
        if definition.decorator_list:
            # A decorator can replace the runtime callable even when the body is byte-identical.
            problems.append("%s:sanctioned_helper_decorated" % name)
            continue
        if _helper_definition_contract(definition) != canonical_helper_contract(name):
            problems.append("%s:sanctioned_helper_body_mismatch" % name)
            continue
        forbidden = False
        for node in ast.walk(definition):
            if isinstance(node, ast.Call):
                func = node.func
                label = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if label in HELPER_FORBIDDEN_CALLS:
                    problems.append("%s:sanctioned_helper_body_mismatch" % name)
                    forbidden = True
                    break
        if not forbidden:
            qualified.add(name)
    return sorted(set(problems)), qualified


def sanctioned_helper_contract_violations(source, require_all=True):
    """Public contract check. ``require_all=True`` is the real-module boundary."""
    problems, _ = _sanctioned_helper_findings(parse_source(source), require_all=require_all)
    return problems


def _sanctioned_binding_findings(tree):
    """Every non-canonical binding, shadowing or callable capture of a sanctioned identifier.

    An exact definition is worthless if the name can later point somewhere else, so any write to
    a helper identifier — before or after its definition — and any load that is not the callee of
    a direct call is rejected. The helpers' closed dependencies (``ROOT``, ``REPO_DEPENDENCIES``,
    ``Path``) may be declared exactly once in their canonical form and never rebound.
    """
    problems = []
    tracked_helpers = set(SANCTIONED_READ_HELPERS)
    tracked = tracked_helpers | set(SANCTIONED_HELPER_DEPENDENCIES)

    canonical_definition_ids = set()
    first_seen = set()
    for stmt in tree.body:
        if (isinstance(stmt, ast.FunctionDef) and stmt.name in tracked_helpers
                and stmt.name not in first_seen):
            first_seen.add(stmt.name)
            canonical_definition_ids.add(id(stmt))
    direct_callee_ids = {id(node.func) for node in ast.walk(tree)
                         if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                         and node.func.id in tracked_helpers}

    # The single canonical declaration of each closed dependency.
    canonical_dependency_ids = set()
    declared = set()
    for stmt in tree.body:
        if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and stmt.targets[0].id in SANCTIONED_HELPER_DEPENDENCIES
                and stmt.targets[0].id not in declared):
            declared.add(stmt.targets[0].id)
            canonical_dependency_ids.add(id(stmt.targets[0]))
        elif isinstance(stmt, ast.ImportFrom) and stmt.module == "pathlib":
            for alias in stmt.names:
                bound = alias.asname or alias.name
                if bound in SANCTIONED_HELPER_DEPENDENCIES and bound not in declared:
                    declared.add(bound)
                    canonical_dependency_ids.add(id(alias))

    def rebound(name):
        problems.append("%s:sanctioned_helper_rebound" % name)

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in tracked and id(node) not in canonical_definition_ids:
                rebound(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                if bound in tracked and id(alias) not in canonical_dependency_ids:
                    rebound(bound)
        elif isinstance(node, ast.arg):
            if node.arg in tracked:
                rebound(node.arg)
        elif isinstance(node, ast.ExceptHandler):
            if node.name in tracked:
                rebound(node.name)
        elif isinstance(node, ast.Global) or isinstance(node, ast.Nonlocal):
            for bound in node.names:
                if bound in tracked:
                    rebound(bound)
        elif isinstance(node, ast.Name):
            if node.id not in tracked:
                continue
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                if id(node) not in canonical_dependency_ids:
                    rebound(node.id)
            elif isinstance(node.ctx, ast.Load) and node.id in tracked_helpers:
                if id(node) not in direct_callee_ids:
                    problems.append("%s:sanctioned_helper_binding_escape" % node.id)
        elif hasattr(ast, "MatchAs") and isinstance(node, ast.MatchAs):
            if node.name in tracked:
                rebound(node.name)
        elif hasattr(ast, "MatchStar") and isinstance(node, ast.MatchStar):
            if node.name in tracked:
                rebound(node.name)
        elif hasattr(ast, "MatchMapping") and isinstance(node, ast.MatchMapping):
            if node.rest in tracked:
                rebound(node.rest)
    return sorted(set(problems))


def sanctioned_helper_binding_violations(source):
    """Public binding-integrity check for the sanctioned helpers and their dependencies."""
    return _sanctioned_binding_findings(parse_source(source))


# Container constructors that cannot read or transmit content; used only when every argument is
# itself proven safe.
PURE_CONTAINER_CONSTRUCTORS = frozenset({"dict", "list", "tuple", "set", "frozenset"})


def _default_is_provably_safe(node, resolve, is_shadowed, seen=None):
    """Positive proof that a default expression cannot carry a path or a reader.

    ``resolve``/``is_shadowed`` come from _default_resolver, so a name is proven only through the
    relevant lexical scope and definition-time ordering. Anything not proven — an imported,
    unresolved, cross-scope, multiply bound, out-of-order or cyclic name, a shadowed container
    constructor, an attribute, a subscript, a dynamic call or an unsupported AST form — is unsafe.
    """
    if node is None:
        return True
    if seen is None:
        seen = frozenset()
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(_default_is_provably_safe(element, resolve, is_shadowed, seen)
                   for element in node.elts)
    if isinstance(node, ast.Dict):
        return (all(_default_is_provably_safe(key, resolve, is_shadowed, seen)
                    for key in node.keys if key is not None)
                and all(_default_is_provably_safe(value, resolve, is_shadowed, seen)
                        for value in node.values))
    if isinstance(node, ast.JoinedStr):
        return all(_default_is_provably_safe(part, resolve, is_shadowed, seen)
                   for part in node.values)
    if isinstance(node, ast.FormattedValue):
        return _default_is_provably_safe(node.value, resolve, is_shadowed, seen)
    if isinstance(node, ast.Call):
        if (isinstance(node.func, ast.Name) and node.func.id in PURE_CONTAINER_CONSTRUCTORS
                and not is_shadowed(node.func.id)):
            # A pure container constructor, and only while the name is still the builtin.
            return (all(_default_is_provably_safe(argument, resolve, is_shadowed, seen)
                        for argument in node.args)
                    and all(_default_is_provably_safe(keyword.value, resolve, is_shadowed, seen)
                            for keyword in node.keywords))
        return False
    if isinstance(node, ast.Name):
        if node.id in seen:
            return False                      # cyclic binding: cannot be proven
        value = resolve(node.id)
        if value is None:
            return False
        return _default_is_provably_safe(value, resolve, is_shadowed, seen | {node.id})
    return False


def repository_read_violations(source):
    """Fail-closed AST guard: the literal registry reader is the ONLY repository-read route.

    Returns sorted "<line>:<kind>" violations. The policy is deliberately conservative: aliases
    of ``open``, captured bound reader methods, dynamic attribute access, dynamic namespace
    mutation, repository-derived path taint, wrappers, lambdas, comprehensions, container-wrapped
    helper returns, cross-scope default names and unresolved indirect calls are all rejected.
    Anything that cannot be proven safe is reported rather than accepted.
    """
    tree = parse_source(source)
    violations = []

    def flag(node, kind):
        violations.append("%d:%s" % (getattr(node, "lineno", 0), kind))

    def is_repo_path_call(node):
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "repo_path")

    def literal_registered_key(node):
        args = getattr(node, "args", [])
        return (len(args) == 1 and isinstance(args[0], ast.Constant)
                and isinstance(args[0].value, str) and args[0].value in REPO_DEPENDENCIES)

    # ---- A7-1/A7-2: the closed dependency boundary gates EVERY exemption ---- #
    # An exact helper body proves nothing unless the declarations it closes over are the reviewed
    # ones and cannot be replaced through a dynamic namespace. Until both hold, the root anchor
    # keeps no exemption, every helper is treated as compromised, and the underlying root
    # derivations and reads stay visible to the checks below.
    scope_index = _build_scope_index(tree)
    dependency_problems, dependencies_intact = _closed_dependency_findings(tree)
    namespace_problems, namespace_compromised, namespace_broke_dependencies = (
        _dynamic_namespace_findings(tree, scope_index=scope_index))
    # A8: the protected objects themselves, and the dangerous callable authorities, are part of
    # the same boundary -- a patched `Path`/`types` or a captured namespace producer changes what
    # the reviewed declarations and helper bodies mean without touching any tracked name.
    authority_problems, authority_broke_dependencies = _protected_authority_findings(
        tree, scope_index=scope_index)
    if namespace_broke_dependencies or authority_broke_dependencies:
        dependencies_intact = False

    # ---- Sanctioned boundary: only helpers matching the EXACT reviewed definition ---- #
    # Nested functions, methods, duplicates and same-named definitions never inherit exemption,
    # and neither does a helper whose full definition has drifted from its canonical contract or
    # whose identifier can be rebound. Generic snippets waive presence noise; the real-module
    # boundary is the explicit require_all=True call in
    # test_real_module_requires_all_three_exact_immutable_helpers.
    helper_problems, qualified_helpers = _sanctioned_helper_findings(tree, require_all=False)
    binding_problems = _sanctioned_binding_findings(tree)
    for problem in (dependency_problems + namespace_problems + authority_problems
                    + helper_problems + binding_problems):
        violations.append("0:%s" % problem.split(":", 1)[1])
    compromised_helpers = {problem.split(":", 1)[0]
                           for problem in helper_problems + binding_problems}
    compromised_helpers |= set(namespace_compromised)
    if not dependencies_intact:
        compromised_helpers |= set(SANCTIONED_READ_HELPERS)

    scope_of, scope_parent, scope_bindings, scope_definitions = scope_index

    top_level_helpers = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.FunctionDef) and stmt.name in SANCTIONED_READ_HELPERS:
            top_level_helpers.setdefault(stmt.name, []).append(stmt)
    sanctioned_ids = set()
    for helper_name, defs in top_level_helpers.items():
        if (len(defs) == 1 and helper_name in qualified_helpers
                and helper_name not in compromised_helpers):
            for sub in ast.walk(defs[0]):
                sanctioned_ids.add(id(sub))

    # The single module-level ROOT anchor is the one permitted checkout derivation, and only
    # while the exact closed-dependency declarations hold: a widened anchor cannot exempt itself.
    anchor_ids = set()
    if dependencies_intact:
        for stmt in tree.body:
            if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Name) and stmt.targets[0].id == "ROOT"):
                for sub in ast.walk(stmt):
                    anchor_ids.add(id(sub))

    # A literal registry resolution seeds taint unconditionally — failing closed means MORE
    # taint, never less — but confers read authority only while the boundary is clean.
    registered_names = set()
    for stmt in tree.body:
        if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and is_repo_path_call(stmt.value) and literal_registered_key(stmt.value)):
            registered_names.add(stmt.targets[0].id)

    module_level_defs = {stmt.name for stmt in tree.body
                         if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    imported_names = set()
    open_aliases = {"open"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported_names.add(alias.asname or alias.name.split(".")[0])
                if alias.name == "open":
                    open_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported_names.add(alias.asname or alias.name.split(".")[0])

    tainted_names = {"ROOT"} | set(registered_names)
    reader_names = set()
    # ---- A7-3: return summaries keyed by definition IDENTITY, not by bare name ---- #
    # Two same-named functions in sibling scopes, and a nested definition shadowing a
    # module-level one, must never share a summary.
    defs_returning_taint = set()
    defs_returning_reader = set()

    # ---- A5-2: lexically scoped default-bound taint ---- #
    # Parent links let a Name consult the parameters of every ENCLOSING function or lambda, so a
    # default value taints only its own scope and the closures nested inside it. A same-named
    # parameter in a sibling scope is unaffected.
    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    scopes = [node for node in ast.walk(tree)
              if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))]
    scope_tainted = {id(scope): set() for scope in scopes}
    scope_readers = {id(scope): set() for scope in scopes}
    resolvers = {id(scope): _default_resolver(scope, scope_of, scope_parent, scope_bindings, tree)
                 for scope in scopes}

    def enclosing_scopes(node):
        found = []
        current = parents.get(id(node))
        while current is not None:
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                found.append(current)
            current = parents.get(id(current))
        return found

    def resolve_call_definitions(node):
        return _resolve_call_definitions(node, scope_of, scope_parent, scope_bindings,
                                         scope_definitions, tree)

    def is_tainted(node):
        if node is None:
            return False
        if isinstance(node, ast.Name):
            if node.id in tainted_names or node.id == "__file__":
                return True
            for scope in enclosing_scopes(node):
                if node.id in scope_tainted.get(id(scope), ()):
                    return True
            return False
        if isinstance(node, ast.Call):
            if is_repo_path_call(node):
                return True
            if isinstance(node.func, ast.Name):
                if node.func.id == "Path":
                    return any(is_tainted(arg) for arg in node.args)
                if node.func.id in PURE_TAINT_SAFE_CALLABLES:
                    # str(<repo path>) is still a repository path in string form.
                    return any(is_tainted(arg) for arg in node.args)
            candidates, ambiguous = resolve_call_definitions(node)
            if candidates and (ambiguous
                               or any(id(definition) in defs_returning_taint
                                      for definition in candidates)):
                return True
            if isinstance(node.func, ast.Attribute):
                if node.func.attr in ("resolve", "absolute", "expanduser", "joinpath"):
                    return is_tainted(node.func.value)
                if node.func.attr in PURE_LEXICAL_METHODS:
                    return is_tainted(node.func.value)
            return False
        if isinstance(node, ast.Attribute):
            if node.attr in ("parent", "parents"):
                return is_tainted(node.value)
            return False
        if isinstance(node, ast.Subscript):
            return is_tainted(node.value)
        if isinstance(node, ast.BinOp):
            if isinstance(node.op, (ast.Div, ast.Add, ast.Mod)):
                return is_tainted(node.left) or is_tainted(node.right)
            return False
        if isinstance(node, ast.JoinedStr):
            return any(is_tainted(part) for part in node.values)
        if isinstance(node, ast.FormattedValue):
            return is_tainted(node.value)
        return False

    def contains_taint(node):
        """Recursive taint search: containers, kwargs, starred args, comprehensions, f-strings."""
        if node is None:
            return False
        return any(is_tainted(sub) for sub in ast.walk(node))

    def contains_reader(node):
        if node is None:
            return False
        return any(is_reader(sub) for sub in ast.walk(node))

    def dynamic_attribute_problem(node):
        """True when a dynamic-attribute or dynamic-import call could yield a repository reader.

        Fails closed whenever the attribute name cannot be statically resolved.
        """
        if not isinstance(node, ast.Call):
            return False
        name = None
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if name in ("import_module", "__import__"):
            return True
        if name == "getattr":
            args = node.args
            if len(args) < 2:
                return True
            attribute = args[1]
            if not (isinstance(attribute, ast.Constant) and isinstance(attribute.value, str)):
                return True                      # unresolvable attribute name
            return attribute.value in REPO_READ_METHODS or is_tainted(args[0])
        if name == "attrgetter":
            args = node.args
            if not args:
                return True
            first = args[0]
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                return True
            return first.value in REPO_READ_METHODS
        return False

    def is_reader(node):
        if node is None:
            return False
        if isinstance(node, ast.Name):
            if node.id in open_aliases or node.id in reader_names:
                return True
            for scope in enclosing_scopes(node):
                if node.id in scope_readers.get(id(scope), ()):
                    return True
            return False
        if isinstance(node, ast.Attribute):
            return node.attr in REPO_READ_METHODS
        if isinstance(node, ast.Subscript):
            # A reader taken back out of a container the helper handed over.
            return is_reader(node.value)
        if isinstance(node, ast.Call):
            if dynamic_attribute_problem(node):
                return True
            candidates, _ambiguous = resolve_call_definitions(node)
            if any(id(definition) in defs_returning_reader for definition in candidates):
                return True
            return False
        return False

    def assigned_names(targets):
        names = set()
        for target in targets:
            for sub in ast.walk(target):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
        return names

    # Fixpoint so alias chains and container-wrapped helper returns propagate.
    # Monotonic over finite sets, so this converges long before the bound; the bound is
    # derived from the finite AST universe purely to guarantee deterministic termination.
    convergence_bound = sum(1 for _ in ast.walk(tree)) + 2
    rounds = 0
    while True:
        rounds += 1
        before = (len(tainted_names), len(reader_names),
                  len(defs_returning_taint), len(defs_returning_reader),
                  sum(len(v) for v in scope_tainted.values()),
                  sum(len(v) for v in scope_readers.values()))
        for node in ast.walk(tree):
            if id(node) in sanctioned_ids:
                continue
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
                # contains_taint, not is_tainted: a list/tuple/set/dict holding a repository
                # path taints the name, so later *args / **kwargs forwarding is still caught.
                targets = (node.targets if isinstance(node, ast.Assign) else [node.target])
                value = getattr(node, "value", None)
                if contains_taint(value):
                    tainted_names |= assigned_names(targets)
                if contains_reader(value):
                    reader_names |= assigned_names(targets)
            elif isinstance(node, ast.Return):
                # A7-3: recursive containment, attributed to the definition that OWNS the return.
                owner = scope_of.get(id(node))
                if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if contains_taint(node.value):
                        defs_returning_taint.add(id(owner))
                    if contains_reader(node.value):
                        defs_returning_reader.add(id(owner))
            elif isinstance(node, ast.Lambda):
                if contains_taint(node.body):
                    defs_returning_taint.add(id(node))
                if contains_reader(node.body):
                    defs_returning_reader.add(id(node))
        # Default-bound parameters, mapped correctly and confined to their own scope.
        for scope in scopes:
            resolve, is_shadowed = resolvers[id(scope)]
            arguments = scope.args
            positional = list(getattr(arguments, "posonlyargs", [])) + list(arguments.args)
            defaults = list(arguments.defaults)
            pairs = []
            if defaults:
                pairs.extend(zip(positional[len(positional) - len(defaults):], defaults))
            pairs.extend((param, default)
                         for param, default in zip(arguments.kwonlyargs, arguments.kw_defaults)
                         if default is not None)
            for param, default in pairs:
                if contains_taint(default):
                    scope_tainted[id(scope)].add(param.arg)
                elif contains_reader(default):
                    scope_readers[id(scope)].add(param.arg)
                elif not _default_is_provably_safe(default, resolve, is_shadowed):
                    scope_tainted[id(scope)].add(param.arg)   # unproven: fail closed
        after = (len(tainted_names), len(reader_names),
                 len(defs_returning_taint), len(defs_returning_reader),
                 sum(len(v) for v in scope_tainted.values()),
                 sum(len(v) for v in scope_readers.values()))
        if before == after or rounds >= convergence_bound:
            break

    def receiver_is_registered(node):
        # Registry-derived read authority requires the exact registry AND an unreplaced resolver.
        if not dependencies_intact or "repo_path" in compromised_helpers:
            return False
        if isinstance(node, ast.Name):
            return node.id in registered_names
        if is_repo_path_call(node):
            return literal_registered_key(node)
        return False

    def call_may_receive_taint(node):
        """True only for the exact permitted registry operation or a pure conversion.

        Everything else — attribute calls, imported callables, module-level definitions,
        dynamically selected callables — is ambiguous by default and fails closed.
        """
        func = node.func
        if isinstance(func, ast.Name):
            if func.id in PURE_TAINT_SAFE_CALLABLES:
                return True
            if func.id in ("repo_path", "read_repo_text"):
                if not dependencies_intact or func.id in compromised_helpers:
                    return False          # the name no longer refers to the reviewed helper
                return literal_registered_key(node)
        return False

    # Attributes that are the callee of a call, so a bare reference to a reader method (capture,
    # storage, passing, returning) is distinguishable from an immediate call.
    called_attributes = {id(node.func) for node in ast.walk(tree)
                         if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}

    for node in ast.walk(tree):
        if id(node) in sanctioned_ids or id(node) in anchor_ids:
            continue

        # ---- Aliases of the built-in reader ---- #
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "open":
                    flag(node, "open_alias_import")
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in open_aliases:
            flag(node, "builtin_open")

        # ---- Bound path-reader methods: captured, passed, stored or called unsafely ---- #
        if isinstance(node, ast.Attribute) and node.attr in REPO_READ_METHODS:
            if id(node) not in called_attributes:
                flag(node, "bound_reader_capture")

        # ---- Dynamic attribute access ---- #
        if isinstance(node, ast.Call):
            func = node.func
            if dynamic_attribute_problem(node):
                flag(node, "dynamic_attribute_access")

            # ---- Registry helper keys ---- #
            if isinstance(func, ast.Name) and func.id in ("repo_path", "read_repo_text"):
                if not literal_registered_key(node):
                    args = getattr(node, "args", [])
                    if len(args) == 1 and isinstance(args[0], ast.Constant) and isinstance(args[0].value, str):
                        flag(node, "unregistered_dependency_key")
                    else:
                        flag(node, "dynamic_dependency_key")

            # ---- Reads ---- #
            if isinstance(func, ast.Attribute) and func.attr in REPO_READ_METHODS:
                if not receiver_is_registered(func.value):
                    flag(node, "unresolved_repository_read")
            if isinstance(func, ast.Name) and func.id in reader_names:
                flag(node, "reader_callable_invocation")
            # A reader taken straight back out of a container or another call.
            if isinstance(func, (ast.Subscript, ast.Call)) and is_reader(func):
                flag(node, "reader_callable_invocation")
            if isinstance(func, ast.Name) and func.id == "Path" and any(is_tainted(a) for a in node.args):
                flag(node, "path_constructor_from_root")
            if isinstance(func, ast.Attribute) and func.attr == "joinpath" and is_tainted(func.value):
                flag(node, "root_joinpath")

            # ---- Any callable receiving repository taint, recursively ---- #
            # Positional args, keyword values, *args, **kwargs, and anything nested inside
            # lists, tuples, sets, dicts (keys and values), comprehensions, generator
            # expressions and f-strings.
            arguments = list(node.args) + [kw.value for kw in node.keywords]
            if not call_may_receive_taint(node):
                if any(contains_reader(arg) for arg in arguments):
                    flag(node, "reader_callable_escape")
                elif any(contains_taint(arg) for arg in arguments):
                    flag(node, "repository_path_escape")

            # ---- Method calls ON a tainted receiver ---- #
            # Only lexical text operations are permitted; anything that could read, stat, copy
            # or transmit the file fails closed.
            if (isinstance(func, ast.Attribute) and is_tainted(func.value)
                    and func.attr not in PURE_LEXICAL_METHODS):
                flag(node, "repository_path_escape")

        # ---- Repository-derived path arithmetic ---- #
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div) and is_tainted(node.left):
            flag(node, "root_path_derivation")

        # ---- Escaping returns ---- #
        if isinstance(node, ast.Return):
            if contains_reader(node.value):
                flag(node, "reader_callable_escape")
            elif contains_taint(node.value):
                flag(node, "repository_path_escape")

        # ---- Lambdas that read or hand back a reader ---- #
        if isinstance(node, ast.Lambda):
            if contains_reader(node.body):
                flag(node, "reader_callable_escape")

        # ---- Defaults that cannot be statically proven safe ---- #
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            resolve, is_shadowed = resolvers[id(node)]
            every_default = (list(node.args.defaults)
                             + [d for d in node.args.kw_defaults if d is not None])
            for default in every_default:
                if contains_taint(default) or contains_reader(default):
                    continue              # a more specific taint category already applies
                if not _default_is_provably_safe(default, resolve, is_shadowed):
                    flag(node, "ambiguous_default_binding")

    return sorted(set(violations))

WRITE_SWITCHES = (
    "EnableExpiryCapabilityProbe",
    "ConfirmSyntheticExpiryDateTest",
    "ConfirmSingleSyntheticMember",
    "ConfirmAutoCountWrite",
    "ConfirmDryRunPreflightPassed",
    "ConfirmNoUpdateOrDelete",
)

# The one canonical claim/result root, duplicated here on purpose: the test asserts the
# reviewed code constant is exactly this value, so the literal must not be imported from it.
CANONICAL_STATE_ROOT = r"C:\XB\create_uat\expiry_probe_state"
SCHEMA_VERSION = "member_expiry_capability_probe/v1"
PUBLICATION_CONTRACT_VERSION = "member_expiry_capability_probe_publication/v1"

# An active probe run with a valid approval reference reaches the canonical root. On a machine
# where that root exists (the AutoCount VM) such a run would create a real artefact inside the
# operator's evidence root, so those script-level tests are skipped there. The equivalent
# behaviour is covered unconditionally at pure-library level with an injected root.
CANONICAL_ROOT_PRESENT = os.path.isdir(CANONICAL_STATE_ROOT)
CANONICAL_ROOT_SKIP = (
    "the canonical probe state root exists on this machine; refusing to run an active probe "
    "that would create an artefact inside the operator's evidence root (library-level tests "
    "cover the trusted-root behaviour)"
)

TERMINAL_CODES = (
    "REFUSED",
    "CLAIM_ROOT_UNAVAILABLE",
    "ATTEMPT_ALREADY_CLAIMED",
    "ATTEMPT_CLAIM_LOST_AFTER_CONTACT",
    "CLAIM_PERSISTENCE_FAILED",
    "BLOCKED_MEMBER_EXISTS",
    "FAILED_BEFORE_WRITE",
    "WRITE_OUTCOME_UNCERTAIN",
    "WRITE_CONFIRMED_READBACK_FAILED",
    "EXPIRY_READBACK_MISMATCH",
    "EXPIRY_VERIFIED",
    "EVIDENCE_PERSISTENCE_FAILED",
)


def find_powershell():
    for exe in ("pwsh", "powershell", "powershell.exe"):
        found = shutil.which(exe)
        if found:
            return found
    return None


PS = find_powershell()
IS_WINDOWS = sys.platform.startswith("win")


def as_list(value):
    """Normalise a PowerShell-serialised array that may collapse to a scalar or null."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


# ---- A3-1: the closed authoritative-result schema ---- #
# These categories mirror the reviewed $result object emitted by the unchanged probe script.
# test_library_schema_matches_the_probe_result_contract proves the library's declared schema and
# this expectation both still match that script exactly.
AUTHORITATIVE_BOOLEAN_FIELDS = (
    "state_root_trusted", "claim_root_unavailable", "activated",
    "confirm_synthetic_expiry_test", "confirm_single_synthetic", "confirm_auto_count_write",
    "confirm_dry_run_preflight", "confirm_no_update_or_delete", "ac_root_exists",
    "required_assemblies_loaded", "autocount_contacted", "authentication_success",
    "member_command_found", "get_member_found", "initial_member_read_attempted",
    "member_exists_initial", "new_member_success", "assignment_success", "expiry_date_assigned",
    "member_recheck_attempted", "member_exists_recheck", "claim_created", "claim_conflict",
    "claim_lost_after_contact", "claim_persist_failed", "save_member_method_found",
    "save_member_attempted", "save_member_confirmed", "readback_found", "expiry_match",
    "synthetic_member_may_remain", "evidence_persisted", "non_authoritative_staging_may_remain",
)
AUTHORITATIVE_STRING_FIELDS = (
    "schema_version", "mode", "operation_id", "approval_reference", "executed_at_utc",
    "target_fingerprint", "synthetic_fingerprint", "attempt_fingerprint",
    "intended_expiry_date", "claim_basename", "result_basename", "staging_basename",
    "save_outcome", "masked_member_no", "residual_record_note",
    "underlying_terminal_outcome", "terminal_outcome",
)
AUTHORITATIVE_NULLABLE_STRING_FIELDS = ("readback_error", "expiry_date_readback_value")
AUTHORITATIVE_INTEGRAL_FIELDS = ("exit_code",)
AUTHORITATIVE_ARRAY_FIELDS = ("claim_root_failure_reasons",)
AUTHORITATIVE_OBJECT_FIELDS = ("publication_contract",)
AUTHORITATIVE_NULLABLE_OBJECT_FIELDS = ("error",)

AUTHORITATIVE_TOP_LEVEL_FIELDS = (
    AUTHORITATIVE_BOOLEAN_FIELDS + AUTHORITATIVE_STRING_FIELDS
    + AUTHORITATIVE_NULLABLE_STRING_FIELDS + AUTHORITATIVE_INTEGRAL_FIELDS
    + AUTHORITATIVE_ARRAY_FIELDS + AUTHORITATIVE_OBJECT_FIELDS
    + AUTHORITATIVE_NULLABLE_OBJECT_FIELDS
)

PUBLICATION_CONTRACT_FIELDS = (
    "publication_contract_version", "authoritative_result_basename", "authority_rule",
)

AUTHORITY_RULE_TEXT = (
    "This artefact is authoritative ONLY when its current file basename is exactly equal to "
    "authoritative_result_basename. Any other basename, including an "
    "expiry_probe_staging_<operation_id>.incomplete staging artefact, is NON-AUTHORITATIVE "
    "regardless of the terminal_outcome, evidence_persisted or exit_code it contains."
)


def _field_setter(field, value):
    """Build a record mutation without binding the value through a parameter default."""
    def mutate(record):
        record[field] = value
    return mutate


def verified_record(operation_id="expop_authoritative01"):
    """A complete, correctly typed, internally consistent authoritative EXPIRY_VERIFIED record.

    Every top-level field the reviewed probe emits is present with its real CLR type: actual
    Booleans, an actual integral exit code and actual strings. Adversarial tests mutate exactly
    one field at a time from this baseline.
    """
    final_basename = "expiry_probe_result_%s.json" % operation_id
    record = {
        "schema_version": SCHEMA_VERSION,
        "mode": "member-expiry-capability-probe",
        "operation_id": operation_id,
        "approval_reference": "APPROVAL-TEST-001",
        "executed_at_utc": "2026-08-03T00:00:00Z",
        "target_fingerprint": "tfp_" + ("a" * 64),
        "synthetic_fingerprint": "smf_" + ("b" * 64),
        "attempt_fingerprint": "afp_" + ("c" * 64),
        "intended_expiry_date": "2028-06-30",
        "claim_basename": "expiry_probe_claim_afp_%s.claim" % ("c" * 64),
        "result_basename": final_basename,
        "staging_basename": "expiry_probe_staging_%s.incomplete" % operation_id,
        "publication_contract": {
            "publication_contract_version": PUBLICATION_CONTRACT_VERSION,
            "authoritative_result_basename": final_basename,
            "authority_rule": AUTHORITY_RULE_TEXT,
        },
        "claim_root_failure_reasons": [],
        "save_outcome": "confirmed",
        "masked_member_no": "XB***1",
        "residual_record_note": "No automatic member update, delete, rollback, or cleanup is performed.",
        "readback_error": None,
        "expiry_date_readback_value": "2028-06-30",
        "underlying_terminal_outcome": "EXPIRY_VERIFIED",
        "terminal_outcome": "EXPIRY_VERIFIED",
        "exit_code": 0,
        "error": None,
    }
    true_flags = (
        "state_root_trusted", "activated", "confirm_synthetic_expiry_test",
        "confirm_single_synthetic", "confirm_auto_count_write", "confirm_dry_run_preflight",
        "confirm_no_update_or_delete", "ac_root_exists", "required_assemblies_loaded",
        "autocount_contacted", "authentication_success", "member_command_found",
        "get_member_found", "initial_member_read_attempted", "new_member_success",
        "assignment_success", "expiry_date_assigned", "member_recheck_attempted",
        "claim_created", "save_member_method_found", "save_member_attempted",
        "save_member_confirmed", "readback_found", "expiry_match",
        "synthetic_member_may_remain", "evidence_persisted",
    )
    for field in AUTHORITATIVE_BOOLEAN_FIELDS:
        record[field] = field in true_flags
    return record


INSPECTOR = r"""
[CmdletBinding()]
param([Parameter(Mandatory)][string]$Path)
$ErrorActionPreference = 'Stop'
$tokens = $null; $errs = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$errs)
$paramNames = @()
if ($null -ne $ast.ParamBlock) {
    $paramNames = @($ast.ParamBlock.Parameters | ForEach-Object { $_.Name.VariablePath.UserPath })
}
$funcs = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)
$narrow = $funcs | Where-Object { $_.Name -eq 'Invoke-ExpiryProbeSaveMemberOnce' } | Select-Object -First 1
$calls = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true) |
    Where-Object { $_.GetCommandName() -eq 'Invoke-ExpiryProbeSaveMemberOnce' })
$removeCalls = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true) |
    Where-Object { @('Remove-Item', 'ri', 'del', 'erase', 'rd') -contains $_.GetCommandName() })
$invokes = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.InvokeMemberExpressionAst] }, $true))
$saveInvokes = @($invokes | Where-Object {
    $_.Member -is [System.Management.Automation.Language.StringConstantExpressionAst] -and
    $_.Member.Value -eq 'Invoke' -and $_.Expression.Extent.Text -match 'saveMemberMethod|SaveMemberMethod' })
$getInvokes = @($invokes | Where-Object {
    $_.Member -is [System.Management.Automation.Language.StringConstantExpressionAst] -and
    $_.Member.Value -eq 'Invoke' -and $_.Expression.Extent.Text -match 'getMemberMethod' })
$forbidden = @($invokes | Where-Object {
    $_.Member -is [System.Management.Automation.Language.StringConstantExpressionAst] -and
    @('DeleteMember','UpdateMember','RemoveMember','Rollback','DeleteMemberType','SaveMemberType') -contains $_.Member.Value })
$fileApiInvokes = @($invokes | Where-Object {
    $_.Member -is [System.Management.Automation.Language.StringConstantExpressionAst] -and
    $_.Expression.Extent.Text -match '^\[(System\.)?IO\.File\]$|^\[System\.IO\.File\]$' })
$fileMoveInvokes = @($fileApiInvokes | Where-Object { $_.Member.Value -eq 'Move' })
$fileDeleteInvokes = @($fileApiInvokes | Where-Object { @('Delete','Replace') -contains $_.Member.Value })
$saveInsideNarrow = [bool]$narrow
if ($narrow) {
    foreach ($si in $saveInvokes) {
        if ($si.Extent.StartOffset -lt $narrow.Extent.StartOffset -or $si.Extent.EndOffset -gt $narrow.Extent.EndOffset) {
            $saveInsideNarrow = $false
        }
    }
}
$loopNames = 'WhileStatementAst', 'ForStatementAst', 'DoWhileStatementAst', 'DoUntilStatementAst', 'ForEachStatementAst'
$callInLoop = $false
$saveCallOffset = -1
foreach ($c in $calls) {
    if ($saveCallOffset -lt 0) { $saveCallOffset = $c.Extent.StartOffset }
    $p = $c.Parent
    while ($null -ne $p) { if ($loopNames -contains $p.GetType().Name) { $callInLoop = $true }; $p = $p.Parent }
}
$minGet = -1
foreach ($g in $getInvokes) { if ($minGet -lt 0 -or $g.Extent.StartOffset -lt $minGet) { $minGet = $g.Extent.StartOffset } }
$dupBeforeSave = ($minGet -ge 0 -and $saveCallOffset -ge 0 -and $minGet -lt $saveCallOffset)
[pscustomobject]@{
    parseErrors            = @($errs).Count
    paramNames             = $paramNames
    funcExists             = [bool]$narrow
    callCount              = $calls.Count
    saveInvokeCount        = $saveInvokes.Count
    saveInsideNarrow       = $saveInsideNarrow
    callInLoop             = $callInLoop
    getMemberInvokeCount   = $getInvokes.Count
    dupCheckBeforeSave     = $dupBeforeSave
    forbiddenMutationCount = $forbidden.Count
    removeItemCount        = $removeCalls.Count
    fileMoveCount          = $fileMoveInvokes.Count
    fileDeleteCount        = $fileDeleteInvokes.Count
} | ConvertTo-Json -Compress
"""

LIBPROBE = r"""
[CmdletBinding()]
param([Parameter(Mandatory)][string]$Lib, [Parameter(Mandatory)][string]$Op,
      [string]$CtxJson, [string]$Text, [string]$Dir, [string]$Extra)
$ErrorActionPreference = 'Stop'
. $Lib
switch ($Op) {
    'terminal' {
        $ctx = Get-Content -LiteralPath $CtxJson -Raw -Encoding UTF8 | ConvertFrom-Json
        $code = Get-ExpiryProbeTerminalOutcome -Flags $ctx
        $contr = @(Get-ExpiryProbeStateContradictions -Flags $ctx).Count
        Write-Output ($code + '|' + $contr)
    }
    'contradictions' {
        $ctx = Get-Content -LiteralPath $CtxJson -Raw -Encoding UTF8 | ConvertFrom-Json
        Write-Output (@(Get-ExpiryProbeStateContradictions -Flags $ctx) -join ',')
    }
    'exit' { Write-Output ([string](Get-ExpiryProbeExitCode -TerminalOutcome $Text)) }
    'final' {
        # $Text = "underlying|durableRequired(0/1)|evidencePersisted(0/1)"
        $parts = $Text.Split('|')
        $o = Get-ExpiryProbeFinalOutcome -UnderlyingOutcome $parts[0] -DurableRequired ([bool][int]$parts[1]) -EvidencePersisted ([bool][int]$parts[2])
        Write-Output ($o + '|' + (Get-ExpiryProbeExitCode -TerminalOutcome $o))
    }
    'redact' { Write-Output (Get-ExpiryProbePathRedacted -Text $Text) }
    'vocabulary' { Write-Output (($script:ExpiryProbeTerminalCodes) -join ',') }
    'canonroot' {
        # The production accessor is nullary: it takes no parameters at all, so nothing can
        # redirect the claim/result root. Report the value and the parameter count.
        $cmd = Get-Command Get-ExpiryProbeCanonicalStateRoot
        $declared = @($cmd.Parameters.Keys | Where-Object { @('Verbose','Debug','ErrorAction','WarningAction','InformationAction','ProgressAction','ErrorVariable','WarningVariable','InformationVariable','OutVariable','OutBuffer','PipelineVariable') -notcontains $_ })
        [pscustomobject]@{
            root = (Get-ExpiryProbeCanonicalStateRoot)
            declaredParameterCount = $declared.Count
        } | ConvertTo-Json -Compress
    }
    'trust' {
        # $Dir = root under test; $Text = '1' to require Windows.
        if ($Text -eq '1') { $v = Test-ExpiryProbeTrustedStateRoot -Root $Dir -RequireWindows }
        else { $v = Test-ExpiryProbeTrustedStateRoot -Root $Dir }
        $v | ConvertTo-Json -Compress -Depth 3
    }
    'paths' {
        # $Dir = injected root; $Text = "<attemptFingerprint>|<operationId>"
        $parts = $Text.Split('|')
        $p = Get-ExpiryProbeStatePaths -Root $Dir -AttemptFingerprint $parts[0] -OperationId $parts[1]
        [pscustomobject]@{
            claim_basename    = $p.claim_basename
            result_basename   = $p.result_basename
            staging_basename  = $p.staging_basename
            claim_path        = $p.claim_path
            result_path       = $p.result_path
            staging_path      = $p.staging_path
            pure_claim        = (Get-ExpiryProbeClaimBasename -AttemptFingerprint $parts[0])
            pure_result       = (Get-ExpiryProbeResultBasename -OperationId $parts[1])
            pure_staging      = (Get-ExpiryProbeStagingBasename -OperationId $parts[1])
            cwd               = (Get-Location).Path
        } | ConvertTo-Json -Compress
    }
    'contract' {
        (New-ExpiryProbePublicationContract -OperationId $Text) | ConvertTo-Json -Compress
    }
    'badclaim' {
        # Inject a durable persistence failure: a path under a non-existent directory
        # cannot be created, so New-ExpiryProbeDurableArtifact must throw and leave no
        # usable artefact (proving failures propagate, never a silent success).
        $bad = Join-Path (Join-Path $Dir 'no_such_dir') 'x.claim'
        $threw = $false
        try { New-ExpiryProbeDurableArtifact -Path $bad -Content 'x' } catch { $threw = $true }
        [pscustomobject]@{ threw = $threw; exists = (Test-Path -LiteralPath $bad) } | ConvertTo-Json -Compress
    }
    'fp' {
        $t1 = Get-ExpiryProbeTargetFingerprint -ServerName 'S' -DatabaseName 'D'
        $t2 = Get-ExpiryProbeTargetFingerprint -ServerName 'S' -DatabaseName 'D'
        $tX = Get-ExpiryProbeTargetFingerprint -ServerName 'S' -DatabaseName 'OTHER'
        # Casing AND surrounding-whitespace variants of the same target must collapse.
        $tUpper = Get-ExpiryProbeTargetFingerprint -ServerName 'SERVER\INSTANCE' -DatabaseName 'AED_DB'
        $tLower = Get-ExpiryProbeTargetFingerprint -ServerName '  server\instance ' -DatabaseName ' aed_db '
        $afU = Get-ExpiryProbeAttemptFingerprint -TargetFingerprint $tUpper -SyntheticFingerprint (Get-ExpiryProbeSyntheticFingerprint -MemberNo 'XBEXPIRYPROBE01') -IntendedExpiry '2028-06-30'
        $afL = Get-ExpiryProbeAttemptFingerprint -TargetFingerprint $tLower -SyntheticFingerprint (Get-ExpiryProbeSyntheticFingerprint -MemberNo 'XBEXPIRYPROBE01') -IntendedExpiry '2028-06-30'
        $sm = Get-ExpiryProbeSyntheticFingerprint -MemberNo 'XBEXPIRYPROBE01'
        $af = Get-ExpiryProbeAttemptFingerprint -TargetFingerprint $t1 -SyntheticFingerprint $sm -IntendedExpiry '2028-06-30'
        [pscustomobject]@{
            targetStable = ($t1 -eq $t2); targetDiffers = ($t1 -ne $tX)
            targetShape = ($t1 -match '^tfp_[0-9a-f]{64}$'); attemptShape = ($af -match '^afp_[0-9a-f]{64}$')
            synthShape = ($sm -match '^smf_[0-9a-f]{64}$')
            caseWsInsensitiveTarget = ($tUpper -eq $tLower); caseWsInsensitiveAttempt = ($afU -eq $afL)
        } | ConvertTo-Json -Compress
    }
    'claim' {
        $p = Join-Path $Dir 'x.claim'
        New-ExpiryProbeDurableArtifact -Path $p -Content 'first'
        $blocked = $false
        try { New-ExpiryProbeDurableArtifact -Path $p -Content 'second' } catch { $blocked = $true }
        $content = Get-Content -LiteralPath $p -Raw
        [pscustomobject]@{ exists = (Test-Path -LiteralPath $p); secondBlocked = $blocked; unchanged = ($content.Trim() -eq 'first') } | ConvertTo-Json -Compress
    }
    'claimrace' {
        # One contender. $Text is the shared canonical claim path; exactly one concurrent
        # process can exclusively create it.
        $created = $false
        try { New-ExpiryProbeDurableArtifact -Path $Text -Content 'contender'; $created = $true } catch { $created = $false }
        [pscustomobject]@{ created = $created } | ConvertTo-Json -Compress
    }
    'publish' {
        # $Dir = injected root; $Text = operation id; $CtxJson = staged record bytes.
        $stg = Join-Path $Dir (Get-ExpiryProbeStagingBasename -OperationId $Text)
        $fin = Join-Path $Dir (Get-ExpiryProbeResultBasename -OperationId $Text)
        $content = Get-Content -LiteralPath $CtxJson -Raw -Encoding UTF8
        # The production write-through rename is Windows-only and fails closed elsewhere, so off
        # Windows this portable test drives the same preflight and no-clobber staging contract
        # through the sanctioned pure-test move seam. The real native publication is covered by
        # the Windows-only nativemove and nativemoveraw tests.
        if ([System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT) {
            Publish-ExpiryProbeResultAtomic -StagingPath $stg -FinalPath $fin -Content $content
        }
        else {
            Publish-ExpiryProbeResultAtomic -StagingPath $stg -FinalPath $fin -Content $content `
                -MoveAction { param($s, $d) [System.IO.File]::Move($s, $d) }
        }
        $secondBlocked = $false
        try { Publish-ExpiryProbeResultAtomic -StagingPath $stg -FinalPath $fin -Content 'beta' } catch { $secondBlocked = $true }
        $published = Get-Content -LiteralPath $fin -Raw -Encoding UTF8 | ConvertFrom-Json
        [pscustomobject]@{
            finalExists = (Test-Path -LiteralPath $fin)
            stagingLeft = (Test-Path -LiteralPath $stg)
            secondBlocked = $secondBlocked
            finalOperationId = $published.operation_id
            finalTerminalOutcome = $published.terminal_outcome
        } | ConvertTo-Json -Compress
    }
    'stagingconflict' {
        # A pre-existing staging artefact is never overwritten, truncated or deleted.
        $stg = Join-Path $Dir (Get-ExpiryProbeStagingBasename -OperationId $Text)
        $fin = Join-Path $Dir (Get-ExpiryProbeResultBasename -OperationId $Text)
        New-ExpiryProbeDurableArtifact -Path $stg -Content 'pre-existing staging bytes'
        $blocked = $false
        try { Publish-ExpiryProbeResultAtomic -StagingPath $stg -FinalPath $fin -Content 'new bytes' } catch { $blocked = $true }
        [pscustomobject]@{
            blocked = $blocked
            stagingUnchanged = ((Get-Content -LiteralPath $stg -Raw).Trim() -eq 'pre-existing staging bytes')
            finalExists = (Test-Path -LiteralPath $fin)
        } | ConvertTo-Json -Compress
    }
    'movefail' {
        # Pure dependency injection: simulate a failing final move (e.g. rename permission
        # denied) with no executable-script parameter and no live bypass.
        $stg = Join-Path $Dir (Get-ExpiryProbeStagingBasename -OperationId $Text)
        $fin = Join-Path $Dir (Get-ExpiryProbeResultBasename -OperationId $Text)
        $content = Get-Content -LiteralPath $CtxJson -Raw -Encoding UTF8
        $threw = $false
        try {
            Publish-ExpiryProbeResultAtomic -StagingPath $stg -FinalPath $fin -Content $content `
                -MoveAction { param($s, $d) throw "simulated rename permission denied" }
        }
        catch { $threw = $true }
        # Read the staged bytes back and report only scalars, so the harness never nests one
        # JSON document inside another.
        $stagedBound = ''
        $stagedTerminal = ''
        $stagedExit = -1
        $stagedPersisted = $null
        if (Test-Path -LiteralPath $stg) {
            $staged = Get-Content -LiteralPath $stg -Raw -Encoding UTF8 | ConvertFrom-Json
            $stagedBound = $staged.publication_contract.authoritative_result_basename
            $stagedTerminal = $staged.terminal_outcome
            $stagedExit = [int]$staged.exit_code
            $stagedPersisted = [bool]$staged.evidence_persisted
        }
        [pscustomobject]@{
            threw = $threw
            stagingExists = (Test-Path -LiteralPath $stg)
            finalExists = (Test-Path -LiteralPath $fin)
            stagingBasename = ([System.IO.Path]::GetFileName($stg))
            stagingPath = $stg
            stagedBoundBasename = $stagedBound
            stagedTerminalOutcome = $stagedTerminal
            stagedExitCode = $stagedExit
            stagedEvidencePersisted = $stagedPersisted
        } | ConvertTo-Json -Compress
    }
    'authoritative' {
        # $Text = candidate current path; $CtxJson = parsed record source.
        $record = Get-Content -LiteralPath $CtxJson -Raw -Encoding UTF8 | ConvertFrom-Json
        $v = Test-ExpiryProbeAuthoritativeResult -Path $Text -Record $record
        [pscustomobject]@{ authoritative = $v.authoritative; reasons = @($v.reasons) } | ConvertTo-Json -Compress -Depth 4
    }
    'schemamatrix' {
        # A3-1 adversarial type matrix, executed in ONE process. $CtxJson = a complete, valid
        # baseline record; $Text = comma-separated field names; $Extra = candidate final path.
        $baseText = Get-Content -LiteralPath $CtxJson -Raw -Encoding UTF8
        $bad = New-Object object[] 9
        $bad[0] = 'false'; $bad[1] = 'true'; $bad[2] = 0; $bad[3] = 1; $bad[4] = $null
        $bad[5] = @(); $bad[6] = @(1, 2); $bad[7] = @{}; $bad[8] = @{ injected = 1 }
        $variant = @('string_false', 'string_true', 'int_zero', 'int_one', 'null',
                     'empty_array', 'array', 'empty_object', 'object')
        $results = New-Object System.Collections.Generic.List[object]
        foreach ($field in $Text.Split(',')) {
            for ($i = 0; $i -lt $bad.Count; $i++) {
                $record = $baseText | ConvertFrom-Json
                $record.$field = $bad[$i]
                $v = Test-ExpiryProbeAuthoritativeResult -Path $Extra -Record $record
                $results.Add([pscustomobject]@{
                    field = $field; variant = $variant[$i]
                    authoritative = [bool]$v.authoritative; reasons = @($v.reasons)
                })
            }
        }
        ConvertTo-Json -InputObject $results.ToArray() -Compress -Depth 5
    }
    'exitcodematrix' {
        # $CtxJson = valid baseline; $Extra = candidate final path.
        $baseText = Get-Content -LiteralPath $CtxJson -Raw -Encoding UTF8
        $bad = New-Object object[] 9
        $bad[0] = '0'; $bad[1] = '1'; $bad[2] = $true; $bad[3] = $false; $bad[4] = 0.0
        # Multi-element arrays: PowerShell unrolls a single-element array on property
        # assignment, so @(0) would reach the validator as a scalar and prove nothing.
        $bad[5] = 1.5; $bad[6] = $null; $bad[7] = @(0, 1); $bad[8] = 2
        $variant = @('string_zero', 'string_one', 'boolean_true', 'boolean_false', 'float_zero',
                     'float', 'null', 'array', 'out_of_range')
        $results = New-Object System.Collections.Generic.List[object]
        for ($i = 0; $i -lt $bad.Count; $i++) {
            $record = $baseText | ConvertFrom-Json
            $record.exit_code = $bad[$i]
            $v = Test-ExpiryProbeAuthoritativeResult -Path $Extra -Record $record
            $results.Add([pscustomobject]@{
                variant = $variant[$i]; authoritative = [bool]$v.authoritative; reasons = @($v.reasons)
            })
        }
        ConvertTo-Json -InputObject $results.ToArray() -Compress -Depth 5
    }
    'stringtypematrix' {
        # Every authority-relevant string field replaced by a non-string of each shape.
        $baseText = Get-Content -LiteralPath $CtxJson -Raw -Encoding UTF8
        $bad = New-Object object[] 6
        # Multi-element array for the same unrolling reason as the exit-code matrix.
        $bad[0] = 1; $bad[1] = $true; $bad[2] = $null; $bad[3] = @('x', 'y'); $bad[4] = @{ a = 1 }; $bad[5] = ''
        $variant = @('integer', 'boolean', 'null', 'array', 'object', 'empty_string')
        $results = New-Object System.Collections.Generic.List[object]
        foreach ($field in $Text.Split(',')) {
            for ($i = 0; $i -lt $bad.Count; $i++) {
                $record = $baseText | ConvertFrom-Json
                $record.$field = $bad[$i]
                $v = Test-ExpiryProbeAuthoritativeResult -Path $Extra -Record $record
                $results.Add([pscustomobject]@{
                    field = $field; variant = $variant[$i]
                    authoritative = [bool]$v.authoritative; reasons = @($v.reasons)
                })
            }
        }
        ConvertTo-Json -InputObject $results.ToArray() -Compress -Depth 5
    }
    'datetimerecord' {
        # PowerShell 7's ConvertFrom-Json converts ISO-8601 text to [datetime] while Windows
        # PowerShell 5.1 leaves it as [string]. This op reproduces the 7.x shape on ANY host so
        # the cross-version contract is covered locally, not only in hosted CI.
        $record = Get-Content -LiteralPath $CtxJson -Raw -Encoding UTF8 | ConvertFrom-Json
        $record.executed_at_utc = [datetime]::SpecifyKind([datetime]::ParseExact(
            '2026-08-03T00:00:00Z', 'yyyy-MM-ddTHH:mm:ssZ',
            [System.Globalization.CultureInfo]::InvariantCulture,
            [System.Globalization.DateTimeStyles]::AdjustToUniversal), [System.DateTimeKind]::Utc)
        $record.intended_expiry_date = [datetime]::ParseExact('2028-06-30', 'yyyy-MM-dd',
            [System.Globalization.CultureInfo]::InvariantCulture)
        $record.expiry_date_readback_value = [datetime]::ParseExact('2028-06-30', 'yyyy-MM-dd',
            [System.Globalization.CultureInfo]::InvariantCulture)
        $v = Test-ExpiryProbeAuthoritativeResult -Path $Extra -Record $record
        [pscustomobject]@{
            authoritative = [bool]$v.authoritative
            reasons = @($v.reasons)
            executedType = $record.executed_at_utc.GetType().Name
        } | ConvertTo-Json -Compress -Depth 4
    }
    'schemashape' {
        # Missing/unknown top-level and publication-contract fields. $Text selects the case.
        $record = Get-Content -LiteralPath $CtxJson -Raw -Encoding UTF8 | ConvertFrom-Json
        switch ($Text) {
            'missing_top' { $record.PSObject.Properties.Remove('activated') }
            'unknown_top' { $record | Add-Member -NotePropertyName 'injected_field' -NotePropertyValue 'x' }
            'missing_contract' { $record.publication_contract.PSObject.Properties.Remove('authority_rule') }
            'unknown_contract' { $record.publication_contract | Add-Member -NotePropertyName 'injected' -NotePropertyValue 'x' }
            'scalar_contract' { $record.publication_contract = 'not-an-object' }
            'null_contract' { $record.publication_contract = $null }
            'array_contract' { $record.publication_contract = @(1, 2) }
        }
        $v = Test-ExpiryProbeAuthoritativeResult -Path $Extra -Record $record
        [pscustomobject]@{ authoritative = [bool]$v.authoritative; reasons = @($v.reasons) } | ConvertTo-Json -Compress -Depth 4
    }
    'schemafields' {
        # The library's declared closed top-level schema, for comparison against the script.
        [pscustomobject]@{
            topLevel = @($script:ExpiryProbeAuthoritativeTopLevelFields)
            booleans = @($script:ExpiryProbeAuthoritativeBooleanFields)
            contract = @($script:ExpiryProbeAuthoritativePublicationFields)
        } | ConvertTo-Json -Compress -Depth 4
    }
    'publishpaths' {
        # A3-2 path preflight. $Text = "<stagingPath>|<finalPath>"; content is never written
        # when the preflight rejects.
        $parts = $Text.Split('|')
        $threw = $false
        $message = ''
        try { Publish-ExpiryProbeResultAtomic -StagingPath $parts[0] -FinalPath $parts[1] -Content 'bytes' `
                -MoveAction { param($s, $d) throw 'the move must never be reached' } }
        catch { $threw = $true; $message = $_.Exception.Message }
        [pscustomobject]@{
            threw = $threw
            stagingCreated = (Test-Path -LiteralPath $parts[0])
            finalCreated = (Test-Path -LiteralPath $parts[1])
            message = $message
        } | ConvertTo-Json -Compress
    }
    'nativemove' {
        # A3-2 real Windows write-through publication between two temporary same-directory
        # paths. Never the canonical root: $Dir is always a test temporary directory.
        $stg = Join-Path $Dir (Get-ExpiryProbeStagingBasename -OperationId $Text)
        $fin = Join-Path $Dir (Get-ExpiryProbeResultBasename -OperationId $Text)
        $content = Get-Content -LiteralPath $CtxJson -Raw -Encoding UTF8
        $published = $false
        $publishError = ''
        try { Publish-ExpiryProbeResultAtomic -StagingPath $stg -FinalPath $fin -Content $content; $published = $true }
        catch { $publishError = $_.Exception.Message }
        # A second publication against the now-existing destination must fail without replacing.
        $secondBlocked = $false
        try { Publish-ExpiryProbeResultAtomic -StagingPath $stg -FinalPath $fin -Content 'replacement bytes' }
        catch { $secondBlocked = $true }
        $finalOperationId = ''
        if (Test-Path -LiteralPath $fin) {
            $finalOperationId = (Get-Content -LiteralPath $fin -Raw -Encoding UTF8 | ConvertFrom-Json).operation_id
        }
        [pscustomobject]@{
            published = $published
            publishError = $publishError
            finalExists = (Test-Path -LiteralPath $fin)
            stagingLeft = (Test-Path -LiteralPath $stg)
            secondBlocked = $secondBlocked
            finalOperationId = $finalOperationId
        } | ConvertTo-Json -Compress
    }
    'nativefailhook' {
        # A4-1: drive the REAL production native-failure branch. The pure-library
        # -PreNativeMoveHook seam runs after the path preflight and after staging is durably
        # created, occupies the destination, and then the genuine no-replace MoveFileExW runs
        # and fails. No alternative move implementation is supplied.
        $stg = Join-Path $Dir (Get-ExpiryProbeStagingBasename -OperationId $Text)
        $fin = Join-Path $Dir (Get-ExpiryProbeResultBasename -OperationId $Text)
        $content = Get-Content -LiteralPath $CtxJson -Raw -Encoding UTF8
        $script:HookRan = $false
        $threw = $false
        $message = ''
        try {
            Publish-ExpiryProbeResultAtomic -StagingPath $stg -FinalPath $fin -Content $content `
                -PreNativeMoveHook {
                    param($s, $d)
                    Set-Content -LiteralPath $d -Value 'occupied by another writer' -NoNewline -Encoding UTF8
                    $script:HookRan = $true
                }
        }
        catch { $threw = $true; $message = $_.Exception.Message }
        $stagingContent = ''
        if (Test-Path -LiteralPath $stg) { $stagingContent = Get-Content -LiteralPath $stg -Raw -Encoding UTF8 }
        $finalContent = ''
        if (Test-Path -LiteralPath $fin) { $finalContent = Get-Content -LiteralPath $fin -Raw -Encoding UTF8 }
        [pscustomobject]@{
            threw = $threw
            hookRan = $script:HookRan
            message = $message
            stagingExists = (Test-Path -LiteralPath $stg)
            stagingContentMatches = ($stagingContent -eq $content)
            finalExists = (Test-Path -LiteralPath $fin)
            finalNotReplaced = ($finalContent -eq 'occupied by another writer')
        } | ConvertTo-Json -Compress
    }
    'nativemoveraw' {
        # Direct native-layer proof that the write-through move is NO-REPLACE: with the
        # destination already present the API must fail and leave both files untouched. This is
        # the guarantee that remains authoritative against a race after the preflight.
        Initialize-ExpiryProbeNativePublicationApi
        $src = Join-Path $Dir 'native_source.tmp'
        $dst = Join-Path $Dir 'native_destination.tmp'
        Set-Content -LiteralPath $src -Value 'source bytes' -NoNewline -Encoding UTF8
        Set-Content -LiteralPath $dst -Value 'destination bytes' -NoNewline -Encoding UTF8
        $blocked = [XbExpiryProbe.NativePublication]::MoveNoReplaceWriteThrough($src, $dst)
        $fresh = Join-Path $Dir 'native_fresh.tmp'
        $allowed = [XbExpiryProbe.NativePublication]::MoveNoReplaceWriteThrough($src, $fresh)
        [pscustomobject]@{
            blockedOk = $blocked.Ok
            blockedErrorCode = $blocked.NativeStatus
            destinationUnchanged = ((Get-Content -LiteralPath $dst -Raw) -eq 'destination bytes')
            sourceStillPresentAfterBlock = $true
            allowedOk = $allowed.Ok
            freshExists = (Test-Path -LiteralPath $fresh)
            sourceGoneAfterMove = (-not (Test-Path -LiteralPath $src))
        } | ConvertTo-Json -Compress
    }
    'leaseacquire' {
        # Acquire and immediately dispose a trusted state-root lease over an injected root.
        # $Dir = injected root; $Text = '1' to require Windows.
        $requireWindows = ($Text -eq '1')
        if ($requireWindows) { $lease = New-ExpiryProbeTrustedRootLease -Root $Dir -RequireWindows }
        else { $lease = New-ExpiryProbeTrustedRootLease -Root $Dir }
        $result = [pscustomobject]@{
            acquired       = $lease.acquired
            reasons        = @($lease.reasons)
            componentCount = $lease.component_count
            identityCount  = $lease.identity_count
            heldCount      = $lease.held_count
        }
        Close-ExpiryProbeTrustedRootLease -Lease $lease
        $result | ConvertTo-Json -Compress -Depth 4
    }
    'leasehold' {
        # Hold a trusted state-root lease across an interactive handshake so an INDEPENDENT
        # process can attempt real filesystem renames while the Windows directory handles
        # are retained. Nothing here touches the canonical root: $Dir is always a temporary
        # directory supplied by the test.
        $lease = New-ExpiryProbeTrustedRootLease -Root $Dir -RequireWindows
        [Console]::Out.WriteLine('ACQUIRED|' + [bool]$lease.acquired + '|' + (@($lease.reasons) -join ';') + '|' + $lease.held_count)
        [Console]::Out.Flush()
        [void][Console]::In.ReadLine()
        Close-ExpiryProbeTrustedRootLease -Lease $lease
        [Console]::Out.WriteLine('RELEASED|' + $lease.held_count)
        [Console]::Out.Flush()
        [void][Console]::In.ReadLine()
    }
}
"""


# The single README bullet that states this probe's public contract. Amendment
# DL-XB-115-001-A1 corrects only that bullet; every other README surface is out of scope.
README_PROBE_BULLET_PREFIX = "- `scripts/ac2_member_expiry_capability_probe.ps1`"


def readme_probe_bullets(text):
    """Every README bullet describing the synthetic ExpiryDate capability probe."""
    return [line for line in text.splitlines() if line.startswith(README_PROBE_BULLET_PREFIX)]


def workflow_path_filters(text):
    """Map every ``on:`` event that declares a ``paths:`` filter to its pattern list."""
    lines = text.splitlines()
    index = 0
    total = len(lines)
    while index < total and not re.match(r"^on:\s*$", lines[index]):
        index += 1
    filters = {}
    if index == total:
        return filters
    index += 1
    event = None
    while index < total:
        line = lines[index]
        stripped = line.strip()
        if stripped == "" or stripped.startswith("#"):
            index += 1
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            break
        if indent == 2:
            event = stripped.split(":", 1)[0]
        elif indent == 4 and stripped == "paths:" and event:
            items = []
            probe = index + 1
            while probe < total:
                candidate = lines[probe]
                inner = candidate.strip()
                if inner == "" or inner.startswith("#"):
                    probe += 1
                    continue
                inner_indent = len(candidate) - len(candidate.lstrip())
                if inner_indent >= 6 and inner.startswith("- "):
                    items.append(inner[2:].strip().strip('"').strip("'"))
                    probe += 1
                    continue
                break
            filters[event] = items
            index = probe
            continue
        index += 1
    return filters


# A2-4: the literal exact-head assertion step both jobs must run immediately after checkout.
EXACT_HEAD_STEP_NAME = "Assert literal exact-head checkout"


def workflow_job_blocks(text):
    """Map each job id under `jobs:` to its raw block text."""
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if re.match(r"^jobs:\s*$", line))
    except StopIteration:
        return {}
    end_of_jobs = len(lines)
    headers = []
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line.strip() and not line.startswith(" "):
            end_of_jobs = i
            break
        match = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if match:
            headers.append((i, match.group(1)))
    blocks = {}
    for index, (line_no, name) in enumerate(headers):
        stop = headers[index + 1][0] if index + 1 < len(headers) else end_of_jobs
        blocks[name] = "\n".join(lines[line_no:stop])
    return blocks


def checkout_head_binding_problems(text):
    """Problems preventing literal exact-head binding. Empty list means compliant."""
    problems = []
    blocks = workflow_job_blocks(text)
    if not blocks:
        return ["no_jobs_found"]
    for job, block in blocks.items():
        if "actions/checkout@" not in block:
            problems.append("%s:no_checkout" % job)
            continue
        checkout_step = block.split("actions/checkout@", 1)[1].split("\n      - ", 1)[0]
        ref = re.search(r"^\s*ref:\s*(.+)$", checkout_step, re.M)
        if not ref:
            problems.append("%s:checkout_without_explicit_ref" % job)
        else:
            expression = ref.group(1)
            if "github.event.pull_request.head.sha" not in expression:
                problems.append("%s:ref_missing_pr_head_sha" % job)
            if "github.sha" not in expression:
                problems.append("%s:ref_missing_dispatch_sha" % job)
            if "merge_commit_sha" in expression or "/merge" in expression:
                problems.append("%s:ref_uses_merge_ref" % job)
        if EXACT_HEAD_STEP_NAME not in block:
            problems.append("%s:missing_exact_head_assertion" % job)
            continue
        assertion_at = block.index(EXACT_HEAD_STEP_NAME)
        assertion_step = block[assertion_at:].split("\n      - ", 1)[0]
        if "merge_commit_sha" in assertion_step:
            problems.append("%s:assertion_uses_merge_sha" % job)
        if "github.event.pull_request.head.sha" not in assertion_step:
            problems.append("%s:assertion_missing_pr_head_sha" % job)
        if "continue-on-error" in assertion_step:
            problems.append("%s:assertion_continue_on_error" % job)
        if "rev-parse HEAD" not in assertion_step:
            problems.append("%s:assertion_does_not_read_head" % job)
        for later in ("PowerShell parse check", "python -m unittest", "_run_ci_full_suite"):
            position = block.find(later)
            if position != -1 and position < assertion_at:
                problems.append("%s:assertion_runs_after_work" % job)
    return sorted(set(problems))


def glob_to_regex(pattern):
    """GitHub Actions path-filter glob: ``**`` crosses separators, ``*`` and ``?`` do not."""
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def uncovered_dependencies(dependencies, patterns):
    compiled = [glob_to_regex(p) for p in patterns]
    return sorted(dep for dep in dependencies if not any(rx.match(dep) for rx in compiled))


# ---- DL-XB-121-001: delegated shared-folder lookup CI trigger closure ---- #
# The delegated in-process dependency closure of the focused suite: modules the workflow's own
# jobs import and execute, plus the Windows job's authoritative full-suite entrypoint, none of
# which previously appeared in any `paths` filter. A change confined to one of them therefore
# left the focused workflow untriggered.
#
# This is DELIBERATELY a separate set from REPO_DEPENDENCIES. That registry is the closed,
# fail-closed inventory of repository files this module itself READS or semantically inspects;
# these eight are expected workflow-filter VALUES only. They are never joined to ROOT, resolved
# through repo_path(), opened, read or stat'ed here, so the closed repository-read contract and
# repository_read_violations stay exactly as narrow as before.
DELEGATED_LOOKUP_WORKFLOW_TRIGGER_PATHS = frozenset({
    "scripts/member_lookup_gate4_real_queue_lookup.py",
    "scripts/ac2_member_lookup_bridge_worker.py",
    "scripts/member_lookup_gate3c_local_bridge_runtime.py",
    "scripts/member_lookup_gate4a_queue_precheck.py",
    "tests/test_member_lookup_gate4_real_queue_lookup.py",
    "tests/test_ac2_member_lookup_bridge_worker.py",
    "tests/test_member_lookup_gate4a_manual_handoff.py",
    "tests/_run_ci_full_suite.py",
})

# Catch-all patterns that would nominally "cover" the entries above while destroying the
# reviewed trigger surface. Coverage is not the contract here; exact membership is.
BROAD_TRIGGER_WILDCARDS = frozenset({"*", "**", "scripts/*", "scripts/**",
                                     "tests/*", "tests/**", ".github/**"})


def missing_trigger_paths(required, patterns):
    """Required trigger entries absent as EXACT literals from a workflow ``paths`` list.

    Membership is literal, not glob coverage: a broad pattern that happens to match a required
    path must not be accepted as a stand-in for the exact Design-Lock entry.
    """
    declared = set(patterns)
    return sorted(path for path in required if path not in declared)


class ExpiryProbeStaticTests(unittest.TestCase):
    def setUp(self):
        self.script = read_repo_text("probe_script")
        self.lib = read_repo_text("probe_lib")

    def test_probe_and_lib_exist(self):
        self.assertTrue(read_repo_text("probe_script").strip())
        self.assertTrue(read_repo_text("probe_lib").strip())
        self.assertIn("member_expiry_capability_probe_lib.ps1", self.script)

    def test_requires_every_write_switch_before_loading_autocount(self):
        for switch in WRITE_SWITCHES:
            self.assertIn(switch, self.script)
        conj = re.search(r"\$allConfirmed\s*=\s*(.+?)\nif \(-not \$allConfirmed\)", self.script, re.S)
        self.assertIsNotNone(conj)
        for switch in WRITE_SWITCHES:
            self.assertIn(switch, conj.group(1))
        # Refusal happens before any assembly load.
        self.assertLess(self.script.index("refused"), self.script.index("LoadFrom"))

    def test_requires_approval_reference(self):
        self.assertIn("$ApprovalReference", self.script)
        self.assertRegex(self.script, r"-ApprovalReference is required")

    # ---- Canonical claim/result authority ---- #
    def test_script_exposes_no_state_directory_or_json_out_anywhere(self):
        # Neither the removed operator-selected claim root nor the removed secondary output
        # path may survive as a parameter, a variable, a validation, or a redaction entry.
        for removed in ("StateDirectory", "JsonOut"):
            self.assertNotIn("$" + removed, self.script, removed)
            self.assertNotIn("-" + removed, self.script, removed)
        self.assertNotIn("$StateDirectory", self.lib)
        self.assertNotIn("$JsonOut", self.lib)
        # The removed operator-path validations are gone with them.
        for gone in ("Test-ExpiryProbeSafePath", "Test-ExpiryProbePathInsideRepo", "Get-ExpiryProbeRepoRoot"):
            self.assertNotIn(gone, self.script, gone)
            self.assertNotIn(gone, self.lib, gone)

    def test_canonical_state_root_is_a_fixed_code_constant(self):
        self.assertIn('$script:ExpiryProbeCanonicalStateRoot = "%s"' % CANONICAL_STATE_ROOT, self.lib)
        # The script takes the root from the nullary accessor and from nothing else. Only
        # executable (non-comment) lines count; the parameter block documents the accessor.
        self.assertIn("$script:ExpiryProbeStateRoot = Get-ExpiryProbeCanonicalStateRoot", self.script)
        code_lines = [ln for ln in self.script.splitlines() if not ln.lstrip().startswith("#")]
        accessor_lines = [ln for ln in code_lines if "Get-ExpiryProbeCanonicalStateRoot" in ln]
        self.assertEqual(len(accessor_lines), 1, accessor_lines)
        self.assertEqual(self.script.count("$script:ExpiryProbeStateRoot ="), 1)
        # Not an environment value, not deployment/current-directory relative. Only the
        # assignment matters; other lines merely read the resolved root (e.g. the sanitiser).
        assignment_lines = [ln for ln in self.script.splitlines()
                            if re.match(r"\s*\$script:ExpiryProbeStateRoot\s*=", ln)]
        self.assertEqual(len(assignment_lines), 1, assignment_lines)
        for line in assignment_lines:
            self.assertNotIn("GetEnvironmentVariable", line, line)
            self.assertNotIn("$env:", line, line)
            self.assertNotIn("$scriptDir", line, line)
            self.assertNotIn("$PSScriptRoot", line, line)
            self.assertNotIn("Get-Location", line, line)
        self.assertNotIn("AC2_PROBE_STATE", self.script)

    def test_canonical_root_is_outside_any_repository_checkout(self):
        # The fixed root replaces the old "reject a state directory inside the repo" guard: it
        # is an absolute local-volume path, while every registered repository dependency is a
        # checkout-relative path, so the two namespaces cannot overlap. Stated without touching
        # a repository path, because A4-2 forbids consuming one outside the registry reader.
        self.assertTrue(re.match(r"^[A-Za-z]:\\", CANONICAL_STATE_ROOT))
        for relative in registered_dependencies():
            self.assertFalse(relative.startswith("/"), relative)
            self.assertIsNone(re.match(r"^[A-Za-z]:", relative), relative)
            self.assertNotIn(CANONICAL_STATE_ROOT.lower(), relative.lower())

    def test_probe_never_creates_repairs_or_cleans_the_state_root(self):
        self.assertNotIn("New-Item", self.script)
        self.assertNotIn("CreateDirectory", self.script)
        self.assertNotIn("New-Item", self.lib)
        self.assertNotIn("CreateDirectory", self.lib)

    def test_trusted_root_validation_precedes_all_live_access(self):
        # The lease performs the trusted-root validation (it reuses the same validator), so
        # acquiring it is the single point that gates every live access.
        trust_idx = self.script.index("New-ExpiryProbeTrustedRootLease")
        self.assertLess(trust_idx, self.script.index("LoadFrom"))
        self.assertLess(trust_idx, self.script.index("$authenticateMethod.Invoke"))
        self.assertLess(trust_idx, self.script.index("$result.autocount_contacted = $true"))
        self.assertLess(trust_idx, self.script.index("$getMemberMethod.Invoke"))
        # Path derivation, and therefore any filesystem use of the root, happens after it.
        self.assertLess(trust_idx, self.script.index("Get-ExpiryProbeStatePaths"))
        # The lease is the only caller of the validator from the executable path.
        self.assertIn("Test-ExpiryProbeTrustedStateRoot", self.lib)
        self.assertNotIn("Test-ExpiryProbeTrustedStateRoot", self.script)

    # ---- A2-2: the trusted-root lease must span the whole irreversible operation ---- #
    def test_lease_is_acquired_before_any_live_access(self):
        acquire = self.script.index("New-ExpiryProbeTrustedRootLease")
        for later in ("LoadFrom", "$authenticateMethod.Invoke",
                      "$result.autocount_contacted = $true", "$getMemberMethod.Invoke",
                      "New-ExpiryProbeDurableArtifact -Path $claimPath",
                      "Invoke-ExpiryProbeSaveMemberOnce -SaveMemberMethod"):
            self.assertLess(acquire, self.script.index(later), later)

    def test_lease_is_released_only_after_terminal_evidence_handling(self):
        release = self.script.index("Close-ExpiryProbeTrustedRootLease")
        # Exactly one disposal site, and it is in the outer cleanup path.
        self.assertEqual(self.script.count("Close-ExpiryProbeTrustedRootLease"), 1)
        for earlier in ("Invoke-ExpiryProbeSaveMemberOnce -SaveMemberMethod",
                        "New-ExpiryProbeDurableArtifact -Path $claimPath",
                        "$result.expiry_match ="):
            self.assertLess(self.script.index(earlier), release, earlier)
        # The disposal must sit in the outer finally, after final publication is adjudicated,
        # so no early release can precede the irreversible boundary.
        publish = self.script.index("Complete-ExpiryProbeRun -DurableEvidence:$stateReady")
        self.assertLess(publish, release,
                        "the lease must outlive result publication and publication-failure handling")
        tail = self.script[publish:]
        self.assertRegex(tail, r"(?s)finally\s*\{[^}]*Close-ExpiryProbeTrustedRootLease")

    def test_no_lease_or_root_override_reaches_the_executable_script(self):
        self.assertIn("New-ExpiryProbeTrustedRootLease -Root $script:ExpiryProbeStateRoot", self.script)
        self.assertEqual(self.script.count("New-ExpiryProbeTrustedRootLease"), 1)
        for override in ("$Lease", "-Lease $", "LeaseRoot", "StateRoot ="):
            if override == "-Lease $":
                continue
            self.assertNotIn("[string]$" + override.strip("$"), self.script, override)
        # The lease helper is reachable only with the fixed canonical root.
        self.assertNotRegex(self.script, r"New-ExpiryProbeTrustedRootLease\s+-Root\s+(?!\$script:ExpiryProbeStateRoot)")

    def test_lease_uses_windows_handles_without_delete_sharing(self):
        for token in ("FILE_FLAG_BACKUP_SEMANTICS", "FILE_FLAG_OPEN_REPARSE_POINT",
                      "FILE_SHARE_READ", "FILE_SHARE_WRITE", "GetFileInformationByHandle",
                      "CreateFileW", "SafeFileHandle"):
            self.assertIn(token, self.lib, token)
        # Delete sharing must never be GRANTED: that is what pins the namespace. Comments may
        # explain its absence, so only executable lines are inspected.
        code_lines = [line for line in self.lib.splitlines()
                      if not line.lstrip().startswith(("#", "//"))]
        code = "\n".join(code_lines)
        self.assertNotIn("FILE_SHARE_DELETE", code)
        self.assertNotIn("0x00000004", code)
        self.assertIn("FILE_SHARE_READ | FILE_SHARE_WRITE,", code)
        # Handles are retained and disposed in reverse order, never re-opened per use.
        self.assertIn("function New-ExpiryProbeTrustedRootLease", self.lib)
        self.assertIn("function Close-ExpiryProbeTrustedRootLease", self.lib)
        self.assertRegex(self.lib, r"(?i)reverse order")
        # Nothing in the lease creates or repairs a component.
        lease_source = self.lib[self.lib.index("function New-ExpiryProbeTrustedRootLease"):]
        for forbidden in ("New-Item", "CreateDirectory", "Remove-Item", "Delete("):
            self.assertNotIn(forbidden, lease_source, forbidden)

    def test_trusted_root_validation_fails_closed_on_every_condition(self):
        for reason in ("platform_not_windows", "root_not_absolute", "root_unresolvable",
                       "root_not_local_volume", "volume_root_unexpected", "root_missing",
                       "component_missing", "root_not_directory", "component_not_directory",
                       "root_reparse_point", "component_reparse_point", "component_stat_failed"):
            self.assertIn("'%s'" % reason, self.lib, reason)
        # Every stat/access exception around component inspection fails closed with the
        # dedicated reason rather than continuing.
        stat_catches = re.findall(r"catch \{ \$reason = 'component_stat_failed'; break \}", self.lib)
        self.assertGreaterEqual(len(stat_catches), 3, "each component stat/access step must fail closed")
        # Redirected components are rejected, never followed or resolved.
        self.assertIn("ReparsePoint", self.lib)
        self.assertNotIn("ResolveLinkTarget", self.lib)
        self.assertNotIn("LinkTarget", self.lib)

    def test_claim_root_unavailable_is_terminal_and_pre_contact(self):
        self.assertIn("CLAIM_ROOT_UNAVAILABLE", self.lib)
        self.assertIn("$result.claim_root_unavailable = $true", self.script)
        unavailable_idx = self.script.index("$result.claim_root_unavailable = $true")
        self.assertLess(unavailable_idx, self.script.index("$result.autocount_contacted = $true"))
        self.assertLess(unavailable_idx, self.script.index("Invoke-ExpiryProbeSaveMemberOnce -SaveMemberMethod"))
        # The raw root is never emitted; only a reason code is.
        self.assertIn("claim_root_failure_reasons", self.script)
        self.assertNotIn(CANONICAL_STATE_ROOT, self.script)

    def test_uses_save_gated_member_command_flow(self):
        for term in ("AutoCount.BonusPoint.Member.MemberCommand", "MemberCommand.Create",
                     "GetMember", "NewMember", "SaveMember", "CreateAutoCountDefaultDBSetting",
                     "Authenticate", '"Login"'):
            self.assertIn(term, self.script)

    def test_assigns_synthetic_expiry_date_2028_06_30(self):
        self.assertIn('$script:SyntheticExpiryDate = "2028-06-30"', self.script)
        self.assertRegex(self.script, r"ExpiryDate\s*=\s*\[datetime\]::ParseExact\(\$script:SyntheticExpiryDate")
        self.assertIn("expiry_date_assigned", self.script)

    def test_reads_password_from_env_var_only(self):
        self.assertIn("AC2_PROBE_PASSWORD", self.script)
        self.assertIn("[Environment]::GetEnvironmentVariable($PasswordEnvVar)", self.script)
        self.assertNotRegex(self.script, r"(?i)\[string\]\s*\$Password\b")
        self.assertNotRegex(self.script, r"(?i)(Password\s*=|PWD\s*=|User\s+ID\s*=|Server\s*=|Database\s*=)")

    def test_no_update_delete_rollback_or_cleanup_path(self):
        self.assertNotRegex(self.script, r"(?i)\b(DeleteMember|UpdateMember|RemoveMember|DeleteMemberType|SaveMemberType)\b")
        self.assertNotRegex(self.script, r"\.\s*(Delete|Update|Rollback)\s*\(")
        self.assertNotRegex(self.script, r"(?im)^\s*function\s+[A-Za-z-]*(Cleanup|Delete|Rollback|Remove)[A-Za-z-]*")
        self.assertNotRegex(self.script, r"\b(SELECT|INSERT|UPDATE|DELETE|MERGE|CREATE\s+TABLE|ALTER|DROP|TRUNCATE)\b")

    # ---- Publication: no clobber, no cleanup, content-bound authority ---- #
    def test_no_artefact_is_ever_deleted_or_overwritten(self):
        # No Set-Content and no Remove-Item in either file: not for a claim, not for a
        # staging artefact, and not for a result.
        self.assertNotIn("Set-Content", self.script)
        self.assertNotIn("Remove-Item", self.script)
        self.assertNotIn("Set-Content", self.lib)
        self.assertNotIn("Remove-Item", self.lib)
        self.assertNotRegex(self.lib, r"\[System\.IO\.File\]::Delete")
        self.assertNotRegex(self.script, r"\[System\.IO\.File\]::Delete")
        self.assertIn("New-ExpiryProbeDurableArtifact", self.script)
        self.assertIn("Publish-ExpiryProbeResultAtomic", self.script)

    def test_lib_publishes_via_no_clobber_staging_and_no_replace_move(self):
        self.assertNotIn("Write-ExpiryProbeResultAtomic", self.lib)
        self.assertNotIn("Write-ExpiryProbeResultAtomic", self.script)
        # The old ".tmp" writer (which deleted a pre-existing temp file) is gone.
        self.assertNotIn('$Path + ".tmp"', self.lib)
        self.assertNotIn(".tmp", self.lib)
        self.assertIn("expiry_probe_staging_", self.lib)
        self.assertIn(".incomplete", self.lib)
        self.assertRegex(self.lib, r"FileMode\]::CreateNew")
        self.assertNotIn("[System.IO.File]::Move", self.lib)
        self.assertIn("MoveNoReplaceWriteThrough($StagingPath, $FinalPath)", self.lib)
        self.assertRegex(self.lib, r"refusing to overwrite evidence")

    # ---- A3-2: native write-through, no-replace publication ---- #
    def test_production_publication_uses_write_through_moveedfileex_only(self):
        code_lines = [line for line in self.lib.splitlines()
                      if not line.lstrip().startswith(("#", "//"))]
        code = "\n".join(code_lines)
        for token in ("MoveFileExW", "MOVEFILE_WRITE_THROUGH", "NativePublication",
                      "MoveNoReplaceWriteThrough"):
            self.assertIn(token, code, token)
        # Within the native publication type, write-through is the ONLY flag: replacement,
        # copying and reboot-delayed scheduling are never declared or requested.
        native = code[code.index("function Initialize-ExpiryProbeNativePublicationApi"):]
        native = native[:native.index("\nfunction ")]
        for forbidden in ("MOVEFILE_REPLACE_EXISTING", "MOVEFILE_COPY_ALLOWED",
                          "MOVEFILE_DELAY_UNTIL_REBOOT", "0x00000001", "0x00000002", "0x00000004"):
            self.assertNotIn(forbidden, native, forbidden)
        self.assertIn("MOVEFILE_WRITE_THROUGH = 0x00000008", native)
        self.assertEqual(native.count("MoveFileExW(source, destination,"), 1)
        self.assertIn("MoveFileExW(source, destination, MOVEFILE_WRITE_THROUGH)", native)
        # No ordinary-move or copy/delete/replace fallback survives anywhere in the library.
        for fallback in ("[System.IO.File]::Move", "Move-Item", "Copy-Item",
                         "[System.IO.File]::Copy", "[System.IO.File]::Replace",
                         "[System.IO.File]::Delete"):
            self.assertNotIn(fallback, code, fallback)
        self.assertNotIn("[System.IO.File]::Move", self.script)

    def test_publication_preflight_contract_is_declared(self):
        for reason in ("staging_not_absolute", "final_not_absolute", "publication_parent_mismatch",
                       "publication_parent_missing", "publication_same_basename",
                       "publication_volume_mismatch", "publication_final_exists"):
            self.assertIn("'%s'" % reason, self.lib, reason)
        # The injected move action stays a pure-test seam and never reaches the probe script.
        self.assertIn("[scriptblock]$MoveAction", self.lib)
        self.assertNotIn("-MoveAction", self.script)
        # Off Windows the production path fails closed rather than falling back.
        publish = self.lib[self.lib.index("function Publish-ExpiryProbeResultAtomic"):]
        publish = publish[:publish.index("\nfunction ")]
        self.assertIn("Win32NT", publish)

    def test_publication_contract_is_content_borne_and_mechanical(self):
        for field in ("publication_contract_version", "authoritative_result_basename", "authority_rule"):
            self.assertIn(field, self.lib, field)
        self.assertIn('$script:ExpiryProbePublicationContractVersion = "%s"' % PUBLICATION_CONTRACT_VERSION, self.lib)
        # The rule states the mechanical test, not a vague warning.
        self.assertRegex(self.lib, r"authoritative ONLY when its current file basename is exactly equal to authoritative_result_basename")
        self.assertRegex(self.lib, r"(?i)NON-AUTHORITATIVE regardless of the terminal_outcome")
        # The staged bytes carry it.
        self.assertIn("publication_contract          = $null", self.script)
        self.assertIn("New-ExpiryProbePublicationContract -OperationId $operationId", self.script)

    def test_move_failure_leaves_staging_and_reports_failure(self):
        self.assertRegex(self.script, r"\$result\.evidence_persisted\s*=\s*\$false")
        self.assertRegex(self.script, r"-EvidencePersisted\s+\$false")
        self.assertIn("$result.non_authoritative_staging_may_remain = $true", self.script)
        self.assertRegex(self.script, r"(?i)NON-AUTHORITATIVE staged artefact may remain")
        self.assertRegex(self.script, r"(?i)Do not delete, rename or republish it")
        # The library does not clean up, rename or rewrite the staged object on failure.
        publish = self.lib[self.lib.index("function Publish-ExpiryProbeResultAtomic"):]
        publish = publish[:publish.index("\nfunction ")]
        self.assertNotIn("Remove-Item", publish)
        self.assertNotIn("catch", publish, "a move failure must propagate, not be swallowed")

    def test_authoritative_validator_is_pure_and_basename_bound(self):
        self.assertIn("function Test-ExpiryProbeAuthoritativeResult", self.lib)
        for reason in ("basename_not_authoritative", "publication_contract_missing",
                       "publication_contract_version_mismatch", "schema_version_mismatch",
                       "operation_id_basename_mismatch", "artefact_is_staging",
                       "state_contradiction", "evidence_not_persisted"):
            self.assertIn("'%s'" % reason, self.lib, reason)
        # The decisive check is exact basename equality against the content-bound value, not
        # a filename glob or suffix test.
        self.assertRegex(self.lib, r"\$basename\.Equals\(\$boundBasename, \[System\.StringComparison\]::Ordinal\)")

    def test_claim_created_before_save_in_source_order(self):
        claim_idx = self.script.index("New-ExpiryProbeDurableArtifact -Path $claimPath")
        save_idx = self.script.index("Invoke-ExpiryProbeSaveMemberOnce -SaveMemberMethod")
        recheck_idx = self.script.index("$result.member_recheck_attempted = $true")
        self.assertLess(recheck_idx, claim_idx, "the claim is created after the second duplicate check")
        self.assertLess(claim_idx, save_idx)

    # ---- Truthful contact and race state ---- #
    def test_autocount_contacted_is_set_before_the_first_authentication_call(self):
        contact_idx = self.script.index("$result.autocount_contacted = $true")
        auth_idx = self.script.index("[void]$authenticateMethod.Invoke")
        self.assertLess(contact_idx, auth_idx, "contact must be recorded before the call, not after success")
        self.assertEqual(self.script.count("$result.autocount_contacted = $true"), 1)
        # It is never reset.
        self.assertNotIn("$result.autocount_contacted = $false", self.script)
        self.assertLess(contact_idx, self.script.index("$result.authentication_success = $loginOk"))

    def test_read_attempt_flags_precede_their_reads(self):
        initial_flag = self.script.index("$result.initial_member_read_attempted = $true")
        initial_read = self.script.index("$existing = $getMemberMethod.Invoke")
        recheck_flag = self.script.index("$result.member_recheck_attempted = $true")
        recheck_read = self.script.index("$recheck = $getMemberMethod.Invoke")
        self.assertLess(initial_flag, initial_read)
        self.assertLess(recheck_flag, recheck_read)
        self.assertLess(initial_read, recheck_flag)

    def test_post_contact_claim_loss_is_a_distinct_outcome(self):
        self.assertIn("ATTEMPT_CLAIM_LOST_AFTER_CONTACT", self.lib)
        self.assertIn("$result.claim_lost_after_contact = $true", self.script)
        lost_idx = self.script.index("$result.claim_lost_after_contact = $true")
        save_idx = self.script.index("Invoke-ExpiryProbeSaveMemberOnce -SaveMemberMethod")
        self.assertLess(lost_idx, save_idx, "the losing contender must throw before any save")
        # The branch is chosen from the truthful contact flag, and the message says contact
        # DID occur (it must not claim a pre-contact refusal).
        self.assertIn("if ($result.autocount_contacted) {", self.script)
        self.assertRegex(self.script, r"Another contender owns the permanent attempt claim")
        self.assertRegex(self.script, r"had already made live AutoCount contact")
        # The pre-contact branch keeps the pre-contact wording.
        self.assertRegex(self.script, r"refusing to write before any AutoCount contact")

    def test_pre_contact_claim_check_happens_before_any_contact(self):
        precheck_idx = self.script.index("if (Test-Path -LiteralPath $claimPath) {")
        self.assertLess(precheck_idx, self.script.index("$result.autocount_contacted = $true"))
        self.assertLess(precheck_idx, self.script.index("LoadFrom"))
        # The claim namespace comes from the fixed root, never from a process argument.
        self.assertIn("$claimPath = $statePaths.claim_path", self.script)
        self.assertIn("Get-ExpiryProbeStatePaths -Root $script:ExpiryProbeStateRoot", self.script)

    def test_readback_failure_is_isolated_from_pre_write_classification(self):
        self.assertIn("readback_error", self.script)
        self.assertRegex(self.script, r'save_outcome -eq "confirmed"')

    def test_truthful_exit_and_terminal_from_library(self):
        self.assertIn("Get-ExpiryProbeExitCode", self.script)
        self.assertIn("Get-ExpiryProbeTerminalOutcome", self.script)
        self.assertIn("exit $script:ProbeExitCode", self.script)

    def test_output_is_sanitised_and_masks_member_no(self):
        self.assertIn("Get-ExpiryProbeMaskedMemberNo", self.script)
        self.assertIn("masked_member_no", self.script)
        for repl in ("<synthetic-member-no>", "<synthetic-email>", "<synthetic-name>"):
            self.assertIn(repl, self.script)
        self.assertNotRegex(self.script, r"\$result\.[A-Za-z_]+\s*=\s*\$script:SyntheticMemberNo\b")
        self.assertNotRegex(self.script, r"\$result\.[A-Za-z_]+\s*=\s*\$script:SyntheticName\b")
        self.assertNotRegex(self.script, r"\$result\.[A-Za-z_]+\s*=\s*\$script:SyntheticEmail\b")

    def test_documents_residual_record_and_owner_approval(self):
        self.assertIn("synthetic_member_may_remain", self.script)
        self.assertIn("residual_record_note", self.script)
        self.assertRegex(self.script, r"(?i)owner approval")
        self.assertRegex(self.script, r"(?i)synthetic capability probe")
        self.assertRegex(self.script, r"(?i)form-derived member")

    # ---- Library source guarantees ---- #
    def test_lib_claim_is_exclusive_create_write_through(self):
        self.assertRegex(self.lib, r"FileMode\]::CreateNew")
        self.assertRegex(self.lib, r"FileOptions\]::WriteThrough")
        self.assertRegex(self.lib, r"Flush\(\$true\)")

    def test_lib_flush_fallback_is_narrowed_to_unsupported_runtime(self):
        self.assertRegex(self.lib, r"catch \[System\.NotSupportedException\]\s*\{\s*\$stream\.Flush\(\)")
        self.assertNotRegex(self.lib, r"catch\s*\{\s*\$stream\.Flush\(\)")

    def test_lib_target_fingerprint_is_case_insensitive(self):
        self.assertIn("ToLowerInvariant()", self.lib)

    def test_claim_durability_failure_fails_closed_before_save(self):
        self.assertIn("could not be durably persisted", self.script)

    def test_claim_persistence_failure_is_distinct_from_conflict(self):
        self.assertIn("claim_persist_failed", self.script)
        self.assertIn("post-create-persist-failed", self.script)
        self.assertIn("post-create-persist-failed", self.lib)

    def test_evidence_persisted_is_pessimistic_and_underlying_retained(self):
        self.assertRegex(self.script, r"evidence_persisted\s*=\s*\$false")
        self.assertIn("underlying_terminal_outcome", self.script)
        self.assertIn("EVIDENCE_PERSISTENCE_FAILED", self.lib)

    def test_approval_reference_alphabet_is_strict_and_rejects_target(self):
        self.assertIn("'^[A-Za-z0-9._-]{3,64}$'", self.script)
        self.assertRegex(self.script, r"must not contain the server or database")
        self.assertIn("ToLowerInvariant()", self.script)

    def test_sanitizer_redacts_runtime_paths(self):
        self.assertIn("Get-ExpiryProbePathRedacted", self.script)
        self.assertIn("Get-ExpiryProbePathRedacted", self.lib)
        # The canonical state root and the AutoCount root are redaction inputs; the removed
        # operator-selected paths are not (they no longer exist).
        self.assertRegex(self.script, r"\$script:ExpiryProbeStateRoot,\s*\$AcRoot")

    def test_approval_reference_recorded_only_after_validation(self):
        activation_idx = self.script.index("$result.activated = $true")
        substr_idx = self.script.index("must not contain the server or database")
        assign_idx = self.script.index("$result.approval_reference = $ApprovalReference")
        self.assertGreater(assign_idx, substr_idx, "approval_reference must be set only after validation")
        self.assertGreater(assign_idx, activation_idx)
        self.assertEqual(self.script.count("$result.approval_reference = $ApprovalReference"), 1)

    def test_gitignore_covers_probe_evidence(self):
        gi = read_repo_text("gitignore")
        self.assertIn("expiry_probe_claim_*.claim", gi)
        self.assertIn("expiry_probe_result_*.json", gi)
        self.assertIn("expiry_probe_staging_*.incomplete", gi)


@unittest.skipIf(PS is None, "no PowerShell executable available")
class ExpiryProbeLibraryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.harness = cls.tmp / "libprobe.ps1"
        cls.harness.write_text(LIBPROBE, encoding="utf-8")
        # The helper library is dot-sourced from a scratch copy whose content crossed the
        # sanctioned registry reader, so no repository path is handed to a subprocess.
        _, cls.scratch_lib = materialise_probe_scratch(cls.tmp / "scratch_probe")

    def _cmd(self, op, **kw):
        cmd = [PS, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-File", str(self.harness), "-Lib", str(self.scratch_lib), "-Op", op]
        for k, v in kw.items():
            cmd += ["-" + k, str(v)]
        return cmd

    def _lib(self, op, **kw):
        return subprocess.run(self._cmd(op, **kw), capture_output=True, text=True)

    def _json(self, op, **kw):
        proc = self._lib(op, **kw)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def _terminal(self, **flags):
        base = dict(activated=True, claim_root_unavailable=False, claim_conflict=False,
                    claim_lost_after_contact=False, claim_persist_failed=False,
                    claim_created=False, autocount_contacted=False,
                    initial_member_read_attempted=False, member_recheck_attempted=False,
                    member_exists_initial=False, member_exists_recheck=False,
                    save_member_attempted=False, save_member_confirmed=False,
                    save_outcome="not_attempted", readback_found=False, expiry_match=False)
        base.update(flags)
        ctx = self.tmp / "ctx.json"
        ctx.write_text(json.dumps(base), encoding="utf-8")
        proc = self._lib("terminal", CtxJson=str(ctx))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        code, contr = proc.stdout.strip().split("|")
        return code, int(contr)

    def _contacted(self, **flags):
        """Flags for a run that authenticated and performed both duplicate reads."""
        base = dict(autocount_contacted=True, initial_member_read_attempted=True,
                    member_recheck_attempted=True)
        base.update(flags)
        return base

    # ---- Canonical authority ---- #
    def test_canonical_root_accessor_is_nullary_and_exact(self):
        info = self._json("canonroot")
        self.assertEqual(info["root"], CANONICAL_STATE_ROOT)
        self.assertEqual(info["declaredParameterCount"], 0,
                         "the production accessor must take no parameters at all")

    def test_artefact_basenames_are_pure_and_working_directory_independent(self):
        root = self.tmp / "stateroot"
        root.mkdir(exist_ok=True)
        elsewhere = self.tmp / "otherwd"
        elsewhere.mkdir(exist_ok=True)
        text = "afp_deadbeef|expop_abc123"
        first = self._json("paths", Dir=str(root), Text=text)
        second = subprocess.run(self._cmd("paths", Dir=str(root), Text=text),
                                capture_output=True, text=True, cwd=str(elsewhere))
        self.assertEqual(second.returncode, 0, second.stderr)
        second = json.loads(second.stdout)
        self.assertNotEqual(first["cwd"], second["cwd"], "the two runs must use different working directories")
        for key in ("claim_basename", "result_basename", "staging_basename", "claim_path"):
            self.assertEqual(first[key], second[key], key)
        self.assertEqual(first["claim_basename"], "expiry_probe_claim_afp_deadbeef.claim")
        self.assertEqual(first["result_basename"], "expiry_probe_result_expop_abc123.json")
        self.assertEqual(first["staging_basename"], "expiry_probe_staging_expop_abc123.incomplete")
        # The path helper and the pure basename helpers cannot diverge.
        self.assertEqual(first["claim_basename"], first["pure_claim"])
        self.assertEqual(first["result_basename"], first["pure_result"])
        self.assertEqual(first["staging_basename"], first["pure_staging"])

    # ---- Trusted-root validation ---- #
    def test_trusted_root_accepts_a_plain_directory_chain(self):
        root = self.tmp / "trusted_root"
        root.mkdir(exist_ok=True)
        verdict = self._json("trust", Dir=str(root), Text="0")
        self.assertTrue(verdict["trusted"], "reasons=%s" % as_list(verdict["reasons"]))
        self.assertEqual(as_list(verdict["reasons"]), [])

    def test_missing_root_fails_closed(self):
        missing = self.tmp / "definitely_absent"
        verdict = self._json("trust", Dir=str(missing), Text="0")
        self.assertFalse(verdict["trusted"])
        self.assertEqual(as_list(verdict["reasons"]), ["root_missing"])

    def test_missing_ancestor_fails_closed(self):
        deep = self.tmp / "absent_parent" / "child"
        verdict = self._json("trust", Dir=str(deep), Text="0")
        self.assertFalse(verdict["trusted"])
        self.assertEqual(as_list(verdict["reasons"]), ["component_missing"])

    def test_non_directory_root_fails_closed(self):
        plain = self.tmp / "root_is_a_file"
        plain.write_text("not a directory", encoding="utf-8")
        verdict = self._json("trust", Dir=str(plain), Text="0")
        self.assertFalse(verdict["trusted"])
        self.assertEqual(as_list(verdict["reasons"]), ["root_not_directory"])

    def test_non_directory_ancestor_fails_closed(self):
        plain = self.tmp / "ancestor_is_a_file"
        plain.write_text("not a directory", encoding="utf-8")
        verdict = self._json("trust", Dir=str(plain / "child"), Text="0")
        self.assertFalse(verdict["trusted"])
        # A file cannot have children, so the chain stops at the non-directory component.
        self.assertIn(as_list(verdict["reasons"])[0], ("component_not_directory", "component_missing"))

    def test_relative_and_empty_roots_fail_closed(self):
        verdict = self._json("trust", Dir="relative/state/root", Text="0")
        self.assertFalse(verdict["trusted"])
        self.assertEqual(as_list(verdict["reasons"]), ["root_not_absolute"])

    def test_non_windows_platform_fails_closed_for_the_active_path(self):
        root = self.tmp / "platform_root"
        root.mkdir(exist_ok=True)
        verdict = self._json("trust", Dir=str(root), Text="1")
        if IS_WINDOWS:
            self.assertTrue(verdict["trusted"], "reasons=%s" % as_list(verdict["reasons"]))
        else:
            self.assertFalse(verdict["trusted"])
            self.assertEqual(as_list(verdict["reasons"]), ["platform_not_windows"],
                             "the active probe path must fail closed off Windows, not skip the contract")

    @unittest.skipUnless(IS_WINDOWS, "UNC rejection is a Windows-only root condition")
    def test_unc_root_is_rejected_without_touching_the_network(self):
        verdict = self._json("trust", Dir=r"\\no-such-host\no-such-share\state", Text="1")
        self.assertFalse(verdict["trusted"])
        self.assertEqual(as_list(verdict["reasons"]), ["root_not_local_volume"])

    @unittest.skipUnless(IS_WINDOWS, "junction coverage is Windows-specific")
    def test_windows_junction_ancestor_and_root_fail_closed(self):
        real = self.tmp / "junction_target"
        (real / "state").mkdir(parents=True, exist_ok=True)
        link = self.tmp / "junction_link"
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(real)],
                              capture_output=True, text=True)
        self.assertEqual(made.returncode, 0, made.stdout + made.stderr)
        ancestor = self._json("trust", Dir=str(link / "state"), Text="1")
        self.assertFalse(ancestor["trusted"])
        self.assertEqual(as_list(ancestor["reasons"]), ["component_reparse_point"])
        leaf = self._json("trust", Dir=str(link), Text="1")
        self.assertFalse(leaf["trusted"])
        self.assertEqual(as_list(leaf["reasons"]), ["root_reparse_point"])

    @unittest.skipIf(IS_WINDOWS, "POSIX symlink equivalent of the junction coverage")
    def test_posix_symlink_ancestor_and_root_fail_closed(self):
        real = self.tmp / "symlink_target"
        (real / "state").mkdir(parents=True, exist_ok=True)
        link = self.tmp / "symlink_link"
        if not link.exists():
            os.symlink(str(real), str(link), target_is_directory=True)
        ancestor = self._json("trust", Dir=str(link / "state"), Text="0")
        self.assertFalse(ancestor["trusted"])
        self.assertEqual(as_list(ancestor["reasons"]), ["component_reparse_point"])
        leaf = self._json("trust", Dir=str(link), Text="0")
        self.assertFalse(leaf["trusted"])
        self.assertEqual(as_list(leaf["reasons"]), ["root_reparse_point"])

    # ---- Terminal truth ---- #
    def test_terminal_vocabulary_is_complete(self):
        proc = self._lib("vocabulary")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        codes = proc.stdout.strip().split(",")
        self.assertEqual(sorted(codes), sorted(TERMINAL_CODES))

    def test_terminal_state_matrix_is_honest_and_consistent(self):
        self.assertEqual(self._terminal(activated=False), ("REFUSED", 0))
        self.assertEqual(self._terminal(claim_root_unavailable=True), ("CLAIM_ROOT_UNAVAILABLE", 0))
        self.assertEqual(self._terminal(claim_conflict=True), ("ATTEMPT_ALREADY_CLAIMED", 0))
        self.assertEqual(self._terminal(**self._contacted(claim_lost_after_contact=True)),
                         ("ATTEMPT_CLAIM_LOST_AFTER_CONTACT", 0))
        self.assertEqual(self._terminal(**self._contacted(claim_persist_failed=True)),
                         ("CLAIM_PERSISTENCE_FAILED", 0))
        self.assertEqual(self._terminal(**self._contacted(member_exists_initial=True)),
                         ("BLOCKED_MEMBER_EXISTS", 0))
        self.assertEqual(self._terminal(**self._contacted(member_exists_recheck=True)),
                         ("BLOCKED_MEMBER_EXISTS", 0))
        self.assertEqual(self._terminal(), ("FAILED_BEFORE_WRITE", 0))
        self.assertEqual(self._terminal(**self._contacted(save_member_attempted=True, claim_created=True,
                                                         save_outcome="uncertain")),
                         ("WRITE_OUTCOME_UNCERTAIN", 0))
        self.assertEqual(self._terminal(**self._contacted(save_member_attempted=True, claim_created=True,
                                                         save_member_confirmed=True,
                                                         save_outcome="confirmed", readback_found=False)),
                         ("WRITE_CONFIRMED_READBACK_FAILED", 0))
        self.assertEqual(self._terminal(**self._contacted(save_member_attempted=True, claim_created=True,
                                                         save_member_confirmed=True, save_outcome="confirmed",
                                                         readback_found=True, expiry_match=False)),
                         ("EXPIRY_READBACK_MISMATCH", 0))
        self.assertEqual(self._terminal(**self._contacted(save_member_attempted=True, claim_created=True,
                                                         save_member_confirmed=True, save_outcome="confirmed",
                                                         readback_found=True, expiry_match=True)),
                         ("EXPIRY_VERIFIED", 0))

    def test_pre_contact_claim_conflict_requires_no_contact(self):
        code, contradictions = self._terminal(claim_conflict=True)
        self.assertEqual(code, "ATTEMPT_ALREADY_CLAIMED")
        self.assertEqual(contradictions, 0)
        # ATTEMPT_ALREADY_CLAIMED with live contact is impossible by definition.
        _, contradictions = self._terminal(**self._contacted(claim_conflict=True))
        self.assertGreater(contradictions, 0)
        reasons = self._contradiction_reasons(**self._contacted(claim_conflict=True))
        self.assertIn("already_claimed_after_contact", reasons)

    def _contradiction_reasons(self, **flags):
        base = dict(activated=True, claim_root_unavailable=False, claim_conflict=False,
                    claim_lost_after_contact=False, claim_persist_failed=False,
                    claim_created=False, autocount_contacted=False,
                    initial_member_read_attempted=False, member_recheck_attempted=False,
                    member_exists_initial=False, member_exists_recheck=False,
                    save_member_attempted=False, save_member_confirmed=False,
                    save_outcome="not_attempted", readback_found=False, expiry_match=False)
        base.update(flags)
        ctx = self.tmp / "ctx_reasons.json"
        ctx.write_text(json.dumps(base), encoding="utf-8")
        proc = self._lib("contradictions", CtxJson=str(ctx))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return [r for r in proc.stdout.strip().split(",") if r]

    def test_post_contact_claim_loss_never_reaches_save(self):
        code, contradictions = self._terminal(**self._contacted(claim_lost_after_contact=True))
        self.assertEqual(code, "ATTEMPT_CLAIM_LOST_AFTER_CONTACT")
        self.assertEqual(contradictions, 0)
        # A loser that somehow recorded a save attempt, a created claim, or no contact at all
        # is an impossible state.
        self.assertIn("claim_lost_but_save_attempted",
                      self._contradiction_reasons(**self._contacted(claim_lost_after_contact=True,
                                                                    save_member_attempted=True,
                                                                    save_outcome="uncertain")))
        self.assertIn("claim_lost_but_claim_created",
                      self._contradiction_reasons(**self._contacted(claim_lost_after_contact=True,
                                                                    claim_created=True)))
        self.assertIn("claim_lost_without_contact",
                      self._contradiction_reasons(claim_lost_after_contact=True))
        self.assertIn("claim_lost_and_pre_contact_conflict",
                      self._contradiction_reasons(**self._contacted(claim_lost_after_contact=True,
                                                                    claim_conflict=True)))

    def test_claim_root_unavailable_excludes_every_live_flag(self):
        reasons = self._contradiction_reasons(claim_root_unavailable=True, autocount_contacted=True,
                                              initial_member_read_attempted=True,
                                              save_member_attempted=True, save_outcome="uncertain",
                                              claim_created=True)
        for expected in ("claim_root_unavailable_after_contact",
                         "claim_root_unavailable_with_save_attempt",
                         "claim_root_unavailable_with_claim",
                         "claim_root_unavailable_with_live_read"):
            self.assertIn(expected, reasons)

    def test_live_reads_and_saves_require_recorded_contact(self):
        self.assertIn("initial_read_without_contact",
                      self._contradiction_reasons(initial_member_read_attempted=True))
        self.assertIn("recheck_read_without_contact",
                      self._contradiction_reasons(member_recheck_attempted=True))
        self.assertIn("save_without_contact",
                      self._contradiction_reasons(save_member_attempted=True, save_outcome="uncertain"))
        self.assertIn("member_exists_without_read_attempt",
                      self._contradiction_reasons(autocount_contacted=True, member_exists_initial=True))
        self.assertIn("recheck_without_read_attempt",
                      self._contradiction_reasons(autocount_contacted=True,
                                                  initial_member_read_attempted=True,
                                                  member_exists_recheck=True))

    def test_post_save_readback_failure_is_never_failed_before_write(self):
        code, contr = self._terminal(**self._contacted(save_member_attempted=True, claim_created=True,
                                                       save_member_confirmed=True,
                                                       save_outcome="confirmed", readback_found=False))
        self.assertEqual(code, "WRITE_CONFIRMED_READBACK_FAILED")
        self.assertEqual(contr, 0)

    def test_impossible_flag_combinations_are_flagged(self):
        _, c1 = self._terminal(**self._contacted(save_member_attempted=True, claim_created=True,
                                                 save_member_confirmed=False, save_outcome="confirmed"))
        self.assertGreater(c1, 0)
        _, c2 = self._terminal(**self._contacted(save_member_attempted=True, claim_created=True,
                                                 save_outcome="uncertain", readback_found=True))
        self.assertGreater(c2, 0)
        _, c3 = self._terminal(activated=False, save_member_attempted=True)
        self.assertGreater(c3, 0)

    def test_exit_codes_only_verified_is_zero(self):
        for code in TERMINAL_CODES:
            proc = self._lib("exit", Text=code)
            expected = "0" if code == "EXPIRY_VERIFIED" else "1"
            self.assertEqual(proc.stdout.strip(), expected, code)

    def _final(self, underlying, durable, persisted):
        proc = self._lib("final", Text="%s|%d|%d" % (underlying, 1 if durable else 0, 1 if persisted else 0))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        outcome, code = proc.stdout.strip().split("|")
        return outcome, int(code)

    def test_evidence_override_gates_success(self):
        self.assertEqual(self._final("EXPIRY_VERIFIED", True, True), ("EXPIRY_VERIFIED", 0))
        self.assertEqual(self._final("EXPIRY_VERIFIED", True, False), ("EVIDENCE_PERSISTENCE_FAILED", 1))
        self.assertEqual(self._final("FAILED_BEFORE_WRITE", True, True), ("FAILED_BEFORE_WRITE", 1))
        self.assertEqual(self._final("FAILED_BEFORE_WRITE", True, False), ("EVIDENCE_PERSISTENCE_FAILED", 1))
        self.assertEqual(self._final("REFUSED", False, True), ("REFUSED", 1))
        self.assertEqual(self._final("CLAIM_ROOT_UNAVAILABLE", False, True), ("CLAIM_ROOT_UNAVAILABLE", 1))
        self.assertEqual(self._final("ATTEMPT_CLAIM_LOST_AFTER_CONTACT", False, True),
                         ("ATTEMPT_CLAIM_LOST_AFTER_CONTACT", 1))

    def test_fingerprints_deterministic_shaped_case_and_whitespace_insensitive(self):
        info = self._json("fp")
        self.assertTrue(info["targetStable"])
        self.assertTrue(info["targetDiffers"])
        self.assertTrue(info["targetShape"])
        self.assertTrue(info["attemptShape"])
        self.assertTrue(info["synthShape"])
        self.assertTrue(info["caseWsInsensitiveTarget"], "casing/whitespace variants must collapse to one target fingerprint")
        self.assertTrue(info["caseWsInsensitiveAttempt"], "attempt claim key must be case/whitespace insensitive on the target")

    def test_path_redaction_masks_private_paths(self):
        proc = self._lib("redact", Text=r"failed at C:\Users\alice\XB\state\r.json and \\HOST\share\x tail")
        out = proc.stdout.strip()
        self.assertIn("<path>", out)
        self.assertNotIn("alice", out)
        self.assertNotIn("HOST", out)
        self.assertIn("tail", out)

    def test_durable_flush_failure_propagates_no_usable_artifact(self):
        d = self.tmp / "badclaimdir"
        d.mkdir(exist_ok=True)
        info = self._json("badclaim", Dir=str(d))
        self.assertTrue(info["threw"], "a durable persistence failure must propagate")
        self.assertFalse(info["exists"], "no usable artefact may remain on failure")

    # ---- Claim exclusivity and concurrency ---- #
    def test_claim_is_exclusive_create_and_never_deleted(self):
        d = self.tmp / "claimdir"
        d.mkdir(exist_ok=True)
        info = self._json("claim", Dir=str(d))
        self.assertTrue(info["exists"])
        self.assertTrue(info["secondBlocked"], "a second claim on the same path must fail closed (one winner)")
        self.assertTrue(info["unchanged"], "the original claim content must survive a second attempt")

    def test_two_concurrent_contenders_for_one_claim_produce_one_winner(self):
        d = self.tmp / "racedir"
        d.mkdir(exist_ok=True)
        claim = d / "expiry_probe_claim_afp_race.claim"
        procs = [subprocess.Popen(self._cmd("claimrace", Text=str(claim)),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for _ in range(2)]
        outputs = [p.communicate() for p in procs]
        created = []
        for stdout, stderr in outputs:
            self.assertTrue(stdout.strip(), stderr)
            created.append(json.loads(stdout)["created"])
        self.assertEqual(created.count(True), 1, "exactly one contender may create the canonical claim")
        self.assertEqual(created.count(False), 1, "the loser must fail closed")
        self.assertTrue(claim.is_file())
        self.assertEqual(read_scratch_text(claim).strip(), "contender")

    # ---- Publication ---- #
    def _record_file(self, name, record):
        path = self.tmp / name
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def test_publication_uses_no_clobber_staging_then_no_replace_move(self):
        d = self.tmp / "publishdir"
        d.mkdir(exist_ok=True)
        operation_id = "expop_publish01"
        record = self._record_file("publish_record.json", verified_record(operation_id))
        info = self._json("publish", Dir=str(d), Text=operation_id, CtxJson=str(record))
        self.assertTrue(info["finalExists"])
        self.assertFalse(info["stagingLeft"], "a successful publication leaves no staging artefact")
        self.assertTrue(info["secondBlocked"], "a second publication must not overwrite prior evidence")
        self.assertEqual(info["finalOperationId"], operation_id)
        self.assertEqual(info["finalTerminalOutcome"], "EXPIRY_VERIFIED")

    def test_existing_staging_artefact_is_never_overwritten_or_removed(self):
        d = self.tmp / "stagingconflictdir"
        d.mkdir(exist_ok=True)
        info = self._json("stagingconflict", Dir=str(d), Text="expop_conflict01")
        self.assertTrue(info["blocked"], "an existing staging artefact must fail closed")
        self.assertTrue(info["stagingUnchanged"], "the pre-existing staging bytes must survive byte-identically")
        self.assertFalse(info["finalExists"], "nothing may be published over a staging conflict")

    def test_move_failure_leaves_a_self_invalidating_staging_artefact(self):
        d = self.tmp / "movefaildir"
        d.mkdir(exist_ok=True)
        operation_id = "expop_movefail01"
        record = self._record_file("movefail_record.json", verified_record(operation_id))
        info = self._json("movefail", Dir=str(d), Text=operation_id, CtxJson=str(record))
        self.assertTrue(info["threw"], "a failed final move must propagate, never be swallowed")
        self.assertTrue(info["stagingExists"], "the staging artefact must be left untouched")
        self.assertFalse(info["finalExists"], "no authoritative result may exist after a failed move")
        self.assertEqual(info["stagingBasename"], "expiry_probe_staging_%s.incomplete" % operation_id)
        # The staged bytes bind themselves to the final basename they never reached, even
        # though they contain a candidate EXPIRY_VERIFIED with exit_code 0.
        self.assertEqual(info["stagedBoundBasename"], "expiry_probe_result_%s.json" % operation_id)
        self.assertNotEqual(info["stagedBoundBasename"], info["stagingBasename"])
        self.assertEqual(info["stagedTerminalOutcome"], "EXPIRY_VERIFIED")
        self.assertEqual(info["stagedExitCode"], 0)
        self.assertTrue(info["stagedEvidencePersisted"])
        # And the authoritative validator rejects it at its actual current path.
        verdict = self._json("authoritative", Text=info["stagingPath"], CtxJson=str(record))
        self.assertFalse(verdict["authoritative"],
                         "a staged candidate EXPIRY_VERIFIED must never validate as authoritative")
        reasons = as_list(verdict["reasons"])
        self.assertIn("basename_not_authoritative", reasons)
        self.assertIn("artefact_is_staging", reasons)

    def test_publication_contract_binds_one_authoritative_basename(self):
        contract = self._json("contract", Text="expop_contract01")
        self.assertEqual(contract["publication_contract_version"], PUBLICATION_CONTRACT_VERSION)
        self.assertEqual(contract["authoritative_result_basename"], "expiry_probe_result_expop_contract01.json")
        self.assertRegex(contract["authority_rule"], r"(?i)authoritative ONLY when its current file basename")

    def test_authoritative_validator_accepts_only_the_bound_final_basename(self):
        operation_id = "expop_validator01"
        record_path = self._record_file("validator_record.json", verified_record(operation_id))
        final = self.tmp / ("expiry_probe_result_%s.json" % operation_id)
        verdict = self._json("authoritative", Text=str(final), CtxJson=str(record_path))
        self.assertTrue(verdict["authoritative"], "reasons=%s" % as_list(verdict["reasons"]))
        self.assertEqual(as_list(verdict["reasons"]), [])
        # A different operation's basename fails, even with otherwise valid content.
        other = self.tmp / "expiry_probe_result_expop_someone_else.json"
        verdict = self._json("authoritative", Text=str(other), CtxJson=str(record_path))
        self.assertFalse(verdict["authoritative"])
        reasons = as_list(verdict["reasons"])
        self.assertIn("basename_not_authoritative", reasons)
        self.assertIn("operation_id_basename_mismatch", reasons)
        # A staging basename fails even though the bytes are the same.
        staging = self.tmp / ("expiry_probe_staging_%s.incomplete" % operation_id)
        verdict = self._json("authoritative", Text=str(staging), CtxJson=str(record_path))
        self.assertFalse(verdict["authoritative"])
        self.assertIn("artefact_is_staging", as_list(verdict["reasons"]))

    def test_authoritative_validator_rejects_inconsistent_records(self):
        operation_id = "expop_validator02"
        final = self.tmp / ("expiry_probe_result_%s.json" % operation_id)

        contradictory = verified_record(operation_id)
        contradictory["claim_conflict"] = True
        path = self._record_file("validator_contradiction.json", contradictory)
        verdict = self._json("authoritative", Text=str(final), CtxJson=str(path))
        self.assertFalse(verdict["authoritative"])
        self.assertIn("state_contradiction", as_list(verdict["reasons"]))

        unpersisted = verified_record(operation_id)
        unpersisted["evidence_persisted"] = False
        path = self._record_file("validator_unpersisted.json", unpersisted)
        verdict = self._json("authoritative", Text=str(final), CtxJson=str(path))
        self.assertFalse(verdict["authoritative"])
        self.assertIn("evidence_not_persisted", as_list(verdict["reasons"]))

        wrong_contract = verified_record(operation_id)
        wrong_contract["publication_contract"]["publication_contract_version"] = "something/v0"
        path = self._record_file("validator_contract.json", wrong_contract)
        verdict = self._json("authoritative", Text=str(final), CtxJson=str(path))
        self.assertFalse(verdict["authoritative"])
        self.assertIn("publication_contract_version_mismatch", as_list(verdict["reasons"]))

        no_contract = verified_record(operation_id)
        del no_contract["publication_contract"]
        path = self._record_file("validator_nocontract.json", no_contract)
        verdict = self._json("authoritative", Text=str(final), CtxJson=str(path))
        self.assertFalse(verdict["authoritative"])
        self.assertIn("schema_missing_field", as_list(verdict["reasons"]))

        wrong_exit = verified_record(operation_id)
        wrong_exit["exit_code"] = 1
        path = self._record_file("validator_exit.json", wrong_exit)
        verdict = self._json("authoritative", Text=str(final), CtxJson=str(path))
        self.assertFalse(verdict["authoritative"])
        self.assertIn("exit_code_inconsistent", as_list(verdict["reasons"]))

    # ---- A2-1: authority must be derived from runtime facts, never from declarations ---- #
    def _reject(self, operation_id, name, mutate):
        """Build a record from the consistent verified template, mutate it, expect rejection."""
        record = verified_record(operation_id)
        mutate(record)
        path = self._record_file(name, record)
        final = self.tmp / ("expiry_probe_result_%s.json" % operation_id)
        verdict = self._json("authoritative", Text=str(final), CtxJson=str(path))
        self.assertFalse(verdict["authoritative"],
                         "%s must not validate as authoritative" % name)
        return as_list(verdict["reasons"])

    def test_fabricated_all_default_verified_record_is_rejected(self):
        # The core A2-1 case, kept meaningful under A3-1: the record is COMPLETE and correctly
        # typed, so it clears the strict schema gate and must still be caught by runtime-fact
        # derivation. Every runtime flag is an actual $false, yet it declares a durably
        # persisted EXPIRY_VERIFIED with exit 0; the flags derive FAILED_BEFORE_WRITE.
        operation_id = "expop_fabricated01"
        fabricated = verified_record(operation_id)
        for field in AUTHORITATIVE_BOOLEAN_FIELDS:
            fabricated[field] = False
        fabricated["activated"] = True
        fabricated["evidence_persisted"] = True
        fabricated["save_outcome"] = "not_attempted"
        fabricated["expiry_date_readback_value"] = None
        # The lie:
        fabricated["underlying_terminal_outcome"] = "EXPIRY_VERIFIED"
        fabricated["terminal_outcome"] = "EXPIRY_VERIFIED"
        fabricated["exit_code"] = 0
        path = self._record_file("validator_fabricated.json", fabricated)
        final = self.tmp / ("expiry_probe_result_%s.json" % operation_id)
        verdict = self._json("authoritative", Text=str(final), CtxJson=str(path))
        self.assertFalse(verdict["authoritative"],
                         "a record whose flags show no contact, claim or save must never be authoritative")
        reasons = as_list(verdict["reasons"])
        self.assertIn("underlying_outcome_not_derived_from_flags", reasons)
        self.assertIn("verified_without_required_runtime_state", reasons)

    def test_save_success_without_a_created_claim_is_rejected(self):
        reasons = self._reject("expop_noclaim01", "validator_noclaim.json",
                               lambda r: r.__setitem__("claim_created", False))
        self.assertIn("save_attempted_without_claim", reasons)

    def test_save_confirmation_without_a_save_attempt_is_rejected(self):
        reasons = self._reject("expop_noattempt01", "validator_noattempt.json",
                               lambda r: r.__setitem__("save_member_attempted", False))
        self.assertIn("state_contradiction", reasons)

    def test_readback_success_without_a_confirmed_save_is_rejected(self):
        def mutate(record):
            record["save_member_confirmed"] = False
            record["save_outcome"] = "uncertain"
        reasons = self._reject("expop_norbsave01", "validator_norbsave.json", mutate)
        self.assertIn("state_contradiction", reasons)

    def test_expiry_match_without_a_found_readback_is_rejected(self):
        reasons = self._reject("expop_nomatchrb01", "validator_nomatchrb.json",
                               lambda r: r.__setitem__("readback_found", False))
        self.assertTrue(reasons)
        self.assertTrue({"state_contradiction", "underlying_outcome_not_derived_from_flags"} & set(reasons),
                        reasons)

    def test_every_declared_underlying_outcome_must_match_the_flag_derivation(self):
        # The flags always describe a verified run; each declared underlying outcome other
        # than the derived one must be rejected.
        for index, code in enumerate(c for c in TERMINAL_CODES if c != "EXPIRY_VERIFIED"):
            operation_id = "expop_underlying%02d" % index
            reasons = self._reject(operation_id, "validator_underlying_%s.json" % code,
                                   _field_setter("underlying_terminal_outcome", code))
            self.assertIn("underlying_outcome_not_derived_from_flags", reasons, code)

    def test_every_declared_final_outcome_must_match_the_derived_final_outcome(self):
        for index, code in enumerate(c for c in TERMINAL_CODES if c != "EXPIRY_VERIFIED"):
            operation_id = "expop_final%02d" % index
            reasons = self._reject(operation_id, "validator_final_%s.json" % code,
                                   _field_setter("terminal_outcome", code))
            self.assertIn("terminal_outcome_inconsistent", reasons, code)

    def test_final_outcome_must_account_for_recorded_persistence_state(self):
        # evidence_persisted=false means the derived final outcome is
        # EVIDENCE_PERSISTENCE_FAILED, so a declared EXPIRY_VERIFIED is inconsistent. The
        # validator must use the RECORD's persistence fact, not assume it is true because the
        # artefact reached the validator.
        reasons = self._reject("expop_persist01", "validator_persist.json",
                               lambda r: r.__setitem__("evidence_persisted", False))
        self.assertIn("evidence_not_persisted", reasons)
        self.assertIn("terminal_outcome_inconsistent", reasons)

    # ---- A3-1: strict authoritative-record schema and types ---- #
    def _valid_baseline(self, operation_id="expop_schema01"):
        record = verified_record(operation_id)
        path = self._record_file("schema_baseline_%s.json" % operation_id, record)
        final = self.tmp / ("expiry_probe_result_%s.json" % operation_id)
        return path, final

    def test_library_schema_matches_the_probe_result_contract(self):
        # The library's closed schema must be exactly the reviewed $result contract emitted by
        # the unchanged probe script, so the schema cannot drift from the producer.
        declared = self._json("schemafields")
        script_fields = re.findall(
            r"^\s{4}([a-z_]+)\s*=", read_repo_text("probe_script").split("$result = [ordered]@{", 1)[1]
            .split("\n}", 1)[0], re.M)
        self.assertEqual(len(script_fields), 56)
        self.assertEqual(sorted(as_list(declared["topLevel"])), sorted(script_fields))
        self.assertEqual(sorted(as_list(declared["topLevel"])), sorted(AUTHORITATIVE_TOP_LEVEL_FIELDS))
        self.assertEqual(sorted(as_list(declared["booleans"])), sorted(AUTHORITATIVE_BOOLEAN_FIELDS))
        self.assertEqual(sorted(as_list(declared["contract"])), sorted(PUBLICATION_CONTRACT_FIELDS))

    def test_every_boolean_authority_field_rejects_every_invalid_type(self):
        path, final = self._valid_baseline("expop_boolmatrix")
        results = as_list(self._json("schemamatrix", CtxJson=str(path), Extra=str(final),
                                     Text=",".join(AUTHORITATIVE_BOOLEAN_FIELDS)))
        self.assertEqual(len(results), len(AUTHORITATIVE_BOOLEAN_FIELDS) * 9)
        for case in results:
            self.assertFalse(case["authoritative"],
                             "%s=%s must not be authoritative" % (case["field"], case["variant"]))
            self.assertTrue(any(r.startswith("schema_") for r in as_list(case["reasons"])),
                            "%s=%s reasons=%s" % (case["field"], case["variant"], as_list(case["reasons"])))

    def test_complete_false_shaped_string_record_is_rejected_before_derivation(self):
        # Every runtime fact is the STRING "true"/"false", which PowerShell would coerce to
        # $true. The record declares a fully successful, persisted, exit-zero run.
        operation_id = "expop_falseshaped"
        record = verified_record(operation_id)
        for field in AUTHORITATIVE_BOOLEAN_FIELDS:
            record[field] = "true" if record[field] else "false"
        record["save_outcome"] = "confirmed"
        record["underlying_terminal_outcome"] = "EXPIRY_VERIFIED"
        record["terminal_outcome"] = "EXPIRY_VERIFIED"
        record["exit_code"] = "0"
        path = self._record_file("schema_false_shaped.json", record)
        final = self.tmp / ("expiry_probe_result_%s.json" % operation_id)
        verdict = self._json("authoritative", Text=str(final), CtxJson=str(path))
        self.assertFalse(verdict["authoritative"],
                         "false-shaped strings must never fabricate an authoritative success")
        reasons = as_list(verdict["reasons"])
        self.assertTrue(any(r.startswith("schema_") for r in reasons), reasons)
        # It must fail at the schema gate, before any terminal derivation runs.
        for derived in ("underlying_outcome_not_derived_from_flags", "terminal_outcome_inconsistent",
                        "state_contradiction", "verified_without_required_runtime_state"):
            self.assertNotIn(derived, reasons,
                             "derivation must not run on an unvalidated record: %s" % reasons)

    def test_exit_code_rejects_every_non_integral_or_out_of_range_value(self):
        path, final = self._valid_baseline("expop_exitmatrix")
        results = as_list(self._json("exitcodematrix", CtxJson=str(path), Extra=str(final)))
        self.assertEqual(len(results), 9)
        for case in results:
            self.assertFalse(case["authoritative"], case["variant"])
            self.assertTrue(any(r.startswith("schema_") for r in as_list(case["reasons"])),
                            "%s reasons=%s" % (case["variant"], as_list(case["reasons"])))

    def test_every_authority_string_field_rejects_non_string_values(self):
        path, final = self._valid_baseline("expop_stringmatrix")
        results = as_list(self._json("stringtypematrix", CtxJson=str(path), Extra=str(final),
                                     Text=",".join(AUTHORITATIVE_STRING_FIELDS)))
        self.assertEqual(len(results), len(AUTHORITATIVE_STRING_FIELDS) * 6)
        for case in results:
            self.assertFalse(case["authoritative"],
                             "%s=%s must not be authoritative" % (case["field"], case["variant"]))
            self.assertTrue(any(r.startswith("schema_") for r in as_list(case["reasons"])),
                            "%s=%s reasons=%s" % (case["field"], case["variant"], as_list(case["reasons"])))

    def test_missing_and_unknown_schema_fields_fail_closed(self):
        path, final = self._valid_baseline("expop_shape")
        expected = {
            "missing_top": "schema_missing_field",
            "unknown_top": "schema_unknown_field",
            "missing_contract": "schema_publication_contract_missing_field",
            "unknown_contract": "schema_publication_contract_unknown_field",
            "scalar_contract": "schema_publication_contract_not_object",
            "null_contract": "schema_publication_contract_not_object",
            "array_contract": "schema_publication_contract_not_object",
        }
        for case, reason in expected.items():
            verdict = self._json("schemashape", CtxJson=str(path), Extra=str(final), Text=case)
            self.assertFalse(verdict["authoritative"], case)
            self.assertIn(reason, as_list(verdict["reasons"]), "%s -> %s" % (case, as_list(verdict["reasons"])))

    def test_schema_accepts_both_json_date_shapes_across_powershell_versions(self):
        # Windows PowerShell 5.1 yields [string] for ISO-8601 JSON values; PowerShell 7 yields
        # [datetime]. Both are the correct output of supported JSON parsing, so both must
        # validate identically. Every other substitute for those fields is still rejected by
        # test_every_authority_string_field_rejects_non_string_values.
        path, final = self._valid_baseline("expop_datetime")
        info = self._json("datetimerecord", CtxJson=str(path), Extra=str(final))
        self.assertEqual(info["executedType"], "DateTime",
                         "the fixture must actually exercise the [datetime] shape")
        self.assertTrue(info["authoritative"], "reasons=%s" % as_list(info["reasons"]))
        self.assertEqual(as_list(info["reasons"]), [])

    def test_schema_reasons_never_echo_malformed_values(self):
        operation_id = "expop_noecho"
        record = verified_record(operation_id)
        record["activated"] = "SENSITIVE-MARKER-VALUE"
        record["operation_id"] = 12345
        path = self._record_file("schema_noecho.json", record)
        final = self.tmp / ("expiry_probe_result_%s.json" % operation_id)
        proc = self._lib("authoritative", Text=str(final), CtxJson=str(path))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("SENSITIVE-MARKER-VALUE", proc.stdout)
        self.assertNotIn("SENSITIVE-MARKER-VALUE", proc.stderr)
        self.assertNotIn("12345", proc.stdout)
        verdict = json.loads(proc.stdout)
        self.assertFalse(verdict["authoritative"])
        for reason in as_list(verdict["reasons"]):
            self.assertRegex(reason, r"^[a-z0-9_]+$", "reason codes must be generic: %s" % reason)

    # ---- A3-2: write-through, no-replace final publication ---- #
    def test_publication_preflight_rejects_paths_before_writing_staging(self):
        directory = self.tmp / "preflight"
        other = self.tmp / "preflight_other"
        directory.mkdir(exist_ok=True)
        other.mkdir(exist_ok=True)
        staging = directory / "expiry_probe_staging_expop_pf.incomplete"
        final_elsewhere = other / "expiry_probe_result_expop_pf.json"
        same_name = directory / "expiry_probe_staging_expop_pf.incomplete"

        cross = self._json("publishpaths", Text="%s|%s" % (staging, final_elsewhere))
        self.assertTrue(cross["threw"], "a different parent directory must be rejected")
        self.assertFalse(cross["stagingCreated"], "staging must not be written before the preflight passes")
        self.assertFalse(cross["finalCreated"])

        identical = self._json("publishpaths", Text="%s|%s" % (staging, same_name))
        self.assertTrue(identical["threw"], "identical source and destination must be rejected")
        self.assertFalse(identical["stagingCreated"])

        relative = self._json("publishpaths", Text="relative_staging.incomplete|relative_result.json")
        self.assertTrue(relative["threw"], "relative paths must be rejected")

    @unittest.skipUnless(IS_WINDOWS, "the production write-through publication is Windows-only")
    def test_real_native_write_through_move_publishes_and_never_replaces(self):
        directory = self.tmp / "nativepublish"
        directory.mkdir(exist_ok=True)
        operation_id = "expop_native01"
        record = self._record_file("native_record.json", verified_record(operation_id))
        info = self._json("nativemove", Dir=str(directory), Text=operation_id, CtxJson=str(record))
        self.assertTrue(info["published"], info["publishError"])
        self.assertTrue(info["finalExists"], "the write-through move must publish the final name")
        self.assertFalse(info["stagingLeft"], "the staging name must not survive a successful move")
        self.assertEqual(info["finalOperationId"], operation_id)
        self.assertTrue(info["secondBlocked"], "an existing destination must fail no-clobber")

    def test_native_status_member_is_consistent_across_the_stack(self):
        # A4-1: the C# declaration, the native method and the PowerShell failure path must use
        # ONE member name, or the failure branch raises a missing-property error under
        # Set-StrictMode instead of reporting the native status.
        lib = read_repo_text("probe_lib")
        declared = set(re.findall(r"public int (Native\w+);", lib))
        assigned = set(re.findall(r"result\.(Native\w+)\s*=", lib))
        consumed = set(re.findall(r"\$move\.(Native\w+)", lib))
        self.assertEqual(len(declared), 1, declared)
        self.assertEqual(declared, assigned, "the native method must assign the declared member")
        self.assertEqual(declared, consumed,
                         "the PowerShell publication caller must read the declared member")

    @unittest.skipUnless(IS_WINDOWS, "the real production failure branch is Windows-only")
    def test_production_native_failure_branch_reports_status_only(self):
        # A4-1: exercise the REAL production branch — staging is created, the destination is
        # occupied after preflight, and the genuine no-replace MoveFileExW fails.
        directory = self.tmp / "nativefail"
        directory.mkdir(exist_ok=True)
        operation_id = "expop_nativefail01"
        record = self._record_file("native_fail_record.json", verified_record(operation_id))
        info = self._json("nativefailhook", Dir=str(directory), Text=operation_id, CtxJson=str(record))
        self.assertTrue(info["threw"], "the real native move must fail against an occupied destination")
        self.assertTrue(info["hookRan"], "the pre-native hook must have occupied the destination")
        self.assertTrue(info["stagingExists"], "staging must be left exactly as written")
        self.assertEqual(info["stagingContentMatches"], True, "staging must be byte-identical")
        self.assertTrue(info["finalExists"])
        self.assertTrue(info["finalNotReplaced"], "the destination must not be replaced")
        # Generic, status-only error: no missing-property/strict-mode masking and no paths.
        self.assertNotIn("NativeErrorCode", info["message"])
        self.assertNotIn("cannot be found on this object", info["message"])
        self.assertRegex(info["message"], r"native status \d+")
        self.assertNotIn(str(directory), info["message"])
        self.assertNotIn(operation_id, info["message"])
        self.assertNotIn(".incomplete", info["message"])
        self.assertNotIn(".json", info["message"])

    @unittest.skipUnless(IS_WINDOWS, "native no-replace semantics are Windows-only")
    def test_native_move_is_no_replace_at_the_api_layer(self):
        directory = self.tmp / "nativeraw"
        directory.mkdir(exist_ok=True)
        info = self._json("nativemoveraw", Dir=str(directory))
        self.assertFalse(info["blockedOk"], "MoveFileExW must fail when the destination exists")
        self.assertNotEqual(info["blockedErrorCode"], 0)
        self.assertTrue(info["destinationUnchanged"], "the destination must never be replaced")
        self.assertTrue(info["allowedOk"], "a fresh destination must succeed")
        self.assertTrue(info["freshExists"])
        self.assertTrue(info["sourceGoneAfterMove"])

    def test_consistent_authoritative_verified_record_still_passes(self):
        operation_id = "expop_stillgood01"
        path = self._record_file("validator_stillgood.json", verified_record(operation_id))
        final = self.tmp / ("expiry_probe_result_%s.json" % operation_id)
        verdict = self._json("authoritative", Text=str(final), CtxJson=str(path))
        self.assertTrue(verdict["authoritative"], "reasons=%s" % as_list(verdict["reasons"]))
        self.assertEqual(as_list(verdict["reasons"]), [])


@unittest.skipIf(PS is None, "no PowerShell executable available")
class ExpiryProbeTrustedRootLeaseTests(unittest.TestCase):
    """A2-2: the trusted state-root namespace must be PINNED, not merely re-validated.

    Every test here operates on temporary directories only. Nothing opens, inspects or
    modifies the real canonical root (asserted mechanically by
    test_no_lease_test_targets_the_real_canonical_root).
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.harness = cls.tmp / "leaseprobe.ps1"
        cls.harness.write_text(LIBPROBE, encoding="utf-8")
        _, cls.scratch_lib = materialise_probe_scratch(cls.tmp / "scratch_probe")

    def _cmd(self, op, **kw):
        cmd = [PS, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-File", str(self.harness), "-Lib", str(self.scratch_lib), "-Op", op]
        for k, v in kw.items():
            cmd += ["-" + k, str(v)]
        return cmd

    def _json(self, op, **kw):
        proc = subprocess.run(self._cmd(op, **kw), capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def _chain(self, name):
        """<tmp>/<name>/outer/inner/state, every level a plain local directory."""
        root = self.tmp / name / "outer" / "inner" / "state"
        root.mkdir(parents=True, exist_ok=True)
        return root

    @staticmethod
    def _rename_fails(target):
        """True when an independent process (this one) cannot rename `target`."""
        moved = target.parent / (target.name + "_moved")
        try:
            os.rename(str(target), str(moved))
        except OSError:
            return True
        os.rename(str(moved), str(target))  # undo: the rename unexpectedly succeeded
        return False

    def _hold(self, root):
        return subprocess.Popen(self._cmd("leasehold", Dir=str(root)),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)

    @unittest.skipUnless(IS_WINDOWS, "the production lease path is Windows-only")
    def test_lease_acquires_over_a_temporary_directory_chain(self):
        root = self._chain("acquirechain")
        info = self._json("leaseacquire", Dir=str(root), Text="1")
        self.assertTrue(info["acquired"], "reasons=%s" % as_list(info["reasons"]))
        self.assertEqual(as_list(info["reasons"]), [])
        # One retained handle and one recorded identity per existing component, volume root
        # through leaf.
        self.assertGreaterEqual(info["componentCount"], 4)
        self.assertEqual(info["heldCount"], info["componentCount"])
        self.assertEqual(info["identityCount"], info["componentCount"])

    @unittest.skipUnless(IS_WINDOWS, "real share-mode semantics are Windows-only")
    def test_lease_blocks_root_and_ancestor_rename_until_released(self):
        root = self._chain("holdchain")
        marker = root / "identity.marker"
        marker.write_text("pinned-namespace", encoding="utf-8")
        inner, outer = root.parent, root.parent.parent
        proc = self._hold(root)
        try:
            line = proc.stdout.readline().strip()
            self.assertTrue(line.startswith("ACQUIRED|True|"),
                            "lease not acquired: %s %s" % (line, proc.stderr.read() if proc.poll() else ""))
            self.assertTrue(line.endswith("|%d" % int(line.rsplit("|", 1)[1])))
            # While the lease is held, an INDEPENDENT process cannot rename the root...
            self.assertTrue(self._rename_fails(root), "the leased root must not be renameable")
            # ...nor any mutable ancestor in the leased chain.
            self.assertTrue(self._rename_fails(inner), "a leased ancestor must not be renameable")
            self.assertTrue(self._rename_fails(outer), "a leased ancestor must not be renameable")
            # The canonical path therefore still resolves to the SAME directory: no second
            # backing claim namespace can be swapped in underneath the running probe.
            self.assertTrue(marker.is_file())
            self.assertEqual(read_scratch_text(marker), "pinned-namespace")

            proc.stdin.write("release\n")
            proc.stdin.flush()
            released = proc.stdout.readline().strip()
            self.assertTrue(released.startswith("RELEASED"), released)
            # After disposal the very same rename succeeds, proving the block came from the
            # retained handles and not from some unrelated condition.
            moved = root.parent / (root.name + "_after_release")
            os.rename(str(root), str(moved))
            self.assertTrue(moved.is_dir())
            os.rename(str(moved), str(root))
        finally:
            try:
                proc.stdin.write("exit\n")
                proc.stdin.flush()
            except (OSError, ValueError):
                pass
            try:
                proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()

    @unittest.skipUnless(IS_WINDOWS, "real share-mode semantics are Windows-only")
    def test_lease_prevents_replacement_creating_a_second_claim_namespace(self):
        root = self._chain("replacechain")
        (root / "original.marker").write_text("original", encoding="utf-8")
        replacement = self.tmp / "replacement_namespace"
        replacement.mkdir(exist_ok=True)
        (replacement / "attacker.marker").write_text("attacker", encoding="utf-8")
        proc = self._hold(root)
        try:
            line = proc.stdout.readline().strip()
            self.assertTrue(line.startswith("ACQUIRED|True|"), line)
            # A replacement needs the leased directory out of the way first; that step fails,
            # so the swap can never complete.
            self.assertTrue(self._rename_fails(root))
            with self.assertRaises(OSError):
                os.rename(str(replacement), str(root))
            # The canonical path still backs exactly one namespace: the original one.
            self.assertTrue((root / "original.marker").is_file())
            self.assertFalse((root / "attacker.marker").exists())
            self.assertTrue((replacement / "attacker.marker").is_file())
            # And only one exclusive claim can exist in that single namespace.
            claim = root / "expiry_probe_claim_afp_pinned.claim"
            first = self._json("claimrace", Text=str(claim))
            second = self._json("claimrace", Text=str(claim))
            self.assertTrue(first["created"])
            self.assertFalse(second["created"],
                             "a second contender must remain ineligible for the save boundary")
        finally:
            try:
                proc.stdin.write("release\n")
                proc.stdin.flush()
                proc.stdout.readline()
                proc.stdin.write("exit\n")
                proc.stdin.flush()
                proc.communicate(timeout=30)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                proc.kill()
                proc.communicate()

    @unittest.skipUnless(IS_WINDOWS, "partial-acquisition handle release is Windows-specific")
    def test_partial_acquisition_releases_already_opened_handles(self):
        chain = self._chain("partialchain")
        missing = chain / "absent_leaf"
        self.assertFalse(missing.exists())
        proc = self._hold(missing)
        try:
            line = proc.stdout.readline().strip()
            self.assertTrue(line.startswith("ACQUIRED|False|"), line)
            self.assertIn("root_missing", line)
            self.assertTrue(line.endswith("|0"), "every partially acquired handle must be disposed: %s" % line)
            # Because the partial handles were released, the ancestors are renameable again.
            self.assertFalse(self._rename_fails(chain),
                             "a failed acquisition must not leave the chain pinned")
        finally:
            try:
                proc.stdin.write("release\n")
                proc.stdin.flush()
                proc.stdout.readline()
                proc.stdin.write("exit\n")
                proc.stdin.flush()
                proc.communicate(timeout=30)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                proc.kill()
                proc.communicate()

    def test_lease_rejects_a_missing_or_non_directory_root(self):
        missing = self.tmp / "no_such_lease_root"
        info = self._json("leaseacquire", Dir=str(missing), Text="0" if not IS_WINDOWS else "1")
        self.assertFalse(info["acquired"])
        self.assertEqual(info["heldCount"], 0)
        expected = "platform_not_windows" if not IS_WINDOWS else "root_missing"
        self.assertIn(expected, as_list(info["reasons"]))

    @unittest.skipIf(IS_WINDOWS, "non-Windows fail-closed behaviour")
    def test_lease_fails_closed_off_windows_without_invoking_windows_apis(self):
        root = self._chain("posixchain")
        info = self._json("leaseacquire", Dir=str(root), Text="1")
        self.assertFalse(info["acquired"],
                         "the active probe path must fail closed off Windows, not skip the contract")
        self.assertEqual(as_list(info["reasons"]), ["platform_not_windows"])
        self.assertEqual(info["heldCount"], 0)
        # The platform gate must precede any native interop in the library source.
        lib = read_repo_text("probe_lib")
        self.assertLess(lib.index("platform_not_windows"), lib.index("Add-Type"),
                        "the Windows-only gate must precede native interop")

    def test_no_lease_test_targets_the_real_canonical_root(self):
        # Mechanical guarantee that no lease/trust/harness invocation in this module can be
        # pointed at the operator's real evidence root. This guard's own body necessarily
        # names the forbidden shapes, so it is excluded from the scan.
        guard_name = "def test_no_lease_test_targets_the_real_canonical_root"
        lines = read_repo_text("focused_tests").splitlines()
        start = next(i for i, line in enumerate(lines) if guard_name in line)
        end = start + 1
        while end < len(lines) and (not lines[end].strip() or lines[end].startswith("        ")):
            end += 1
        scanned = lines[:start] + lines[end:]
        self.assertLess(start, end, "the guard body must be locatable")
        for line in scanned:
            if re.search(r"\b(Dir|_hold)\s*[=(]", line):
                self.assertNotIn(CANONICAL_STATE_ROOT, line, line)
                self.assertNotIn("CANONICAL_STATE_ROOT", line, line)
        # The canonical root is referenced only as a constant, a static expectation and the
        # skip guard: never as a filesystem target opened by a test.
        joined = "\n".join(scanned)
        for forbidden in ("Path(CANONICAL_STATE_ROOT)", "mkdir(CANONICAL_STATE_ROOT",
                          "New-ExpiryProbeTrustedRootLease -Root " + CANONICAL_STATE_ROOT):
            self.assertNotIn(forbidden, joined, forbidden)


@unittest.skipIf(PS is None, "no PowerShell executable available")
class ExpiryProbeScriptExecutionTests(unittest.TestCase):
    """Runs the probe so it always stops before any AutoCount assembly load.

    Runs with a VALID approval reference reach the canonical state-root check, so they are
    skipped on a machine where that root exists (see CANONICAL_ROOT_SKIP).
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # The probe and its library are executed from a scratch pair whose content crossed the
        # sanctioned registry reader; the repository copies are never handed to a subprocess.
        self.scratch_script, self.scratch_lib = materialise_probe_scratch(self.tmp / "scratch_probe")
        self.env = dict(os.environ)
        self.env["AC2_PROBE_PASSWORD"] = ""  # force a pre-AutoCount stop for active runs

    def _run(self, *args, **kw):
        cmd = [PS, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-File", str(self.scratch_script), *args]
        return subprocess.run(cmd, capture_output=True, text=True, env=self.env, **kw)

    def _active_args(self, appref="APPROVAL-TEST-001"):
        return ("-EnableExpiryCapabilityProbe", "-ConfirmSyntheticExpiryDateTest",
                "-ConfirmSingleSyntheticMember", "-ConfirmAutoCountWrite",
                "-ConfirmDryRunPreflightPassed", "-ConfirmNoUpdateOrDelete",
                "-ApprovalReference", appref,
                "-ServerName", "SYN_SERVER", "-DatabaseName", "SYN_DB", "-UserId", "SYN_USER",
                "-AcRoot", "C:\\NoSuchAcRoot")

    def test_removed_parameters_are_rejected_by_the_parameter_binder(self):
        # Passing the removed switches must fail: there is no state-directory or JSON-output
        # parameter to bind, so no operator-selected path can reach the probe.
        for removed in ("-StateDirectory", "-JsonOut"):
            proc = self._run(*self._active_args(), removed, str(self.tmp))
            self.assertNotEqual(proc.returncode, 0, removed)
            self.assertNotIn('"terminal_outcome"', proc.stdout, removed)

    def test_approval_reference_containing_target_fails_closed(self):
        proc = self._run(*self._active_args(appref="XB-SYN_DB-01"))  # embeds the database name
        self.assertNotEqual(proc.returncode, 0)
        r = json.loads(proc.stdout)
        self.assertEqual(r["terminal_outcome"], "FAILED_BEFORE_WRITE")
        self.assertFalse(r["required_assemblies_loaded"])
        self.assertFalse(r["autocount_contacted"])
        self.assertFalse(r["claim_created"])
        self.assertFalse(r["state_root_trusted"])
        # Rejected before the claim namespace is even derived.
        self.assertIsNone(r["claim_basename"])
        self.assertIsNone(r["publication_contract"])
        # The rejected target-bearing reference must NOT be retained or emitted.
        self.assertIsNone(r["approval_reference"])
        self.assertNotIn("SYN_DB", proc.stdout)

    def test_approval_reference_bad_characters_fail_closed(self):
        proc = self._run(*self._active_args(appref="XB:BAD/REF"))  # forbidden characters
        self.assertNotEqual(proc.returncode, 0)
        r = json.loads(proc.stdout)
        self.assertEqual(r["terminal_outcome"], "FAILED_BEFORE_WRITE")
        self.assertFalse(r["claim_created"])
        self.assertFalse(r["required_assemblies_loaded"])
        self.assertFalse(r["autocount_contacted"])

    def test_refusal_is_nonzero_with_clean_json_stdout(self):
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0)
        self.assertTrue(proc.stdout.lstrip().startswith("{"), proc.stdout[:120])
        r = json.loads(proc.stdout)
        self.assertEqual(r["terminal_outcome"], "REFUSED")
        self.assertFalse(r["activated"])
        self.assertFalse(r["autocount_contacted"])
        self.assertFalse(r["evidence_persisted"])
        self.assertEqual(r["exit_code"], proc.returncode)

    @unittest.skipIf(CANONICAL_ROOT_PRESENT, CANONICAL_ROOT_SKIP)
    def test_untrusted_canonical_root_fails_closed_before_any_contact(self):
        proc = self._run(*self._active_args())
        self.assertNotEqual(proc.returncode, 0)
        r = json.loads(proc.stdout)
        self.assertEqual(r["terminal_outcome"], "CLAIM_ROOT_UNAVAILABLE")
        self.assertEqual(r["underlying_terminal_outcome"], "CLAIM_ROOT_UNAVAILABLE")
        self.assertFalse(r["state_root_trusted"])
        self.assertTrue(r["claim_root_unavailable"])
        self.assertFalse(r["required_assemblies_loaded"], "no AutoCount assembly may load")
        self.assertFalse(r["autocount_contacted"])
        self.assertFalse(r["initial_member_read_attempted"])
        self.assertFalse(r["member_recheck_attempted"])
        self.assertFalse(r["save_member_attempted"])
        self.assertFalse(r["claim_created"])
        self.assertFalse(r["evidence_persisted"], "no artefact may be created without a trusted root")
        self.assertFalse(r["non_authoritative_staging_may_remain"])
        # A reason code is reported; the raw private path never is.
        reasons = as_list(r["claim_root_failure_reasons"])
        self.assertTrue(reasons)
        expected = "platform_not_windows" if not IS_WINDOWS else "component_missing"
        self.assertIn(expected, reasons, reasons)
        self.assertNotIn(CANONICAL_STATE_ROOT, proc.stdout)
        self.assertNotIn("XB\\\\create_uat", proc.stdout)
        # Evidence binding is present and the claim namespace is already fixed.
        for field in ("operation_id", "approval_reference", "executed_at_utc", "target_fingerprint",
                      "claim_basename", "result_basename", "staging_basename"):
            self.assertTrue(r[field], field)
        self.assertNotIn("SYN_SERVER", proc.stdout)
        self.assertNotIn("SYN_DB", proc.stdout)
        self.assertNotIn("xb.expirydate.probe", proc.stdout)
        self.assertNotIn("XB EXPIRYDATE PROBE", proc.stdout)

    @unittest.skipIf(CANONICAL_ROOT_PRESENT, CANONICAL_ROOT_SKIP)
    def test_working_directory_cannot_change_the_claim_namespace(self):
        first_dir = self.tmp / "wd_one"
        second_dir = self.tmp / "wd_two"
        first_dir.mkdir()
        second_dir.mkdir()
        first = json.loads(self._run(*self._active_args(), cwd=str(first_dir)).stdout)
        second = json.loads(self._run(*self._active_args(), cwd=str(second_dir)).stdout)
        self.assertEqual(first["claim_basename"], second["claim_basename"])
        self.assertEqual(first["attempt_fingerprint"], second["attempt_fingerprint"])
        self.assertEqual(first["terminal_outcome"], "CLAIM_ROOT_UNAVAILABLE")
        self.assertEqual(second["terminal_outcome"], "CLAIM_ROOT_UNAVAILABLE")
        # Per-run identity still differs, so results never collide.
        self.assertNotEqual(first["operation_id"], second["operation_id"])
        self.assertNotEqual(first["result_basename"], second["result_basename"])

    @unittest.skipIf(CANONICAL_ROOT_PRESENT, CANONICAL_ROOT_SKIP)
    def test_no_artefact_is_written_anywhere_outside_the_canonical_root(self):
        before = sorted(p.name for p in self.tmp.iterdir())
        self._run(*self._active_args(), cwd=str(self.tmp))
        after = sorted(p.name for p in self.tmp.iterdir())
        self.assertEqual(before, after, "the probe must not write evidence into the working directory")


# ---- DL-XB-118-001: create-UAT physical-host sync approval contract ---- #
# The create-UAT runbook's step-3 `git pull --ff-only origin main` runs on physical host
# DESKTOP-Q43QKQF: it contacts a remote and mutates that machine's checkout, so it needs its own
# current-turn owner approval. No later gate (review/merge, VM deployment, no-write preflight,
# SaveMember write) may stand in for it.
#
# The checker below is deliberately PURE and TEXT-ONLY: it takes runbook text and returns finding
# keys. It performs no repository read and no path derivation, so it stays outside the closed
# dependency contract entirely, and every search is non-throwing, so a degraded fixture yields
# findings rather than a ValueError. The same function drives both the live assertion and every
# in-memory negative control, which is what proves the contract can actually fail.

HOST_SYNC_HOST = "DESKTOP-Q43QKQF"
HOST_SYNC_PULL_COMMAND = "git pull --ff-only origin main"
# The executable invocation, not a prose mention: the gate must precede the runnable command.
HOST_SYNC_PULL_INVOCATION = "```bash\n" + HOST_SYNC_PULL_COMMAND
HOST_SYNC_GATE_MARKER = "host-sync gate"
HOST_SYNC_SAFETY_HEADING = "## Safety boundary"

# Each later gate that must be stated as NON-substituting, plus the phrase that must appear
# inside that step's OWN clause. The phrase carries its polarity ("does **not**"): matching the
# bare "cover this host sync" would accept the inverted claim just as happily as the denial.
HOST_SYNC_NON_SUBSTITUTING_STEPS = ("(step 2)", "(step 4)", "(step 5)", "(step 7)")
HOST_SYNC_NON_SUBSTITUTION_PHRASE = "does **not** cover this host sync"
# A clause ends at the first of these after its step token. This syntactic bound replaces the
# earlier fixed character window, which was wide enough for a neighbouring compliant bullet to
# satisfy a mutated one. `;` and `.` alone are not enough: they belong to the bullet an editor is
# already rewriting, so the edit that inverts a denial can delete its terminator in the same
# stroke and let the clause run into the next list item. `_flat()` collapses the newline between
# Markdown bullets to " <marker> ", so that separator is a structural bound the mutated bullet does
# not own -- removing it means deleting the NEXT bullet, which the removal controls already catch.
# All three CommonMark bullet markers must be listed: an editor may write the list with "-", "*" or
# "+", and a bound that knows only one of them lets the mutated bullet run across the other two.
HOST_SYNC_CLAUSE_TERMINATORS = (" - ", " * ", " + ", ";", ".")
HOST_SYNC_FORWARD_PHRASE = (
    "does not authorise deployment, package execution, preflight or a member write")
HOST_SYNC_CURRENT_TURN_PHRASE = "current-turn owner approval"
HOST_SYNC_PRIOR_TURN_PHRASE = "prior-turn approval is not reusable"
HOST_SYNC_STOP_PHRASE = "stop before contacting"
# What the guarded command actually does. An approver cannot judge the request without all three,
# so the gate's own prose must carry them. Each phrase is composite on purpose: bare
# "fast-forwards" also occurs in the physical-host paragraph inside the same bounded slice, so
# only the full clause proves the disclosure itself is still there. "not a read-only check"
# likewise disappears when inverted to "a read-only check".
HOST_SYNC_MUTATION_DISCLOSURES = (
    "contacts the remote",
    "fast-forwards (mutates) that host's checkout",
    "not a read-only check",
)
# The Safety-boundary sentence must keep the step-3 host sync and the step-7 SaveMember write
# independent of one another: separate approvals, neither implying the other, neither reusable.
HOST_SYNC_SAFETY_TOKENS = ("desktop-q43qkqf", "step 3", "step 7", "savemember",
                           "each require their own prior current-turn owner approval",
                           "neither implies the other",
                           "a prior-turn approval is never reusable for either")

# Every finding key this contract can report.
HOST_SYNC_FINDING_KEYS = (
    "gate_after_pull", "gate_missing", "host_not_named", "mutation_disclosure_missing",
    "not_current_turn", "operation_not_named", "prior_turn_not_denied", "pull_missing",
    "safety_boundary_missing", "stop_boundary_missing", "substitution_not_denied",
)


def _flat(text):
    """Whitespace-collapsed view, so harmless Markdown line wrapping cannot break a check."""
    return " ".join(text.split())


def _clause_after(text, token):
    """Return ``token``'s own clause: the token up to its first clause terminator, else "".

    This is the scope bound for a per-step assertion. Stopping at the terminator is what keeps an
    adjacent compliant bullet from answering for a mutated one, so the bound must survive a bullet
    that drops its own punctuation: the flattened list-item separator ends the clause at the next
    bullet even then. Non-throwing: an absent token yields an empty clause, which every phrase
    check then fails.
    """
    at = text.find(token)
    if at == -1:
        return ""
    ends = [idx for idx in (text.find(end, at) for end in HOST_SYNC_CLAUSE_TERMINATORS)
            if idx != -1]
    return text[at:min(ends)] if ends else text[at:]


def host_sync_gate_findings(text):
    """Return sorted contract findings for the create-UAT physical-host sync approval gate.

    Pure and text-only: no repository read, no path derivation, and only non-throwing ``find()``
    searches, so a degraded in-memory fixture reports findings instead of raising. An empty list
    means the whole contract holds.
    """
    findings = set()

    gate_idx = text.find(HOST_SYNC_GATE_MARKER)
    pull_idx = text.find(HOST_SYNC_PULL_INVOCATION)
    if pull_idx == -1:
        findings.add("pull_missing")

    if gate_idx == -1:
        # Without the gate anchor there is no bounded prose slice, so every wording requirement
        # that lives inside the gate is unmet by definition.
        findings.update(("gate_missing", "host_not_named", "operation_not_named",
                         "not_current_turn", "substitution_not_denied",
                         "prior_turn_not_denied", "stop_boundary_missing",
                         "mutation_disclosure_missing"))
    else:
        if pull_idx != -1 and pull_idx < gate_idx:
            findings.add("gate_after_pull")
        # Bound the gate to its own prose: marker -> executable pull block when that follows,
        # otherwise -> the next step heading. Excluding the fenced command keeps "the gate names
        # the operation" an honest check on the prose rather than on the command it guards.
        heading_idx = text.find("\n### ", gate_idx)
        stops = [idx for idx in (pull_idx, heading_idx) if idx > gate_idx]
        prose = _flat(text[gate_idx:min(stops)] if stops else text[gate_idx:])
        lowered = prose.lower()

        if HOST_SYNC_HOST not in prose:
            findings.add("host_not_named")
        if HOST_SYNC_PULL_COMMAND not in prose:
            findings.add("operation_not_named")
        if HOST_SYNC_CURRENT_TURN_PHRASE not in lowered:
            findings.add("not_current_turn")
        if HOST_SYNC_PRIOR_TURN_PHRASE not in lowered:
            findings.add("prior_turn_not_denied")
        if HOST_SYNC_STOP_PHRASE not in lowered:
            findings.add("stop_boundary_missing")
        # The gate must disclose that the guarded command reaches out and changes the host.
        if any(phrase not in lowered for phrase in HOST_SYNC_MUTATION_DISCLOSURES):
            findings.add("mutation_disclosure_missing")

        # Backward non-substitution (no later gate covers this sync) and forward non-authorisation
        # (this sync covers no later action) are one contract: either gap is a substitution.
        # Each step is judged inside its own clause, so an absent, inverted or borrowed denial
        # all read the same way: that step is not denied.
        denied = HOST_SYNC_FORWARD_PHRASE in lowered
        for step in HOST_SYNC_NON_SUBSTITUTING_STEPS:
            if HOST_SYNC_NON_SUBSTITUTION_PHRASE not in _clause_after(lowered, step):
                denied = False
        if not denied:
            findings.add("substitution_not_denied")

    safety_idx = text.find(HOST_SYNC_SAFETY_HEADING)
    if safety_idx == -1:
        findings.add("safety_boundary_missing")
    else:
        next_idx = text.find("\n## ", safety_idx + 1)
        section = text[safety_idx:next_idx] if next_idx != -1 else text[safety_idx:]
        if any(token not in _flat(section).lower() for token in HOST_SYNC_SAFETY_TOKENS):
            findings.add("safety_boundary_missing")

    return sorted(findings)


# A minimal, self-contained COMPLIANT document. The negative controls degrade this rather than the
# live runbook, so they stay meaningful independently of the runbook's current state and localise
# any live failure to the live assertions. It is not a copy of the runbook: the live
# ``findings == []`` assertion remains the authority on the real document.
HOST_SYNC_CANONICAL_FIXTURE = """### 3. Physical host pulls reviewed `main`

**Separate current-turn owner approval required (host-sync gate).** The command below runs
on the physical host `DESKTOP-Q43QKQF`, contacts the remote, and fast-forwards (mutates)
that host's checkout. It is a change to an external machine, not a read-only check. Before
running it, obtain an explicit current-turn owner approval that names the physical host
(`DESKTOP-Q43QKQF`) and the pull/sync operation on it. This approval is distinct and is
**not** implied by any other gate:

- the PR review and merge decision (step 2) does **not** cover this host sync;
- the VM deployment stage (step 4) does **not** cover this host sync;
- the no-write dry-run preflight (step 5) does **not** cover this host sync;
- the separate current-turn write approval (step 7) does **not** cover this host sync.

This host-sync approval does not authorise deployment, package execution, preflight or a
member write. A prior-turn approval is not reusable. Without the named current-turn
approval, stop before contacting `DESKTOP-Q43QKQF` and do not run
`git pull --ff-only origin main`.

**`PHYSICAL HOST - DESKTOP-Q43QKQF`** After the PR is reviewed and merged, and only after
the host-sync approval above, the physical host fast-forwards to the reviewed, merged
`main`. It is never used for implementation or manual edits.

```bash
git pull --ff-only origin main
```

## Safety boundary

- The host sync on `DESKTOP-Q43QKQF` in step 3 and the `SaveMember` write in step 7
  each require their own prior current-turn owner approval. Neither implies the other,
  and a prior-turn approval is never reusable for either.
"""

# The four locked step-denial bullets, verbatim from the fixture above, keyed by step token. One
# source of truth so the removal controls and the polarity-inversion controls cannot drift apart.
HOST_SYNC_STEP_DENIAL_BULLETS = {
    "(step 2)": "- the PR review and merge decision (step 2) does **not** cover this host sync;\n",
    "(step 4)": "- the VM deployment stage (step 4) does **not** cover this host sync;\n",
    "(step 5)": "- the no-write dry-run preflight (step 5) does **not** cover this host sync;\n",
    "(step 7)": "- the separate current-turn write approval (step 7) does **not** cover this host sync.\n",
}

# CommonMark accepts `-`, `*` and `+` interchangeably as bullet markers, so an editor may rewrite
# the list with any of them. `_flat()` renders every one of those breaks as " <marker> ", which is
# why the clause bound must recognise all three and not just the hyphen the fixture happens to use.
HOST_SYNC_BULLET_MARKERS = ("-", "*", "+")


# ---- DL-XB-123-001: create-UAT VM deployment and no-write preflight approval gates ---- #
# Child #123 (parent #117). Two DIFFERENT external actions sit inside the create-UAT procedure and
# the runbook gated neither:
#
#   step 4 -- deployment MUTATES the AutoCount VM DESKTOP-4I042L6: reviewed files are copied or
#             replaced there and a VM-owned state directory is created/prepared;
#   step 5 -- preflight TRANSFERS the approved package to that VM and then AUTHENTICATES to
#             AutoCount and reads live data.
#
# They are different risk classes, so they take separate current-turn approvals rather than one
# combined one; the ExpiryDate probe runbook already treats them that way. With the step-3 host
# sync (#118) and the step-7 SaveMember write, that makes FOUR independent approval surfaces.
#
# Like the #118 checker, vm_gate_findings is PURE and TEXT-ONLY: text in, finding keys out, no
# repository read and no path derivation, and every search is non-throwing so a degraded in-memory
# fixture yields findings rather than a ValueError. It is bounded STRUCTURALLY in two layers:
#
#   1. by the runbook's own NUMBERED Markdown step headings, so step 4's gate can never be
#      answered by step 5's prose or the reverse;
#   2. inside each step, from the gate marker to that step's FIRST external action, so a
#      requirement stated only after the operator has already acted does not count.
#
# No fixed character window, no Markdown parser and no cross-bullet borrowing: per-source denials
# reuse the same syntactic clause bound #118 established, which is the generic CommonMark bound
# rather than anything host-sync specific.

VM_GATE_VM = "DESKTOP-4I042L6"

# Layer 1. Only a NUMBERED `### <n>. ` heading delimits a section. Step 5 contains many
# UNNUMBERED `### ` subsections (store admission, reconciliation, ...), so a bound that accepted
# any `### ` would end step 5 hundreds of lines early and stop covering the transfer/dry-run
# instruction that closes it.
#
# A3: this is the ONE numbered-ATX opening grammar. Discovery, duplicate detection and section
# bounding all read step identity out of its `step` group, so no second, narrower encoding of
# "what a numbered heading looks like" exists for them to drift apart on. The accepted final-G4-A2
# finding was exactly that drift: `^### \d+\. ` plus a `startswith("### <n>. ")` probe recognised
# one spelling, while CommonMark renders the same top-level `h3` for
#
#   * 0-3 leading ASCII spaces -- FOUR is an indented code block and stays excluded, so an
#     indented Markdown example inside a step cannot make the real step ambiguous;
#   * exactly three `#`, since `####` opens an h4 and is not a step;
#   * one or more spaces OR TABS after `###`;
#   * one or more spaces OR TABS after `<n>.`.
#
# A duplicate written in an unrecognised spelling is invisible rather than mis-parsed, and that is
# the severity: its body is absorbed into the neighbouring section's ACTION region, where only
# presence checks run, so an ungated instruction rides along with no findings at all.
VM_GATE_STEP_HEADING = re.compile(r"(?m)^ {0,3}###(?!#)[ \t]+(?P<step>\d+)\.[ \t]+")

# A CommonMark ATX closing sequence: a run of `#` preceded by whitespace and followed by nothing
# but optional whitespace. It is SYNTAX -- stripped before rendering -- so it must not read as a
# wording change. The preceding-whitespace requirement and the end anchor are what keep this from
# becoming a bypass in the other direction: in `### 4. Title ### and push now` the run is followed
# by content, so CommonMark keeps the whole line as heading text and this pattern declines to
# strip it, leaving the trailing instruction visible as drift.
VM_GATE_ATX_CLOSING = re.compile(r"[ \t]+#+[ \t]*$")
VM_GATE_DEPLOY_STEP = 4
VM_GATE_PREFLIGHT_STEP = 5

# The reviewed numbered heading LINES, verbatim. Accepted G4 finding F-4 was that the heading line
# sits outside both pre-gate authorities -- step 4's blank-body rule and step 5's frozen digest --
# so actionable external wording could ride in the heading itself and still precede the gate.
# Exact-string authority closes that without touching the frozen prefix digest, which is derived
# from the text AFTER this line and must stay byte-stable.
VM_GATE_REVIEWED_HEADINGS = {
    VM_GATE_DEPLOY_STEP: "### 4. Deploy the inactive UAT components",
    VM_GATE_PREFLIGHT_STEP: "### 5. No-write preflight (dry-run)",
}

VM_GATE_DEPLOY_MARKER = "deployment gate"
VM_GATE_PREFLIGHT_MARKER = "preflight gate"
VM_GATE_SAFETY_HEADING = "## Safety boundary"
# The same line-anchored ATX discipline the numbered steps use, so a deeper `### Safety boundary`
# subsection is not counted as a second top-level authority merely because the literal heading
# text is a substring of it.
VM_GATE_SAFETY_OPENING = re.compile(r"(?m)^ {0,3}##(?!#)[ \t]+Safety boundary")

# Layer 2 (A1). Ordering is no longer inferred from a vocabulary of action phrases. Accepted G4
# finding F-1 showed that inference is unsound: a synonym before the gate goes unrecognised while
# a listed phrase after the gate keeps satisfying the check, and the oracle reports clean. The
# right edge of a gate is now the START of that step's post-gate operational prose, so the gate
# block ends BEFORE the operational banner and cannot borrow its identity (accepted F-2).
VM_GATE_DEPLOY_BOUNDARY = "Copy the reviewed"
# A4: the step-5 gate's right edge is now the FIRST post-gate operation -- the laptop build
# banner -- and no longer the later AutoCount VM banner. Accepted findings
# PRRT_kwDOSbJI_s6YQTNV and PRRT_kwDOSbJI_s6YQTMq were two instances of one mistake: treating the
# private-data package build and the environment configuration as preparation that happens BEFORE
# the gated part of the step. They are gated work themselves, so they belong inside the region
# the gate governs. Moving the boundary here is what makes the ordering rule mean "nothing at all
# precedes this approval" rather than "nothing external precedes it".
VM_GATE_PREFLIGHT_BOUNDARY = "**`LAPTOP DEVELOPMENT MACHINE`**"

# The ordering finding each step reports when its action boundary precedes its gate. Kept under
# the original key names so the pre-A1 ordering controls keep asserting the same contract.
VM_GATE_ORDERING_KEYS = {"deploy": "deploy_gate_after_mutation",
                         "preflight": "preflight_gate_after_external_action"}

# Operation EXISTENCE, checked in the action region and deliberately separate from ordering. The
# gate's own copies of these names are approval prose, not the operation, so they do not count.
VM_GATE_DEPLOY_ACTION_FILES = (
    "scripts/ac2_member_create_uat_runner.ps1",
    "scripts/member_create_uat_runner_lib.ps1",
    "config/member_create_uat_business_confirmation.json",
)
VM_GATE_DEPLOY_STATE_ANCHOR = r'new-item -itemtype directory -path "c:\xb\create_uat\state"'
# The executable invocation is required because the summary sentence immediately after the gate
# must never stand in for the real operation -- that substitution is what allowed F-1 to pass a
# document whose actual dry-run had been moved before the gate or deleted outright. The summary
# says "copy the APPROVED package to the vm", so it cannot satisfy the transfer anchor either.
VM_GATE_PREFLIGHT_TRANSFER_ANCHOR = "copy the package to the vm"
VM_GATE_PREFLIGHT_RUNNER_ANCHOR = r"& scripts\ac2_member_create_uat_runner.ps1 -packagepath"

# ---- A4: required operations must be ACTIVE, EXECUTABLE command lines ---- #
# Accepted finding PRRT_kwDOSbJI_s6YQTM_: a required command was proven by substring over the
# whole flattened action region, so commenting the dry-run out as
# `# & scripts\ac2_member_create_uat_runner.ps1 -PackagePath ...` kept the anchor satisfied while
# the document no longer invoked the preflight at all. A mention of a command is not the command.
# These openers are the narrow, line-level test for "this line cannot execute": a comment in
# PowerShell or shell, or an HTML comment in Markdown. It is deliberately NOT a fenced-code,
# list, blockquote or container model -- A4 does not authorise one, and the accepted fenced-code
# false positive PRRT_kwDOSbJI_s6YQTNF is left exactly as it was.
VM_GATE_COMMENT_OPENERS = ("#", "//", "<!--")

# The two argparse commands the gated step-5 build actually runs, VERBATIM and CASE-SENSITIVE.
# Accepted finding PRRT_kwDOSbJI_s6YQTM4: the retired prefix digest folded case, so `--input`
# becoming `--INPUT` left it unchanged even though `member_create_uat_approval.py` is argparse and
# would reject the mutated flag outright. These are therefore compared WITHOUT lowercasing. The
# PowerShell runner anchor above stays folded on purpose: PowerShell parameter names really are
# case-insensitive, so demanding exact case there would assert a contract the tool does not have,
# which is a false guard rather than a stronger one.
VM_GATE_PREFLIGHT_APPROVE_COMMAND = (
    "python scripts/member_create_uat_approval.py approve --reviewer <handle>"
    " --input <form.csv> --decision-rows <member_intake_decision_rows.csv>"
    " --row-number <N> --ledger <ledger.jsonl>")
VM_GATE_PREFLIGHT_BUILD_COMMAND = (
    "python scripts/member_create_uat_approval.py build-package"
    " --input <form.csv> --decision-rows <member_intake_decision_rows.csv>"
    " --row-number <N> --ledger <ledger.jsonl>"
    " --package-out <member_create_uat_package_v2.json>")

# The AutoCount process-environment configuration, which accepted finding PRRT_kwDOSbJI_s6YQTMq
# showed running under no approval at all: it sat in step 4 AFTER the deployment gate, which
# authorises no AutoCount contact, and BEFORE the step-5 gate. A4 moves it under the step-5
# approval. Only the variable NAMES are ever contracted -- no host, database, account book or
# password value belongs in this repository, so the contract requires the names and nothing else.
VM_GATE_PREFLIGHT_ENV_ANCHOR = "set the autocount connection through the process environment only"
VM_GATE_ENV_VARIABLE_NAMES = ("AC2_PROBE_SERVER_NAME", "AC2_PROBE_DATABASE_NAME",
                              "AC2_PROBE_USER_ID", "-PasswordEnvVar")

# ---- A4: the approval must be stated AFFIRMATIVELY ---- #
# Accepted finding PRRT_kwDOSbJI_s6YQTNO: the gates were proven by the bare presence of the token
# `current-turn owner approval`, so "do NOT obtain an explicit current-turn owner approval" kept
# every tested token and returned no findings. Presence is not polarity.
#
# The repair stays inside the NARROW reviewed gate grammar rather than attempting to judge
# arbitrary English: the reviewed affirmative clause must be present, and no clause that mentions
# the approval token may carry a negation. Clause bounds reuse the CommonMark terminator set the
# #118 contract established, so a neighbouring compliant sentence cannot answer for an inverted
# one, and an inverted one cannot hide behind a compliant neighbour.
VM_GATE_AFFIRMATIVE_APPROVAL = ("obtain an explicit current-turn owner approval that names the"
                                " autocount vm")
VM_GATE_APPROVAL_NEGATIONS = (" not", "n't", " never", " without", " no ", " rather than",
                              " instead of", " skip ", " unnecessary")

# What the step-4 approval must actually bind. Naming the VM is not enough: an approver cannot
# judge "deploy to the VM" without knowing which components are replaced and what state is
# prepared, so each component and the state directory must appear in the gate's own prose.
VM_GATE_DEPLOY_BINDINGS = (
    "copying or replacing",
    "scripts/ac2_member_create_uat_runner.ps1",
    "scripts/member_create_uat_runner_lib.ps1",
    "config/member_create_uat_business_confirmation.json",
    "creating or preparing",
    r"c:\xb\create_uat\state",
)
# What the step-5 approval must bind, each with its own finding so a control can prove exactly
# which binding was lost. The target is named IN THE APPROVAL: connection values and secrets are
# never written into the runbook, so the contract requires the phrase, never a value.
# A4 adds the first three. Under the old layout the private-data read, the decision-store and
# ledger mutations, the package build and the environment configuration all happened BEFORE the
# gate, so the approval never had to mention them; now that the gate governs the whole step, an
# approver has to be told what they are actually approving. Each entry carries the contract
# phrase in the document's own case and a single-LINE fragment a control can rewrite, because the
# phrase itself may span a Markdown line break. The checker lowercases at comparison rather than
# keeping a second lower-cased copy, so the controls and the contract cannot drift apart.
VM_GATE_A4_NEW_BINDINGS = (
    ("preflight_private_data_not_bound",
     "the bounded access to the selected private form response and its decision row",
     "the bounded access to the selected private form response and its decision row"),
    ("preflight_package_build_not_bound",
     "the local reviewer-decision store and approval-ledger operations and the immutable"
     " package build",
     "the local reviewer-decision store and approval-ledger operations"),
    ("preflight_environment_not_bound",
     "the AutoCount process-environment configuration, by variable name only",
     "the AutoCount process-environment configuration, by variable name only"),
)
VM_GATE_PREFLIGHT_BINDINGS = tuple(
    (key, _flat(phrase).lower()) for key, phrase, _fragment in VM_GATE_A4_NEW_BINDINGS
) + (
    ("preflight_target_not_bound",
     "the intended autocount target (the server and database / account book)"),
    ("preflight_transfer_not_bound", "the bounded transfer of the approved package to that vm"),
    ("preflight_dry_run_not_bound", "the no-write dry-run / preflight operation"),
)

VM_GATE_CURRENT_TURN = "current-turn owner approval"
VM_GATE_PRIOR_TURN = "a prior-turn approval is not reusable"

# Every other approval surface must be denied inside its OWN clause, carrying its own polarity:
# matching a bare "authorise this deployment" would accept the inverted claim just as happily.
VM_GATE_DEPLOY_SOURCES = ("(step 2)", "(step 3)", "(step 5)", "(step 7)")
VM_GATE_DEPLOY_DENIAL = "does **not** authorise this deployment"
VM_GATE_PREFLIGHT_SOURCES = ("(step 2)", "(step 3)", "(step 4)", "(step 7)")
VM_GATE_PREFLIGHT_DENIAL = "does **not** authorise this preflight"

VM_GATE_DEPLOY_STOP = ("stop before copying or replacing files or creating or preparing state "
                       "on the vm")
# Deployment authorises placement only. Execution and AutoCount contact belong to steps 5 and 7,
# so the gate must say so or "deployed" quietly becomes "may now be run".
# A4 adds the environment clause: accepted finding PRRT_kwDOSbJI_s6YQTMq was possible partly
# because step 4 said nothing either way about configuring the connection, so a reader could take
# the deployment approval to cover it.
VM_GATE_DEPLOY_NO_EXECUTION = ("this deployment approval authorises no runner execution, no "
                               "autocount environment configuration and no autocount contact")

# A4 widens the step-5 stop boundary to the operations the gate now actually precedes. Under the
# old layout it named only the transfer and AutoCount contact, which is exactly the reading that
# left the private-data build and the environment setup outside any approval at all.
VM_GATE_PREFLIGHT_STOP = ("stop before reading the private form response or decision row, before "
                          "building the package, before setting the autocount environment, and "
                          "before transferring the package to the vm or contacting autocount")
# The dry-run's permitted reach and its hard limit, as one proposition: stating what it MAY do
# without stating that it still may not save would license the write this contract excludes.
VM_GATE_PREFLIGHT_SAVE_MEMBER = (
    "may authenticate, check the duplicate and construct the member in memory",
    "does **not** authorise or call `savemember`",
)

# The Safety boundary must keep all four surfaces independent. These tokens are additive to the
# #118 host-sync/write sentence, which stays exactly as it is.
VM_GATE_SAFETY_TOKENS = (
    "the step-3 host sync, the step-4 vm deployment, the step-5 package transfer and no-write "
    "preflight, and the step-7 `savemember` write are four independent approval surfaces",
    "each requires its own current-turn owner approval",
    "none implies or covers another",
    "a prior-turn approval is never reusable for any of them",
)

# Everything a resolved step must prove. When the structural layout CANNOT be resolved -- no
# gate, two gates, no boundary, two boundaries, or a boundary before its gate -- the step is
# marked wholly unmet instead of being partially evaluated against a slice that may not mean what
# it appears to. That is the fail-closed half of the A1 contract.
VM_GATE_DEPLOY_UNMET = frozenset((
    "deploy_pre_gate_content", "deploy_vm_not_named", "deploy_operation_not_bound",
    "deploy_not_current_turn", "deploy_substitution_not_denied", "deploy_prior_turn_not_denied",
    "deploy_stop_boundary_missing", "deploy_execution_not_denied", "deploy_operation_missing",
    "deploy_state_preparation_missing",
))
# A4 replaces `preflight_prefix_changed` with the same structural rule step 4 already carries.
# Once the gate moves to the top of the step there is nothing legitimate left in front of it, so
# the pre-gate region is simply blank -- strictly stronger than any digest, and the reason the
# case-folding finding is answered by deleting the digest rather than by re-hashing it.
VM_GATE_PREFLIGHT_UNMET = frozenset((
    "preflight_pre_gate_content", "preflight_vm_not_named", "preflight_private_data_not_bound",
    "preflight_package_build_not_bound", "preflight_environment_not_bound",
    "preflight_target_not_bound",
    "preflight_transfer_not_bound", "preflight_dry_run_not_bound", "preflight_not_current_turn",
    "preflight_substitution_not_denied", "preflight_prior_turn_not_denied",
    "preflight_save_member_not_denied", "preflight_stop_boundary_missing",
    "preflight_approval_command_missing", "preflight_package_build_missing",
    "preflight_environment_setup_missing",
    "preflight_operation_missing", "preflight_runner_invocation_missing",
))

# Every finding key this contract can report. A4 retires `preflight_prefix_changed` and adds
# eight: the step-5 blank pre-gate rule, the three new step-5 approval bindings, the three
# post-gate operation-existence keys (package approval, package build, environment setup) and
# safety-boundary ambiguity. 39 keys become 46.
VM_GATE_FINDING_KEYS = (
    "deploy_boundary_ambiguous", "deploy_boundary_missing", "deploy_execution_not_denied",
    "deploy_gate_after_mutation", "deploy_gate_marker_ambiguous", "deploy_gate_missing",
    "deploy_heading_changed", "deploy_not_current_turn", "deploy_operation_missing",
    "deploy_operation_not_bound",
    "deploy_pre_gate_content", "deploy_prior_turn_not_denied", "deploy_state_preparation_missing",
    "deploy_step_ambiguous", "deploy_step_missing", "deploy_stop_boundary_missing",
    "deploy_substitution_not_denied",
    "deploy_vm_not_named", "preflight_approval_command_missing", "preflight_boundary_ambiguous",
    "preflight_boundary_missing",
    "preflight_dry_run_not_bound", "preflight_environment_not_bound",
    "preflight_environment_setup_missing", "preflight_gate_after_external_action",
    "preflight_gate_marker_ambiguous", "preflight_gate_missing", "preflight_heading_changed",
    "preflight_not_current_turn",
    "preflight_operation_missing", "preflight_package_build_missing",
    "preflight_package_build_not_bound", "preflight_pre_gate_content",
    "preflight_prior_turn_not_denied", "preflight_private_data_not_bound",
    "preflight_runner_invocation_missing", "preflight_save_member_not_denied",
    "preflight_step_ambiguous", "preflight_step_missing", "preflight_stop_boundary_missing",
    "preflight_substitution_not_denied", "preflight_target_not_bound",
    "preflight_transfer_not_bound", "preflight_vm_not_named", "safety_boundary_ambiguous",
    "safety_boundary_not_four_way",
)


def _semantic_heading(line):
    """The RENDERED identity of an ATX heading line, as a reviewer sees it.

    Whitespace is collapsed, as everywhere else in this contract, and a CommonMark closing `#`
    sequence is removed because it is syntax rather than content. ``line.strip()`` first, so a
    CRLF checkout's trailing ``\\r`` cannot sit between the closing run and the end anchor and
    quietly defeat the strip.
    """
    return _flat(VM_GATE_ATX_CLOSING.sub("", line.strip()))


def _numbered_heading_openings(text):
    """Every top-level numbered ATX opening in ``text``, as ``(offset, step number)``.

    The single enumeration every other numbered-step helper is built on. Opening discovery and
    section bounding MUST share this grammar: a heading one of them accepted and the other did not
    would once again orphan whatever followed it, which is the accepted A3 defect.
    """
    return [(match.start(), int(match.group("step")))
            for match in VM_GATE_STEP_HEADING.finditer(text)]


def _numbered_step_section(text, number):
    """Return one numbered Markdown step section, bounded by the numbered step headings.

    Layer 1 of the structural bound. Non-throwing: an absent step yields "", which then fails
    every requirement that lives inside it rather than raising. This is section EXTRACTION only:
    ``_resolve_numbered_step`` owns the uniqueness and heading-identity authority, because taking
    the first of two same-number headings is exactly the accepted F-3 defect.

    A3: the section closes at the next opening of ANY numbered step, drawn from the same shared
    enumeration that found this one, so a duplicate cannot be simultaneously invisible to
    discovery and inert as a bound.
    """
    openings = _numbered_step_openings(text, number)
    if not openings:
        return ""
    opening = openings[0]
    closing = next((at for at, _ in _numbered_heading_openings(text) if at > opening), -1)
    return text[opening:closing] if closing != -1 else text[opening:]


def _line_start(text, at):
    """Start of the line containing ``at``. A missing offset stays missing."""
    return -1 if at == -1 else text.rfind("\n", 0, at) + 1


def _numbered_step_openings(text, number):
    """Every offset at which a NUMBERED heading opens step ``number``.

    Step identity comes from the shared match's own ``step`` group, never from a literal probe
    such as ``text.startswith("### 4. ", start)``. A literal probe is a second, narrower grammar,
    and the accepted A3 finding is precisely what happens when the two disagree.
    """
    return [at for at, step in _numbered_heading_openings(text) if step == number]


def _resolve_numbered_step(text, number, prefix, findings):
    """Resolve step ``number`` to its ONE authoritative section, or fail closed.

    Layer 0 of the structural bound, and the whole A2 repair. Two accepted final-G4 findings,
    both demonstrated as false cleans:

    * the step number must open EXACTLY once. Zero is the pre-existing missing-step case; MORE
      than one is ambiguous authority, because silently taking the first occurrence is what let a
      second same-number section orphan an ungated external instruction while the genuine section
      stayed compliant -- accepted F-3. A harmless duplicate fails closed too: once the number
      appears twice there is no answer to which section governs, and guessing is the defect.
    * the heading LINE is itself immutable authority. It sits outside both pre-gate authorities
      (step 4's blank body, step 5's frozen digest), so without this an actionable heading could
      carry external-action semantics ahead of the gate -- accepted F-4. Exact-string authority is
      used rather than folding the heading into the digest, so the frozen step-5 prefix digest
      stays byte-stable.

    Whitespace is non-material, matching the rest of this contract: a CRLF checkout or a reflowed
    heading is not drift, while case, punctuation and wording changes all fail closed.
    Non-throwing: every failure yields "", which then fails every requirement inside the step.

    A3 widens WHICH openings count, not WHAT they must say. Any mixture of spellings for the same
    step number is still more than one opening and still fails closed here -- there is no majority
    vote and no "the strict one wins" -- while ``_semantic_heading`` keeps identity at the rendered
    heading, so a respelling that renders the reviewed heading exactly stays clean and substantive
    drift still fails closed.
    """
    openings = _numbered_step_openings(text, number)
    if not openings:
        findings.add(prefix + "_step_missing")
        return ""
    if len(openings) > 1:
        findings.add(prefix + "_step_ambiguous")
        return ""
    section = _numbered_step_section(text, number)
    if _semantic_heading(section.partition("\n")[0]) \
            != _semantic_heading(VM_GATE_REVIEWED_HEADINGS[number]):
        findings.add(prefix + "_heading_changed")
        return ""
    return section


def _resolve_gate_layout(section, marker, boundary, prefix, findings):
    """Resolve one step into ``(pre_gate, gate_block, action_region)``, or fail closed.

    This is the whole A1 repair. Three landmarks, all structural:

    * the gate marker, which must occur EXACTLY once -- zero and many both fail closed, because
      silently taking the first occurrence is how a decoy mention could shift the bounded slice;
    * the action boundary, the START of the step's post-gate operational prose, which must also
      occur exactly once. Ending the gate block here (rather than at some inner action verb) is
      what stops the operational banner satisfying a gate-local proposition -- accepted F-2;
    * their order. Ordering is judged on the EARLIEST occurrence of each and independently of
      ambiguity, so a boundary that straddles the gate is reported as misordered as well.

    Nothing here consults action vocabulary, so a synonym cannot evade it -- accepted F-1. When
    the layout cannot be resolved every element is ``None`` and the caller marks the whole step
    unmet rather than guessing around the gap.
    """
    gate_at, gate_count = section.find(marker), section.count(marker)
    boundary_at, boundary_count = section.find(boundary), section.count(boundary)

    misordered = gate_at != -1 and boundary_at != -1 and boundary_at < gate_at
    if misordered:
        findings.add(VM_GATE_ORDERING_KEYS[prefix])
    if gate_count == 0:
        findings.add(prefix + "_gate_missing")
    elif gate_count > 1:
        findings.add(prefix + "_gate_marker_ambiguous")
    if boundary_count == 0:
        findings.add(prefix + "_boundary_missing")
    elif boundary_count > 1:
        findings.add(prefix + "_boundary_ambiguous")
    if gate_count != 1 or boundary_count != 1 or misordered:
        return None, None, None

    # The pre-gate region is everything between the step's heading line and the gate's own line.
    # Step 4 requires it to be blank; step 5's is the frozen reviewed-safe prefix.
    heading_end = section.find("\n")
    gate_line = _line_start(section, gate_at)
    pre_gate = section[heading_end + 1:gate_line] if heading_end != -1 else ""
    return pre_gate, section[gate_line:boundary_at], section[boundary_at:]


def _active_command_lines(region):
    """The lines of ``region`` that could actually execute, whitespace-collapsed.

    A required operation must be a live command, not a mention of one. Accepted finding
    PRRT_kwDOSbJI_s6YQTM_ was exactly this: a commented-out
    ``# & scripts\\ac2_member_create_uat_runner.ps1 -PackagePath ...`` still satisfied a substring
    anchor taken over the whole flattened action region, so a runbook whose dry-run had been
    disabled reported no findings at all.

    Deliberately line-level and deliberately narrow. A line whose first non-space character opens
    a comment cannot execute -- in PowerShell, in shell, or as an HTML comment in Markdown -- and
    that is the entire test. This is NOT a fenced-code, list, blockquote or container model: A4
    does not authorise one, and the accepted conservative false positive PRRT_kwDOSbJI_s6YQTNF is
    left exactly as it was.
    """
    return [_flat(line) for line in region.splitlines()
            if line.strip() and not line.strip().startswith(VM_GATE_COMMENT_OPENERS)]


def _actively_invokes(region, anchor, fold_case=True):
    """True when ``anchor`` occurs on a line of ``region`` that is not commented out.

    ``fold_case`` follows the TOOL, not a house style. PowerShell parameter names really are
    case-insensitive, so folding there matches reality; ``member_create_uat_approval.py`` is
    argparse and genuinely case-sensitive, so its commands are compared verbatim. Accepted
    finding PRRT_kwDOSbJI_s6YQTM4 is what happens when the comparison is more permissive than the
    tool: ``--INPUT`` breaks the build while the guard stays clean.
    """
    return any(anchor in (line.lower() if fold_case else line)
               for line in _active_command_lines(region))


def _clause_before(text, at):
    """The clause ending at ``at``, back to its own opening terminator.

    The mirror of ``_clause_after``, bounded by the same CommonMark terminator set, so a negation
    in a neighbouring sentence cannot be blamed on this clause and a negation in THIS clause
    cannot hide behind a compliant neighbour.
    """
    starts = [idx for idx in (text.rfind(end, 0, at) for end in HOST_SYNC_CLAUSE_TERMINATORS)
              if idx != -1]
    return text[max(starts):at] if starts else text[:at]


def _approval_is_affirmative(prose):
    """True only when ``prose`` REQUIRES the current-turn approval rather than mentioning it.

    Accepted finding PRRT_kwDOSbJI_s6YQTNO: presence is not polarity. Testing only that the token
    ``current-turn owner approval`` occurs accepts "do not obtain an explicit current-turn owner
    approval", which keeps every tested token while stating the opposite of the contract.

    Two conditions, both inside the narrow reviewed gate grammar rather than any attempt to decide
    arbitrary English:

    * the reviewed affirmative clause must be present, so the requirement is actually stated;
    * no clause MENTIONING the approval token may carry a negation. Only such clauses are
      inspected, which is what leaves the gate's own legitimate denials alone -- "authorises no
      runner execution", "does **not** authorise this preflight", "a prior-turn approval is not
      reusable" -- because none of them mentions the token.
    """
    if VM_GATE_AFFIRMATIVE_APPROVAL not in prose:
        return False
    at = prose.find(VM_GATE_CURRENT_TURN)
    while at != -1:
        clause = " " + _clause_before(prose, at) + _clause_after(prose[at:], VM_GATE_CURRENT_TURN)
        if any(negation in clause for negation in VM_GATE_APPROVAL_NEGATIONS):
            return False
        at = prose.find(VM_GATE_CURRENT_TURN, at + 1)
    return True


def _every_source_denied(prose, sources, denial):
    """True only when EVERY other approval is denied inside its own clause.

    Reusing ``_clause_after`` is deliberate: its terminator set is the generic CommonMark bullet
    and punctuation bound, not anything host-sync specific, and duplicating that carefully
    reasoned bound would let the two copies drift while both claim to stop clause borrowing.
    """
    return all(denial in _clause_after(prose, source) for source in sources)


def _deployment_findings(text, findings):
    """Step 4: a current-turn approval must precede every VM mutation the step performs."""
    section = _resolve_numbered_step(text, VM_GATE_DEPLOY_STEP, "deploy", findings)
    pre_gate, block, action = _resolve_gate_layout(
        section, VM_GATE_DEPLOY_MARKER, VM_GATE_DEPLOY_BOUNDARY, "deploy", findings)
    if block is None:
        findings.update(VM_GATE_DEPLOY_UNMET)
        return

    # Ordering, structurally. Nothing may stand between the step heading and its gate, so no
    # instruction -- transfer, place, send, move, or a verb nobody has thought of yet -- can be
    # smuggled in ahead of the approval. Whitespace stays non-material so ordinary Markdown
    # reflow does not fire the guard.
    if pre_gate.strip():
        findings.add("deploy_pre_gate_content")

    # Gate propositions, judged ONLY inside the gate's own block.
    prose = _flat(block).lower()
    if VM_GATE_VM.lower() not in prose:
        findings.add("deploy_vm_not_named")
    if any(token not in prose for token in VM_GATE_DEPLOY_BINDINGS):
        findings.add("deploy_operation_not_bound")
    if not _approval_is_affirmative(prose):
        findings.add("deploy_not_current_turn")
    if VM_GATE_PRIOR_TURN not in prose:
        findings.add("deploy_prior_turn_not_denied")
    if VM_GATE_DEPLOY_STOP not in prose:
        findings.add("deploy_stop_boundary_missing")
    if VM_GATE_DEPLOY_NO_EXECUTION not in prose:
        findings.add("deploy_execution_not_denied")
    if not _every_source_denied(prose, VM_GATE_DEPLOY_SOURCES, VM_GATE_DEPLOY_DENIAL):
        findings.add("deploy_substitution_not_denied")

    # Operation existence, judged ONLY in the action region and separately from ordering. A gate
    # guarding nothing is not a pass; equally, the approval prose naming these components is not
    # the operation, so its copies cannot answer for a deleted instruction.
    region = _flat(action).lower()
    if any(path not in region for path in VM_GATE_DEPLOY_ACTION_FILES):
        findings.add("deploy_operation_missing")
    # The state directory is created by a real PowerShell command, so it is held to the same
    # active-command rule as the step-5 operations. Leaving it a substring would have kept an
    # identical commented-out bypass open one step to the left of the one Codex reported.
    if not _actively_invokes(action, VM_GATE_DEPLOY_STATE_ANCHOR):
        findings.add("deploy_state_preparation_missing")


def _preflight_findings(text, findings):
    """Step 5: a current-turn approval must precede the package transfer AND the dry-run."""
    section = _resolve_numbered_step(text, VM_GATE_PREFLIGHT_STEP, "preflight", findings)
    pre_gate, block, action = _resolve_gate_layout(
        section, VM_GATE_PREFLIGHT_MARKER, VM_GATE_PREFLIGHT_BOUNDARY, "preflight", findings)
    if block is None:
        findings.update(VM_GATE_PREFLIGHT_UNMET)
        return

    # Ordering, structurally -- and under A4 by exactly the rule step 4 already uses. The frozen
    # reviewed-safe prefix is retired: the package build it protected is itself gated work, so it
    # now sits AFTER the gate and nothing legitimate remains in front of it. Accepted findings
    # PRRT_kwDOSbJI_s6YQTNV (private form/decision-row reads and decision-store, ledger and
    # package mutations ahead of the gate) and PRRT_kwDOSbJI_s6YQTMq (environment configuration
    # ahead of it) are both closed by the move rather than by a better digest, and the accepted
    # case-folding finding PRRT_kwDOSbJI_s6YQTM4 disappears with the digest it was about.
    if pre_gate.strip():
        findings.add("preflight_pre_gate_content")

    prose = _flat(block).lower()
    if VM_GATE_VM.lower() not in prose:
        findings.add("preflight_vm_not_named")
    for key, token in VM_GATE_PREFLIGHT_BINDINGS:
        if token not in prose:
            findings.add(key)
    if any(name.lower() not in prose for name in VM_GATE_ENV_VARIABLE_NAMES):
        findings.add("preflight_environment_not_bound")
    if not _approval_is_affirmative(prose):
        findings.add("preflight_not_current_turn")
    if VM_GATE_PRIOR_TURN not in prose:
        findings.add("preflight_prior_turn_not_denied")
    if any(token not in prose for token in VM_GATE_PREFLIGHT_SAVE_MEMBER):
        findings.add("preflight_save_member_not_denied")
    if VM_GATE_PREFLIGHT_STOP not in prose:
        findings.add("preflight_stop_boundary_missing")
    if not _every_source_denied(prose, VM_GATE_PREFLIGHT_SOURCES, VM_GATE_PREFLIGHT_DENIAL):
        findings.add("preflight_substitution_not_denied")

    # Operation existence in the action region. Every load-bearing COMMAND must be an active,
    # executable line; the transfer instruction stays a prose anchor because that is what it is in
    # the document. The gate block is excluded from this region, so the approval's own copies of
    # these names cannot answer for a deleted or commented-out instruction.
    region = _flat(action).lower()
    if VM_GATE_PREFLIGHT_TRANSFER_ANCHOR not in region:
        findings.add("preflight_operation_missing")
    if not _actively_invokes(action, VM_GATE_PREFLIGHT_APPROVE_COMMAND, fold_case=False):
        findings.add("preflight_approval_command_missing")
    if not _actively_invokes(action, VM_GATE_PREFLIGHT_BUILD_COMMAND, fold_case=False):
        findings.add("preflight_package_build_missing")
    if VM_GATE_PREFLIGHT_ENV_ANCHOR not in region \
            or any(name.lower() not in region for name in VM_GATE_ENV_VARIABLE_NAMES):
        findings.add("preflight_environment_setup_missing")
    if not _actively_invokes(action, VM_GATE_PREFLIGHT_RUNNER_ANCHOR):
        findings.add("preflight_runner_invocation_missing")


def _four_way_safety_findings(text, findings):
    """The Safety boundary must be UNIQUE, and must keep all four approval surfaces independent.

    Accepted finding PRRT_kwDOSbJI_s6YQTMw: the old implementation read the FIRST raw occurrence
    only, so appending a second boundary that contradicts or weakens the four surfaces left the
    checker clean while the document carried two irreconcilable statements of its own safety
    authority. Uniqueness is therefore established BEFORE any token is validated, exactly as
    ``_resolve_numbered_step`` already does for steps 4 and 5. A harmless duplicate fails closed
    for the same reason a harmless duplicate step does: once the authority appears twice there is
    no answer to which one governs, and guessing is the defect rather than the inconvenience.
    """
    openings = [match.start() for match in VM_GATE_SAFETY_OPENING.finditer(text)]
    if not openings:
        findings.add("safety_boundary_not_four_way")
        return
    if len(openings) > 1:
        findings.add("safety_boundary_ambiguous")
        return
    at = openings[0]
    end = text.find("\n## ", at + 1)
    section = _flat(text[at:end] if end != -1 else text[at:]).lower()
    if any(token not in section for token in VM_GATE_SAFETY_TOKENS):
        findings.add("safety_boundary_not_four_way")


def vm_gate_findings(text):
    """Return sorted contract findings for the create-UAT VM deployment and preflight gates.

    Pure and text-only: no repository read, no path derivation and only non-throwing searches. An
    empty list means the whole DL-XB-123-001 contract holds.
    """
    findings = set()
    _deployment_findings(text, findings)
    _preflight_findings(text, findings)
    _four_way_safety_findings(text, findings)
    return sorted(findings)


# A minimal, self-contained COMPLIANT document. Every negative control degrades THIS rather than
# the live runbook, so the controls stay meaningful independently of the runbook's current state
# and any live failure localises to the single live assertion. It is not a copy of the runbook:
# the live ``findings == []`` assertion remains the authority on the real document.
# ---- DL-XB-123-001-A4: the canonical fixture is the A4 target document ---- #
# The frozen reviewed-safe step-5 prefix that used to live here is retired together with the
# digest it fed. Under A4 nothing legitimate precedes the step-5 gate, so there is no prefix
# left to freeze and the step-5 pre-gate rule is simply step 4's: blank. VM_GATE_A4_FIXTURE,
# introduced at the controls commit and promoted below, is now the canonical fixture.

# The gate paragraph openings and the operation openings, verbatim from the fixture. One source of
# truth so the relocation controls and the wording controls cannot drift apart.
VM_GATE_DEPLOY_OPENING = "**Separate current-turn owner approval required (deployment gate).**"
VM_GATE_PREFLIGHT_OPENING = "**Separate current-turn owner approval required (preflight gate).**"
# Aliases of the structural action boundaries, so the pre-A1 controls that split the fixture at
# "where the operation starts" stay pinned to the same landmark the checker uses. Under A1 the
# preflight boundary is the operational banner itself, not the inner "Only after ..." clause.
VM_GATE_DEPLOY_OPERATION_OPENING = VM_GATE_DEPLOY_BOUNDARY
VM_GATE_PREFLIGHT_OPERATION_OPENING = VM_GATE_PREFLIGHT_BOUNDARY

# The eight locked denial bullets, verbatim from the fixture, keyed by source step.
VM_GATE_DEPLOY_DENIAL_BULLETS = {
    "(step 2)": "- the PR review and merge decision (step 2) does **not** authorise this"
                " deployment;\n",
    "(step 3)": "- the physical-host sync approval (step 3) does **not** authorise this"
                " deployment;\n",
    "(step 5)": "- the no-write preflight approval (step 5) does **not** authorise this"
                " deployment;\n",
    "(step 7)": "- the separate current-turn write approval (step 7) does **not** authorise this"
                " deployment.\n",
}
VM_GATE_PREFLIGHT_DENIAL_BULLETS = {
    "(step 2)": "- the PR review and merge decision (step 2) does **not** authorise this"
                " preflight;\n",
    "(step 3)": "- the physical-host sync approval (step 3) does **not** authorise this"
                " preflight;\n",
    "(step 4)": "- the VM deployment approval (step 4) does **not** authorise this preflight;\n",
    "(step 7)": "- the separate current-turn write approval (step 7) does **not** authorise this"
                " preflight.\n",
}

# The A1 structural landmarks (gate markers, action boundaries and post-gate operation anchors)
# are declared beside the checker that consumes them, above.

# ---- DL-XB-123-001-A3: the CommonMark numbered-ATX opening families ---- #
# Accepted final-G4-A2 finding: numbered-step discovery recognised ONE spelling of a top-level
# numbered ATX heading -- column 0, exactly one space after `###`, exactly one space after `<n>.`.
# CommonMark renders an `h3` for considerably more than that: 0-3 leading spaces, any run of
# spaces or tabs after the opening `###`, any run of spaces or tabs after `<n>.`, and an optional
# closing `#` sequence. Every template below therefore renders the SAME heading a reviewer sees.
#
# A second Step 4 or Step 5 written in any of them is a real duplicate section that the narrower
# discovery cannot enumerate. Its contents are silently absorbed into a neighbouring section --
# in practice the genuine step's ACTION region, where only presence checks run -- so an ungated
# deployment, transfer or preflight instruction can sit in the document while the oracle reports
# clean. That is accepted B-1 / F-3 recurrence, and duplication is ambiguity even when the
# duplicate prose is harmless.
VM_GATE_A3_DUPLICATE_HEADINGS = (
    ("one_leading_space", " ### %d. %s"),
    ("two_leading_spaces", "  ### %d. %s"),
    ("three_leading_spaces", "   ### %d. %s"),
    ("two_spaces_after_hashes", "###  %d. %s"),
    ("three_spaces_after_hashes", "###   %d. %s"),
    ("tab_after_hashes", "###\t%d. %s"),
    ("tab_after_step_number", "### %d.\t%s"),
    ("indent_plus_closing_hashes", "  ### %d. %s ###"),
)

# The strict column-zero form the narrow discovery already recognised. Kept as the A3 control
# group: it must keep failing closed exactly as A2 left it, because a grammar widened carelessly
# could just as easily have stopped recognising the one form that already worked.
VM_GATE_A3_STRICT_HEADING = "### %d. %s"

# Four leading spaces is an indented CODE BLOCK in CommonMark, never a heading. It must stay
# OUTSIDE top-level numbered-step authority: promoting it would let an ordinary indented Markdown
# example inside a step silently make the real step ambiguous, which is a false positive severe
# enough to make the contract unmaintainable.
VM_GATE_A3_CODE_BLOCK_HEADING = "    ### %d. %s"

# Whitespace-only spellings of the SINGLE genuine heading. Each renders the reviewed heading
# exactly, so each must stay clean: the reviewed heading's identity is semantic, and a CRLF
# checkout, a re-indent or a syntax-only closing `#` run is not drift. The closing sequence is
# included because CommonMark strips it before rendering, so treating it as content would make a
# purely syntactic marker look like a wording change.
VM_GATE_A3_SEMANTIC_HEADING_VARIANTS = (
    ("one_leading_space", " ### %d. %s"),
    ("three_leading_spaces", "   ### %d. %s"),
    ("two_spaces_after_hashes", "###  %d. %s"),
    ("tab_after_hashes", "###\t%d. %s"),
    ("tab_after_step_number", "### %d.\t%s"),
    ("closing_hash_sequence", "### %d. %s ###"),
    ("indent_and_padded_closing_hashes", "  ### %d. %s   ###  "),
)

# Substantive drift wearing a whitespace-valid opening. Widening the opening grammar must not
# widen heading IDENTITY: case, punctuation and wording still have to fail closed, and a run of
# `#` followed by further CONTENT is not a CommonMark closing sequence at all, so the trailing
# words remain part of the heading and must be seen as drift rather than stripped as syntax.
VM_GATE_A3_DRIFT_HEADINGS = {
    VM_GATE_DEPLOY_STEP: (
        ("case", "  ### %d. deploy the inactive uat components"),
        ("punctuation", " ###  %d. Deploy the inactive UAT components."),
        ("wording", "###\t%d. Deploy the UAT components"),
        ("content_after_hashes", "### %d. Deploy the inactive UAT components ### and push now"),
    ),
    VM_GATE_PREFLIGHT_STEP: (
        ("case", "  ### %d. NO-WRITE PREFLIGHT (DRY-RUN)"),
        ("punctuation", " ###  %d. No write preflight (dry run)"),
        ("wording", "###\t%d. Preflight the approved package"),
        ("content_after_hashes", "### %d. No-write preflight (dry-run) ### then transfer"),
    ),
}

# The rogue duplicate's own title, deliberately distinct from the reviewed heading: a duplicate
# that reused the reviewed title verbatim could be dismissed as an accidental copy, whereas a
# retitled section is what an editor actually writes when revising a step.
VM_GATE_A3_DUPLICATE_TITLES = {
    VM_GATE_DEPLOY_STEP: "Deploy the inactive UAT components (revised)",
    VM_GATE_PREFLIGHT_STEP: "No-write preflight (dry-run) (revised)",
}

# Explicit ungated external action, so the controls prove a SAFETY consequence rather than a
# tidiness preference. Neither body carries a gate marker or an action boundary, so neither can
# satisfy -- or trip -- a landmark check and let a control pass for the wrong reason.
VM_GATE_A3_UNSAFE_BODIES = {
    VM_GATE_DEPLOY_STEP: (
        "Push the reviewed runner and library onto DESKTOP-4I042L6 now, replace the files\n"
        "already there and prepare the VM-owned state directory, without waiting for any\n"
        "owner approval.\n"),
    VM_GATE_PREFLIGHT_STEP: (
        "Move the approved package onto DESKTOP-4I042L6 and run the dry-run against\n"
        "AutoCount now, without waiting for any owner approval.\n"),
}
VM_GATE_A3_HARMLESS_BODY = "Editorial note only. Nothing to add.\n"

# Where the duplicate sits relative to the genuine step. The end-of-section placement is the
# dangerous one and the reason both are exercised: it leaves the genuine step's heading, gate and
# action region completely intact, so nothing except opening enumeration can notice it.
VM_GATE_A3_AFTER = "end of the genuine section"
VM_GATE_A3_BEFORE = "before the genuine step"
VM_GATE_A3_PLACEMENTS = (VM_GATE_A3_AFTER, VM_GATE_A3_BEFORE)


# ---- DL-XB-123-001-A4: post-ready Codex review remediation ---- #
# Automatic Codex reviewed exact head 23ddf88 after PR #125 was marked ready and opened seven
# threads. Six are accepted as actionable, and every one of them is a demonstrated FALSE CLEAN
# against the head-23ddf88 checker rather than a stylistic preference:
#
#   * PRRT_kwDOSbJI_s6YQTMq (P1) -- the AutoCount process-environment setup sits in Step 4 AFTER
#     the deployment gate and BEFORE the Step-5 preflight gate. Neither gate names credential or
#     environment configuration, so a documented environment mutation runs under no approval.
#   * PRRT_kwDOSbJI_s6YQTNV (P1) -- the Step-5 `approve` and `build-package` commands read the
#     private form response and decision rows and mutate the local decision store, the approval
#     ledger and the package, all BEFORE the Step-5 gate. Laptop locality does not waive the
#     current-turn approval requirement for private/customer data.
#   * PRRT_kwDOSbJI_s6YQTMw (P1) -- `_four_way_safety_findings()` reads the FIRST raw
#     `## Safety boundary` occurrence only, so a second, contradictory boundary can be appended
#     while the checker still reports clean.
#   * PRRT_kwDOSbJI_s6YQTNO (P1) -- current-turn approval is proven by bare token presence, so
#     "do NOT obtain an explicit current-turn owner approval" keeps every tested token and
#     false-cleans. Both Step 4 and Step 5 are affected.
#   * PRRT_kwDOSbJI_s6YQTM4 (P2) -- the frozen Step-5 prefix hashes `_flat(pre_gate).lower()`, so
#     case-sensitive CLI drift such as `--input` -> `--INPUT` leaves the digest unchanged.
#   * PRRT_kwDOSbJI_s6YQTM_ (P2) -- the dry-run runner is a substring check over flattened prose,
#     so `# & scripts\ac2_member_create_uat_runner.ps1 -PackagePath ...` satisfies it while the
#     document no longer invokes the preflight at all.
#
# The seventh thread, PRRT_kwDOSbJI_s6YQTNF, is the previously accepted conservative fenced-code
# false positive. A4 does NOT authorise a Markdown parser, fenced-code modelling, or list,
# blockquote or HTML-comment containers, so it is deliberately left unchanged here.
#
# The accepted A4 architecture moves the Step-5 gate to the TOP of its step, ahead of every
# private-data, package-build, environment, transfer and runner action, which is what retires the
# frozen-prefix design: once nothing legitimate precedes the gate, the Step-5 pre-gate region is
# simply blank, exactly as Step 4's already is. That is strictly stronger than any digest, and it
# is why A4 does not answer the case-folding finding with a second, case-preserving digest.

# The finding keys A4 introduces. Declared here so the controls below and the repaired checker
# cannot drift apart on spelling, and so a reviewer can see the whole added surface in one place.
VM_GATE_A4_NEW_KEYS = (
    "preflight_pre_gate_content",
    "preflight_private_data_not_bound",
    "preflight_package_build_not_bound",
    "preflight_environment_not_bound",
    "preflight_approval_command_missing",
    "preflight_package_build_missing",
    "preflight_environment_setup_missing",
    "safety_boundary_ambiguous",
)

# The controls exercise the SAME constants the repaired checker consumes, never private copies.
# A control that degraded its own duplicate of a command or a variable name could keep passing
# after the contract had drifted away from it, which would make the whole A4 control set decorative.
VM_GATE_A4_APPROVE_COMMAND = VM_GATE_PREFLIGHT_APPROVE_COMMAND
VM_GATE_A4_BUILD_COMMAND = VM_GATE_PREFLIGHT_BUILD_COMMAND
VM_GATE_A4_ENV_NAMES = VM_GATE_ENV_VARIABLE_NAMES

# The A4 TARGET document shape: Step 4 reduced to deployment and state preparation only, and
# Step 5 gated from its first line. It is introduced here, at the controls commit, because the
# controls have to name a document the repaired checker must accept -- at this commit the
# head-23ddf88 checker still rejects it, which is part of the RED evidence. Commit J promotes it
# to THE canonical fixture, so these controls survive the repair unchanged.
VM_GATE_A4_FIXTURE_STEP_4 = r"""### 4. Deploy the inactive UAT components

**Separate current-turn owner approval required (deployment gate).** The instructions below
change an external machine: they place reviewed files on the AutoCount VM `DESKTOP-4I042L6` and
prepare a directory that the VM then owns. Before any of them, obtain an explicit current-turn
owner approval that names the AutoCount VM (`DESKTOP-4I042L6`) and binds this exact deployment
operation:

- copying or replacing `scripts/ac2_member_create_uat_runner.ps1` on that VM;
- copying or replacing `scripts/member_create_uat_runner_lib.ps1` on that VM;
- copying or replacing `config/member_create_uat_business_confirmation.json` on that VM;
- creating or preparing the VM-owned state directory `C:\XB\create_uat\state`.

This approval is distinct and is **not** implied by any other gate:

- the PR review and merge decision (step 2) does **not** authorise this deployment;
- the physical-host sync approval (step 3) does **not** authorise this deployment;
- the no-write preflight approval (step 5) does **not** authorise this deployment;
- the separate current-turn write approval (step 7) does **not** authorise this deployment.

A prior-turn approval is not reusable. This deployment approval authorises no runner execution,
no AutoCount environment configuration and no AutoCount contact. Without the named current-turn
deployment approval, stop before copying or replacing files or creating or preparing state on
the VM.

Copy the reviewed `scripts/ac2_member_create_uat_runner.ps1`,
`scripts/member_create_uat_runner_lib.ps1`, and
`config/member_create_uat_business_confirmation.json` to the AutoCount VM working area.

**`AUTOCOUNT VM — DESKTOP-4I042L6`**

```powershell
New-Item -ItemType Directory -Path "C:\XB\create_uat\state" -Force
```

"""

VM_GATE_A4_FIXTURE_STEP_5 = r"""### 5. No-write preflight (dry-run)

**Separate current-turn owner approval required (preflight gate).** The whole of this step is
gated. It reads the selected private form response and its decision row, mutates the local
reviewer-decision store and the approval ledger, builds an immutable package, configures the
AutoCount connection in the process environment, moves that package onto the AutoCount VM
`DESKTOP-4I042L6`, and then authenticates to AutoCount and reads live data. Laptop locality does
not waive the approval for the private-data work. Before any of it, obtain an explicit
current-turn owner approval that names the AutoCount VM (`DESKTOP-4I042L6`) and binds:

- the bounded access to the selected private form response and its decision row for this one
  package, whose values are never written into this runbook;
- the local reviewer-decision store and approval-ledger operations and the immutable package
  build they produce;
- the AutoCount process-environment configuration, by variable name only:
  `AC2_PROBE_SERVER_NAME`, `AC2_PROBE_DATABASE_NAME`, `AC2_PROBE_USER_ID`, and the password
  environment variable named by `-PasswordEnvVar`;
- the intended AutoCount target (the server and database / account book), named in the approval
  itself and never written into this runbook as a connection value or secret;
- the bounded transfer of the approved package to that VM;
- the no-write dry-run / preflight operation.

This approval is distinct and is **not** implied by any other gate:

- the PR review and merge decision (step 2) does **not** authorise this preflight;
- the physical-host sync approval (step 3) does **not** authorise this preflight;
- the VM deployment approval (step 4) does **not** authorise this preflight;
- the separate current-turn write approval (step 7) does **not** authorise this preflight.

A prior-turn approval is not reusable. The dry-run may authenticate, check the duplicate and
construct the member in memory, but it does **not** authorise or call `SaveMember`; that write
remains gated by step 7. Without the named current-turn preflight approval, stop before reading
the private form response or decision row, before building the package, before setting the
AutoCount environment, and before transferring the package to the VM or contacting AutoCount.

**`LAPTOP DEVELOPMENT MACHINE`** Only after the preflight approval above, build the approved
package on the laptop, using the decision-review output that shows the chosen row as
`READY_FOR_CREATE_REVIEW`:

```bash
python scripts/member_create_uat_approval.py approve --reviewer <handle> --input <form.csv> --decision-rows <member_intake_decision_rows.csv> --row-number <N> --ledger <ledger.jsonl>
```

```bash
python scripts/member_create_uat_approval.py build-package --input <form.csv> --decision-rows <member_intake_decision_rows.csv> --row-number <N> --ledger <ledger.jsonl> --package-out <member_create_uat_package_v2.json>
```

Set the AutoCount connection through the process environment only (never in files, never in this
runbook): `AC2_PROBE_SERVER_NAME`, `AC2_PROBE_DATABASE_NAME`, `AC2_PROBE_USER_ID`, and the
password environment variable named by `-PasswordEnvVar`.

**`AUTOCOUNT VM — DESKTOP-4I042L6`** Under the same preflight approval, copy the approved package
to the VM and run the runner in dry-run mode (the default; no write switches).

Copy the package to the VM, then dry-run:

```powershell
& scripts\ac2_member_create_uat_runner.ps1 -PackagePath "C:\XB\create_uat\member_create_uat_package.json" -StateDir "C:\XB\create_uat\state" -JsonOut "C:\XB\create_uat\member_create_uat_result.json"
```

### 6. Review aggregate evidence

The runner prints and writes a sanitized aggregate result only.

## Safety boundary

- The host sync on `DESKTOP-Q43QKQF` in step 3 and the `SaveMember` write in step 7
  each require their own prior current-turn owner approval. Neither implies the other,
  and a prior-turn approval is never reusable for either.
- The step-3 host sync, the step-4 VM deployment, the step-5 package transfer and no-write
  preflight, and the step-7 `SaveMember` write are four independent approval surfaces. Each
  requires its own current-turn owner approval, none implies or covers another, and a prior-turn
  approval is never reusable for any of them.
"""

VM_GATE_A4_FIXTURE = VM_GATE_A4_FIXTURE_STEP_4 + VM_GATE_A4_FIXTURE_STEP_5

# A minimal, self-contained COMPLIANT document, and the base every negative control degrades
# rather than the live runbook, so the controls stay meaningful independently of the
# runbook's current state and any live failure localises to the single live assertion. It is
# not a copy of the runbook: the live ``findings == []`` assertion remains the authority on
# the real document. It is a faithful miniature -- same gate wording, same post-gate ordering,
# and the same real operations (package approval, package build, environment setup, transfer
# and the executable runner invocation) the contract anchors on.
VM_GATE_CANONICAL_FIXTURE = VM_GATE_A4_FIXTURE

# The Step-5 action boundary MOVES under A4: the gate's right edge is now the FIRST post-gate
# operation -- the laptop build banner -- not the later AutoCount VM banner, because the
# private-data build is itself gated work and must sit inside the region the gate governs rather
# than ahead of it. The alias keeps the controls on the checker's own landmark.
VM_GATE_A4_PREFLIGHT_BOUNDARY = VM_GATE_PREFLIGHT_BOUNDARY
# The environment-setup anchor in the document's own case, so a control can rewrite the real
# sentence; the checker compares it lowercased, and a test proves the two agree.
VM_GATE_A4_ENV_ANCHOR = "Set the AutoCount connection through the process environment only"

# Each inversion rewrites the SAME reviewed affirmative clause, so it applies verbatim to both
# gates and neither control can pass for a step-specific reason.
VM_GATE_A4_AFFIRMED_CLAUSE = "obtain an explicit current-turn owner approval that names"
VM_GATE_A4_POLARITY_INVERSIONS = (
    ("do_not_obtain",
     "do not obtain an explicit current-turn owner approval that names"),
    ("not_required",
     "note that a current-turn owner approval is not required, and skip the approval that names"),
    ("without_approval",
     "proceed without current-turn owner approval, ignoring the approval that names"),
)


class ExpiryProbeRunbookAndCiTests(unittest.TestCase):
    def setUp(self):
        self.runbook = read_repo_text("probe_runbook")
        self.create_runbook = read_repo_text("create_uat_runbook")
        self.readme = read_repo_text("readme")
        self.workflow = read_repo_text("workflow")

    def test_runbook_exists_and_separates_eight_stages(self):
        self.assertTrue(self.runbook.strip())
        for n in range(1, 9):
            self.assertRegex(self.runbook, rf"(?m)^### {n}\. ")

    def test_runbook_states_required_safety_facts(self):
        self.assertRegex(self.runbook, r"(?i)current-turn owner approval is required before the synthetic")
        self.assertIn("the AutoCount target", self.runbook)
        self.assertIn("exactly one synthetic record", self.runbook)
        self.assertRegex(self.runbook, r"(?i)uncertain save outcome is terminal and must never be retried automatically")
        self.assertRegex(self.runbook, r"(?i)No real,\s+form-derived member is ever used")
        self.assertRegex(self.runbook, r"(?i)not the permanent production member-intake workflow")

    def test_runbook_documents_current_controls(self):
        self.assertIn("-ApprovalReference", self.runbook)
        self.assertIn("WRITE_CONFIRMED_READBACK_FAILED", self.runbook)
        self.assertIn("ATTEMPT_ALREADY_CLAIMED", self.runbook)
        self.assertRegex(self.runbook, r"(?i)permanent single-use\s+attempt claim")
        self.assertRegex(self.runbook, r"(?i)exit code")
        self.assertRegex(self.runbook, r"(?i)never removes the attempt claim")
        for outcome in ("CLAIM_PERSISTENCE_FAILED", "EVIDENCE_PERSISTENCE_FAILED",
                        "CLAIM_ROOT_UNAVAILABLE", "ATTEMPT_CLAIM_LOST_AFTER_CONTACT"):
            self.assertIn(outcome, self.runbook, outcome)

    def test_runbook_documents_the_fixed_canonical_state_root(self):
        self.assertIn(CANONICAL_STATE_ROOT, self.runbook)
        self.assertRegex(self.runbook, r"(?i)fixed canonical state root")
        self.assertRegex(self.runbook, r"(?i)not selectable")
        self.assertRegex(self.runbook, r"(?i)no `-StateDirectory` and no `-JsonOut`")
        self.assertRegex(self.runbook, r"(?i)never creates, repairs, migrates, cleans or\s+redirects it")
        self.assertRegex(self.runbook, r"(?i)junction, symbolic link or other reparse point")

    def test_runbook_probe_command_passes_no_path_parameters(self):
        # The operator-facing invocation must not reintroduce a selectable path.
        commands = [block for block in re.findall(r"```powershell\n(.*?)```", self.runbook, re.S)
                    if "ac2_member_expiry_capability_probe.ps1 -EnableExpiryCapabilityProbe" in block]
        self.assertEqual(len(commands), 1, "exactly one probe invocation command is expected")
        command = commands[0]
        self.assertIn("-ApprovalReference", command)
        self.assertNotIn("-StateDirectory", command)
        self.assertNotIn("-JsonOut", command)

    def test_runbook_documents_the_publication_contract_and_validator(self):
        for token in ("expiry_probe_staging_<operation_id>.incomplete",
                      "authoritative_result_basename", "publication_contract_version",
                      "authority_rule", "Test-ExpiryProbeAuthoritativeResult",
                      "non_authoritative_staging_may_remain"):
            self.assertIn(token, self.runbook, token)
        self.assertRegex(self.runbook, r"(?i)left exactly as written")
        self.assertRegex(self.runbook, r"(?i)do \*\*not\*\* rely on a filename glob alone")

    def test_runbook_host_sync_gate_precedes_the_pull_command(self):
        gate_idx = self.runbook.index("host-sync gate")
        pull_idx = self.runbook.index("git pull --ff-only origin main")
        self.assertLess(gate_idx, pull_idx, "the host-sync approval gate must precede the pull command")
        # The gate names the host and the operation.
        gate_text = self.runbook[gate_idx:pull_idx]
        self.assertIn("DESKTOP-Q43QKQF", gate_text)
        self.assertRegex(gate_text, r"(?i)pull/sync operation")
        self.assertRegex(gate_text, r"(?i)current-turn owner approval")
        # And it states that no later approval covers it.
        self.assertRegex(gate_text, r"(?i)deployment approval \(stage 3\) does \*\*not\*\* cover this host sync")
        self.assertRegex(gate_text, r"(?i)preflight approval \(stage 4\) does \*\*not\*\* cover this host sync")
        self.assertRegex(gate_text, r"(?i)`SaveMember` write approval \(stage 5\) does \*\*not\*\* cover this host sync")

    def test_runbook_has_distinct_bounded_deployment_stage(self):
        self.assertRegex(self.runbook, r"(?m)^### 3\. Deploy the reviewed probe files")
        self.assertIn("scripts/member_expiry_capability_probe_lib.ps1", self.runbook)
        self.assertIn("Get-FileHash", self.runbook)
        self.assertRegex(self.runbook, r"(?i)hyper-v\s*/\s*smb|shared-folder bridge")
        self.assertRegex(self.runbook, r"(?i)bounded[^.]*backup")
        self.assertRegex(self.runbook, r"(?i)exact-version verification|exact equality|exactly equal")
        self.assertIn(r"C:\XB\create_uat\probe\ac2_member_expiry_capability_probe.ps1", self.runbook)
        deploy_idx = self.runbook.index("### 3. Deploy the reviewed probe files")
        approval_idx = self.runbook.index("### 5.")
        self.assertLess(deploy_idx, approval_idx, "deployment must precede the approval stage")

    def test_runbook_followup_requires_runner_wiring_before_flip(self):
        self.assertRegex(self.runbook, r"(?i)not sufficient and is unsafe")
        self.assertRegex(self.runbook, r"(?i)could save a member with")
        self.assertIn("CREATED_VERIFIED", self.runbook)
        self.assertIn("Wire the runner", self.runbook)
        self.assertRegex(self.runbook, r"(?i)read-back")
        self.assertIn("member_create_uat_approval.py", self.runbook)

    def test_runbook_has_separate_approval_gates_for_each_external_action(self):
        self.assertRegex(self.runbook, r"(?i)host-sync gate")
        self.assertRegex(self.runbook, r"(?i)deployment gate")
        self.assertRegex(self.runbook, r"(?i)preflight gate")
        self.assertRegex(self.runbook, r"(?i)destructive mutation of live AutoCount")
        self.assertRegex(self.runbook, r"(?i)carry over\s+to deletion")
        self.assertRegex(self.runbook, r"(?i)does \*\*not\*\* authorise it|does \*\*not\*\* cover it")

    def test_runbook_requires_opaque_approval_reference(self):
        self.assertIn("<opaque-approval-id>", self.runbook)
        self.assertNotIn("approval-ref-naming-target", self.runbook)
        self.assertRegex(self.runbook, r"(?i)opaque, non-secret approval identifier")
        self.assertRegex(self.runbook, r"(?i)put the server or database")

    def test_runbook_cross_reference_to_the_main_create_uat_runbook_resolves(self):
        # The probe runbook links the main single-member creation UAT runbook by relative
        # path; that target must exist (its contents are owned by the create-UAT lane).
        self.assertIn("(member_create_uat_runbook.md)", self.runbook)
        self.assertTrue(read_repo_text("create_uat_runbook").strip(),
                        "the linked main create-UAT runbook must exist and be non-empty")

    # ---- DL-XB-118-001: create-UAT physical-host sync approval gate ---- #
    def _create_uat_gate_prose(self):
        """The live gate's own prose, marker -> executable pull block, whitespace-collapsed."""
        gate_idx = self.create_runbook.find(HOST_SYNC_GATE_MARKER)
        pull_idx = self.create_runbook.find(HOST_SYNC_PULL_INVOCATION)
        self.assertNotEqual(gate_idx, -1, "the create-UAT host-sync gate marker must be present")
        self.assertNotEqual(pull_idx, -1, "the fenced physical-host pull command must be present")
        self.assertLess(gate_idx, pull_idx,
                        "the host-sync gate must precede the physical-host pull command")
        return _flat(self.create_runbook[gate_idx:pull_idx])

    def test_create_uat_runbook_satisfies_the_whole_host_sync_gate_contract(self):
        self.assertEqual(host_sync_gate_findings(self.create_runbook), [],
                         "the create-UAT runbook must satisfy every host-sync gate requirement")

    def test_create_uat_gate_precedes_the_pull_and_names_host_and_operation(self):
        prose = self._create_uat_gate_prose()          # also asserts gate-before-pull ordering
        self.assertIn(HOST_SYNC_HOST, prose, "the gate must name the physical host")
        self.assertIn(HOST_SYNC_PULL_COMMAND, prose, "the gate must name the exact operation")
        self.assertRegex(prose, r"(?i)pull/sync operation")

    def test_create_uat_gate_requires_a_current_turn_approval(self):
        prose = self._create_uat_gate_prose().lower()
        self.assertIn(HOST_SYNC_CURRENT_TURN_PHRASE, prose)
        self.assertIn(HOST_SYNC_PRIOR_TURN_PHRASE, prose,
                      "a prior-turn approval must be stated as non-reusable")

    def test_create_uat_gate_denies_substitution_in_both_directions(self):
        prose = self._create_uat_gate_prose().lower()
        # Backward: no later gate covers this sync. Each denial must carry its own polarity
        # inside its own clause, so a neighbouring bullet cannot stand in for a missing one.
        for step in HOST_SYNC_NON_SUBSTITUTING_STEPS:
            self.assertNotEqual(prose.find(step), -1, "the gate must name %s" % step)
            self.assertIn(HOST_SYNC_NON_SUBSTITUTION_PHRASE, _clause_after(prose, step),
                          "%s's own clause must deny that it covers this host sync" % step)
        # Forward: this sync authorises no later action.
        self.assertIn(HOST_SYNC_FORWARD_PHRASE, prose)

    def test_create_uat_gate_discloses_remote_contact_mutation_and_not_read_only(self):
        prose = self._create_uat_gate_prose().lower()
        for phrase in HOST_SYNC_MUTATION_DISCLOSURES:
            self.assertIn(phrase, prose,
                          "the gate must disclose that the command %r" % (phrase,))

    def test_create_uat_gate_stops_execution_before_contacting_the_host(self):
        self.assertIn(HOST_SYNC_STOP_PHRASE, self._create_uat_gate_prose().lower())

    def test_create_uat_safety_boundary_keeps_host_sync_and_write_independent(self):
        safety_idx = self.create_runbook.find(HOST_SYNC_SAFETY_HEADING)
        self.assertNotEqual(safety_idx, -1, "the create-UAT runbook must keep a safety boundary")
        section = _flat(self.create_runbook[safety_idx:]).lower()
        for token in HOST_SYNC_SAFETY_TOKENS:
            self.assertIn(token, section, token)

    def test_create_uat_runbook_makes_no_blanket_off_laptop_gating_claim(self):
        # The runbook gates the four surfaces it names (steps 3, 4, 5 and 7); it does not gate
        # every conceivable off-laptop action, so a blanket claim would be untrue however many
        # individual gates exist. #118 must not add one and #123 must not either.
        flat = _flat(self.create_runbook).lower()
        for claim in ("every off-laptop", "each off-laptop", "all off-laptop"):
            self.assertNotIn(claim, flat,
                             "a blanket off-laptop gating claim is false while #123 is open")

    def test_create_uat_runbook_read_stays_inside_the_closed_dependency_contract(self):
        self.assertEqual(repository_read_violations(read_repo_text("focused_tests")), [],
                         "the host-sync guardrail must add no repository read outside the registry")

    # ---- Negative controls: every one degrades an IN-MEMORY fixture, never a repository file ----
    def test_canonical_host_sync_fixture_is_itself_compliant(self):
        # The control group. Without this, a degraded fixture proving "findings appear" would be
        # worthless: the findings might have been there all along.
        self.assertEqual(host_sync_gate_findings(HOST_SYNC_CANONICAL_FIXTURE), [],
                         "the canonical fixture must satisfy the contract before it is degraded")

    def _degraded_gate(self, old, new):
        """Degrade the fixture's gate prose only, proving the fixture actually changed."""
        split = HOST_SYNC_CANONICAL_FIXTURE.find(HOST_SYNC_PULL_INVOCATION)
        degraded = (HOST_SYNC_CANONICAL_FIXTURE[:split].replace(old, new)
                    + HOST_SYNC_CANONICAL_FIXTURE[split:])
        self.assertNotEqual(degraded, HOST_SYNC_CANONICAL_FIXTURE,
                            "the degraded fixture must actually differ: %r" % (old,))
        return degraded

    def _degraded_safety(self, old, new):
        """Degrade the fixture's safety boundary only, proving the fixture actually changed."""
        split = HOST_SYNC_CANONICAL_FIXTURE.find(HOST_SYNC_SAFETY_HEADING)
        degraded = (HOST_SYNC_CANONICAL_FIXTURE[:split]
                    + HOST_SYNC_CANONICAL_FIXTURE[split:].replace(old, new))
        self.assertNotEqual(degraded, HOST_SYNC_CANONICAL_FIXTURE,
                            "the degraded fixture must actually differ: %r" % (old,))
        return degraded

    def test_control_pull_moved_before_the_gate_is_detected(self):
        fence = HOST_SYNC_PULL_INVOCATION + "\n```\n"
        self.assertIn(fence, HOST_SYNC_CANONICAL_FIXTURE)
        degraded = fence + HOST_SYNC_CANONICAL_FIXTURE.replace(fence, "", 1)
        self.assertNotEqual(degraded, HOST_SYNC_CANONICAL_FIXTURE)
        self.assertIn("gate_after_pull", host_sync_gate_findings(degraded))

    def test_control_missing_pull_command_is_detected(self):
        degraded = HOST_SYNC_CANONICAL_FIXTURE.replace(HOST_SYNC_PULL_INVOCATION, "```bash\ntrue")
        self.assertNotEqual(degraded, HOST_SYNC_CANONICAL_FIXTURE)
        self.assertIn("pull_missing", host_sync_gate_findings(degraded))

    def test_control_unnamed_host_is_detected(self):
        degraded = self._degraded_gate(HOST_SYNC_HOST, "the physical host")
        self.assertIn("host_not_named", host_sync_gate_findings(degraded))

    def test_control_unnamed_operation_is_detected(self):
        degraded = self._degraded_gate("`" + HOST_SYNC_PULL_COMMAND + "`", "`the pull command`")
        self.assertIn("operation_not_named", host_sync_gate_findings(degraded))

    def test_control_generic_approval_wording_replacing_the_gate_is_detected(self):
        degraded = self._degraded_gate(HOST_SYNC_GATE_MARKER, "approval required")
        self.assertIn("gate_missing", host_sync_gate_findings(degraded))

    def test_control_missing_current_turn_wording_is_detected(self):
        degraded = self._degraded_gate(HOST_SYNC_CURRENT_TURN_PHRASE, "owner approval")
        self.assertIn("not_current_turn", host_sync_gate_findings(degraded))

    def test_control_each_removed_non_substitution_statement_is_detected(self):
        for step, statement in HOST_SYNC_STEP_DENIAL_BULLETS.items():
            with self.subTest(step=step):
                degraded = self._degraded_gate(statement, "")
                self.assertIn("substitution_not_denied", host_sync_gate_findings(degraded))

    def test_control_removed_forward_non_authorisation_is_detected(self):
        # Degrade against RAW fixture text: HOST_SYNC_FORWARD_PHRASE is the whitespace-collapsed
        # form and the fixture wraps that sentence, so it is not a literal substring here.
        degraded = self._degraded_gate("does not authorise deployment", "authorises deployment")
        self.assertIn("substitution_not_denied", host_sync_gate_findings(degraded))

    def test_control_removed_prior_turn_non_reuse_is_detected(self):
        degraded = self._degraded_gate("A prior-turn approval is not reusable. ", "")
        self.assertIn("prior_turn_not_denied", host_sync_gate_findings(degraded))

    def test_control_removed_stop_boundary_is_detected(self):
        degraded = self._degraded_gate(
            "stop before contacting `DESKTOP-Q43QKQF` and do not run", "do not run")
        self.assertIn("stop_boundary_missing", host_sync_gate_findings(degraded))

    def test_control_removed_safety_boundary_independence_is_detected(self):
        degraded = self._degraded_safety("Neither implies the other,", "")
        self.assertIn("safety_boundary_missing", host_sync_gate_findings(degraded))

    # -- Polarity controls: a denial INVERTED in place, not removed -- #
    # Removal controls only prove the oracle notices an absent bullet. These prove it notices a
    # bullet that is still present, still carries its step token, and now says the opposite.
    def _inverted_step_denial(self, step):
        """Flip one bullet's `does **not**` to `does`, leaving the rest of the bullet intact."""
        bullet = HOST_SYNC_STEP_DENIAL_BULLETS[step]
        inverted = bullet.replace("does **not** cover", "does cover")
        self.assertNotEqual(inverted, bullet, "the inversion must change %s" % step)
        self.assertIn(step, inverted, "the inverted bullet must keep its step token")
        return self._degraded_gate(bullet, inverted)

    def test_control_inverted_step_2_non_substitution_is_detected(self):
        degraded = self._inverted_step_denial("(step 2)")
        self.assertIn("substitution_not_denied", host_sync_gate_findings(degraded))

    def test_control_inverted_step_4_non_substitution_is_detected(self):
        degraded = self._inverted_step_denial("(step 4)")
        self.assertIn("substitution_not_denied", host_sync_gate_findings(degraded))

    def test_control_inverted_step_5_non_substitution_is_detected(self):
        degraded = self._inverted_step_denial("(step 5)")
        self.assertIn("substitution_not_denied", host_sync_gate_findings(degraded))

    def test_control_inverted_step_7_non_substitution_is_detected(self):
        degraded = self._inverted_step_denial("(step 7)")
        self.assertIn("substitution_not_denied", host_sync_gate_findings(degraded))

    def test_control_a_neighbouring_compliant_bullet_cannot_satisfy_an_inverted_one(self):
        # The specific bypass a fixed proximity window allows: invert one bullet and let the
        # untouched bullet next to it supply the denial phrase. Each denial must stand alone.
        degraded = self._inverted_step_denial("(step 4)")
        for neighbour in ("(step 2)", "(step 5)", "(step 7)"):
            self.assertIn(HOST_SYNC_STEP_DENIAL_BULLETS[neighbour], degraded,
                          "%s must remain compliant and adjacent" % neighbour)
        self.assertIn("substitution_not_denied", host_sync_gate_findings(degraded))

    # -- Unterminated inversion: the bound must not depend on the bullet's own punctuation -- #
    # `;` and `.` belong to the bullet an editor is already rewriting, so the same edit that
    # inverts a denial can delete its terminator. A clause bounded only by that punctuation then
    # runs on into the NEXT list item, whose untouched denial answers for the mutated one.
    def _inverted_unterminated_step_denial(self, step, neighbour):
        """Invert one bullet AND drop its own trailing `;`, leaving ``neighbour`` intact."""
        bullet = HOST_SYNC_STEP_DENIAL_BULLETS[step]
        mutated = bullet.replace("does **not** cover", "does cover").replace(" sync;\n", " sync\n")
        self.assertNotEqual(mutated, bullet, "the mutation must change %s" % step)
        self.assertIn(step, mutated, "the mutated bullet must keep its step token")
        self.assertNotIn(";", mutated, "%s must lose its own clause terminator" % step)
        self.assertNotIn(HOST_SYNC_NON_SUBSTITUTION_PHRASE, mutated,
                         "%s must no longer deny anything by itself" % step)
        degraded = self._degraded_gate(bullet, mutated)
        self.assertIn(HOST_SYNC_STEP_DENIAL_BULLETS[neighbour], degraded,
                      "%s must remain present and compliant to be borrowable" % neighbour)
        return degraded

    def test_control_unterminated_inverted_step_2_cannot_borrow_step_4(self):
        degraded = self._inverted_unterminated_step_denial("(step 2)", "(step 4)")
        self.assertIn("substitution_not_denied", host_sync_gate_findings(degraded))

    def test_control_unterminated_inverted_step_4_cannot_borrow_step_5(self):
        degraded = self._inverted_unterminated_step_denial("(step 4)", "(step 5)")
        self.assertIn("substitution_not_denied", host_sync_gate_findings(degraded))

    def test_control_unterminated_inverted_step_5_cannot_borrow_step_7(self):
        degraded = self._inverted_unterminated_step_denial("(step 5)", "(step 7)")
        self.assertIn("substitution_not_denied", host_sync_gate_findings(degraded))

    # -- Neighbour marker variants: the bound must not depend on WHICH bullet marker is used -- #
    # The controls above all leave the neighbour hyphen-marked, so they only prove the " - " break
    # is a bound. CommonMark treats `*` and `+` as equally valid, and a list rewritten with either
    # still reads as a compliant runbook -- so an unterminated inverted bullet must not be able to
    # run across such a break and borrow the untouched denial that follows it.
    HOST_SYNC_ADJACENT_STEP_NEIGHBOURS = (("(step 2)", "(step 4)"),
                                          ("(step 4)", "(step 5)"),
                                          ("(step 5)", "(step 7)"))

    def _unterminated_inversion_across_marker(self, step, neighbour, marker):
        """Invert+unterminate ``step`` and re-mark the adjacent ``neighbour`` with ``marker``.

        The mutated bullet keeps its step token and loses both its own denial and its own
        punctuation, so nothing but a structural bound can stop its clause. ``neighbour`` stays
        verbatim-compliant and immediately adjacent, carrying only a different bullet marker.
        """
        bullet = HOST_SYNC_STEP_DENIAL_BULLETS[step]
        mutated = bullet.replace("does **not** cover", "does cover").replace(" sync;\n", " sync\n")
        self.assertNotEqual(mutated, bullet, "the mutation must change %s" % step)
        self.assertIn(step, mutated, "the mutated bullet must keep its step token")
        for own in (";", "."):
            self.assertNotIn(own, mutated,
                             "%s must lose its own %r clause terminator" % (step, own))
        self.assertNotIn(HOST_SYNC_NON_SUBSTITUTION_PHRASE, mutated,
                         "%s must no longer deny anything by itself" % step)

        original = HOST_SYNC_STEP_DENIAL_BULLETS[neighbour]
        remarked = marker + original[1:]
        self.assertIn(HOST_SYNC_NON_SUBSTITUTION_PHRASE, remarked,
                      "%s must keep its denial verbatim to be borrowable" % neighbour)

        # Replacing the adjacent PAIR in one step makes adjacency an asserted precondition: if the
        # fixture's bullet order ever changed, this control would fail loudly rather than silently
        # stop testing a bullet break.
        pair = bullet + original
        self.assertIn(pair, HOST_SYNC_CANONICAL_FIXTURE,
                      "%s must sit immediately before %s" % (step, neighbour))
        degraded = self._degraded_gate(pair, mutated + remarked)
        self.assertIn(remarked, degraded,
                      "%s must remain present, compliant and %r-marked" % (neighbour, marker))
        return degraded

    def test_control_unterminated_inversion_cannot_borrow_across_any_bullet_marker(self):
        for step, neighbour in self.HOST_SYNC_ADJACENT_STEP_NEIGHBOURS:
            for marker in HOST_SYNC_BULLET_MARKERS:
                with self.subTest(step=step, neighbour=neighbour, marker=marker):
                    degraded = self._unterminated_inversion_across_marker(step, neighbour, marker)
                    self.assertIn(
                        "substitution_not_denied", host_sync_gate_findings(degraded),
                        "%s borrowed %s's denial across a %r-marked bullet break"
                        % (step, neighbour, marker))

    # -- Safety-boundary proposition controls -- #
    def test_control_weakened_safety_own_approval_requirement_is_detected(self):
        degraded = self._degraded_safety(
            "each require their own prior current-turn owner approval",
            "are both covered by the owner's standing approval")
        self.assertIn("safety_boundary_missing", host_sync_gate_findings(degraded))

    def test_control_weakened_safety_prior_turn_non_reuse_is_detected(self):
        degraded = self._degraded_safety(
            "a prior-turn approval is never reusable for either",
            "either may rely on an earlier approval")
        self.assertIn("safety_boundary_missing", host_sync_gate_findings(degraded))

    # -- Mutation / not-read-only disclosure controls -- #
    # The gate must say what the guarded command DOES, or a reader cannot judge the approval.
    def test_control_removed_remote_contact_disclosure_is_detected(self):
        degraded = self._degraded_gate("contacts the remote, and ", "")
        self.assertIn("mutation_disclosure_missing", host_sync_gate_findings(degraded))

    def test_control_removed_checkout_mutation_disclosure_is_detected(self):
        degraded = self._degraded_gate(
            "fast-forwards (mutates)\nthat host's checkout", "runs there")
        self.assertIn("mutation_disclosure_missing", host_sync_gate_findings(degraded))

    def test_control_inverted_read_only_disclosure_is_detected(self):
        degraded = self._degraded_gate("not a read-only check", "a read-only check")
        self.assertIn("mutation_disclosure_missing", host_sync_gate_findings(degraded))

    # ---- DL-XB-123-001: create-UAT VM deployment and no-write preflight approval gates ---- #
    # Exactly ONE live assertion, so a gap in the real runbook fails here once and reports what is
    # missing. Every other test below degrades the in-memory fixture and proves the shared checker
    # emits a specific finding, which is what shows this contract can actually fail.
    def test_create_uat_runbook_satisfies_the_whole_vm_gate_contract(self):
        self.assertEqual(vm_gate_findings(self.create_runbook), [],
                         "the create-UAT runbook must satisfy every DL-XB-123-001 deployment "
                         "and preflight approval requirement")

    def test_canonical_vm_gate_fixture_is_itself_compliant(self):
        # The control group. Without it, a degraded fixture proving "findings appear" would be
        # worthless: the findings might have been there all along.
        self.assertEqual(vm_gate_findings(VM_GATE_CANONICAL_FIXTURE), [],
                         "the canonical fixture must satisfy the contract before it is degraded")

    def test_vm_gate_finding_keys_are_declared_and_every_one_is_reachable(self):
        # An empty document fails every requirement that needs neither an ordering comparison nor
        # a second occurrence of a landmark, so this pins the declared key set as exhaustive. The
        # eleven keys below need a document that actually contains the landmarks, and each has its
        # own control: the two ordering keys, the four gate/boundary ambiguity keys, the four A2
        # numbered-step keys (a duplicate step number and a changed heading both require a heading
        # to exist in the first place), and the A4 safety-boundary ambiguity key, which needs a
        # boundary present twice rather than absent.
        needs_a_real_document = {
            "deploy_gate_after_mutation", "preflight_gate_after_external_action",
            "deploy_gate_marker_ambiguous", "deploy_boundary_ambiguous",
            "preflight_gate_marker_ambiguous", "preflight_boundary_ambiguous",
            "deploy_step_ambiguous", "deploy_heading_changed",
            "preflight_step_ambiguous", "preflight_heading_changed",
            "safety_boundary_ambiguous",
        }
        self.assertLess(needs_a_real_document, set(VM_GATE_FINDING_KEYS),
                        "the ordering, ambiguity and numbered-step keys must all be declared")
        self.assertEqual(set(vm_gate_findings("")),
                         set(VM_GATE_FINDING_KEYS) - needs_a_real_document,
                         "an empty document must report every other declared finding key")

    # -- Fixture degradation helpers: in-memory only, never a repository file -- #
    def _vm_gate_replace_section(self, section, mutated):
        """Swap one whole fixture section for a mutated copy, proving both actually changed."""
        self.assertNotEqual(mutated, section, "the degraded step must actually differ")
        degraded = VM_GATE_CANONICAL_FIXTURE.replace(section, mutated, 1)
        self.assertNotEqual(degraded, VM_GATE_CANONICAL_FIXTURE,
                            "the degraded fixture must actually differ")
        return degraded

    def _degraded_vm_gate(self, number, boundary, old, new):
        """Degrade only a step's GATE prose, leaving the operation it guards untouched."""
        section = _numbered_step_section(VM_GATE_CANONICAL_FIXTURE, number)
        self.assertNotEqual(section, "", "step %d must exist in the fixture" % number)
        split = section.find(boundary)
        self.assertNotEqual(split, -1, "the %r operation boundary must exist" % boundary)
        mutated = section[:split].replace(old, new) + section[split:]
        return self._vm_gate_replace_section(section, mutated)

    def _degraded_deploy_gate(self, old, new):
        return self._degraded_vm_gate(VM_GATE_DEPLOY_STEP, VM_GATE_DEPLOY_OPERATION_OPENING,
                                      old, new)

    def _degraded_preflight_gate(self, old, new):
        return self._degraded_vm_gate(VM_GATE_PREFLIGHT_STEP,
                                      VM_GATE_PREFLIGHT_OPERATION_OPENING, old, new)

    @staticmethod
    def _token_pattern(token):
        """Match a lower-cased contract token however the document happens to spell it.

        Tokens are compared against a flattened, lower-cased view, so the fixture may capitalise
        one differently or wrap it across a line. A control that edited the token literally would
        silently change nothing in some of those spellings and pass for the wrong reason.
        """
        return re.compile(r"\s+".join(re.escape(part) for part in token.split()), re.IGNORECASE)

    def _degraded_vm_gate_token(self, number, boundary, token, new):
        """Remove EVERY occurrence of one contract token before a step's operation boundary.

        All of them, not the first: a phrase repeated in the step heading line and again in the
        gate body would otherwise let an untouched copy satisfy the requirement the control is
        supposed to have taken away.
        """
        section = _numbered_step_section(VM_GATE_CANONICAL_FIXTURE, number)
        split = section.find(boundary)
        self.assertNotEqual(split, -1, "the %r operation boundary must exist" % boundary)
        pattern = self._token_pattern(token)
        self.assertTrue(pattern.search(section[:split]),
                        "the fixture must carry the token %r before its operation" % (token,))
        mutated = pattern.sub(lambda _: new, section[:split]) + section[split:]
        return self._vm_gate_replace_section(section, mutated)

    def _degraded_deploy_gate_token(self, token, new):
        return self._degraded_vm_gate_token(VM_GATE_DEPLOY_STEP,
                                            VM_GATE_DEPLOY_OPERATION_OPENING, token, new)

    def _degraded_preflight_gate_token(self, token, new):
        return self._degraded_vm_gate_token(VM_GATE_PREFLIGHT_STEP,
                                            VM_GATE_PREFLIGHT_OPERATION_OPENING, token, new)

    def _degraded_four_way_safety(self, old, new):
        split = VM_GATE_CANONICAL_FIXTURE.find(VM_GATE_SAFETY_HEADING)
        self.assertNotEqual(split, -1, "the fixture must carry a safety boundary")
        degraded = (VM_GATE_CANONICAL_FIXTURE[:split]
                    + VM_GATE_CANONICAL_FIXTURE[split:].replace(old, new))
        self.assertNotEqual(degraded, VM_GATE_CANONICAL_FIXTURE,
                            "the degraded fixture must actually differ: %r" % (old,))
        return degraded

    def _relocated_gate_after_operation(self, number, gate_opening, operation_opening):
        """Move a step's whole gate block to AFTER the operation it is supposed to precede.

        The prose is not weakened or removed, only moved, so the ordering requirement is the only
        thing that can still detect it.
        """
        section = _numbered_step_section(VM_GATE_CANONICAL_FIXTURE, number)
        gate_at = section.find(gate_opening)
        operation_at = section.find(operation_opening)
        self.assertNotEqual(gate_at, -1, "the fixture must open step %d's gate" % number)
        self.assertNotEqual(operation_at, -1, "step %d's operation must be present" % number)
        self.assertLess(gate_at, operation_at, "the fixture must start out compliant")
        moved = section[:gate_at] + section[operation_at:] + section[gate_at:operation_at]
        return self._vm_gate_replace_section(section, moved)

    # -- Step-4 deployment gate controls -- #
    def test_control_missing_deployment_gate_is_detected(self):
        degraded = self._degraded_deploy_gate("(deployment gate)", "(approval required)")
        self.assertIn("deploy_gate_missing", vm_gate_findings(degraded))

    def test_control_deployment_gate_after_the_mutation_boundary_is_detected(self):
        degraded = self._relocated_gate_after_operation(
            VM_GATE_DEPLOY_STEP, VM_GATE_DEPLOY_OPENING, VM_GATE_DEPLOY_OPERATION_OPENING)
        self.assertIn("deploy_gate_after_mutation", vm_gate_findings(degraded))

    def test_control_missing_deployment_step_is_detected(self):
        section = _numbered_step_section(VM_GATE_CANONICAL_FIXTURE, VM_GATE_DEPLOY_STEP)
        degraded = VM_GATE_CANONICAL_FIXTURE.replace(section, "", 1)
        self.assertNotEqual(degraded, VM_GATE_CANONICAL_FIXTURE)
        self.assertIn("deploy_step_missing", vm_gate_findings(degraded))

    def test_control_removed_deployment_operation_is_detected(self):
        # A gate that guards nothing is not a pass: the mutation boundary this contract anchors
        # its ordering check on has gone, so the ordering check has silently stopped testing.
        section = _numbered_step_section(VM_GATE_CANONICAL_FIXTURE, VM_GATE_DEPLOY_STEP)
        split = section.find(VM_GATE_DEPLOY_OPERATION_OPENING)
        mutated = section[:split]
        self.assertIn("deploy_operation_missing",
                      vm_gate_findings(self._vm_gate_replace_section(section, mutated)))

    def test_control_deployment_vm_name_removed_is_detected(self):
        degraded = self._degraded_deploy_gate_token(VM_GATE_VM, "the AutoCount VM")
        self.assertIn("deploy_vm_not_named", vm_gate_findings(degraded))

    def test_control_each_weakened_deployment_operation_binding_is_detected(self):
        for binding in VM_GATE_DEPLOY_BINDINGS:
            with self.subTest(binding=binding):
                degraded = self._degraded_deploy_gate_token(binding,
                                                            "the reviewed UAT components")
                self.assertIn("deploy_operation_not_bound", vm_gate_findings(degraded))

    def test_control_deployment_missing_current_turn_wording_is_detected(self):
        degraded = self._degraded_deploy_gate_token(VM_GATE_CURRENT_TURN, "owner approval")
        self.assertIn("deploy_not_current_turn", vm_gate_findings(degraded))

    def test_control_each_removed_deployment_non_substitution_statement_is_detected(self):
        for step, bullet in VM_GATE_DEPLOY_DENIAL_BULLETS.items():
            with self.subTest(step=step):
                degraded = self._degraded_deploy_gate(bullet, "")
                self.assertIn("deploy_substitution_not_denied", vm_gate_findings(degraded))

    def test_control_each_inverted_deployment_non_substitution_statement_is_detected(self):
        # Removal only proves the checker notices an absent bullet. These bullets are still
        # present, still carry their step token, and now say the opposite.
        for step, bullet in VM_GATE_DEPLOY_DENIAL_BULLETS.items():
            with self.subTest(step=step):
                inverted = bullet.replace("does **not** authorise", "does authorise")
                self.assertNotEqual(inverted, bullet, "the inversion must change %s" % step)
                self.assertIn(step, inverted, "the inverted bullet must keep its step token")
                degraded = self._degraded_deploy_gate(bullet, inverted)
                self.assertIn("deploy_substitution_not_denied", vm_gate_findings(degraded))

    def test_control_unterminated_inverted_deployment_denial_cannot_borrow_a_neighbour(self):
        # The bypass a proximity window allows: invert one bullet, delete its own `;` in the same
        # stroke, and let the untouched neighbour supply the denial phrase. Each must stand alone.
        for step, neighbour in (("(step 2)", "(step 3)"), ("(step 3)", "(step 5)"),
                                ("(step 5)", "(step 7)")):
            with self.subTest(step=step, neighbour=neighbour):
                bullet = VM_GATE_DEPLOY_DENIAL_BULLETS[step]
                mutated = (bullet.replace("does **not** authorise", "does authorise")
                           .replace(" deployment;\n", " deployment\n"))
                self.assertNotIn(";", mutated, "%s must lose its own clause terminator" % step)
                self.assertNotIn(VM_GATE_DEPLOY_DENIAL, mutated,
                                 "%s must no longer deny anything by itself" % step)
                degraded = self._degraded_deploy_gate(bullet, mutated)
                self.assertIn(VM_GATE_DEPLOY_DENIAL_BULLETS[neighbour], degraded,
                              "%s must remain compliant and adjacent to be borrowable" % neighbour)
                self.assertIn("deploy_substitution_not_denied", vm_gate_findings(degraded))

    def test_control_reusable_prior_turn_deployment_approval_is_detected(self):
        degraded = self._degraded_deploy_gate("A prior-turn approval is not reusable.",
                                              "A prior-turn approval may be reused here.")
        self.assertIn("deploy_prior_turn_not_denied", vm_gate_findings(degraded))

    def test_control_removed_deployment_stop_boundary_is_detected(self):
        degraded = self._degraded_deploy_gate(
            "stop before copying or replacing files or creating or preparing state on\nthe VM.",
            "proceed.")
        self.assertIn("deploy_stop_boundary_missing", vm_gate_findings(degraded))

    def test_control_deployment_stop_boundary_stated_only_after_the_mutation_is_rejected(self):
        # Layer 2 of the structural bound, on its own: the sentence is still in step 4 and still
        # verbatim, but it now sits after the copy instruction, where it can no longer stop it.
        section = _numbered_step_section(VM_GATE_CANONICAL_FIXTURE, VM_GATE_DEPLOY_STEP)
        sentence = ("Without the named current-turn\n"
                    "deployment approval, stop before copying or replacing files or creating or"
                    " preparing state on\nthe VM.\n")
        self.assertIn(sentence, section, "the fixture must carry the stop sentence verbatim")
        split = section.find(VM_GATE_DEPLOY_OPERATION_OPENING)
        mutated = section[:split].replace(sentence, "") + section[split:] + "\n" + sentence
        degraded = self._vm_gate_replace_section(section, mutated)
        self.assertIn(sentence, degraded, "the sentence must be relocated, not deleted")
        self.assertIn("deploy_stop_boundary_missing", vm_gate_findings(degraded))

    def test_control_deployment_approval_extended_to_execution_or_autocount_is_detected(self):
        degraded = self._degraded_deploy_gate(
            "This deployment approval authorises no runner execution,\n"
            "no AutoCount environment configuration and no AutoCount contact.",
            "This deployment approval also authorises running the runner, configuring the"
            " AutoCount environment and contacting AutoCount.")
        self.assertIn("deploy_execution_not_denied", vm_gate_findings(degraded))

    # -- Step-5 preflight gate controls -- #
    def test_control_missing_preflight_gate_is_detected(self):
        degraded = self._degraded_preflight_gate("(preflight gate)", "(approval required)")
        self.assertIn("preflight_gate_missing", vm_gate_findings(degraded))

    def test_control_preflight_gate_after_the_external_boundary_is_detected(self):
        degraded = self._relocated_gate_after_operation(
            VM_GATE_PREFLIGHT_STEP, VM_GATE_PREFLIGHT_OPENING,
            VM_GATE_PREFLIGHT_OPERATION_OPENING)
        self.assertIn("preflight_gate_after_external_action", vm_gate_findings(degraded))

    def test_control_missing_preflight_step_is_detected(self):
        section = _numbered_step_section(VM_GATE_CANONICAL_FIXTURE, VM_GATE_PREFLIGHT_STEP)
        degraded = VM_GATE_CANONICAL_FIXTURE.replace(section, "", 1)
        self.assertNotEqual(degraded, VM_GATE_CANONICAL_FIXTURE)
        self.assertIn("preflight_step_missing", vm_gate_findings(degraded))

    def test_control_removed_preflight_operation_is_detected(self):
        section = _numbered_step_section(VM_GATE_CANONICAL_FIXTURE, VM_GATE_PREFLIGHT_STEP)
        split = section.find(VM_GATE_PREFLIGHT_OPERATION_OPENING)
        mutated = section[:split]
        self.assertIn("preflight_operation_missing",
                      vm_gate_findings(self._vm_gate_replace_section(section, mutated)))

    def test_control_preflight_vm_name_removed_is_detected(self):
        degraded = self._degraded_preflight_gate_token(VM_GATE_VM, "the AutoCount VM")
        self.assertIn("preflight_vm_not_named", vm_gate_findings(degraded))

    def test_control_each_removed_preflight_binding_reports_its_own_finding(self):
        # Target, transfer and dry-run are three separate bindings; losing one must not be
        # concealed by the other two, so each carries its own finding key.
        for key, token in VM_GATE_PREFLIGHT_BINDINGS:
            with self.subTest(binding=key):
                degraded = self._degraded_preflight_gate_token(
                    token, "the usual preflight arrangements")
                self.assertIn(key, vm_gate_findings(degraded))

    def test_control_preflight_missing_current_turn_wording_is_detected(self):
        degraded = self._degraded_preflight_gate_token(VM_GATE_CURRENT_TURN, "owner approval")
        self.assertIn("preflight_not_current_turn", vm_gate_findings(degraded))

    def test_control_each_removed_preflight_non_substitution_statement_is_detected(self):
        for step, bullet in VM_GATE_PREFLIGHT_DENIAL_BULLETS.items():
            with self.subTest(step=step):
                degraded = self._degraded_preflight_gate(bullet, "")
                self.assertIn("preflight_substitution_not_denied", vm_gate_findings(degraded))

    def test_control_each_inverted_preflight_non_substitution_statement_is_detected(self):
        for step, bullet in VM_GATE_PREFLIGHT_DENIAL_BULLETS.items():
            with self.subTest(step=step):
                inverted = bullet.replace("does **not** authorise", "does authorise")
                self.assertNotEqual(inverted, bullet, "the inversion must change %s" % step)
                self.assertIn(step, inverted, "the inverted bullet must keep its step token")
                degraded = self._degraded_preflight_gate(bullet, inverted)
                self.assertIn("preflight_substitution_not_denied", vm_gate_findings(degraded))

    def test_control_unterminated_inverted_preflight_denial_cannot_borrow_a_neighbour(self):
        for step, neighbour in (("(step 2)", "(step 3)"), ("(step 3)", "(step 4)"),
                                ("(step 4)", "(step 7)")):
            with self.subTest(step=step, neighbour=neighbour):
                bullet = VM_GATE_PREFLIGHT_DENIAL_BULLETS[step]
                mutated = (bullet.replace("does **not** authorise", "does authorise")
                           .replace(" preflight;\n", " preflight\n"))
                self.assertNotIn(";", mutated, "%s must lose its own clause terminator" % step)
                self.assertNotIn(VM_GATE_PREFLIGHT_DENIAL, mutated,
                                 "%s must no longer deny anything by itself" % step)
                degraded = self._degraded_preflight_gate(bullet, mutated)
                self.assertIn(VM_GATE_PREFLIGHT_DENIAL_BULLETS[neighbour], degraded,
                              "%s must remain compliant and adjacent to be borrowable" % neighbour)
                self.assertIn("preflight_substitution_not_denied", vm_gate_findings(degraded))

    def test_control_reusable_prior_turn_preflight_approval_is_detected(self):
        degraded = self._degraded_preflight_gate("A prior-turn approval is not reusable.",
                                                 "A prior-turn approval may be reused here.")
        self.assertIn("preflight_prior_turn_not_denied", vm_gate_findings(degraded))

    def test_control_removed_preflight_save_member_non_authorisation_is_detected(self):
        degraded = self._degraded_preflight_gate(
            ", but it does **not** authorise or call `SaveMember`; that write\nremains gated by"
            " step 7", "")
        self.assertIn("preflight_save_member_not_denied", vm_gate_findings(degraded))

    def test_control_inverted_preflight_save_member_non_authorisation_is_detected(self):
        degraded = self._degraded_preflight_gate(
            "it does **not** authorise or call `SaveMember`",
            "it also authorises and may call `SaveMember`")
        self.assertIn("preflight_save_member_not_denied", vm_gate_findings(degraded))

    def test_control_removed_preflight_dry_run_reach_statement_is_detected(self):
        degraded = self._degraded_preflight_gate(
            "The dry-run may authenticate, check the duplicate and\nconstruct the member in"
            " memory, but it", "The dry-run")
        self.assertIn("preflight_save_member_not_denied", vm_gate_findings(degraded))

    def test_control_removed_preflight_stop_boundary_is_detected(self):
        degraded = self._degraded_preflight_gate(
            "stop before reading\nthe private form response or decision row, before building the"
            " package, before setting the\nAutoCount environment, and before transferring the"
            " package to the VM or contacting AutoCount.",
            "proceed.")
        self.assertIn("preflight_stop_boundary_missing", vm_gate_findings(degraded))

    # -- Step-heading isolation: neither gate may be satisfied from the other step -- #
    def test_control_deployment_gate_relocated_into_step_5_is_still_missing_from_step_4(self):
        deploy = _numbered_step_section(VM_GATE_CANONICAL_FIXTURE, VM_GATE_DEPLOY_STEP)
        preflight = _numbered_step_section(VM_GATE_CANONICAL_FIXTURE, VM_GATE_PREFLIGHT_STEP)
        gate_at = deploy.find(VM_GATE_DEPLOY_OPENING)
        operation_at = deploy.find(VM_GATE_DEPLOY_OPERATION_OPENING)
        block = deploy[gate_at:operation_at]
        heading, newline, body = preflight.partition("\n")
        degraded = (VM_GATE_CANONICAL_FIXTURE
                    .replace(deploy, deploy[:gate_at] + deploy[operation_at:], 1)
                    .replace(preflight, heading + newline + "\n" + block + body, 1))
        self.assertNotEqual(degraded, VM_GATE_CANONICAL_FIXTURE)
        self.assertIn(block, degraded, "the gate prose must be relocated, not deleted")
        findings = vm_gate_findings(degraded)
        self.assertIn("deploy_gate_missing", findings,
                      "step 4's gate must not be satisfiable from step 5's section")
        self.assertNotIn("preflight_gate_missing", findings,
                         "step 5's own gate must remain intact and independently satisfied")

    def test_control_preflight_gate_removal_leaves_the_deployment_gate_satisfied(self):
        degraded = self._degraded_preflight_gate("(preflight gate)", "(approval required)")
        findings = vm_gate_findings(degraded)
        self.assertIn("preflight_gate_missing", findings)
        self.assertNotIn("deploy_gate_missing", findings,
                         "a step-5 regression must not be reported against step 4")

    # -- Four-way safety-boundary controls -- #
    def test_control_removed_four_way_safety_statement_is_detected(self):
        degraded = self._degraded_four_way_safety("are four independent approval surfaces",
                                                  "are handled together")
        self.assertIn("safety_boundary_not_four_way", vm_gate_findings(degraded))

    def test_control_weakened_four_way_own_approval_requirement_is_detected(self):
        degraded = self._degraded_four_way_safety(
            "Each\n  requires its own current-turn owner approval",
            "They are covered by the owner's standing approval")
        self.assertIn("safety_boundary_not_four_way", vm_gate_findings(degraded))

    def test_control_weakened_four_way_non_implication_is_detected(self):
        degraded = self._degraded_four_way_safety("none implies or covers another",
                                                  "an earlier one may cover a later one")
        self.assertIn("safety_boundary_not_four_way", vm_gate_findings(degraded))

    def test_control_weakened_four_way_prior_turn_non_reuse_is_detected(self):
        degraded = self._degraded_four_way_safety(
            "a prior-turn\n  approval is never reusable for any of them",
            "any of them may rely on an earlier approval")
        self.assertIn("safety_boundary_not_four_way", vm_gate_findings(degraded))

    def test_control_missing_safety_boundary_heading_is_detected(self):
        degraded = VM_GATE_CANONICAL_FIXTURE.replace(VM_GATE_SAFETY_HEADING, "## Notes", 1)
        self.assertNotEqual(degraded, VM_GATE_CANONICAL_FIXTURE)
        self.assertIn("safety_boundary_not_four_way", vm_gate_findings(degraded))

    def test_four_way_safety_statement_preserves_the_host_sync_independence_rule(self):
        # #123 ADDS a fourth surface; it must not quietly relax the #118 sentence it sits beside.
        # The fixture carries no step-3 gate, so only the safety-boundary finding is meaningful
        # here -- and it must be absent, proving the two safety contracts coexist rather than one
        # rewording the other's proposition away.
        self.assertNotIn("safety_boundary_missing",
                         host_sync_gate_findings(VM_GATE_CANONICAL_FIXTURE),
                         "the four-way statement must not displace the #118 independence rule")

    # ---- DL-XB-123-001-A1 regression controls for accepted G4-002 findings F-1 / F-2 ---- #
    # Every control degrades the in-memory fixture only, never a repository file. Each reproduces
    # a case the pre-A1 checker reported clean, and each names the deterministic finding the
    # repaired checker must emit, so RED localises to exactly what G4 accepted.

    def _a1_step(self, number):
        section = _numbered_step_section(VM_GATE_CANONICAL_FIXTURE, number)
        self.assertNotEqual(section, "", "step %d must exist in the fixture" % number)
        return section

    def _a1_swap(self, section, mutated):
        self.assertNotEqual(mutated, section, "the A1 mutation must change the step")
        degraded = VM_GATE_CANONICAL_FIXTURE.replace(section, mutated, 1)
        self.assertNotEqual(degraded, VM_GATE_CANONICAL_FIXTURE,
                            "the degraded fixture must actually differ")
        return degraded

    def _a1_line_start(self, section, token):
        at = section.find(token)
        self.assertNotEqual(at, -1, "the fixture must carry %r" % (token,))
        return section.rfind("\n", 0, at) + 1

    def _a1_insert_before_gate(self, number, marker, text):
        """Put substantive prose into a step's PRE-GATE region, leaving everything else intact."""
        section = self._a1_step(number)
        at = self._a1_line_start(section, marker)
        return self._a1_swap(section, section[:at] + text + section[at:])

    def _a1_split_at_boundary(self, number, boundary):
        """Return (section, gate-side text, action-region text) split at the action boundary."""
        section = self._a1_step(number)
        at = section.find(boundary)
        self.assertNotEqual(at, -1, "the fixture must carry the %r boundary" % (boundary,))
        return section, section[:at], section[at:]

    # -- A. Step-4 pre-gate region: substantive content before the gate, any vocabulary -- #
    def test_a1_control_step4_transfer_wording_before_the_gate_is_detected(self):
        degraded = self._a1_insert_before_gate(
            VM_GATE_DEPLOY_STEP, VM_GATE_DEPLOY_MARKER,
            "Transfer the reviewed `scripts/ac2_member_create_uat_runner.ps1`,\n"
            "`scripts/member_create_uat_runner_lib.ps1`, and\n"
            "`config/member_create_uat_business_confirmation.json` to the AutoCount VM.\n\n")
        self.assertIn("deploy_pre_gate_content", vm_gate_findings(degraded),
                      "an ungated 'Transfer ...' instruction before the step-4 gate must fail")

    def test_a1_control_step4_place_wording_before_the_gate_is_detected(self):
        degraded = self._a1_insert_before_gate(
            VM_GATE_DEPLOY_STEP, VM_GATE_DEPLOY_MARKER,
            "Place the reviewed runner, helper library and business-confirmation file on the\n"
            "AutoCount VM `DESKTOP-4I042L6` now.\n\n")
        self.assertIn("deploy_pre_gate_content", vm_gate_findings(degraded),
                      "an ungated 'Place ... on the VM' instruction before the gate must fail")

    def test_a1_control_any_substantive_step4_pre_gate_text_is_detected(self):
        # The point of the structural rule: detection cannot depend on guessing the verb, so a
        # future synonym -- or prose with no action verb at all -- is caught just the same.
        for lead in ("Transfer", "Place", "Send", "Move", "Deploy", "Push", "Sync",
                     "As a preparatory note,"):
            with self.subTest(lead=lead):
                degraded = self._a1_insert_before_gate(
                    VM_GATE_DEPLOY_STEP, VM_GATE_DEPLOY_MARKER,
                    "%s the reviewed components to the AutoCount VM working area.\n\n" % lead)
                self.assertIn("deploy_pre_gate_content", vm_gate_findings(degraded),
                              "%r must not evade the step-4 pre-gate rule" % lead)

    def test_a1_step4_pre_gate_whitespace_remains_acceptable(self):
        # The rule is "no SUBSTANTIVE content", not "no change": harmless blank lines must not
        # manufacture a finding, or the guard would fail on ordinary Markdown reflow.
        degraded = self._a1_insert_before_gate(
            VM_GATE_DEPLOY_STEP, VM_GATE_DEPLOY_MARKER, "\n   \n\n")
        self.assertNotIn("deploy_pre_gate_content", vm_gate_findings(degraded),
                         "whitespace-only pre-gate padding must stay acceptable")

    # -- B. Step-5 pre-gate region must be BLANK (A4 retires the frozen prefix) -- #
    # The reviewed-safe prefix existed only because the laptop package build legitimately preceded
    # the gate. Accepted finding PRRT_kwDOSbJI_s6YQTNV establishes that it never did: reading the
    # private form response and decision row and mutating the decision store, ledger and package
    # is gated work, so it now sits AFTER the gate. Nothing legitimate precedes the gate, so the
    # rule becomes step 4's -- blank -- which is strictly stronger than the digest it replaces.
    # Every accepted F-1 attack is re-run below against the new rule, so the protection is
    # carried forward rather than dropped with the mechanism.
    def _a1_insert_into_pre_gate(self, text):
        """Insert prose between the step-5 heading line and its gate line."""
        section = self._a1_step(VM_GATE_PREFLIGHT_STEP)
        at = self._a1_line_start(section, VM_GATE_PREFLIGHT_MARKER)
        return self._a1_swap(section, section[:at] + text + section[at:])

    def test_a1_control_step5_pre_123_transfer_and_start_wording_is_detected(self):
        # The exact regression G4 reproduced: the pre-#123 "build, move it across, run it"
        # opening restored with vocabulary the old marker list did not know.
        degraded = self._a1_insert_into_pre_gate(
            "Build the approved package on the laptop first, transfer it to the VM, then\n"
            "start the runner in preflight mode.\n\n")
        self.assertIn("preflight_pre_gate_content", vm_gate_findings(degraded),
                      "an ungated transfer/start instruction before the step-5 gate must fail")

    def test_a1_control_step5_send_and_start_wording_is_detected(self):
        degraded = self._a1_insert_into_pre_gate(
            "Send the approved package to the VM and start the runner against AutoCount now.\n\n")
        self.assertIn("preflight_pre_gate_content", vm_gate_findings(degraded),
                      "an ungated 'Send ... start the runner' instruction must fail")

    def test_a1_control_step5_executable_preflight_moved_before_the_gate_is_detected(self):
        # Move the REAL executable behaviour ahead of the gate while carefully avoiding every
        # phrase the old marker list recognised. This is the strongest form of the F-1 bypass.
        section = self._a1_step(VM_GATE_PREFLIGHT_STEP)
        moved = ("Move the approved package across to the VM and start the runner:\n\n"
                 "```powershell\n"
                 + VM_GATE_PREFLIGHT_RUNNER_ANCHOR + " <package>\n```\n\n")
        at = self._a1_line_start(section, VM_GATE_PREFLIGHT_MARKER)
        degraded = self._a1_swap(section, section[:at] + moved + section[at:])
        self.assertIn("preflight_pre_gate_content", vm_gate_findings(degraded),
                      "an executable preflight moved before the gate must fail closed")

    def test_a1_control_each_unrecognised_verb_in_the_step5_prefix_is_detected(self):
        for lead in ("transfer", "place", "send", "move", "push", "start"):
            with self.subTest(lead=lead):
                degraded = self._a1_insert_into_pre_gate(
                    "Then %s the package to the AutoCount VM.\n\n" % lead)
                self.assertIn("preflight_pre_gate_content", vm_gate_findings(degraded),
                              "%r must not evade the step-5 pre-gate rule" % lead)

    def test_a1_step5_pre_gate_is_blank_on_both_authorities(self):
        # Replaces the retired digest test in the same role: it proves the rule is not vacuous.
        # Without it, "the pre-gate region is blank" could hold simply because the checker never
        # located a gate, and every degradation above would be proving nothing.
        for label, text in (("runbook", self.create_runbook),
                            ("fixture", VM_GATE_CANONICAL_FIXTURE)):
            with self.subTest(source=label):
                section = _numbered_step_section(text, VM_GATE_PREFLIGHT_STEP)
                at = section.find(VM_GATE_PREFLIGHT_MARKER)
                self.assertNotEqual(at, -1, "%s must carry the step-5 gate marker" % label)
                prefix = section[section.find("\n") + 1:section.rfind("\n", 0, at) + 1]
                self.assertEqual(prefix.strip(), "",
                                 "%s step-5 pre-gate region must be blank" % label)

    # -- C. F-2: no gate proposition may be satisfied from post-gate prose -- #
    def test_a1_control_preflight_gate_cannot_borrow_the_post_gate_vm_banner(self):
        section, gate_side, action = self._a1_split_at_boundary(
            VM_GATE_PREFLIGHT_STEP, VM_GATE_PREFLIGHT_BOUNDARY)
        stripped = gate_side.replace(VM_GATE_VM, "the AutoCount VM")
        self.assertNotIn(VM_GATE_VM, stripped, "every gate-local VM name must be gone")
        self.assertIn(VM_GATE_VM, action, "the post-gate operational banner must survive intact")
        degraded = self._a1_swap(section, stripped + action)
        self.assertIn("preflight_vm_not_named", vm_gate_findings(degraded),
                      "the gate must carry its own VM identity, not borrow the later banner")

    def test_a1_control_no_gate_proposition_is_satisfiable_from_the_action_region(self):
        for sentence in ("A prior-turn approval is not reusable.",
                         "Without the named current-turn preflight approval, stop before reading\n"
                         "the private form response or decision row, before building the package,"
                         " before setting the\nAutoCount environment, and before transferring the"
                         " package to the VM or contacting AutoCount."):
            with self.subTest(sentence=sentence[:40]):
                section, gate_side, action = self._a1_split_at_boundary(
                    VM_GATE_PREFLIGHT_STEP, VM_GATE_PREFLIGHT_BOUNDARY)
                self.assertIn(sentence, gate_side, "the sentence must start inside the gate")
                degraded = self._a1_swap(
                    section, gate_side.replace(sentence, "", 1) + action + "\n" + sentence + "\n")
                self.assertNotEqual(vm_gate_findings(degraded), [],
                                    "relocating %r past the boundary must fail" % sentence[:40])

    # -- D. Real post-gate operation existence -- #
    def _a1_degrade_action(self, number, boundary, old, new):
        """Mutate only a step's ACTION region, leaving its gate untouched."""
        section, gate_side, action = self._a1_split_at_boundary(number, boundary)
        self.assertIn(old, action, "the action region must carry %r" % (old[:48],))
        return self._a1_swap(section, gate_side + action.replace(old, new))

    def test_a1_control_removed_preflight_transfer_anchor_fails_closed(self):
        degraded = self._a1_degrade_action(
            VM_GATE_PREFLIGHT_STEP, VM_GATE_PREFLIGHT_BOUNDARY,
            "Copy the package to the VM, then dry-run:", "Then dry-run:")
        self.assertIn("preflight_operation_missing", vm_gate_findings(degraded),
                      "losing the real transfer instruction must fail closed, never pass")

    def test_a1_control_removed_executable_runner_invocation_fails_closed(self):
        # The anchor is compared against a lower-cased view, so the mutation has to use the
        # document's own spelling; asserting the two agree keeps them from drifting apart.
        verbatim = "& scripts\\ac2_member_create_uat_runner.ps1 -PackagePath"
        self.assertIn(VM_GATE_PREFLIGHT_RUNNER_ANCHOR, _flat(verbatim).lower(),
                      "the verbatim invocation must normalise to the declared anchor")
        degraded = self._a1_degrade_action(
            VM_GATE_PREFLIGHT_STEP, VM_GATE_PREFLIGHT_BOUNDARY, verbatim, "& <the runner>")
        self.assertIn("preflight_runner_invocation_missing", vm_gate_findings(degraded),
                      "the summary sentence must not stand in for the real invocation")

    def test_a1_control_each_removed_deployment_action_file_fails_closed(self):
        for path in VM_GATE_DEPLOY_ACTION_FILES:
            with self.subTest(path=path):
                degraded = self._a1_degrade_action(
                    VM_GATE_DEPLOY_STEP, VM_GATE_DEPLOY_BOUNDARY, path, "the reviewed component")
                self.assertIn("deploy_operation_missing", vm_gate_findings(degraded),
                              "%s must remain a real deployed component" % path)

    def test_a1_control_gate_prose_copies_do_not_satisfy_deployment_anchors(self):
        # Each path is named twice: once in the approval bullets, once in the real instruction.
        # Only the second is the operation, so the first must not answer for it.
        section, gate_side, action = self._a1_split_at_boundary(
            VM_GATE_DEPLOY_STEP, VM_GATE_DEPLOY_BOUNDARY)
        for path in VM_GATE_DEPLOY_ACTION_FILES:
            self.assertIn(path, gate_side, "%s must stay in the approval prose" % path)
        stripped = action
        for path in VM_GATE_DEPLOY_ACTION_FILES:
            stripped = stripped.replace(path, "the reviewed component")
        degraded = self._a1_swap(section, gate_side + stripped)
        self.assertIn("deploy_operation_missing", vm_gate_findings(degraded),
                      "approval prose copies must not satisfy the action-region anchors")

    def test_a1_control_removed_state_directory_preparation_fails_closed(self):
        degraded = self._a1_degrade_action(
            VM_GATE_DEPLOY_STEP, VM_GATE_DEPLOY_BOUNDARY,
            'New-Item -ItemType Directory -Path "C:\\XB\\create_uat\\state"',
            "New-Item -ItemType Directory -Path <somewhere>")
        self.assertIn("deploy_state_preparation_missing", vm_gate_findings(degraded),
                      "losing the state preparation must fail closed")

    # -- E. Gate-marker and action-boundary integrity -- #
    def test_a1_control_duplicate_deployment_gate_marker_fails_closed(self):
        degraded = self._a1_insert_before_gate(
            VM_GATE_DEPLOY_STEP, VM_GATE_DEPLOY_MARKER,
            "The deployment gate is restated below.\n\n")
        self.assertIn("deploy_gate_marker_ambiguous", vm_gate_findings(degraded),
                      "two gate markers must fail closed, not silently pick one")

    def test_a1_control_duplicate_preflight_gate_marker_fails_closed(self):
        section, gate_side, action = self._a1_split_at_boundary(
            VM_GATE_PREFLIGHT_STEP, VM_GATE_PREFLIGHT_BOUNDARY)
        degraded = self._a1_swap(
            section, gate_side + action + "\nSee the preflight gate above.\n")
        self.assertIn("preflight_gate_marker_ambiguous", vm_gate_findings(degraded),
                      "two gate markers must fail closed, not silently pick one")

    def test_a1_control_missing_deployment_boundary_fails_closed(self):
        degraded = self._a1_degrade_action(
            VM_GATE_DEPLOY_STEP, VM_GATE_DEPLOY_BOUNDARY,
            VM_GATE_DEPLOY_BOUNDARY, "Transfer the reviewed")
        self.assertIn("deploy_boundary_missing", vm_gate_findings(degraded),
                      "a reworded action boundary must fail closed")

    def test_a1_control_duplicate_deployment_boundary_fails_closed(self):
        section, gate_side, action = self._a1_split_at_boundary(
            VM_GATE_DEPLOY_STEP, VM_GATE_DEPLOY_BOUNDARY)
        degraded = self._a1_swap(section, gate_side + action + "\n" + VM_GATE_DEPLOY_BOUNDARY
                                 + " components again.\n")
        self.assertIn("deploy_boundary_ambiguous", vm_gate_findings(degraded),
                      "an ambiguous action boundary must fail closed")

    def test_a1_control_missing_preflight_boundary_fails_closed(self):
        degraded = self._a1_degrade_action(
            VM_GATE_PREFLIGHT_STEP, VM_GATE_PREFLIGHT_BOUNDARY,
            VM_GATE_PREFLIGHT_BOUNDARY, "**`AUTOCOUNT VM`**")
        self.assertIn("preflight_boundary_missing", vm_gate_findings(degraded),
                      "a reworded operational banner must fail closed")

    def test_a1_control_duplicate_preflight_boundary_fails_closed(self):
        section, gate_side, action = self._a1_split_at_boundary(
            VM_GATE_PREFLIGHT_STEP, VM_GATE_PREFLIGHT_BOUNDARY)
        degraded = self._a1_swap(section, gate_side + action + "\n"
                                 + VM_GATE_PREFLIGHT_BOUNDARY + " Repeat as needed.\n")
        self.assertIn("preflight_boundary_ambiguous", vm_gate_findings(degraded),
                      "an ambiguous action boundary must fail closed")

    def test_a1_control_deployment_boundary_before_its_gate_fails_closed(self):
        section, gate_side, action = self._a1_split_at_boundary(
            VM_GATE_DEPLOY_STEP, VM_GATE_DEPLOY_BOUNDARY)
        head, _, rest = section.partition("\n")
        degraded = self._a1_swap(section, head + "\n\n" + action + "\n" + rest)
        self.assertIn("deploy_gate_after_mutation", vm_gate_findings(degraded),
                      "an action boundary before its gate must fail closed")

    def test_a1_control_preflight_boundary_before_its_gate_fails_closed(self):
        section, gate_side, action = self._a1_split_at_boundary(
            VM_GATE_PREFLIGHT_STEP, VM_GATE_PREFLIGHT_BOUNDARY)
        head, _, rest = section.partition("\n")
        degraded = self._a1_swap(section, head + "\n\n" + action + "\n" + rest)
        self.assertIn("preflight_gate_after_external_action", vm_gate_findings(degraded),
                      "an action boundary before its gate must fail closed")

    # ---- DL-XB-123-001-A2: numbered-step identity is structural authority ---- #
    # Two accepted final-G4 findings, both demonstrated as false CLEANS rather than as
    # merely-weak checks:
    #
    #   F-3 -- a SECOND `### 4. ` or `### 5. ` section ORPHANS whatever it contains. The section
    #          bound silently takes the FIRST target heading and closes at the next numbered
    #          heading, so an ungated external instruction can sit in the duplicate while the
    #          genuine section stays perfectly compliant and the oracle reports clean.
    #   F-4 -- the numbered heading LINE is excluded from both pre-gate authorities (step 4's
    #          blank-body rule and step 5's frozen prefix digest), so actionable external wording
    #          can ride in the heading itself and still precede the gate.
    #
    # Duplication is ambiguity even when the duplicate prose is harmless: once the target number
    # appears twice, "which section is authoritative" has no answer, and guessing is precisely
    # what made F-3 invisible. Fail closed and make the editor disambiguate.
    def _a2_duplicate_step(self, number, rogue):
        """Append a SECOND same-number section immediately after the genuine one."""
        section = self._a1_step(number)
        self.assertTrue(rogue.startswith("### %d. " % number),
                        "the rogue section must reuse the same step number")
        return self._a1_swap(section, section + rogue)

    def _a2_retitle_step(self, number, heading):
        """Rewrite ONLY a step's numbered heading line, leaving the whole body compliant."""
        section = self._a1_step(number)
        reviewed = section.partition("\n")[0]
        self.assertEqual(reviewed, VM_GATE_REVIEWED_HEADINGS[number],
                         "the fixture must start from the reviewed heading line")
        self.assertNotEqual(heading, reviewed, "the retitle must actually change the heading")
        return self._a1_swap(section, heading + section[len(reviewed):])

    # -- A/B. A duplicate target step carrying a genuinely unsafe external instruction -- #
    def test_a2_control_duplicate_deployment_step_hiding_an_ungated_action_fails_closed(self):
        degraded = self._a2_duplicate_step(
            VM_GATE_DEPLOY_STEP,
            "### 4. Deploy the inactive UAT components (revised)\n\n"
            "Push the reviewed runner onto DESKTOP-4I042L6 immediately, before obtaining\n"
            "approval.\n\n")
        self.assertIn("deploy_step_ambiguous", vm_gate_findings(degraded),
                      "a second Step 4 must never orphan an ungated VM deployment")

    def test_a2_control_duplicate_preflight_step_hiding_an_ungated_action_fails_closed(self):
        degraded = self._a2_duplicate_step(
            VM_GATE_PREFLIGHT_STEP,
            "### 5. No-write preflight (dry-run) (revised)\n\n"
            "Transfer the approved package to DESKTOP-4I042L6 and start the runner now,\n"
            "without any approval.\n\n")
        self.assertIn("preflight_step_ambiguous", vm_gate_findings(degraded),
                      "a second Step 5 must never orphan an ungated transfer or dry-run")

    # -- C. Harmless duplication is still ambiguous authority -- #
    def test_a2_control_harmlessly_duplicated_target_step_still_fails_closed(self):
        for number, prefix, rogue in (
                (VM_GATE_DEPLOY_STEP, "deploy",
                 "### 4. Deploy the inactive UAT components (notes)\n\nNothing to add.\n\n"),
                (VM_GATE_PREFLIGHT_STEP, "preflight",
                 "### 5. No-write preflight (dry-run) (notes)\n\nNothing to add.\n\n")):
            with self.subTest(step=number):
                degraded = self._a2_duplicate_step(number, rogue)
                self.assertIn(prefix + "_step_ambiguous", vm_gate_findings(degraded),
                              "ambiguous numbered-step authority must fail closed even when the "
                              "duplicate itself is harmless")

    # -- D/E. An actionable heading is an ungated instruction ahead of the gate -- #
    def test_a2_control_actionable_deployment_heading_fails_closed(self):
        degraded = self._a2_retitle_step(
            VM_GATE_DEPLOY_STEP, "### 4. Push the runner onto DESKTOP-4I042L6 immediately")
        self.assertIn("deploy_heading_changed", vm_gate_findings(degraded),
                      "the heading must not be usable as an ungated operational instruction")

    def test_a2_control_actionable_preflight_heading_fails_closed(self):
        degraded = self._a2_retitle_step(
            VM_GATE_PREFLIGHT_STEP,
            "### 5. Send the package to DESKTOP-4I042L6 and preflight it")
        self.assertIn("preflight_heading_changed", vm_gate_findings(degraded),
                      "the heading must not be usable as an ungated operational instruction")

    # -- F. Ordinary heading drift. Fail-closed is the intended answer: the reviewed heading is
    # the authority, and a re-titled step comes back through a reviewed amendment. -- #
    def test_a2_control_every_reviewed_heading_drift_fails_closed(self):
        drifts = (
            (VM_GATE_DEPLOY_STEP, "deploy", "case",
             "### 4. deploy the inactive uat components"),
            (VM_GATE_DEPLOY_STEP, "deploy", "punctuation",
             "### 4. Deploy the inactive UAT components."),
            (VM_GATE_DEPLOY_STEP, "deploy", "wording",
             "### 4. Deploy the UAT components"),
            (VM_GATE_PREFLIGHT_STEP, "preflight", "case",
             "### 5. NO-WRITE PREFLIGHT (DRY-RUN)"),
            (VM_GATE_PREFLIGHT_STEP, "preflight", "punctuation",
             "### 5. No write preflight (dry run)"),
            (VM_GATE_PREFLIGHT_STEP, "preflight", "wording",
             "### 5. Preflight the approved package"),
        )
        for number, prefix, kind, heading in drifts:
            with self.subTest(step=number, drift=kind):
                degraded = self._a2_retitle_step(number, heading)
                self.assertIn(prefix + "_heading_changed", vm_gate_findings(degraded),
                              "%s drift in the Step-%d heading must fail closed" % (kind, number))

    # -- G. The exact reviewed headings stay clean, and stay tied to the real runbook -- #
    def test_a2_reviewed_headings_match_the_live_runbook_exactly_once_each(self):
        lines = self.create_runbook.splitlines()
        for number, heading in VM_GATE_REVIEWED_HEADINGS.items():
            with self.subTest(step=number):
                opened = [line for line in lines if line.startswith("### %d. " % number)]
                self.assertEqual(opened, [heading],
                                 "the runbook must open Step %d exactly once, with the reviewed "
                                 "heading line" % number)

    def test_a2_exact_reviewed_headings_remain_clean(self):
        # The control group for every A2 mutation above: unmutated headings must report nothing,
        # otherwise "a finding appeared" would prove nothing about the mutation.
        self.assertEqual(vm_gate_findings(VM_GATE_CANONICAL_FIXTURE), [],
                         "the reviewed headings must leave the fixture compliant")
        for number, heading in VM_GATE_REVIEWED_HEADINGS.items():
            with self.subTest(step=number):
                self.assertEqual(_numbered_step_section(
                    VM_GATE_CANONICAL_FIXTURE, number).partition("\n")[0], heading)

    # ---- DL-XB-123-001-A3: CommonMark numbered-ATX opening authority ---- #
    # Accepted final-G4-A2 finding, demonstrated as a false CLEAN rather than a merely-weak check:
    # numbered-step discovery recognised only the column-0, single-space spelling, so a duplicate
    # Step 4 or Step 5 written in any other CommonMark-valid form was INVISIBLE to enumeration.
    # An invisible duplicate is worse than a mis-parsed one: its body is absorbed into the
    # neighbouring section's action region, where only presence checks run, so an ungated
    # deployment or transfer instruction rides along while the oracle returns no findings at all.
    #
    # Every control below runs against BOTH authorities -- the in-memory fixture and the live
    # create-UAT runbook -- because a bypass that only the miniature fixture exhibits would not
    # prove anything about the document the operator actually follows.
    def _a3_bases(self):
        return (("canonical fixture", VM_GATE_CANONICAL_FIXTURE),
                ("live create-UAT runbook", self.create_runbook))

    def _a3_span(self, text, number):
        """Half-open offsets of the genuine numbered section inside its own document."""
        section = _numbered_step_section(text, number)
        self.assertNotEqual(section, "", "step %d must exist in the base document" % number)
        at = text.find(section)
        self.assertNotEqual(at, -1, "step %d's section must locate in its own document" % number)
        return at, at + len(section)

    def _a3_assert_carries_no_landmark(self, rogue):
        """The rogue must not carry a gate marker or an action boundary.

        Those landmarks have findings of their own. A duplicate that smuggled one in could make a
        control pass through ``*_gate_marker_ambiguous`` or ``*_boundary_ambiguous`` while the
        opening-enumeration defect this contract is about stayed wide open.
        """
        prose = _flat(rogue).lower()
        for landmark in (VM_GATE_DEPLOY_MARKER, VM_GATE_PREFLIGHT_MARKER,
                         VM_GATE_DEPLOY_BOUNDARY, VM_GATE_PREFLIGHT_BOUNDARY):
            self.assertNotIn(_flat(landmark).lower(), prose,
                             "an A3 duplicate must not borrow the %r landmark" % (landmark,))

    def _a3_duplicate(self, text, number, template, body, placement):
        """Insert a SECOND same-number section, spelled with a CommonMark-valid opening."""
        rogue = "%s\n\n%s\n" % (template % (number, VM_GATE_A3_DUPLICATE_TITLES[number]), body)
        self._a3_assert_carries_no_landmark(rogue)
        start, end = self._a3_span(text, number)
        at = end if placement == VM_GATE_A3_AFTER else start
        degraded = text[:at] + rogue + text[at:]
        self.assertNotEqual(degraded, text, "the A3 duplicate must actually change the document")
        return degraded

    def _a3_heading_span(self, text, number):
        """Offsets and text of the genuine heading LINE, proven to be the reviewed one."""
        start, _ = self._a3_span(text, number)
        line_end = text.find("\n", start)
        self.assertNotEqual(line_end, -1, "the heading line must terminate")
        reviewed = text[start:line_end]
        self.assertEqual(reviewed, VM_GATE_REVIEWED_HEADINGS[number],
                         "the base must start from the reviewed heading line")
        return start, line_end, reviewed

    def _a3_reheaded_line(self, text, number, line):
        """Replace the SINGLE genuine heading line, leaving the whole body untouched."""
        start, line_end, reviewed = self._a3_heading_span(text, number)
        self.assertNotEqual(line, reviewed, "the respelling must actually change the line")
        return text[:start] + line + text[line_end:]

    def _a3_reheaded(self, text, number, template):
        """Respell the genuine heading, preserving its reviewed TITLE exactly."""
        _, _, reviewed = self._a3_heading_span(text, number)
        title = reviewed[len("### %d. " % number):]
        return self._a3_reheaded_line(text, number, template % (number, title))

    # -- A. Every CommonMark-valid duplicate family, both steps, both placements, unsafe and
    # harmless. This is the accepted finding itself: each of these reported NO findings before
    # the repair, including the ones carrying an explicit ungated external action. -- #
    def test_a3_control_every_commonmark_duplicate_family_fails_closed(self):
        for base_name, base in self._a3_bases():
            for number, prefix in ((VM_GATE_DEPLOY_STEP, "deploy"),
                                   (VM_GATE_PREFLIGHT_STEP, "preflight")):
                for variant, template in VM_GATE_A3_DUPLICATE_HEADINGS:
                    for safety, body in (("unsafe", VM_GATE_A3_UNSAFE_BODIES[number]),
                                         ("harmless", VM_GATE_A3_HARMLESS_BODY)):
                        for placement in VM_GATE_A3_PLACEMENTS:
                            with self.subTest(base=base_name, step=number, variant=variant,
                                              safety=safety, placement=placement):
                                degraded = self._a3_duplicate(base, number, template, body,
                                                              placement)
                                self.assertIn(
                                    prefix + "_step_ambiguous", vm_gate_findings(degraded),
                                    "a CommonMark-valid second Step %d must never orphan an "
                                    "instruction" % number)

    # -- B. The strict form must keep failing closed. A widened grammar that lost the spelling it
    # already recognised would trade one bypass for another. -- #
    def test_a3_strict_duplicate_family_still_fails_closed(self):
        for base_name, base in self._a3_bases():
            for number, prefix in ((VM_GATE_DEPLOY_STEP, "deploy"),
                                   (VM_GATE_PREFLIGHT_STEP, "preflight")):
                for placement in VM_GATE_A3_PLACEMENTS:
                    with self.subTest(base=base_name, step=number, placement=placement):
                        degraded = self._a3_duplicate(base, number, VM_GATE_A3_STRICT_HEADING,
                                                      VM_GATE_A3_UNSAFE_BODIES[number], placement)
                        self.assertIn(prefix + "_step_ambiguous", vm_gate_findings(degraded),
                                      "the strict duplicate family must stay closed")

    # -- C. A mixture of spellings is still one ambiguous step, not a majority vote. -- #
    def test_a3_mixed_strict_and_whitespace_openings_are_ambiguous(self):
        for base_name, base in self._a3_bases():
            for number, prefix in ((VM_GATE_DEPLOY_STEP, "deploy"),
                                   (VM_GATE_PREFLIGHT_STEP, "preflight")):
                with self.subTest(base=base_name, step=number):
                    once = self._a3_duplicate(base, number, VM_GATE_A3_STRICT_HEADING,
                                              VM_GATE_A3_HARMLESS_BODY, VM_GATE_A3_AFTER)
                    twice = self._a3_duplicate(once, number, "   ### %d. %s",
                                               VM_GATE_A3_UNSAFE_BODIES[number],
                                               VM_GATE_A3_BEFORE)
                    self.assertIn(prefix + "_step_ambiguous", vm_gate_findings(twice),
                                  "three openings in two spellings must fail closed")

    # -- D. Four leading spaces is an indented code block. Code indentation must NOT be promoted
    # into top-level heading authority, or ordinary sample Markdown would break the contract. -- #
    def test_a3_four_leading_space_form_is_not_top_level_heading_authority(self):
        for base_name, base in self._a3_bases():
            for number in (VM_GATE_DEPLOY_STEP, VM_GATE_PREFLIGHT_STEP):
                for placement in VM_GATE_A3_PLACEMENTS:
                    with self.subTest(base=base_name, step=number, placement=placement):
                        degraded = self._a3_duplicate(base, number,
                                                      VM_GATE_A3_CODE_BLOCK_HEADING,
                                                      VM_GATE_A3_HARMLESS_BODY, placement)
                        self.assertEqual(vm_gate_findings(degraded), [],
                                         "a four-space indented code line must not open a "
                                         "top-level numbered step")

    # -- E. One genuine heading, respelled. Semantically identical means clean. -- #
    def test_a3_whitespace_only_respelling_of_the_genuine_heading_stays_clean(self):
        for base_name, base in self._a3_bases():
            for number in (VM_GATE_DEPLOY_STEP, VM_GATE_PREFLIGHT_STEP):
                for variant, template in VM_GATE_A3_SEMANTIC_HEADING_VARIANTS:
                    with self.subTest(base=base_name, step=number, variant=variant):
                        respelled = self._a3_reheaded(base, number, template)
                        self.assertEqual(vm_gate_findings(respelled), [],
                                         "a whitespace-only respelling of the reviewed Step-%d "
                                         "heading is not drift" % number)

    # -- F. Substantive drift still fails closed, whatever spelling it wears. -- #
    def test_a3_substantive_heading_drift_still_fails_closed(self):
        for base_name, base in self._a3_bases():
            for number, prefix in ((VM_GATE_DEPLOY_STEP, "deploy"),
                                   (VM_GATE_PREFLIGHT_STEP, "preflight")):
                for kind, template in VM_GATE_A3_DRIFT_HEADINGS[number]:
                    with self.subTest(base=base_name, step=number, drift=kind):
                        drifted = self._a3_reheaded_line(base, number, template % number)
                        self.assertIn(prefix + "_heading_changed", vm_gate_findings(drifted),
                                      "%s drift in the Step-%d heading must fail closed"
                                      % (kind, number))

    # -- G. The control group: the reviewed headings, untouched, stay clean on both authorities.
    # Without this every "a finding appeared" above would prove nothing. -- #
    def test_a3_exact_reviewed_headings_remain_clean_on_both_authorities(self):
        for base_name, base in self._a3_bases():
            with self.subTest(base=base_name):
                self.assertEqual(vm_gate_findings(base), [],
                                 "the undegraded base must satisfy the whole contract")

    # ---- DL-XB-123-001-A4: post-ready Codex review remediation controls ---- #
    # Every control below is exercised against BOTH authorities: the A4 target fixture and the
    # live create-UAT runbook. At this commit neither satisfies the A4 contract -- the runbook is
    # still the head-23ddf88 shape and the checker still carries the six accepted defects -- so
    # these are the intentional RED. Commit J moves the Step-5 gate, retires the frozen prefix
    # and repairs the checker, and every control here turns GREEN without being rewritten.
    def _a4_bases(self):
        return (("A4 target fixture", VM_GATE_A4_FIXTURE),
                ("live create-UAT runbook", self.create_runbook))

    def _a4_section(self, base, number):
        section = _numbered_step_section(base, number)
        self.assertNotEqual(section, "", "step %d must exist in the base document" % number)
        return section

    def _a4_swap(self, base, section, mutated):
        self.assertNotEqual(mutated, section, "the degraded step must actually differ")
        degraded = base.replace(section, mutated, 1)
        self.assertNotEqual(degraded, base, "the degraded document must actually differ")
        return degraded

    def _a4_gate_line(self, section, marker):
        """Start of the line the gate opens on, so an insertion lands strictly before it."""
        at = section.find(marker)
        self.assertNotEqual(at, -1, "the %r gate marker must exist" % (marker,))
        return section.rfind("\n", 0, at) + 1

    def _a4_insert_before_gate(self, base, number, marker, text):
        section = self._a4_section(base, number)
        at = self._a4_gate_line(section, marker)
        return self._a4_swap(base, section, section[:at] + text + section[at:])

    def _a4_move_before_gate(self, base, snippet):
        """Move one real Step-5 operation from the action region to ahead of the gate.

        The snippet must START in the post-gate region: an operation that is already ungated is
        the defect itself, not a control, so the assertion below is deliberately load-bearing.
        """
        section = self._a4_section(base, VM_GATE_PREFLIGHT_STEP)
        at = self._a4_gate_line(section, VM_GATE_PREFLIGHT_MARKER)
        self.assertIn(snippet, section[at:],
                      "the Step-5 action region must carry %r" % (snippet[:56],))
        body = section[at:].replace(snippet, "", 1)
        return self._a4_swap(base, section, section[:at] + snippet + "\n\n" + body)

    # -- A. The Step-5 gate must precede every private-data, package, environment and external
    # action. Accepted findings PRRT_kwDOSbJI_s6YQTNV and PRRT_kwDOSbJI_s6YQTMq. -- #
    def test_a4_control_any_step5_pre_gate_content_fails_closed(self):
        # The same structural rule Step 4 already carries, and the reason the frozen prefix can
        # be retired: once nothing legitimate precedes the gate, "blank" is the whole contract
        # and no vocabulary, digest or case-folding question arises at all.
        for base_name, base in self._a4_bases():
            for lead in ("Build", "Approve", "Read the chosen form response and", "Transfer",
                         "As a preparatory note,"):
                with self.subTest(base=base_name, lead=lead):
                    degraded = self._a4_insert_before_gate(
                        base, VM_GATE_PREFLIGHT_STEP, VM_GATE_PREFLIGHT_MARKER,
                        "%s the approved package for the chosen row now.\n\n" % lead)
                    self.assertIn("preflight_pre_gate_content", vm_gate_findings(degraded),
                                  "%r before the Step-5 gate must fail closed" % lead)

    def test_a4_control_each_step5_operation_moved_before_the_gate_fails_closed(self):
        operations = (
            ("package approval", VM_GATE_A4_APPROVE_COMMAND),
            ("package build", VM_GATE_A4_BUILD_COMMAND),
            ("environment setup", VM_GATE_A4_ENV_ANCHOR),
            ("package transfer", "Copy the package to the VM"),
            ("dry-run runner", r"& scripts\ac2_member_create_uat_runner.ps1 -PackagePath"),
        )
        for base_name, base in self._a4_bases():
            for label, snippet in operations:
                with self.subTest(base=base_name, operation=label):
                    degraded = self._a4_move_before_gate(base, snippet)
                    self.assertIn("preflight_pre_gate_content", vm_gate_findings(degraded),
                                  "an ungated %s must fail closed" % label)

    def test_a4_step5_pre_gate_whitespace_remains_acceptable(self):
        # "No SUBSTANTIVE content", not "no change", exactly as Step 4 already reads. Without
        # this the guard would fire on ordinary Markdown reflow and invite being switched off.
        for base_name, base in self._a4_bases():
            with self.subTest(base=base_name):
                degraded = self._a4_insert_before_gate(
                    base, VM_GATE_PREFLIGHT_STEP, VM_GATE_PREFLIGHT_MARKER, "\n   \n\n")
                self.assertNotIn("preflight_pre_gate_content", vm_gate_findings(degraded),
                                 "whitespace-only Step-5 pre-gate padding must stay acceptable")

    # -- B. Approval polarity must be affirmative. Accepted finding PRRT_kwDOSbJI_s6YQTNO. -- #
    def _a4_invert_polarity(self, base, number, boundary, replacement):
        section = self._a4_section(base, number)
        split = section.find(boundary)
        self.assertNotEqual(split, -1, "the %r action boundary must exist" % (boundary,))
        gate = section[:split]
        # Matched wrap-tolerantly: the reviewed clause spans a Markdown line break, and it breaks
        # in a different place in each gate, so a literal search would silently test nothing.
        pattern = re.compile(r"\s+".join(re.escape(word)
                                         for word in VM_GATE_A4_AFFIRMED_CLAUSE.split()))
        inverted, count = pattern.subn(replacement, gate, 1)
        self.assertEqual(count, 1,
                         "the step-%d gate must state the reviewed affirmative clause" % number)
        return self._a4_swap(base, section, inverted + section[split:])

    def test_a4_control_negated_deployment_approval_fails_closed(self):
        for base_name, base in self._a4_bases():
            for kind, replacement in VM_GATE_A4_POLARITY_INVERSIONS:
                with self.subTest(base=base_name, inversion=kind):
                    degraded = self._a4_invert_polarity(
                        base, VM_GATE_DEPLOY_STEP, VM_GATE_DEPLOY_BOUNDARY, replacement)
                    self.assertIn("deploy_not_current_turn", vm_gate_findings(degraded),
                                  "a negated step-4 approval (%s) must fail closed" % kind)

    def test_a4_control_negated_preflight_approval_fails_closed(self):
        for base_name, base in self._a4_bases():
            for kind, replacement in VM_GATE_A4_POLARITY_INVERSIONS:
                with self.subTest(base=base_name, inversion=kind):
                    degraded = self._a4_invert_polarity(
                        base, VM_GATE_PREFLIGHT_STEP, VM_GATE_A4_PREFLIGHT_BOUNDARY, replacement)
                    self.assertIn("preflight_not_current_turn", vm_gate_findings(degraded),
                                  "a negated step-5 approval (%s) must fail closed" % kind)

    # -- C. Safety-boundary authority must be unique. Accepted finding PRRT_kwDOSbJI_s6YQTMw. -- #
    def test_a4_control_missing_safety_boundary_fails_closed(self):
        for base_name, base in self._a4_bases():
            with self.subTest(base=base_name):
                at = base.find(VM_GATE_SAFETY_HEADING)
                self.assertNotEqual(at, -1, "the base must carry a Safety boundary")
                self.assertIn("safety_boundary_not_four_way", vm_gate_findings(base[:at]),
                              "a document with no Safety boundary must fail closed")

    def _a4_duplicate_safety_boundary(self, base, second):
        at = base.find(VM_GATE_SAFETY_HEADING)
        self.assertNotEqual(at, -1, "the base must carry a Safety boundary")
        end = base.find("\n## ", at + 1)
        genuine = base[at:] if end == -1 else base[at:end]
        return base[:at] + genuine.rstrip("\n") + "\n\n" + second + "\n"

    def test_a4_control_duplicate_identical_safety_boundary_fails_closed(self):
        # A harmless duplicate fails closed for the same reason a duplicate numbered step does:
        # once the authority appears twice there is no answer to which one governs, and guessing
        # is precisely the defect. Reading only the first occurrence is what Codex reproduced.
        for base_name, base in self._a4_bases():
            with self.subTest(base=base_name):
                at = base.find(VM_GATE_SAFETY_HEADING)
                end = base.find("\n## ", at + 1)
                genuine = (base[at:] if end == -1 else base[at:end]).rstrip("\n")
                degraded = self._a4_duplicate_safety_boundary(base, genuine)
                self.assertIn("safety_boundary_ambiguous", vm_gate_findings(degraded),
                              "a duplicated Safety boundary must fail closed")

    def test_a4_control_duplicate_contradictory_safety_boundary_fails_closed(self):
        # The dangerous form: the second boundary weakens the four independent surfaces, and the
        # first one keeps the checker clean.
        contradictory = (
            "## Safety boundary\n\n"
            "- The step-4 VM deployment approval also covers the step-5 package transfer and\n"
            "  no-write preflight, and a prior-turn approval may be reused for either.\n")
        for base_name, base in self._a4_bases():
            with self.subTest(base=base_name):
                degraded = self._a4_duplicate_safety_boundary(base, contradictory)
                self.assertIn("safety_boundary_ambiguous", vm_gate_findings(degraded),
                              "a contradictory second Safety boundary must fail closed")

    # -- D. Required operations must be ACTIVE executable commands, with the case sensitivity of
    # the tool that runs them. Accepted findings PRRT_kwDOSbJI_s6YQTM_ and PRRT_kwDOSbJI_s6YQTM4.
    def _a4_comment_out(self, base, number, command):
        section = self._a4_section(base, number)
        self.assertIn(command, section, "step %d must carry %r" % (number, command[:56]))
        return self._a4_swap(base, section, section.replace(command, "# " + command, 1))

    def test_a4_control_commented_out_commands_fail_closed(self):
        commands = (
            (VM_GATE_PREFLIGHT_STEP, "package approval", VM_GATE_A4_APPROVE_COMMAND,
             "preflight_approval_command_missing"),
            (VM_GATE_PREFLIGHT_STEP, "package build", VM_GATE_A4_BUILD_COMMAND,
             "preflight_package_build_missing"),
            (VM_GATE_PREFLIGHT_STEP, "dry-run runner",
             r"& scripts\ac2_member_create_uat_runner.ps1 -PackagePath",
             "preflight_runner_invocation_missing"),
            (VM_GATE_DEPLOY_STEP, "state preparation",
             'New-Item -ItemType Directory -Path "C:\\XB\\create_uat\\state"',
             "deploy_state_preparation_missing"),
        )
        for base_name, base in self._a4_bases():
            for number, label, command, key in commands:
                with self.subTest(base=base_name, command=label):
                    degraded = self._a4_comment_out(base, number, command)
                    self.assertIn(key, vm_gate_findings(degraded),
                                  "a commented-out %s must fail closed" % label)

    def test_a4_control_case_changed_python_cli_flag_fails_closed(self):
        # `member_create_uat_approval.py` is argparse: `--INPUT` is simply not `--input`, so a
        # region lowercased before comparison cannot see the break. The PowerShell runner is
        # deliberately excluded -- its parameter names really are case-insensitive, and pretending
        # otherwise would be a false contract rather than a stronger one.
        mutations = ((VM_GATE_A4_APPROVE_COMMAND, "preflight_approval_command_missing"),
                     (VM_GATE_A4_BUILD_COMMAND, "preflight_package_build_missing"))
        for base_name, base in self._a4_bases():
            for command, key in mutations:
                with self.subTest(base=base_name, command=command[:64]):
                    section = self._a4_section(base, VM_GATE_PREFLIGHT_STEP)
                    self.assertIn(command, section, "step 5 must carry %r" % (command[:56],))
                    mutated = command.replace("--input", "--INPUT", 1)
                    self.assertNotEqual(mutated, command, "the flag must actually change case")
                    degraded = self._a4_swap(base, section,
                                             section.replace(command, mutated, 1))
                    self.assertIn(key, vm_gate_findings(degraded),
                                  "`--input` -> `--INPUT` must fail closed")

    def test_a4_active_commands_remain_clean(self):
        # The control group for section D. Without it, "a finding appeared" above would not
        # distinguish an active-command rule from a rule that rejects the real document too.
        for base_name, base in self._a4_bases():
            with self.subTest(base=base_name):
                self.assertEqual(vm_gate_findings(base), [],
                                 "the undegraded base must satisfy the whole A4 contract")

    # -- E. The AutoCount environment configuration is bound by the gate and performed after it,
    # by variable NAME only. Accepted finding PRRT_kwDOSbJI_s6YQTMq. -- #
    def test_a4_control_each_removed_environment_variable_name_fails_closed(self):
        for base_name, base in self._a4_bases():
            for name in VM_GATE_A4_ENV_NAMES:
                with self.subTest(base=base_name, variable=name):
                    section = self._a4_section(base, VM_GATE_PREFLIGHT_STEP)
                    self.assertIn(name, section,
                                  "step 5 must name the %s connection variable" % name)
                    degraded = self._a4_swap(base, section, section.replace(name, "REDACTED"))
                    findings = vm_gate_findings(degraded)
                    self.assertIn("preflight_environment_not_bound", findings,
                                  "the Step-5 approval must bind %s by name" % name)
                    self.assertIn("preflight_environment_setup_missing", findings,
                                  "the Step-5 environment setup must name %s" % name)

    def test_a4_control_removed_environment_setup_fails_closed(self):
        for base_name, base in self._a4_bases():
            with self.subTest(base=base_name):
                section = self._a4_section(base, VM_GATE_PREFLIGHT_STEP)
                self.assertIn(VM_GATE_A4_ENV_ANCHOR, section,
                              "step 5 must carry the environment-setup instruction")
                degraded = self._a4_swap(base, section,
                                         section.replace(VM_GATE_A4_ENV_ANCHOR, "Note only:", 1))
                self.assertIn("preflight_environment_setup_missing", vm_gate_findings(degraded),
                              "a deleted environment-setup instruction must fail closed")

    # -- F. The three propositions A4 adds to the Step-5 approval, each with its own finding. -- #
    def test_a4_control_each_missing_step5_binding_fails_closed(self):
        for base_name, base in self._a4_bases():
            for key, phrase, fragment in VM_GATE_A4_NEW_BINDINGS:
                with self.subTest(base=base_name, binding=key):
                    section = self._a4_section(base, VM_GATE_PREFLIGHT_STEP)
                    self.assertIn(_flat(phrase), _flat(section),
                                  "the step-5 gate must bind %r" % (phrase[:56],))
                    self.assertIn(fragment, section,
                                  "the degradation fragment %r must be one line"
                                  % (fragment[:56],))
                    degraded = self._a4_swap(
                        base, section, section.replace(fragment, "other matters", 1))
                    self.assertIn(key, vm_gate_findings(degraded),
                                  "a lost %s binding must fail closed" % key)

    def test_readme_references_probe_and_runbook(self):
        self.assertIn("scripts/ac2_member_expiry_capability_probe.ps1", self.readme)
        self.assertIn("member_expiry_capability_probe_runbook.md", self.readme)

    # ---- README probe contract (Design Lock amendment DL-XB-115-001-A1) ---- #
    def _probe_bullet(self):
        bullets = readme_probe_bullets(self.readme)
        self.assertEqual(len(bullets), 1, "exactly one README bullet must describe this probe")
        return bullets[0]

    def test_readme_probe_contract_has_no_removed_path_parameter(self):
        # The public contract must not advertise an operator-selected claim/result root or a
        # secondary output path: both parameters were removed from the probe.
        bullet = self._probe_bullet()
        for removed in ("-StateDirectory", "StateDirectory", "-JsonOut", "JsonOut"):
            self.assertNotIn(removed, bullet, removed)

    def test_readme_probe_contract_names_the_fixed_canonical_root(self):
        self.assertIn(CANONICAL_STATE_ROOT, self._probe_bullet())

    def test_readme_probe_contract_states_root_is_not_operator_selectable(self):
        bullet = self._probe_bullet()
        self.assertRegex(bullet, r"(?i)not operator-selectable")
        self.assertRegex(bullet, r"(?i)fixed in reviewed code")

    def test_readme_probe_contract_states_the_root_must_pre_exist_and_is_never_managed(self):
        bullet = self._probe_bullet()
        self.assertRegex(bullet, r"(?i)must already exist")
        self.assertRegex(bullet, r"(?i)never creates, repairs, redirects, migrates or cleans it")

    def test_readme_probe_contract_states_validation_precedes_autocount_contact(self):
        self.assertRegex(self._probe_bullet(), r"(?i)validated before any AutoCount contact")

    def test_readme_probe_contract_preserves_the_permanent_claim_boundary(self):
        bullet = self._probe_bullet()
        self.assertRegex(bullet, r"(?i)permanent single-use attempt claim")
        self.assertRegex(bullet, r"(?i)never overwritten or deleted")
        self.assertRegex(bullet, r"(?i)fail closed")
        self.assertRegex(bullet, r"(?i)exactly one synthetic member")
        self.assertRegex(bullet, r"(?i)never updates, deletes, rolls back, or cleans up")
        self.assertRegex(bullet, r"(?i)never retried")
        self.assertRegex(bullet, r"(?i)owner approval")

    def test_readme_probe_contract_states_path_bound_publication(self):
        bullet = self._probe_bullet()
        self.assertRegex(bullet, r"(?i)no-clobber")
        self.assertRegex(bullet, r"(?i)staging")
        self.assertRegex(bullet, r"(?i)non-authoritative")
        self.assertRegex(bullet, r"(?i)no-replace")
        # A failed publication is nonzero, and only an authoritative verified result exits 0.
        self.assertRegex(bullet, r"(?i)nonzero")
        self.assertRegex(bullet, r"exit `0` only for an authoritative `EXPIRY_VERIFIED`")

    def test_readme_remains_in_the_mechanical_dependency_inventory(self):
        self.assertIn("README.md", registered_dependencies())

    def test_readme_remains_covered_by_every_workflow_path_filter(self):
        filters = workflow_path_filters(self.workflow)
        self.assertTrue(filters, "the focused workflow must declare at least one path filter")
        for event, patterns in filters.items():
            self.assertEqual(uncovered_dependencies({"README.md"}, patterns), [],
                             "event '%s' does not trigger for README.md" % event)

    def test_workflow_triggers_on_validated_documents(self):
        paths_block = self.workflow.split("paths:", 1)[1].split("workflow_dispatch", 1)[0]
        for needed in ("README.md",
                       "docs/autocount2-automation/member_expiry_capability_probe_runbook.md",
                       "docs/autocount2-automation/member_create_uat_runbook.md",
                       "scripts/member_expiry_capability_probe_lib.ps1",
                       "tests/test_ac2_member_expiry_capability_probe.py",
                       ".gitignore"):
            self.assertIn(needed, paths_block, needed)

    # ---- Mechanical focused-CI dependency closure ---- #
    def test_dependency_inventory_covers_every_known_contract_file(self):
        inventory = registered_dependencies()
        for required in (".gitignore",
                         ".github/workflows/member-create-uat-tests.yml",
                         "tests/test_ac2_member_expiry_capability_probe.py",
                         "scripts/ac2_member_expiry_capability_probe.ps1",
                         "scripts/member_expiry_capability_probe_lib.ps1",
                         "docs/autocount2-automation/member_expiry_capability_probe_runbook.md",
                         "docs/autocount2-automation/member_create_uat_runbook.md",
                         "README.md"):
            self.assertIn(required, inventory, required)

    def test_focused_workflow_filter_closes_over_every_dependency(self):
        filters = workflow_path_filters(self.workflow)
        self.assertTrue(filters, "the focused workflow must declare at least one path filter")
        # paths-ignore would invert the semantics this closure relies on.
        self.assertNotIn("paths-ignore", self.workflow)
        inventory = registered_dependencies()
        for event, patterns in filters.items():
            missing = uncovered_dependencies(inventory, patterns)
            self.assertEqual(missing, [],
                             "event '%s' does not trigger for: %s" % (event, missing))

    # ---- DL-XB-121-001: delegated lookup trigger closure ---- #
    def test_workflow_triggers_on_every_delegated_lookup_dependency(self):
        # The focused jobs import and execute these modules, and the Windows job's full-suite
        # entrypoint is itself one of them, so a change confined to any of them must trigger
        # this workflow. Exact literal membership, not glob coverage.
        filters = workflow_path_filters(self.workflow)
        self.assertTrue(filters, "the focused workflow must declare at least one path filter")
        for event, patterns in filters.items():
            missing = missing_trigger_paths(DELEGATED_LOOKUP_WORKFLOW_TRIGGER_PATHS, patterns)
            self.assertEqual(missing, [],
                             "event '%s' has no exact trigger entry for: %s" % (event, missing))
        # A broad wildcard must not be substituted for the exact entries, and paths-ignore
        # would invert the trigger semantics this closure relies on.
        for event, patterns in filters.items():
            broad = sorted(set(patterns) & BROAD_TRIGGER_WILDCARDS)
            self.assertEqual(broad, [],
                             "event '%s' declares broad wildcard patterns: %s" % (event, broad))
            crossing = sorted(pattern for pattern in patterns if "**" in pattern)
            self.assertEqual(crossing, [],
                             "event '%s' declares separator-crossing patterns: %s"
                             % (event, crossing))
        self.assertNotIn("paths-ignore", self.workflow)

    # ---- A2-3: the closed contract must be fail-closed, not best-effort ---- #
    def test_module_performs_no_repository_read_outside_the_closed_registry(self):
        violations = repository_read_violations(read_repo_text("focused_tests"))
        self.assertEqual(violations, [],
                         "every repository read must resolve through repo_path/read_repo_text")

    def test_ast_guard_rejects_every_escaping_repository_read_form(self):
        header = CANONICAL_DEPENDENCY_SOURCE
        fixtures = {
            "joinpath": (header + 'X = ROOT.joinpath("new-contract.md").read_text()\n',
                         "root_joinpath"),
            "path_constructor": (header + 'X = Path(ROOT, "new-contract.md").read_text()\n',
                                 "path_constructor_from_root"),
            "variable_path": (header + 'name = "new-contract.md"\nX = (ROOT / name).read_text()\n',
                              "root_path_derivation"),
            "helper_returned": (header + 'def helper(n):\n    return ROOT / n\n'
                                         'X = helper("new-contract.md").read_text()\n',
                                "unresolved_repository_read"),
            "dynamic_key": (header + 'key = "readme"\nX = read_repo_text(key)\n',
                            "dynamic_dependency_key"),
            "unregistered_key": (header + 'X = read_repo_text("not_registered")\n',
                                 "unregistered_dependency_key"),
            "builtin_open": (header + 'X = open(str(ROOT) + "/new-contract.md").read()\n',
                             "builtin_open"),
        }
        for name, (source, expected) in fixtures.items():
            violations = repository_read_violations(source)
            self.assertTrue(violations, "%s must not be silently ignored" % name)
            self.assertTrue(any(v.endswith(expected) for v in violations),
                            "%s: expected %s, got %s" % (name, expected, violations))

    def test_ast_guard_rejects_alias_and_indirection_read_routes(self):
        # A3-3: realistic indirection must fail closed, not merely the direct forms.
        header = CANONICAL_DEPENDENCY_SOURCE
        derived = "target = Path(__file__).resolve().parents[1] / 'new-contract.md'\n"
        fixtures = {
            "bound_read_text": (header + derived + "reader = target.read_text\ntext = reader()\n",
                                "bound_reader_capture"),
            "bound_read_bytes": (header + derived + "reader = target.read_bytes\nblob = reader()\n",
                                 "bound_reader_capture"),
            "bound_path_open": (header + derived + "opener = target.open\nhandle = opener()\n",
                                "bound_reader_capture"),
            "imported_open_alias": ("from io import open as io_open\n" + header + derived
                                    + "text = io_open(target).read()\n", "open_alias_import"),
            "assigned_open_alias": (header + derived + "reader = open\ntext = reader(target).read()\n",
                                    "builtin_open"),
            "alias_of_alias": (header + derived + "first = open\nsecond = first\ntext = second(target).read()\n",
                               "builtin_open"),
            "getattr_literal": (header + derived + "reader = getattr(target, 'read_text')\ntext = reader()\n",
                                "dynamic_attribute_access"),
            "getattr_variable": (header + derived + "name = 'read_text'\nreader = getattr(target, name)\ntext = reader()\n",
                                 "dynamic_attribute_access"),
            "constructed_attribute": (header + derived + "name = 'read' + '_text'\nreader = getattr(target, name)\ntext = reader()\n",
                                      "dynamic_attribute_access"),
            "attrgetter": ("from operator import attrgetter\n" + header + derived
                           + "reader = attrgetter('read_text')(target)\ntext = reader()\n",
                           "dynamic_attribute_access"),
            "wrapper_reads_path": (header + derived + "def load():\n    return target.read_text()\n",
                                   "unresolved_repository_read"),
            "wrapper_returns_path": (header + "def where():\n    return ROOT / 'new-contract.md'\n"
                                     + "text = where().read_text()\n", "repository_path_escape"),
            "wrapper_returns_reader": (header + derived + "def make():\n    return target.read_text\n"
                                       + "text = make()()\n", "reader_callable_escape"),
            "lambda_reader": (header + derived + "load = lambda: target.read_text()\ntext = load()\n",
                              "unresolved_repository_read"),
            "list_comprehension": (header + derived + "texts = [target.read_text() for _ in range(1)]\n",
                                   "unresolved_repository_read"),
            "dict_comprehension": (header + derived + "texts = {i: target.read_text() for i in range(1)}\n",
                                   "unresolved_repository_read"),
            "generator_reader": (header + derived + "texts = (target.read_text() for _ in range(1))\n",
                                 "unresolved_repository_read"),
            "closure_over_path": (header + derived + "def outer():\n    def inner():\n        return target.read_text()\n    return inner\n",
                                  "unresolved_repository_read"),
            "file_derived_path": (header
                                  + "base = Path(__file__).resolve().parents[1]\n"
                                  + "text = (base / 'new-contract.md').read_text()\n",
                                  "root_path_derivation"),
            "file_derived_alias": (header
                                   + "base = Path(__file__).resolve().parents[1]\n"
                                   + "alias = base\ntext = (alias / 'new-contract.md').read_text()\n",
                                   "root_path_derivation"),
            "helper_returned_path": (header + "def helper(n):\n    return ROOT / n\n"
                                     + "text = helper('new-contract.md').read_text()\n",
                                     "unresolved_repository_read"),
            "helper_returned_reader": (header + derived + "def helper():\n    return target.read_bytes\n"
                                       + "blob = helper()()\n", "reader_callable_escape"),
            "unresolved_call_with_path": (header + derived + "sink(target)\n",
                                          "repository_path_escape"),
            "unresolved_call_with_reader": (header + derived + "reader = target.read_text\nsink(reader)\n",
                                            "reader_callable_escape"),
            "reader_in_collection": (header + derived + "readers = [target.read_text]\ntext = readers[0]()\n",
                                     "bound_reader_capture"),
            "dynamic_import_reader": ("import importlib\n" + header + derived
                                      + "mod = importlib.import_module('io')\ntext = mod.open(target).read()\n",
                                      "dynamic_attribute_access"),
        }
        self.assertGreaterEqual(len(fixtures), 25, "the A3-3 fixture matrix must stay complete")
        for name, (source, expected) in fixtures.items():
            violations = repository_read_violations(source)
            self.assertTrue(violations, "%s must not be silently accepted" % name)
            self.assertTrue(any(v.endswith(expected) for v in violations),
                            "%s: expected %s, got %s" % (name, expected, violations))

    def test_sanctioned_helper_exemption_cannot_be_borrowed(self):
        # A nested or same-named function must not inherit the sanctioned-helper exemption.
        header = CANONICAL_DEPENDENCY_SOURCE
        nested = header + ("class Sneaky:\n"
                           "    def read_repo_text(self, key):\n"
                           "        return (ROOT / key).read_text()\n")
        self.assertTrue(repository_read_violations(nested),
                        "a method named like a sanctioned helper must not be exempt")
        inner = header + ("def outer():\n"
                          "    def read_scratch_text(p):\n"
                          "        return (ROOT / p).read_text()\n"
                          "    return read_scratch_text\n")
        self.assertTrue(repository_read_violations(inner),
                        "a nested function named like a sanctioned helper must not be exempt")

    def test_ast_guard_rejects_external_and_imported_repository_path_consumption(self):
        # A4-2: an attribute call or imported callable is NOT safe merely because its syntax
        # resolves. Any external or ambiguous callable that receives repository taint — however
        # deeply nested — must fail closed.
        header = ("import shutil\n"
                  "import subprocess\n"
                  "from helpers import slurp, reader as imported_reader\n"
                  + CANONICAL_DEPENDENCY_SOURCE
                  + "SCRIPT = repo_path('probe_script')\n"
                  "scratch = Path('/scratch/copy.ps1')\n")
        fixtures = {
            "shutil_copyfile": "shutil.copyfile(SCRIPT, scratch)\n",
            "shutil_copy2": "shutil.copy2(SCRIPT, scratch)\n",
            "imported_slurp": "text = slurp(SCRIPT)\n",
            "imported_reader_alias": "text = imported_reader(SCRIPT)\n",
            "module_attribute_read": "text = imported_reader.read(SCRIPT)\n",
            "module_callable": "text = shutil.disk_usage(SCRIPT)\n",
            "subprocess_check_output": "out = subprocess.check_output(['tool', str(SCRIPT)])\n",
            "subprocess_input_kwarg": "subprocess.run(['tool'], input=str(SCRIPT))\n",
            "nested_in_list": "run_tool([SCRIPT, '--flag'])\n",
            "nested_in_tuple": "run_tool((SCRIPT, '--flag'))\n",
            "nested_in_set": "run_tool({SCRIPT})\n",
            "nested_in_dict_value": "run_tool({'path': SCRIPT})\n",
            "nested_in_dict_key": "run_tool({SCRIPT: 'path'})\n",
            "keyword_argument": "run_tool(target=SCRIPT)\n",
            "star_args": "args = [SCRIPT]\nrun_tool(*args)\n",
            "star_kwargs": "options = {'target': SCRIPT}\nrun_tool(**options)\n",
            "dynamic_callable_from_mapping": "handlers = {'a': slurp}\nhandlers['a'](SCRIPT)\n",
            "callable_returned_from_helper": "def pick():\n    return slurp\npick()(SCRIPT)\n",
            "wrapper_forwards_path": "def forward(p):\n    return slurp(p)\nforward(SCRIPT)\n",
            "lambda_forwards_path": "send = lambda: slurp(SCRIPT)\nsend()\n",
            "comprehension_external_call": "results = [slurp(SCRIPT) for _ in range(1)]\n",
            "generator_external_call": "results = (slurp(SCRIPT) for _ in range(1))\n",
            "closure_over_registered_path": "def outer():\n    def inner():\n        return slurp(SCRIPT)\n    return inner\n",
            "fstring_path_to_external": "subprocess.run(f'tool {SCRIPT}', shell=False)\n",
            "arbitrary_imported_callable": "slurp(SCRIPT, encoding='utf-8')\n",
            "arbitrary_attribute_callable": "imported_reader.load(SCRIPT)\n",
        }
        self.assertGreaterEqual(len(fixtures), 24, "the A4-2 fixture matrix must stay complete")
        accepted = []
        for name, body in fixtures.items():
            violations = repository_read_violations(header + body)
            if not violations:
                accepted.append(name)
                continue
            self.assertTrue(
                any(v.endswith(("repository_path_escape", "reader_callable_escape",
                                "unresolved_repository_read", "dynamic_attribute_access"))
                    for v in violations),
                "%s: unexpected categories %s" % (name, violations))
        self.assertEqual(accepted, [], "these bypasses were silently accepted: %s" % accepted)

    # ---- A5-1: the exemption is bound to the EXACT reviewed helper bodies ---- #
    def _helper_module(self, repo_path_src=None, read_repo_text_src=None,
                       read_scratch_text_src=None, prologue="", extra=""):
        return (prologue
                + CANONICAL_DEPENDENCY_SOURCE
                + (repo_path_src if repo_path_src is not None else CANONICAL_HELPER_SOURCES["repo_path"])
                + (read_repo_text_src if read_repo_text_src is not None else CANONICAL_HELPER_SOURCES["read_repo_text"])
                + (read_scratch_text_src if read_scratch_text_src is not None else CANONICAL_HELPER_SOURCES["read_scratch_text"])
                + extra)

    def test_real_helpers_satisfy_the_exact_reviewed_contract(self):
        self.assertEqual(sanctioned_helper_contract_violations(read_repo_text("focused_tests")), [],
                         "the reviewed helper bodies must match their independent canonical contract")

    def test_helper_contract_ignores_comments_docstrings_and_line_movement(self):
        cosmetic = self._helper_module(
            repo_path_src=('def repo_path(key):\n'
                           '    """A different but behaviour-free docstring."""\n'
                           '    # an added comment\n'
                           '\n'
                           '    if key not in REPO_DEPENDENCIES:\n'
                           '        raise KeyError("unregistered repository dependency key: %r" % (key,))\n'
                           '    return ROOT / REPO_DEPENDENCIES[key]\n'))
        self.assertEqual(sanctioned_helper_contract_violations(cosmetic), [],
                         "comments, docstrings and blank lines must not invalidate the contract")

    def test_every_helper_body_mutation_is_rejected(self):
        canonical = CANONICAL_HELPER_SOURCES
        mutations = {
            "second_read_text": dict(read_repo_text_src=(
                'def read_repo_text(key):\n'
                '    extra = repo_path(key).read_text(encoding="utf-8")\n'
                '    return repo_path(key).read_text(encoding="utf-8")\n')),
            "read_bytes_added": dict(read_repo_text_src=(
                'def read_repo_text(key):\n'
                '    blob = repo_path(key).read_bytes()\n'
                '    return repo_path(key).read_text(encoding="utf-8")\n')),
            "builtin_open_added": dict(read_repo_text_src=(
                'def read_repo_text(key):\n'
                '    handle = open(repo_path(key))\n'
                '    return repo_path(key).read_text(encoding="utf-8")\n')),
            "imported_open_alias_added": dict(
                prologue="from io import open as io_open\n",
                read_repo_text_src=('def read_repo_text(key):\n'
                                    '    handle = io_open(repo_path(key))\n'
                                    '    return repo_path(key).read_text(encoding="utf-8")\n')),
            "copy_added": dict(
                prologue="import shutil\n",
                read_repo_text_src=('def read_repo_text(key):\n'
                                    '    shutil.copyfile(repo_path(key), Path("/scratch/leak"))\n'
                                    '    return repo_path(key).read_text(encoding="utf-8")\n')),
            "subprocess_added": dict(
                prologue="import subprocess\n",
                read_repo_text_src=('def read_repo_text(key):\n'
                                    '    subprocess.run(["tool", str(repo_path(key))])\n'
                                    '    return repo_path(key).read_text(encoding="utf-8")\n')),
            "wrapper_receiving_path": dict(read_repo_text_src=(
                'def read_repo_text(key):\n'
                '    forward(repo_path(key))\n'
                '    return repo_path(key).read_text(encoding="utf-8")\n')),
            "registry_check_changed": dict(repo_path_src=(
                'def repo_path(key):\n'
                '    if key in REPO_DEPENDENCIES:\n'
                '        raise KeyError("unregistered repository dependency key: %r" % (key,))\n'
                '    return ROOT / REPO_DEPENDENCIES[key]\n')),
            "registry_lookup_changed": dict(repo_path_src=(
                'def repo_path(key):\n'
                '    if key not in REPO_DEPENDENCIES:\n'
                '        raise KeyError("unregistered repository dependency key: %r" % (key,))\n'
                '    return ROOT / key\n')),
            "encoding_changed": dict(read_repo_text_src=(
                'def read_repo_text(key):\n'
                '    return repo_path(key).read_text(encoding="latin-1")\n')),
            "containment_removed": dict(read_scratch_text_src=(
                'def read_scratch_text(path):\n'
                '    resolved = Path(path).resolve()\n'
                '    return resolved.read_text(encoding="utf-8")\n')),
            "statement_before": dict(read_scratch_text_src=(
                'def read_scratch_text(path):\n'
                '    audit = 1\n'
                '    resolved = Path(path).resolve()\n'
                '    if resolved == ROOT or ROOT in resolved.parents:\n'
                '        raise AssertionError("the scratch reader refuses a repository path: %s" % resolved)\n'
                '    return resolved.read_text(encoding="utf-8")\n')),
            "statement_after": dict(repo_path_src=(
                'def repo_path(key):\n'
                '    if key not in REPO_DEPENDENCIES:\n'
                '        raise KeyError("unregistered repository dependency key: %r" % (key,))\n'
                '    result = ROOT / REPO_DEPENDENCIES[key]\n'
                '    return result\n')),
            "statements_reordered": dict(read_scratch_text_src=(
                'def read_scratch_text(path):\n'
                '    if resolved == ROOT or ROOT in resolved.parents:\n'
                '        raise AssertionError("the scratch reader refuses a repository path: %s" % resolved)\n'
                '    resolved = Path(path).resolve()\n'
                '    return resolved.read_text(encoding="utf-8")\n')),
            "exception_type_changed": dict(repo_path_src=(
                'def repo_path(key):\n'
                '    if key not in REPO_DEPENDENCIES:\n'
                '        raise ValueError("unregistered repository dependency key: %r" % (key,))\n'
                '    return ROOT / REPO_DEPENDENCIES[key]\n')),
            "missing_helper": dict(read_scratch_text_src=""),
            "duplicate_helper": dict(extra=canonical["read_repo_text"]),
            "duplicate_plus_correct": dict(extra=canonical["repo_path"]),
            "nested_only": dict(read_scratch_text_src=(
                'def outer():\n'
                '    def read_scratch_text(path):\n'
                '        return Path(path).read_text(encoding="utf-8")\n'
                '    return read_scratch_text\n')),
            "method_only": dict(read_scratch_text_src=(
                'class Holder:\n'
                '    def read_scratch_text(self, path):\n'
                '        return Path(path).read_text(encoding="utf-8")\n')),
        }
        self.assertGreaterEqual(len(mutations), 20, "the A5-1 mutation matrix must stay complete")
        accepted = []
        expected_categories = ("sanctioned_helper_missing", "sanctioned_helper_duplicate",
                               "sanctioned_helper_body_mismatch", "sanctioned_helper_not_top_level")
        for name, kwargs in mutations.items():
            source = self._helper_module(**kwargs)
            problems = sanctioned_helper_contract_violations(source)
            if not problems:
                accepted.append(name)
                continue
            self.assertTrue(any(p.endswith(expected_categories) for p in problems),
                            "%s: unexpected categories %s" % (name, problems))
            # A helper that fails its contract must receive no exemption in the main guard.
            if not all(problem.endswith("sanctioned_helper_missing") for problem in problems):
                self.assertTrue(repository_read_violations(source),
                                "%s: a mismatched helper must not keep its exemption" % name)
        self.assertEqual(accepted, [], "these helper mutations were accepted: %s" % accepted)

    def test_mismatched_helper_loses_its_exemption_in_the_main_guard(self):
        tampered = self._helper_module(read_repo_text_src=(
            'def read_repo_text(key):\n'
            '    leaked = repo_path(key).read_bytes()\n'
            '    return repo_path(key).read_text(encoding="utf-8")\n'))
        violations = repository_read_violations(tampered)
        self.assertTrue(any(v.endswith("sanctioned_helper_body_mismatch") for v in violations),
                        violations)
        self.assertTrue(any(v.endswith(("unresolved_repository_read", "bound_reader_capture"))
                            for v in violations),
                        "the unexempted body's reads must now be visible: %s" % violations)

    # ---- A5-2: default-bound taint and closure propagation ---- #
    def test_ast_guard_rejects_repository_taint_bound_through_defaults(self):
        header = ("from helpers import slurp\n"
                  + CANONICAL_DEPENDENCY_SOURCE
                  + "SCRIPT = repo_path('probe_script')\n")
        fixtures = {
            "positional_default": "def load(p=SCRIPT):\n    return slurp(p)\nload()\n",
            "keyword_only_default": "def load(*, p=SCRIPT):\n    return slurp(p)\nload()\n",
            "lambda_default": "load = lambda p=SCRIPT: slurp(p)\nload()\n",
            "async_default": "async def load(p=SCRIPT):\n    return slurp(p)\n",
            "str_default": "def load(p=str(SCRIPT)):\n    return slurp(p)\nload()\n",
            "list_default": "def load(p=[SCRIPT]):\n    return slurp(p)\nload()\n",
            "tuple_default": "def load(p=(SCRIPT,)):\n    return slurp(p)\nload()\n",
            "set_default": "def load(p={SCRIPT}):\n    return slurp(p)\nload()\n",
            "dict_value_default": "def load(p={'k': SCRIPT}):\n    return slurp(p)\nload()\n",
            "dict_key_default": "def load(p={SCRIPT: 'k'}):\n    return slurp(p)\nload()\n",
            "helper_returned_default": ("def where():\n    return SCRIPT\n"
                                        "def load(p=where()):\n    return slurp(p)\nload()\n"),
            "nested_closure": ("def outer(p=SCRIPT):\n"
                               "    def inner():\n        return slurp(p)\n    return inner\n"),
            "returns_default_path": "def leak(p=SCRIPT):\n    return p\n",
            "reader_default": "def load(reader=open):\n    return reader('x')\nload()\n",
            "container_reader_default": "def load(readers=[open]):\n    return readers[0]('x')\nload()\n",
            "callable_default_returns_reader": ("def maker():\n    return open\n"
                                                "def load(factory=maker()):\n    return factory('x')\nload()\n"),
            "star_args_default": "def load(p=SCRIPT):\n    return slurp(*[p])\nload()\n",
            "star_kwargs_default": "def load(p=SCRIPT):\n    return slurp(**{'target': p})\nload()\n",
            "zero_argument_wrapper": "def load(p=SCRIPT):\n    return slurp(p)\nresult = load()\n",
            "zero_argument_lambda": "grab = lambda p=SCRIPT: slurp(p)\nresult = grab()\n",
            "fstring_default": "def load(p=f'{SCRIPT}'):\n    return slurp(p)\nload()\n",
            "comprehension_default": "def load(p=[x for x in [SCRIPT]]):\n    return slurp(p)\nload()\n",
            "generator_default": "def load(p=(x for x in [SCRIPT])):\n    return slurp(p)\nload()\n",
            "ambiguous_default": "def load(p=unknown_source()):\n    return slurp(p)\nload()\n",
        }
        self.assertGreaterEqual(len(fixtures), 24, "the A5-2 default matrix must stay complete")
        accepted = []
        for name, body in fixtures.items():
            violations = repository_read_violations(header + body)
            if not violations:
                accepted.append(name)
                continue
            self.assertTrue(
                any(v.endswith(("repository_path_escape", "reader_callable_escape",
                                "unresolved_repository_read", "ambiguous_default_binding",
                                "builtin_open", "bound_reader_capture"))
                    for v in violations),
                "%s: unexpected categories %s" % (name, violations))
        self.assertEqual(accepted, [], "these default bypasses were accepted: %s" % accepted)

    def test_default_bound_taint_does_not_leak_across_scopes(self):
        # Positive control: an unrelated function using the same parameter name with a safe
        # default must stay accepted, proving taint is lexically scoped.
        header = ("from helpers import slurp\n"
                  + CANONICAL_DEPENDENCY_SOURCE
                  + "SCRIPT = repo_path('probe_script')\n")
        safe = "def unrelated(p='plain-scratch-name'):\n    return slurp(p)\nunrelated()\n"
        self.assertEqual(repository_read_violations(header + safe), [],
                         "a same-named parameter in an unrelated function must not inherit taint")
        both = ("def tainted(p=SCRIPT):\n    return slurp(p)\n"
                "def unrelated(p='plain-scratch-name'):\n    return slurp(p)\n")
        violations = repository_read_violations(header + both)
        self.assertEqual(len(violations), 1,
                         "exactly the tainted scope must be reported: %s" % violations)

    # ---- A6-1: full definition contract, required helpers and binding integrity ---- #
    def test_real_module_requires_all_three_exact_immutable_helpers(self):
        # The explicit require_all boundary: generic snippets may waive presence, the real
        # module may not.
        source = read_repo_text("focused_tests")
        self.assertEqual(sanctioned_helper_contract_violations(source, require_all=True), [],
                         "the real module must declare and satisfy all three exact helpers")
        self.assertEqual(sanctioned_helper_binding_violations(source), [],
                         "no sanctioned helper identifier may be rebound or captured")
        self.assertEqual(repository_read_violations(source), [])

    def test_missing_helpers_fail_only_under_the_required_mode(self):
        snippet = ("from pathlib import Path\n"
                   "ROOT = Path(__file__).resolve().parents[1]\n"
                   "value = 1\n")
        self.assertEqual(sanctioned_helper_contract_violations(snippet, require_all=False), [],
                         "generic snippets may waive presence noise")
        required = sanctioned_helper_contract_violations(snippet, require_all=True)
        self.assertEqual(len(required), 3, required)
        for name in ("repo_path", "read_repo_text", "read_scratch_text"):
            self.assertTrue(any(problem.startswith(name + ":")
                                and problem.endswith("sanctioned_helper_missing")
                                for problem in required), required)

    def test_helper_definition_contract_covers_decorators_and_annotations(self):
        cases = {
            "decorated_repo_path": dict(repo_path_src=(
                "@replacement\n" + CANONICAL_HELPER_SOURCES["repo_path"])),
            "decorated_read_repo_text": dict(read_repo_text_src=(
                "@replacement\n" + CANONICAL_HELPER_SOURCES["read_repo_text"])),
            "decorated_read_scratch_text": dict(read_scratch_text_src=(
                "@replacement\n" + CANONICAL_HELPER_SOURCES["read_scratch_text"])),
            "decorator_call_with_arguments": dict(read_repo_text_src=(
                "@replacement(mode='swap')\n" + CANONICAL_HELPER_SOURCES["read_repo_text"])),
            "return_annotation": dict(read_repo_text_src=(
                'def read_repo_text(key) -> str:\n'
                '    return repo_path(key).read_text(encoding="utf-8")\n')),
            "argument_annotation": dict(read_repo_text_src=(
                'def read_repo_text(key: str):\n'
                '    return repo_path(key).read_text(encoding="utf-8")\n')),
            "async_helper": dict(read_repo_text_src=(
                'async def read_repo_text(key):\n'
                '    return repo_path(key).read_text(encoding="utf-8")\n')),
        }
        accepted = []
        for name, kwargs in cases.items():
            source = self._helper_module(**kwargs)
            problems = sanctioned_helper_contract_violations(source, require_all=True)
            if not problems:
                accepted.append(name)
                continue
            self.assertTrue(any(problem.endswith(("sanctioned_helper_body_mismatch",
                                                  "sanctioned_helper_decorated",
                                                  "sanctioned_helper_missing",
                                                  "sanctioned_helper_not_top_level"))
                                for problem in problems),
                            "%s: %s" % (name, problems))
            self.assertTrue(repository_read_violations(source),
                            "%s must lose its exemption" % name)
        self.assertEqual(accepted, [], "these definition mutations were accepted: %s" % accepted)

    def test_helper_binding_integrity_rejects_every_rebinding_form(self):
        canonical = (CANONICAL_HELPER_SOURCES["repo_path"]
                     + CANONICAL_HELPER_SOURCES["read_repo_text"]
                     + CANONICAL_HELPER_SOURCES["read_scratch_text"])
        header = CANONICAL_DEPENDENCY_SOURCE
        cases = {
            "rebound_after_definition": canonical + "read_repo_text = imported_reader\n",
            "rebound_before_definition": "read_repo_text = imported_reader\n" + canonical,
            "rebound_repo_path": canonical + "repo_path = imported_reader\n",
            "rebound_read_scratch_text": canonical + "read_scratch_text = imported_reader\n",
            "import_alias_shadow": "from helpers import reader as read_repo_text\n" + canonical,
            "annotated_assignment": canonical + "read_repo_text: object = imported_reader\n",
            "augmented_assignment": canonical + "read_repo_text += 1\n",
            "named_expression": canonical + "value = (read_repo_text := imported_reader)\n",
            "destructuring": canonical + "read_repo_text, other = imported_reader, 1\n",
            "loop_target": canonical + "for read_repo_text in candidates:\n    pass\n",
            "comprehension_target": canonical + "items = [read_repo_text for read_repo_text in candidates]\n",
            "with_target": canonical + "with opened() as read_repo_text:\n    pass\n",
            "except_target": canonical + "try:\n    pass\nexcept Exception as read_repo_text:\n    pass\n",
            "parameter_shadow": canonical + "def outer(read_repo_text):\n    return 1\n",
            "lambda_parameter_shadow": canonical + "grab = lambda read_repo_text: 1\n",
            "class_shadow": canonical + "class read_repo_text:\n    pass\n",
            "second_function_definition": canonical + "def read_repo_text(key):\n    return 1\n",
            "alias_capture": canonical + "alias = read_repo_text\n",
            "container_capture": canonical + "holders = [read_repo_text]\n",
            "returned_capture": canonical + "def leak():\n    return read_repo_text\n",
            "passed_capture": canonical + "wrapper(read_repo_text)\n",
            "rebound_root": canonical + "ROOT = Path('/elsewhere')\n",
            "rebound_registry": canonical + "REPO_DEPENDENCIES = {}\n",
            "rebound_path": canonical + "from other import Path\n",
        }
        self.assertGreaterEqual(len(cases), 23, "the A6-1 binding matrix must stay complete")
        accepted = []
        for name, body in cases.items():
            source = header + body
            problems = sanctioned_helper_binding_violations(source)
            guard = repository_read_violations(source)
            if not problems:
                accepted.append(name)
                continue
            self.assertTrue(any(problem.endswith(("sanctioned_helper_rebound",
                                                  "sanctioned_helper_binding_escape",
                                                  "sanctioned_helper_duplicate"))
                                for problem in problems),
                            "%s: %s" % (name, problems))
            self.assertTrue(guard, "%s must surface through the main guard" % name)
        self.assertEqual(accepted, [], "these rebindings were accepted: %s" % accepted)

    def test_rebound_helper_loses_literal_sanctioned_call_authority(self):
        header = CANONICAL_DEPENDENCY_SOURCE
        canonical = (CANONICAL_HELPER_SOURCES["repo_path"]
                     + CANONICAL_HELPER_SOURCES["read_repo_text"]
                     + CANONICAL_HELPER_SOURCES["read_scratch_text"])
        # A literal registered key is normally the one sanctioned route; after rebinding, the
        # name no longer refers to the reviewed helper and the allowance must be withheld.
        clean = header + canonical + 'text = read_repo_text("readme")\n'
        self.assertEqual(repository_read_violations(clean), [])
        rebound = (header + canonical
                   + "read_repo_text = imported_reader\n"
                   + 'text = read_repo_text("readme")\n')
        violations = repository_read_violations(rebound)
        self.assertTrue(any(v.endswith("sanctioned_helper_rebound") for v in violations), violations)

    # ---- A6-2: positively proven default safety and finite convergence ---- #
    def test_default_safety_requires_positive_proof(self):
        header = ("from config import SETTINGS, CONFIG_PATH\n"
                  "from helpers import slurp\n"
                  "import config\n"
                  + CANONICAL_DEPENDENCY_SOURCE)
        chain_paths = "".join("alias%d = alias%d\n" % (i + 1, i) for i in range(9))
        chain_helpers = "".join("def hop%d():\n    return hop%d()\n" % (i + 1, i) for i in range(9))
        cases = {
            "imported_path_name": "def load(p=CONFIG_PATH):\n    return slurp(p)\nload()\n",
            "imported_reader_name": "def load(r=slurp):\n    return r('x')\nload()\n",
            "unresolved_name": "def load(p=MISSING_NAME):\n    return slurp(p)\nload()\n",
            "imported_module_attribute": "def load(p=config.PATH):\n    return slurp(p)\nload()\n",
            "nested_module_attribute": "def load(p=config.section.PATH):\n    return slurp(p)\nload()\n",
            "imported_subscript": "def load(p=SETTINGS['path']):\n    return slurp(p)\nload()\n",
            "annotated_path_alias": ("alias: object = CONFIG_PATH\n"
                                     "def load(p=alias):\n    return slurp(p)\nload()\n"),
            "annotated_reader_alias": ("reader: object = slurp\n"
                                       "def load(r=reader):\n    return r('x')\nload()\n"),
            "named_expression_path_alias": ("value = (alias := CONFIG_PATH)\n"
                                            "def load(p=alias):\n    return slurp(p)\nload()\n"),
            "named_expression_reader_alias": ("value = (reader := slurp)\n"
                                              "def load(r=reader):\n    return r('x')\nload()\n"),
            "destructured_path_alias": ("alias, other = CONFIG_PATH, 1\n"
                                        "def load(p=alias):\n    return slurp(p)\nload()\n"),
            "destructured_reader_alias": ("reader, other = slurp, 1\n"
                                          "def load(r=reader):\n    return r('x')\nload()\n"),
            "long_alias_chain": ("alias0 = CONFIG_PATH\n" + chain_paths
                                 + "def load(p=alias9):\n    return slurp(p)\nload()\n"),
            "long_helper_return_chain": ("def hop0():\n    return CONFIG_PATH\n" + chain_helpers
                                         + "def load(p=hop9()):\n    return slurp(p)\nload()\n"),
            "mixed_container_chain": ("def hop0():\n    return CONFIG_PATH\n" + chain_helpers
                                      + "bundle = [hop9()]\n"
                                      + "def load(p=bundle):\n    return slurp(p)\nload()\n"),
            "unresolved_name_cycle": ("first = second\nsecond = first\n"
                                      "def load(p=first):\n    return slurp(p)\nload()\n"),
            "attribute_subscript_cycle": ("first = config.A[second]\nsecond = config.B[first]\n"
                                          "def load(p=first):\n    return slurp(p)\nload()\n"),
            "reader_in_nested_container": ("def load(bundle={'inner': [slurp]}):\n"
                                           "    return bundle['inner'][0]('x')\nload()\n"),
        }
        self.assertGreaterEqual(len(cases), 18, "the A6-2 default matrix must stay complete")
        accepted = []
        for name, body in cases.items():
            violations = repository_read_violations(header + body)
            if not violations:
                accepted.append(name)
                continue
            self.assertTrue(
                any(v.endswith(("ambiguous_default_binding", "repository_path_escape",
                                "reader_callable_escape", "unresolved_repository_read",
                                "builtin_open", "bound_reader_capture"))
                    for v in violations),
                "%s: unexpected categories %s" % (name, violations))
        self.assertEqual(accepted, [], "these unsafe defaults were accepted: %s" % accepted)

    def test_provably_safe_defaults_are_accepted(self):
        header = ("from helpers import slurp\n"
                  + CANONICAL_DEPENDENCY_SOURCE)
        controls = {
            "literal_scalar": "def load(p='plain'):\n    return slurp(p)\nload()\n",
            "literal_container": "def load(p=['a', ('b', 1), {'c': 2}]):\n    return slurp(p)\nload()\n",
            "safe_fstring": "def load(p=f'{1}-plain'):\n    return slurp(p)\nload()\n",
            "unique_immutable_constant": ("SAFE_NAME = 'plain'\n"
                                          "def load(p=SAFE_NAME):\n    return slurp(p)\nload()\n"),
            "sibling_same_parameter_name": ("def one(p='plain'):\n    return slurp(p)\n"
                                            "def two(p='other'):\n    return slurp(p)\n"),
            "none_default": "def load(p=None):\n    return slurp(p)\nload()\n",
        }
        for name, body in controls.items():
            self.assertEqual(repository_read_violations(header + body), [],
                             "%s must be accepted as provably safe" % name)

    def test_taint_analysis_converges_without_a_fixed_pass_count(self):
        # A monotonic fixpoint, not a hard-coded pass budget: chains far longer than the old
        # five-iteration cap must still resolve.
        source = read_repo_text("focused_tests")
        fixed_pass_needle = "for _ in range(" + "5):"
        self.assertNotIn(fixed_pass_needle, source,
                         "the fixpoint must not be bounded by an arbitrary constant")
        self.assertIn("convergence_bound", source,
                      "termination must be bounded by the finite AST universe")
        header = ("from helpers import slurp\n"
                  + CANONICAL_DEPENDENCY_SOURCE
                  + "SCRIPT = repo_path('probe_script')\n")
        chain = "alias0 = SCRIPT\n" + "".join("alias%d = alias%d\n" % (i + 1, i) for i in range(12))
        violations = repository_read_violations(header + chain + "slurp(alias12)\n")
        self.assertTrue(any(v.endswith("repository_path_escape") for v in violations),
                        "a 13-link alias chain must converge to a violation: %s" % violations)

    def test_sanctioned_helper_body_tampering_is_detected(self):
        # A4-2: the exemption covers the exact reviewed helper bodies. A helper body that grows
        # an external read or copy must fail the guard contract rather than inherit exemption.
        tampered = ("import shutil\n"
                    + CANONICAL_DEPENDENCY_SOURCE
                    + "def repo_path(key):\n"
                    "    return ROOT / key\n"
                    "def read_repo_text(key):\n"
                    "    shutil.copyfile(repo_path(key), Path('/scratch/leak'))\n"
                    "    return repo_path(key).read_text(encoding='utf-8')\n")
        self.assertTrue(sanctioned_helper_contract_violations(tampered),
                        "a helper body that copies repository content must fail the contract")
        clean = read_repo_text("focused_tests")
        self.assertEqual(sanctioned_helper_contract_violations(clean), [],
                         "the reviewed helper bodies must satisfy their own contract")

    def test_ast_guard_is_independent_of_the_registry_contents(self):
        # Even a REGISTERED file read through an escaping form must fail: the guard checks the
        # shape of the read expression, so closure cannot pass merely because the expected and
        # actual inventories came from the same incomplete resolver.
        header = CANONICAL_DEPENDENCY_SOURCE
        violations = repository_read_violations(header + 'X = ROOT.joinpath("README.md").read_text()\n')
        self.assertTrue(any(v.endswith("root_joinpath") for v in violations), violations)
        self.assertTrue(any(v.endswith("unresolved_repository_read") for v in violations), violations)
        # The sanctioned form for the same registered file is accepted.
        self.assertEqual(repository_read_violations(header + 'X = read_repo_text("readme")\n'), [])

    def test_registry_is_immutable(self):
        with self.assertRaises(TypeError):
            REPO_DEPENDENCIES["injected"] = "somewhere/else.md"

    def test_unregistered_dependency_key_fails_closed_at_runtime(self):
        # The canonical snippet the live helper is pinned to byte-for-byte, executed in a FRESH
        # interpreter from a scratch file. Aliasing the live helper is an A6-1 binding escape and
        # in-process exec/compile is an A7-2 dynamic-binding route, so the proof uses neither.
        scratch = Path(tempfile.mkdtemp())
        try:
            driver = scratch / "canonical_repo_path_probe.py"
            driver.write_text(
                "from pathlib import Path\n"
                'REPO_DEPENDENCIES = {"readme": "README.md"}\n'
                'ROOT = Path("scratch-root")\n'
                + CANONICAL_HELPER_SOURCES["repo_path"]
                + 'assert repo_path("readme") == ROOT / "README.md"\n'
                "try:\n"
                '    repo_path("definitely_not_registered")\n'
                "except KeyError:\n"
                '    print("FAILED_CLOSED")\n'
                "else:\n"
                '    raise SystemExit("the unregistered key did not fail closed")\n',
                encoding="utf-8")
            proc = subprocess.run([sys.executable, str(driver)], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("FAILED_CLOSED", proc.stdout)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        self.assertNotIn("definitely_not_registered", REPO_DEPENDENCIES)

    def test_a_newly_registered_dependency_must_also_enter_the_workflow_filter(self):
        # Closure in the other direction: a registry entry with no matching path pattern is
        # reported, so adding a dependency without wiring CI cannot pass.
        filters = workflow_path_filters(self.workflow)
        self.assertTrue(filters)
        new_dependency = "docs/autocount2-automation/new-contract.md"
        for patterns in filters.values():
            self.assertEqual(uncovered_dependencies({new_dependency}, patterns), [new_dependency])

    def test_closure_assertion_fails_when_a_required_path_is_dropped(self):
        # Negative control: the closure check must actually bite. Removing one required
        # filter entry from a fixture copy of the workflow must be detected.
        fixture = "\n".join(line for line in self.workflow.splitlines()
                            if line.strip() != '- ".gitignore"')
        self.assertNotEqual(fixture, self.workflow, "the fixture must differ from the real workflow")
        filters = workflow_path_filters(fixture)
        self.assertTrue(filters)
        inventory = registered_dependencies()
        for patterns in filters.values():
            self.assertIn(".gitignore", uncovered_dependencies(inventory, patterns))

    def test_delegated_trigger_assertion_fails_when_a_required_entry_is_dropped(self):
        # Negative control for the DL-XB-121-001 closure, run entirely in memory against a
        # synthetic workflow so no repository file is read or written. A fixture that satisfies
        # the requirement is degraded by exactly one required entry, and the same detection
        # logic must report exactly that path.
        omitted = "scripts/member_lookup_gate4_real_queue_lookup.py"
        self.assertIn(omitted, DELEGATED_LOOKUP_WORKFLOW_TRIGGER_PATHS)
        complete = "\n".join(
            ["on:", "  pull_request:", "    paths:"]
            + ['      - "%s"' % path
               for path in sorted(DELEGATED_LOOKUP_WORKFLOW_TRIGGER_PATHS)]
            + ["  workflow_dispatch: {}", ""])
        complete_filters = workflow_path_filters(complete)
        self.assertTrue(complete_filters)
        for patterns in complete_filters.values():
            self.assertEqual(
                missing_trigger_paths(DELEGATED_LOOKUP_WORKFLOW_TRIGGER_PATHS, patterns), [],
                "the complete fixture must satisfy the closure before it is degraded")
        degraded = "\n".join(line for line in complete.splitlines()
                             if line.strip() != '- "%s"' % omitted)
        self.assertNotEqual(degraded, complete, "the fixture must actually lose an entry")
        degraded_filters = workflow_path_filters(degraded)
        self.assertTrue(degraded_filters)
        for patterns in degraded_filters.values():
            self.assertEqual(
                missing_trigger_paths(DELEGATED_LOOKUP_WORKFLOW_TRIGGER_PATHS, patterns),
                [omitted])

    # ---- A7-1: the exact closed-dependency contract ---- #
    def _dependency_module(self, prologue="", types_import=None, path_import=None,
                           root=None, registry=None, extra="", helpers=True, tail=""):
        """A module built from the INDEPENDENT canonical dependency declarations.

        Each declaration can be replaced individually, so a fixture mutates exactly one part of
        the boundary the sanctioned helpers close over.
        """
        body = ((CANONICAL_TYPES_IMPORT if types_import is None else types_import)
                + (CANONICAL_PATH_IMPORT if path_import is None else path_import)
                + (CANONICAL_ROOT_ANCHOR if root is None else root)
                + (CANONICAL_REGISTRY if registry is None else registry))
        if helpers:
            body += (CANONICAL_HELPER_SOURCES["repo_path"]
                     + CANONICAL_HELPER_SOURCES["read_repo_text"]
                     + CANONICAL_HELPER_SOURCES["read_scratch_text"])
        return prologue + body + extra + tail

    def test_canonical_dependency_source_pins_all_four_declarations(self):
        contract = canonical_dependency_contract()
        self.assertEqual(sorted(contract), sorted(CLOSED_DEPENDENCY_NAMES))
        # The canonical declarations are hand-maintained, not derived from the live nodes.
        self.assertNotIn("read_repo_text(", CANONICAL_DEPENDENCY_SOURCE)
        for key, value in (("probe_script", "scripts/ac2_member_expiry_capability_probe.ps1"),
                           ("focused_tests", "tests/test_ac2_member_expiry_capability_probe.py")):
            self.assertIn('"%s": "%s"' % (key, value), CANONICAL_REGISTRY)

    def test_real_module_satisfies_the_exact_closed_dependency_contract(self):
        source = read_repo_text("focused_tests")
        self.assertEqual(closed_dependency_violations(source), [],
                         "the live declarations must match the independent canonical contract")
        self.assertEqual(dynamic_namespace_violations(source), [],
                         "the live module must use no dynamic namespace route")

    def test_canonical_dependency_boundary_is_accepted_end_to_end(self):
        source = self._dependency_module(tail='text = read_repo_text("readme")\n')
        self.assertEqual(closed_dependency_violations(source), [])
        self.assertEqual(sanctioned_helper_contract_violations(source, require_all=True), [])
        self.assertEqual(repository_read_violations(source), [],
                         "the exact canonical boundary must keep its anchor and helper exemptions")

    def test_every_closed_dependency_mutation_is_rejected(self):
        alias_registry = CANONICAL_REGISTRY.replace("types.MappingProxyType", "t.MappingProxyType")
        cases = {
            "parents_zero": dict(root="ROOT = Path(__file__).resolve().parents[0]\n"),
            "parents_two": dict(root="ROOT = Path(__file__).resolve().parents[2]\n"),
            "path_cwd": dict(root="ROOT = Path.cwd()\n"),
            "path_parent": dict(root="ROOT = Path(__file__).parent\n"),
            "operator_selected_root": dict(
                root="ROOT = Path('/elsewhere') if FLAG else Path(__file__).resolve().parents[1]\n"),
            "path_import_alias": dict(path_import="from pathlib import Path as P\n",
                                      root="ROOT = P(__file__).resolve().parents[1]\n"),
            "module_qualified_path": dict(path_import="import pathlib\n",
                                          root="ROOT = pathlib.Path(__file__).resolve().parents[1]\n"),
            "path_rebound_before_anchor": dict(
                path_import="from pathlib import Path\nPath = replacement\n"),
            "dunder_file_rebound_before_anchor": dict(
                path_import="from pathlib import Path\n__file__ = '/elsewhere/tests/x.py'\n"),
            "types_import_missing": dict(types_import=""),
            "types_import_aliased": dict(types_import="import types as t\n",
                                         registry=alias_registry),
            "mutable_dictionary_registry": dict(registry=CANONICAL_REGISTRY.replace(
                "types.MappingProxyType({", "{").replace("})", "}")),
            "dict_constructor_registry": dict(
                registry='REPO_DEPENDENCIES = dict(readme="README.md")\n'),
            "proxy_around_helper_dictionary": dict(
                registry="REPO_DEPENDENCIES = types.MappingProxyType(build_registry())\n"),
            "registry_entry_missing": dict(registry=CANONICAL_REGISTRY.replace(
                '    "gitignore": ".gitignore",\n', "")),
            "registry_entry_added": dict(registry=CANONICAL_REGISTRY.replace(
                "})\n", '    "extra": "docs/extra.md",\n})\n')),
            "registry_key_changed": dict(registry=CANONICAL_REGISTRY.replace(
                '"readme":', '"read_me":')),
            "registry_value_changed": dict(registry=CANONICAL_REGISTRY.replace(
                '"readme": "README.md"', '"readme": "docs/README.md"')),
            "registry_pairing_swapped": dict(registry=CANONICAL_REGISTRY.replace(
                '"readme": "README.md",\n    "gitignore": ".gitignore",',
                '"readme": ".gitignore",\n    "gitignore": "README.md",')),
            "registry_unpacking": dict(registry=CANONICAL_REGISTRY.replace(
                "MappingProxyType({\n", "MappingProxyType({\n        **BASE,\n")),
            "registry_comprehension": dict(registry=(
                "REPO_DEPENDENCIES = types.MappingProxyType(\n"
                "    {key: value for key, value in PAIRS})\n")),
            "registry_inside_function": dict(registry=(
                "def build():\n"
                "    REPO_DEPENDENCIES = types.MappingProxyType({})\n"
                "    return REPO_DEPENDENCIES\n")),
            "registry_inside_conditional": dict(registry=(
                "if FLAG:\n"
                "    REPO_DEPENDENCIES = types.MappingProxyType({})\n")),
            "duplicate_root": dict(root=CANONICAL_ROOT_ANCHOR + CANONICAL_ROOT_ANCHOR),
            "duplicate_registry": dict(registry=CANONICAL_REGISTRY + CANONICAL_REGISTRY),
            "root_then_rebinding": dict(root=CANONICAL_ROOT_ANCHOR + "ROOT = Path('/elsewhere')\n"),
            "registry_then_rebinding": dict(
                registry=CANONICAL_REGISTRY + "REPO_DEPENDENCIES = {}\n"),
            "registry_before_types_import": dict(types_import="", path_import="",
                                                 root="", registry=(
                CANONICAL_REGISTRY + CANONICAL_TYPES_IMPORT + CANONICAL_PATH_IMPORT
                + CANONICAL_ROOT_ANCHOR)),
            "root_before_path_import": dict(types_import=CANONICAL_TYPES_IMPORT,
                                            path_import="", root=(
                CANONICAL_ROOT_ANCHOR + CANONICAL_PATH_IMPORT)),
        }
        self.assertGreaterEqual(len(cases), 26, "the A7-1 dependency matrix must stay complete")
        expected_categories = ("closed_dependency_missing", "closed_dependency_duplicate",
                               "closed_dependency_body_mismatch", "closed_dependency_not_top_level",
                               "closed_dependency_rebound", "closed_dependency_out_of_order")
        accepted = []
        kept_authority = []
        for name, kwargs in cases.items():
            source = self._dependency_module(tail='text = read_repo_text("readme")\n', **kwargs)
            problems = closed_dependency_violations(source)
            if not problems:
                accepted.append(name)
                continue
            self.assertTrue(any(problem.endswith(expected_categories) for problem in problems),
                            "%s: unexpected categories %s" % (name, problems))
            # Authority gating: with the boundary broken, the anchor derivation and the helper
            # bodies' own reads must become visible to the main guard.
            guard = repository_read_violations(source)
            if not any(violation.endswith(("root_path_derivation", "unresolved_repository_read",
                                           "path_constructor_from_root", "repository_path_escape"))
                       for violation in guard):
                kept_authority.append((name, guard))
        self.assertEqual(accepted, [], "these dependency mutations were accepted: %s" % accepted)
        self.assertEqual(kept_authority, [],
                         "these mutations kept anchor or helper authority: %s" % kept_authority)

    def test_broken_dependency_boundary_withholds_literal_call_authority(self):
        clean = self._dependency_module(tail='text = read_repo_text("readme")\n')
        self.assertEqual(repository_read_violations(clean), [],
                         "the exact boundary must keep literal registry-read authority")
        widened = self._dependency_module(
            root="ROOT = Path(__file__).resolve().parents[0]\n",
            tail='text = read_repo_text("readme")\n')
        violations = repository_read_violations(widened)
        self.assertTrue(any(v.endswith("closed_dependency_body_mismatch") for v in violations),
                        violations)
        self.assertTrue(any(v.endswith("unresolved_repository_read") for v in violations),
                        "a widened root must withhold read authority: %s" % violations)
        self.assertTrue(any(v.endswith("root_path_derivation") for v in violations),
                        "the helper's own root derivation must become visible: %s" % violations)

    # ---- A7-2: dynamic namespace and binding integrity ---- #
    def test_every_dynamic_namespace_route_is_rejected(self):
        prologue = "import sys\nfrom helpers import replacement\n"
        cases = {
            "globals_subscript_reader": 'globals()["read_repo_text"] = replacement\n',
            "globals_subscript_repo_path": 'globals()["repo_path"] = replacement\n',
            "globals_subscript_root": 'globals()["ROOT"] = replacement\n',
            "globals_update": 'globals().update({"read_repo_text": replacement})\n',
            "globals_setitem": 'globals().__setitem__("read_repo_text", replacement)\n',
            "globals_dynamic_key": 'name = "read_repo_text"\nglobals()[name] = replacement\n',
            "exec_binding": 'exec("read_repo_text = replacement")\n',
            "eval_binding": 'value = eval("read_repo_text")\n',
            "compile_then_store": 'code = compile("x = 1", "<f>", "exec")\n',
            "module_dict_subscript": 'sys.modules[__name__].__dict__["read_repo_text"] = replacement\n',
            "module_dict_update": 'sys.modules[__name__].__dict__.update({"repo_path": replacement})\n',
            "setattr_module": 'setattr(sys.modules[__name__], "read_repo_text", replacement)\n',
            "delattr_module": 'delattr(sys.modules[__name__], "read_repo_text")\n',
            "setattr_unresolved_sanctioned_attribute": 'setattr(holder, "read_repo_text", replacement)\n',
            "namespace_passed_to_callable": "register(globals())\n",
            "namespace_returned": "def expose():\n    return globals()\n",
            "namespace_stored": "holders = [globals()]\n",
            "locals_mutation": 'locals().update({"read_repo_text": replacement})\n',
            "vars_mutation": 'vars().update({"read_repo_text": replacement})\n',
        }
        self.assertGreaterEqual(len(cases), 18, "the A7-2 namespace matrix must stay complete")
        expected_categories = ("dynamic_namespace_binding", "dynamic_namespace_escape",
                               "sanctioned_helper_dynamic_binding",
                               "closed_dependency_dynamic_binding")
        accepted = []
        for name, body in cases.items():
            source = self._dependency_module(prologue=prologue, extra=body)
            problems = dynamic_namespace_violations(source)
            if not problems:
                accepted.append(name)
                continue
            self.assertTrue(any(problem.endswith(expected_categories) for problem in problems),
                            "%s: unexpected categories %s" % (name, problems))
            guard = repository_read_violations(source)
            self.assertTrue(any(violation.endswith(expected_categories) for violation in guard),
                            "%s must surface through the main guard: %s" % (name, guard))
        self.assertEqual(accepted, [], "these namespace routes were accepted: %s" % accepted)

    def test_dynamic_replacement_withholds_literal_sanctioned_call_authority(self):
        prologue = "from helpers import replacement\n"
        clean = self._dependency_module(
            prologue=prologue, tail='text = read_repo_text("readme")\n')
        self.assertEqual(repository_read_violations(clean), [])
        tampered = self._dependency_module(
            prologue=prologue,
            tail=('globals()["repo_path"] = replacement\n'
                  'text = read_repo_text("readme")\n'))
        violations = repository_read_violations(tampered)
        self.assertTrue(any(v.endswith("sanctioned_helper_dynamic_binding") for v in violations),
                        violations)
        self.assertTrue(any(v.endswith(("unresolved_repository_read", "root_path_derivation"))
                            for v in violations),
                        "a dynamically replaced helper must lose literal authority: %s" % violations)
        literal = self._dependency_module(
            prologue=prologue,
            tail=('globals()["read_repo_text"] = replacement\n'
                  'text = read_repo_text("probe_script")\n'))
        literal_violations = repository_read_violations(literal)
        self.assertTrue(any(v.endswith("sanctioned_helper_dynamic_binding")
                            for v in literal_violations), literal_violations)
        self.assertTrue(any(v.endswith(("unresolved_repository_read", "root_path_derivation"))
                            for v in literal_violations),
                        "the replaced helper's own body must become visible: %s"
                        % literal_violations)

    # ---- A7-3: recursive return summaries and definition identity ---- #
    def _return_summary_module(self, body):
        return self._dependency_module(
            prologue="from helpers import slurp, sink\n", helpers=False,
            extra="SCRIPT = repo_path('probe_script')\n" + body)

    def test_container_returned_paths_and_readers_propagate(self):
        hops = "".join("def hop%d():\n    return [hop%d()]\n" % (index + 1, index)
                       for index in range(9))
        cases = {
            "list_of_paths": ("def box():\n    return [SCRIPT]\nslurp(box()[0])\n",
                              "repository_path_escape"),
            "tuple_of_paths": ("def box():\n    return (SCRIPT,)\nslurp(box()[0])\n",
                               "repository_path_escape"),
            "set_of_paths": ("def box():\n    return {SCRIPT}\nslurp(box())\n",
                             "repository_path_escape"),
            "dict_key_path": ("def box():\n    return {SCRIPT: 'k'}\nslurp(box())\n",
                              "repository_path_escape"),
            "dict_value_path": ("def box():\n    return {'p': SCRIPT}\nslurp(box())\n",
                                "repository_path_escape"),
            "nested_path_containers": ("def box():\n    return {'n': [(SCRIPT,)]}\nslurp(box())\n",
                                       "repository_path_escape"),
            "starred_container": ("paths = [SCRIPT]\ndef box():\n    return [*paths]\n"
                                  "slurp(box())\n", "repository_path_escape"),
            "comprehension_return": ("def box():\n    return [p for p in [SCRIPT]]\n"
                                     "slurp(box())\n", "repository_path_escape"),
            "generator_return": ("def box():\n    return (p for p in [SCRIPT])\n"
                                 "slurp(box())\n", "repository_path_escape"),
            "fstring_return": ("def box():\n    return f'{SCRIPT}'\nslurp(box())\n",
                               "repository_path_escape"),
            "conditional_return": ("def box():\n    return SCRIPT if FLAG else 'plain'\n"
                                   "slurp(box())\n", "repository_path_escape"),
            "named_expression_return": ("def box():\n    return (held := SCRIPT)\n"
                                        "slurp(box())\n", "repository_path_escape"),
            "lambda_path_container": ("box = lambda: [SCRIPT]\nslurp(box()[0])\n",
                                      "repository_path_escape"),
            "list_of_readers": ("def rbox():\n    return [open]\nsink(rbox())\n",
                                "reader_callable_escape"),
            "dict_of_readers": ("def rbox():\n    return {'reader': open}\n"
                                "rbox()['reader'](SCRIPT)\n", "reader_callable_invocation"),
            "nested_reader_containers": ("def rbox():\n    return [{'reader': open}]\n"
                                         "sink(rbox())\n", "reader_callable_escape"),
            "async_reader_container": ("async def rbox():\n    return [open]\nsink(rbox())\n",
                                       "reader_callable_escape"),
            "long_container_hop_chain": ("def hop0():\n    return [SCRIPT]\n" + hops
                                         + "slurp(hop9())\n", "repository_path_escape"),
            "mixed_path_and_reader_chain": ("def first():\n    return {'nested': [SCRIPT]}\n"
                                            "def second():\n    return (first(),)\n"
                                            "def third():\n    return [second(), open]\n"
                                            "sink(third())\n", "reader_callable_escape"),
            "mixed_chain_path_leg": ("def first():\n    return {'nested': [SCRIPT]}\n"
                                     "def second():\n    return (first(),)\n"
                                     "slurp(second())\n", "repository_path_escape"),
        }
        self.assertGreaterEqual(len(cases), 20, "the A7-3 return matrix must stay complete")
        accepted = []
        for name, (body, expected) in cases.items():
            violations = repository_read_violations(self._return_summary_module(body))
            if not violations:
                accepted.append(name)
                continue
            self.assertTrue(any(violation.endswith(expected) for violation in violations),
                            "%s: expected %s, got %s" % (name, expected, violations))
        self.assertEqual(accepted, [], "these container returns were accepted: %s" % accepted)

    def test_safe_container_returns_and_chains_stay_clean(self):
        controls = {
            "literal_container": "def box():\n    return ['plain', ('a', 1)]\nslurp(box()[0])\n",
            "literal_dict": "def box():\n    return {'k': 'plain'}\nslurp(box())\n",
            "safe_helper_chain": ("def first():\n    return 'plain'\n"
                                  "def second():\n    return [first()]\nslurp(second())\n"),
            "unrelated_sibling_definitions": ("def one():\n    return 'a'\n"
                                              "def two():\n    return 'b'\n"
                                              "slurp([one(), two()])\n"),
        }
        for name, body in controls.items():
            self.assertEqual(repository_read_violations(self._return_summary_module(body)), [],
                             "%s must stay clean" % name)

    def test_lexical_definition_identity_keeps_same_named_functions_apart(self):
        siblings = ("def one():\n"
                    "    def load():\n        return SCRIPT\n"
                    "    return slurp(load())\n"
                    "def two():\n"
                    "    def load():\n        return 'plain'\n"
                    "    return slurp(load())\n")
        source = self._return_summary_module(siblings)
        violations = repository_read_violations(source)
        reported = {int(violation.split(":", 1)[0]) for violation in violations}
        lines = source.splitlines()
        tainted_call = lines.index("    return slurp(load())") + 1
        safe_call = lines.index("    return slurp(load())", lines.index("def two():")) + 1
        self.assertIn(tainted_call, reported,
                      "the tainted sibling's call must be reported: %s" % violations)
        self.assertNotIn(safe_call, reported,
                         "the safe sibling must contribute nothing: %s" % violations)
        self.assertTrue(all(violation.endswith("repository_path_escape")
                            for violation in violations), violations)
        nested = ("def load():\n    return 'plain'\n"
                  "def outer():\n"
                  "    def load():\n        return SCRIPT\n"
                  "    return slurp(load())\n"
                  "safe = slurp(load())\n")
        source = self._return_summary_module(nested)
        violations = repository_read_violations(source)
        reported = {int(violation.split(":", 1)[0]) for violation in violations}
        lines = source.splitlines()
        self.assertIn(lines.index("    return slurp(load())") + 1, reported, violations)
        self.assertNotIn(lines.index("safe = slurp(load())") + 1, reported,
                         "a nested definition must not mark the module function: %s" % violations)
        rebound = ("def load():\n    return 'plain'\n"
                   "load = chooser\n"
                   "slurp(load())\n")
        self.assertTrue(repository_read_violations(self._return_summary_module(rebound)),
                        "a rebound same-name function must fail closed")

    # ---- A7-4: lexical, ordered default-name proof ---- #
    def _default_module(self, body):
        return self._dependency_module(
            prologue="from helpers import slurp\nfrom config import CONFIG_PATH\n",
            helpers=False, extra=body)

    def test_default_names_resolve_only_through_the_relevant_lexical_scope(self):
        cases = {
            "sibling_function_local": ("def other():\n    NAME = 'plain'\n    return NAME\n"
                                       "def load(p=NAME):\n    return slurp(p)\nload()\n"),
            "nested_function_local": ("def other():\n"
                                      "    def inner():\n        NAME = 'plain'\n"
                                      "    return inner\n"
                                      "def load(p=NAME):\n    return slurp(p)\nload()\n"),
            "later_module_assignment": ("def load(p=NAME):\n    return slurp(p)\n"
                                        "NAME = 'plain'\nload()\n"),
            "two_prior_module_bindings": ("NAME = 'a'\nNAME = 'b'\n"
                                          "def load(p=NAME):\n    return slurp(p)\nload()\n"),
            "conditional_prior_binding": ("if FLAG:\n    NAME = 'plain'\n"
                                          "def load(p=NAME):\n    return slurp(p)\nload()\n"),
            "enclosing_assignment_after_definition": ("def outer():\n"
                                                      "    def load(p=NAME):\n"
                                                      "        return slurp(p)\n"
                                                      "    NAME = 'plain'\n"
                                                      "    return load\n"),
            "sibling_scope_local_for_nested_default": ("def sibling():\n    NAME = 'plain'\n"
                                                       "def outer():\n"
                                                       "    def load(p=NAME):\n"
                                                       "        return slurp(p)\n"
                                                       "    return load\n"),
            "branch_dependent_enclosing_binding": ("def outer(flag):\n"
                                                   "    if flag:\n        NAME = 'plain'\n"
                                                   "    def load(p=NAME):\n"
                                                   "        return slurp(p)\n"
                                                   "    return load\n"),
            "loop_assigned_enclosing_binding": ("def outer(items):\n"
                                                "    for NAME in items:\n        pass\n"
                                                "    def load(p=NAME):\n"
                                                "        return slurp(p)\n"
                                                "    return load\n"),
            "enclosing_parameter": ("def outer(NAME):\n"
                                    "    def load(p=NAME):\n        return slurp(p)\n"
                                    "    return load\n"),
            "imported_name": "def load(p=CONFIG_PATH):\n    return slurp(p)\nload()\n",
            "cyclic_pair": ("first = second\nsecond = first\n"
                            "def load(p=first):\n    return slurp(p)\nload()\n"),
            "constructor_shadowed_by_assignment": ("tuple = chooser\n"
                                                   "def load(p=tuple(['a'])):\n"
                                                   "    return slurp(p)\nload()\n"),
            "constructor_shadowed_by_import": ("from helpers import dict\n"
                                               "def load(p=dict(a=1)):\n"
                                               "    return slurp(p)\nload()\n"),
            "constructor_shadowed_by_parameter": ("def outer(dict):\n"
                                                  "    def load(p=dict(a=1)):\n"
                                                  "        return slurp(p)\n"
                                                  "    return load\n"),
            "class_body_assignment": ("class Holder:\n    NAME = 'plain'\n"
                                      "def load(p=NAME):\n    return slurp(p)\nload()\n"),
        }
        self.assertGreaterEqual(len(cases), 15, "the A7-4 default matrix must stay complete")
        accepted = []
        for name, body in cases.items():
            violations = repository_read_violations(self._default_module(body))
            if not violations:
                accepted.append(name)
                continue
            self.assertTrue(any(violation.endswith(("ambiguous_default_binding",
                                                    "repository_path_escape",
                                                    "reader_callable_escape"))
                                for violation in violations),
                            "%s: unexpected categories %s" % (name, violations))
        self.assertEqual(accepted, [], "these unresolved defaults were accepted: %s" % accepted)

    def test_scope_aware_default_positive_controls_are_accepted(self):
        controls = {
            "unique_module_constant": ("SAFE = 'plain'\n"
                                       "def load(p=SAFE):\n    return slurp(p)\nload()\n"),
            "module_literal_container": ("SAFE = ['plain', ('a', 1)]\n"
                                         "def load(p=SAFE):\n    return slurp(p)\nload()\n"),
            "unconditional_enclosing_assignment": ("def outer():\n"
                                                   "    NAME = 'plain'\n"
                                                   "    def load(p=NAME):\n"
                                                   "        return slurp(p)\n"
                                                   "    return load\n"),
            "sibling_scopes_share_a_name": ("def one():\n"
                                            "    NAME = 'a'\n"
                                            "    def load(p=NAME):\n        return slurp(p)\n"
                                            "    return load\n"
                                            "def two():\n"
                                            "    NAME = 'b'\n"
                                            "    def load(p=NAME):\n        return slurp(p)\n"
                                            "    return load\n"),
            "unshadowed_container_constructor": ("def load(p=tuple(['a'])):\n"
                                                 "    return slurp(p)\nload()\n"),
        }
        for name, body in controls.items():
            self.assertEqual(repository_read_violations(self._default_module(body)), [],
                             "%s must be accepted as provably safe" % name)
        # Real-module constant defaults remain accepted by the scope-aware resolver.
        self.assertEqual(repository_read_violations(read_repo_text("focused_tests")), [])

    # ---- A7-5: type comments are parsed and contract-bearing ---- #
    def test_parser_captures_function_type_comments(self):
        annotated = ("def read_repo_text(key):\n"
                     "    # type: (str) -> str\n"
                     '    return repo_path(key).read_text(encoding="utf-8")\n')
        captured = parse_source(annotated).body[0]
        self.assertEqual(captured.type_comment, "(str) -> str",
                         "the centralised parser must capture function type comments")
        self.assertIsNone(ast.parse(annotated).body[0].type_comment,
                          "plain ast.parse discards the field, which is why it is not used")

    def test_all_module_parsing_is_centralised_through_the_type_comment_parser(self):
        tree = parse_source(read_repo_text("focused_tests"))
        # parse_source and its capability probe own the option; the one test that documents what
        # plain ast.parse discards must obviously still call it.
        owners = ("parse_source", "_type_comments_supported",
                  "test_parser_captures_function_type_comments")
        allowed = set()
        for statement in ast.walk(tree):
            if isinstance(statement, ast.FunctionDef) and statement.name in owners:
                for node in ast.walk(statement):
                    allowed.add(id(node))
        offenders = [node.lineno for node in ast.walk(tree)
                     if isinstance(node, ast.Call) and id(node) not in allowed
                     and isinstance(node.func, ast.Attribute) and node.func.attr == "parse"
                     and isinstance(node.func.value, ast.Name) and node.func.value.id == "ast"]
        self.assertEqual(offenders, [],
                         "every analysed source must be parsed through parse_source")

    def test_type_comment_mutations_invalidate_the_helper_contract(self):
        cases = {
            "type_comment_added": dict(read_repo_text_src=(
                "def read_repo_text(key):\n"
                "    # type: (str) -> str\n"
                '    return repo_path(key).read_text(encoding="utf-8")\n')),
            "argument_type_changed": dict(read_repo_text_src=(
                "def read_repo_text(key):\n"
                "    # type: (bytes) -> str\n"
                '    return repo_path(key).read_text(encoding="utf-8")\n')),
            "return_type_changed": dict(read_repo_text_src=(
                "def read_repo_text(key):\n"
                "    # type: (str) -> bytes\n"
                '    return repo_path(key).read_text(encoding="utf-8")\n')),
            "type_comment_moved_to_another_helper": dict(read_scratch_text_src=(
                "def read_scratch_text(path):\n"
                "    # type: (str) -> str\n"
                "    resolved = Path(path).resolve()\n"
                "    if resolved == ROOT or ROOT in resolved.parents:\n"
                '        raise AssertionError("the scratch reader refuses a repository path: %s" % resolved)\n'
                '    return resolved.read_text(encoding="utf-8")\n')),
        }
        accepted = []
        for name, kwargs in cases.items():
            source = self._helper_module(**kwargs)
            problems = sanctioned_helper_contract_violations(source, require_all=True)
            if not problems:
                accepted.append(name)
                continue
            self.assertTrue(any(problem.endswith("sanctioned_helper_body_mismatch")
                                for problem in problems), "%s: %s" % (name, problems))
            self.assertTrue(repository_read_violations(source),
                            "%s must lose its exemption" % name)
        self.assertEqual(accepted, [], "these type-comment mutations were accepted: %s" % accepted)
        # Two differing signatures must not produce equal contracts, and neither may equal the
        # canonical (comment-free) helper.
        first = parse_source("def read_repo_text(key):\n"
                             "    # type: (str) -> str\n"
                             "    return 1\n").body[0]
        second = parse_source("def read_repo_text(key):\n"
                              "    # type: (bytes) -> bytes\n"
                              "    return 1\n").body[0]
        bare = parse_source("def read_repo_text(key):\n    return 1\n").body[0]
        contracts = [_helper_definition_contract(node) for node in (first, second, bare)]
        self.assertEqual(len(set(contracts)), 3,
                         "added, changed and absent type comments must all differ")

    def test_parser_fails_closed_without_type_comment_support(self):
        annotated = ("def read_repo_text(key):\n"
                     "    # type: (str) -> str\n"
                     "    return 1\n")
        with self.assertRaises(AssertionError):
            parse_source(annotated, supported=False)
        # A source with no type-comment syntax may still be analysed on such a runtime.
        self.assertTrue(parse_source("value = 1\n", supported=False).body)

    # ---- A8-1: protected closed-dependency OBJECT semantics ---- #
    def _authority_withheld(self, source):
        """Violations proving the boundary collapsed rather than a side finding being added.

        With the boundary intact these cannot appear: the root anchor and the three exact helper
        bodies are exempt. Once a protected object can be patched or a dangerous callable
        captured, both exemptions must be withheld and the helpers' own derivations and reads
        must become visible again.
        """
        return [violation for violation in repository_read_violations(source)
                if violation.endswith(("root_path_derivation", "unresolved_repository_read",
                                       "path_constructor_from_root", "repository_path_escape"))]

    def test_real_module_keeps_protected_object_and_builtin_authority_clean(self):
        source = read_repo_text("focused_tests")
        self.assertEqual(protected_authority_violations(source), [],
                         "the live module's canonical Path and types uses must stay clean")
        # The canonical boundary plus a literal registered-key read stays fully authorised.
        canonical = self._dependency_module(tail='text = read_repo_text("readme")\n')
        self.assertEqual(protected_authority_violations(canonical), [])
        self.assertEqual(repository_read_violations(canonical), [])

    def test_every_documented_protected_path_semantic_is_bound(self):
        # The minimum protected surface: construction, resolution, parent walking, the readers,
        # path joining -- plus any unresolved attribute, which fails closed by the same rule.
        for attribute in sorted(PROTECTED_PATH_SEMANTICS) + ["totally_unreviewed_attribute"]:
            source = self._dependency_module(
                prologue="from helpers import replacement\n",
                tail="Path.%s = replacement\n" % attribute
                + 'text = read_repo_text("probe_script")\n')
            problems = protected_authority_violations(source)
            self.assertTrue(any(problem.endswith("closed_dependency_object_mutation")
                                for problem in problems),
                            "Path.%s must be protected: %s" % (attribute, problems))
            self.assertTrue(self._authority_withheld(source),
                            "Path.%s mutation must withhold every exemption" % attribute)
        for attribute in sorted(PROTECTED_TYPES_SEMANTICS):
            source = self._dependency_module(
                prologue="from helpers import replacement\n",
                tail="types.%s = replacement\n" % attribute
                + 'text = read_repo_text("probe_script")\n')
            self.assertTrue(any(problem.endswith("closed_dependency_object_mutation")
                                for problem in protected_authority_violations(source)))
            self.assertTrue(self._authority_withheld(source))

    def test_every_protected_object_mutation_route_is_rejected(self):
        prologue = "from helpers import replacement, mutate, pick\n"
        chain = "P0 = Path\nP1 = P0\nP2 = P1\nP2.resolve = replacement\n"
        types_chain = "T0 = types\nT1 = T0\nT1.MappingProxyType = replacement\n"
        cases = {
            "path_resolve_assigned": "Path.resolve = replacement\n",
            "path_read_text_assigned": "Path.read_text = replacement\n",
            "path_truediv_assigned": "Path.__truediv__ = replacement\n",
            "path_new_assigned": "Path.__new__ = replacement\n",
            "path_attribute_deleted": "del Path.resolve\n",
            "path_attribute_augmented": "Path.resolve += replacement\n",
            "path_alias_mutated": "P = Path\nP.resolve = replacement\n",
            "path_alias_chain_mutated": chain,
            "path_annotated_alias_mutated": "P: object = Path\nP.resolve = replacement\n",
            "path_named_expression_alias": "value = (P := Path)\nP.resolve = replacement\n",
            "path_destructured_alias": "P, other = Path, 1\nP.resolve = replacement\n",
            "path_setattr": 'setattr(Path, "resolve", replacement)\n',
            "path_setattr_alias": 'assign = setattr\nassign(Path, "resolve", replacement)\n',
            "path_delattr": 'delattr(Path, "resolve")\n',
            "path_delattr_alias": 'drop = delattr\ndrop(Path, "resolve")\n',
            "path_object_setattr": 'object.__setattr__(Path, "resolve", replacement)\n',
            "path_type_setattr": 'type.__setattr__(Path, "resolve", replacement)\n',
            "path_class_dict_subscript": 'Path.__dict__["resolve"] = replacement\n',
            "path_class_dict_update": 'Path.__dict__.update({"resolve": replacement})\n',
            "path_vars_subscript": 'vars(Path)["resolve"] = replacement\n',
            "path_dynamic_attribute": 'name = "resolve"\nsetattr(Path, name, replacement)\n',
            "path_wrapper_returned": ("def source():\n    return Path\n"
                                      "source().resolve = replacement\n"),
            "path_container_recovered": "holder = [Path]\nholder[0].resolve = replacement\n",
            "path_passed_to_mutator": "mutate(Path)\n",
            "types_mapping_proxy_assigned": "types.MappingProxyType = replacement\n",
            "types_mapping_proxy_deleted": "del types.MappingProxyType\n",
            "types_alias_mutated": "t = types\nt.MappingProxyType = replacement\n",
            "types_alias_chain_mutated": types_chain,
            "types_setattr": 'setattr(types, "MappingProxyType", replacement)\n',
            "types_module_dict_subscript": 'types.__dict__["MappingProxyType"] = replacement\n',
            "types_vars_subscript": 'vars(types)["MappingProxyType"] = replacement\n',
            "types_dynamic_attribute": 'name = "MappingProxyType"\nsetattr(types, name, replacement)\n',
            "types_passed_to_mutator": "mutate(types)\n",
        }
        self.assertGreaterEqual(len(cases), 32, "the A8-1 protected-object matrix must stay complete")
        expected = ("closed_dependency_object_mutation", "closed_dependency_object_escape",
                    "closed_dependency_object_alias",
                    "closed_dependency_object_dynamic_mutation")
        accepted = []
        kept_authority = []
        for name, body in cases.items():
            source = self._dependency_module(
                prologue=prologue, tail=body + 'text = read_repo_text("probe_script")\n')
            problems = protected_authority_violations(source)
            if not problems:
                accepted.append(name)
                continue
            self.assertTrue(any(problem.endswith(expected) for problem in problems),
                            "%s: unexpected categories %s" % (name, problems))
            guard = repository_read_violations(source)
            self.assertTrue(any(violation.endswith(expected) for violation in guard),
                            "%s must surface through the main guard: %s" % (name, guard))
            if not self._authority_withheld(source):
                kept_authority.append((name, guard))
        self.assertEqual(accepted, [], "these protected-object routes were accepted: %s" % accepted)
        self.assertEqual(kept_authority, [],
                         "these routes kept anchor or helper authority: %s" % kept_authority)

    def test_protected_object_mutation_withholds_literal_call_authority(self):
        prologue = "from helpers import replacement\n"
        clean = self._dependency_module(
            prologue=prologue, tail='text = read_repo_text("probe_script")\n')
        self.assertEqual(repository_read_violations(clean), [],
                         "an unpatched boundary keeps literal registered-key authority")
        patched = self._dependency_module(
            prologue=prologue,
            tail="Path.resolve = replacement\n" + 'text = read_repo_text("probe_script")\n')
        violations = repository_read_violations(patched)
        self.assertTrue(any(v.endswith("closed_dependency_object_mutation") for v in violations),
                        violations)
        self.assertTrue(any(v.endswith("root_path_derivation") for v in violations),
                        "the helper's root derivation must become visible: %s" % violations)
        self.assertTrue(any(v.endswith("unresolved_repository_read") for v in violations),
                        "the helper's own read must become visible: %s" % violations)
        registry = self._dependency_module(
            prologue=prologue,
            tail="types.MappingProxyType = replacement\n"
            + 'text = read_repo_text("probe_script")\n')
        registry_violations = repository_read_violations(registry)
        self.assertTrue(any(v.endswith("closed_dependency_object_mutation")
                            for v in registry_violations), registry_violations)
        self.assertTrue(any(v.endswith("root_path_derivation") for v in registry_violations),
                        "a replaced registry constructor must withhold authority: %s"
                        % registry_violations)

    # ---- A8-2: dangerous builtin callable authority ---- #
    def test_every_dangerous_builtin_authority_route_is_rejected(self):
        prologue = "import builtins\nfrom helpers import wrapper, replacement\n"
        long_chain = ("e0 = exec\n" + "".join("e%d = e%d\n" % (i + 1, i) for i in range(5)))
        cases = {
            "globals_alias": "g = globals\n",
            "globals_annotated_alias": "g: object = globals\n",
            "globals_named_expression_alias": "value = (g := globals)\n",
            "globals_destructured_alias": "g, other = globals, 1\n",
            "globals_alias_chain": "g0 = globals\ng1 = g0\n",
            "globals_import_alias": "from builtins import globals as g\n",
            "globals_module_attribute": "g = builtins.globals\n",
            "globals_builtins_subscript": 'g = __builtins__["globals"]\n',
            "globals_getattr": 'g = getattr(builtins, "globals")\n',
            "globals_dynamic_selection": 'name = "globals"\ng = getattr(builtins, name)\n',
            "globals_wrapper_return": "def source():\n    return globals\n",
            "globals_lambda_return": "source = lambda: globals\n",
            "globals_container": "holder = [globals]\n",
            "globals_default_parameter": "def load(producer=globals):\n    return producer\n",
            "globals_closure_capture": ("def outer():\n    captured = globals\n"
                                        "    def inner():\n        return captured()\n"
                                        "    return inner\n"),
            "exec_alias": "e = exec\n",
            "exec_import_alias": "from builtins import exec as run\n",
            "exec_module_attribute": "e = builtins.exec\n",
            "exec_builtins_subscript": 'e = __builtins__["exec"]\n',
            "exec_getattr": 'e = getattr(builtins, "exec")\n',
            "exec_wrapper_return": "def source():\n    return exec\n",
            "exec_container": "holder = (exec,)\n",
            "exec_long_alias_chain": long_chain,
            "eval_alias": "value = eval\n",
            "compile_alias": "builder = compile\n",
            "setattr_alias": "assign = setattr\n",
            "delattr_alias": "drop = delattr\n",
            "parameter_receives_builtin": ("def take(callable_argument):\n"
                                           "    return callable_argument\ntake(eval)\n"),
            "star_args_forwarding": "payload = [globals]\nwrapper(*payload)\n",
            "star_kwargs_forwarding": 'options = {"producer": delattr}\nwrapper(**options)\n',
            "locals_alias": "collect = locals\n",
            "vars_alias": "inspect_namespace = vars\n",
        }
        self.assertGreaterEqual(len(cases), 30, "the A8-2 builtin matrix must stay complete")
        expected = ("dangerous_builtin_alias", "dangerous_builtin_escape",
                    "dangerous_builtin_dynamic_selection")
        accepted = []
        kept_authority = []
        for name, body in cases.items():
            source = self._dependency_module(
                prologue=prologue, tail=body + 'text = read_repo_text("probe_script")\n')
            problems = protected_authority_violations(source)
            if not problems:
                accepted.append(name)
                continue
            self.assertTrue(any(problem.endswith(expected) for problem in problems),
                            "%s: unexpected categories %s" % (name, problems))
            guard = repository_read_violations(source)
            self.assertTrue(any(violation.endswith(expected) for violation in guard),
                            "%s must surface through the main guard: %s" % (name, guard))
            if not self._authority_withheld(source):
                kept_authority.append((name, guard))
        self.assertEqual(accepted, [], "these builtin authority routes were accepted: %s" % accepted)
        self.assertEqual(kept_authority, [],
                         "these routes kept anchor or helper authority: %s" % kept_authority)

    def test_dangerous_builtin_alias_replacement_loses_literal_call_authority(self):
        prologue = "from helpers import replacement\n"
        aliased_globals = self._dependency_module(
            prologue=prologue,
            tail=("g = globals\n"
                  'g()["read_repo_text"] = replacement\n'
                  'text = read_repo_text("probe_script")\n'))
        violations = repository_read_violations(aliased_globals)
        self.assertTrue(any(v.endswith("dangerous_builtin_alias") for v in violations), violations)
        self.assertTrue(any(v.endswith(("root_path_derivation", "unresolved_repository_read"))
                            for v in violations),
                        "the aliased namespace producer must withhold literal authority: %s"
                        % violations)
        aliased_exec = self._dependency_module(
            prologue=prologue,
            tail=("from builtins import exec as run\n"
                  'run("read_repo_text = replacement")\n'
                  'text = read_repo_text("probe_script")\n'))
        exec_violations = repository_read_violations(aliased_exec)
        self.assertTrue(any(v.endswith("dangerous_builtin_alias") for v in exec_violations),
                        exec_violations)
        self.assertTrue(any(v.endswith(("root_path_derivation", "unresolved_repository_read"))
                            for v in exec_violations),
                        "an imported exec alias must withhold literal authority: %s"
                        % exec_violations)

    def test_safe_lookalike_callables_and_attribute_calls_stay_clean(self):
        controls = {
            "regex_compile_attribute": "import re\npattern = re.compile('x')\n",
            "shadowed_builtin_name": ("def compile(text):\n    return text\n"
                                      "value = compile('x')\n"),
            "shadowed_vars_name": ("def vars(record):\n    return record\n"
                                   "value = vars({'a': 1})\n"),
            "unrelated_sibling_scopes": ("def one():\n"
                                         "    def helper():\n        return 'a'\n"
                                         "    return helper()\n"
                                         "def two():\n"
                                         "    def helper():\n        return 'b'\n"
                                         "    return helper()\n"),
            "plain_builtins_import": "import builtins\nvalue = builtins.len('abc')\n",
        }
        for name, body in controls.items():
            source = self._dependency_module(
                tail=body + 'text = read_repo_text("readme")\n')
            self.assertEqual(protected_authority_violations(source), [],
                             "%s must not be treated as dangerous authority" % name)
            self.assertEqual(repository_read_violations(source), [],
                             "%s must stay clean end to end" % name)

    def test_direct_builtin_callee_forms_keep_their_existing_treatment(self):
        # A8-2 must not reclassify the exact direct-callee forms A7 already fails closed on.
        for body, category in (('globals()["read_repo_text"] = replacement\n',
                                "sanctioned_helper_dynamic_binding"),
                               ('exec("read_repo_text = replacement")\n',
                                "dynamic_namespace_binding")):
            source = self._dependency_module(
                prologue="from helpers import replacement\n",
                tail=body + 'text = read_repo_text("probe_script")\n')
            self.assertEqual(protected_authority_violations(source), [],
                             "a direct builtin callee is not an alias: %s" % body.strip())
            violations = repository_read_violations(source)
            self.assertTrue(any(v.endswith(category) for v in violations), violations)
            self.assertTrue(self._authority_withheld(source),
                            "the existing dynamic-namespace route must still collapse authority")

    # ---- A2-4: literal exact-head CI checkout ---- #
    def test_both_jobs_check_out_the_literal_exact_head(self):
        self.assertEqual(checkout_head_binding_problems(self.workflow), [])
        blocks = workflow_job_blocks(self.workflow)
        self.assertEqual(sorted(blocks), ["ubuntu-focused", "windows-full-suite"])

    def test_checkout_ref_resolves_pr_head_for_pull_request_and_github_sha_otherwise(self):
        for job, block in workflow_job_blocks(self.workflow).items():
            checkout_step = block.split("actions/checkout@", 1)[1].split("\n      - ", 1)[0]
            ref = re.search(r"^\s*ref:\s*(.+)$", checkout_step, re.M)
            self.assertIsNotNone(ref, job)
            expression = ref.group(1)
            # One expression that selects the PR head on pull_request and github.sha on
            # workflow_dispatch, where the pull_request context is empty.
            self.assertIn("github.event.pull_request.head.sha", expression, job)
            self.assertIn("github.sha", expression, job)
            self.assertNotIn("merge_commit_sha", expression, job)

    def test_exact_head_assertion_is_required_and_precedes_all_work(self):
        for job, block in workflow_job_blocks(self.workflow).items():
            self.assertIn(EXACT_HEAD_STEP_NAME, block, job)
            assertion_at = block.index(EXACT_HEAD_STEP_NAME)
            for later in ("PowerShell parse check", "python -m unittest", "_run_ci_full_suite"):
                position = block.find(later)
                if position != -1:
                    self.assertLess(assertion_at, position, "%s: %s" % (job, later))
            assertion_step = block[assertion_at:].split("\n      - ", 1)[0]
            self.assertNotIn("continue-on-error", assertion_step, job)
            self.assertIn("rev-parse HEAD", assertion_step, job)
            # It reports the two SHAs and nothing else; no environment dump.
            for dump in ("env |", "Get-ChildItem Env:", "printenv", "${{ toJSON("):
                self.assertNotIn(dump, assertion_step, job)

    def test_default_unqualified_checkout_fixture_fails(self):
        fixture = "\n".join(line for line in self.workflow.splitlines()
                            if not re.match(r"^\s*ref:\s*\$\{\{", line))
        self.assertNotEqual(fixture, self.workflow)
        problems = checkout_head_binding_problems(fixture)
        self.assertTrue(any(p.endswith("checkout_without_explicit_ref") for p in problems), problems)

    def test_merge_sha_assertion_fixture_fails(self):
        fixture = self.workflow.replace("github.event.pull_request.head.sha",
                                        "github.event.pull_request.merge_commit_sha")
        self.assertNotEqual(fixture, self.workflow)
        problems = checkout_head_binding_problems(fixture)
        self.assertTrue(any("merge" in p for p in problems), problems)

    def test_missing_or_masked_assertion_fixtures_fail(self):
        removed = self.workflow.replace(EXACT_HEAD_STEP_NAME, "Unrelated step name")
        self.assertTrue(any(p.endswith("missing_exact_head_assertion")
                            for p in checkout_head_binding_problems(removed)))
        masked = self.workflow.replace('name: %s' % EXACT_HEAD_STEP_NAME,
                                       'name: %s\n        continue-on-error: true' % EXACT_HEAD_STEP_NAME)
        self.assertNotEqual(masked, self.workflow)
        self.assertTrue(any(p.endswith("assertion_continue_on_error")
                            for p in checkout_head_binding_problems(masked)),
                        checkout_head_binding_problems(masked))

    def test_glob_matcher_respects_separator_boundaries(self):
        self.assertTrue(glob_to_regex("tests/test_member_create_uat_*.py").match("tests/test_member_create_uat_x.py"))
        self.assertFalse(glob_to_regex("tests/test_member_create_uat_*.py").match("tests/sub/test_member_create_uat_x.py"))
        self.assertTrue(glob_to_regex("a/**/b.py").match("a/x/y/b.py"))
        self.assertTrue(glob_to_regex(".gitignore").match(".gitignore"))
        self.assertFalse(glob_to_regex(".gitignore").match("sub/.gitignore"))


@unittest.skipIf(PS is None, "no PowerShell executable available")
class ExpiryProbeAstTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.inspector = cls.tmp / "expiry_inspector.ps1"
        cls.inspector.write_text(INSPECTOR, encoding="utf-8")
        cls.scratch_script, cls.scratch_lib = materialise_probe_scratch(cls.tmp / "scratch_probe")

    def _inspect(self, target=None):
        target = target or self.scratch_script
        cmd = [PS, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-File", str(self.inspector), "-Path", str(target)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_parameter_block_exposes_no_path_or_output_override(self):
        info = self._inspect()
        self.assertEqual(info["parseErrors"], 0)
        names = as_list(info["paramNames"])
        self.assertIn("ApprovalReference", names)
        for forbidden in ("StateDirectory", "JsonOut"):
            self.assertNotIn(forbidden, names, forbidden)
        # No parameter of any name may select a claim/result location or a secondary output.
        for name in names:
            lowered = name.lower()
            for fragment in ("statedir", "stateroot", "claimdir", "claimroot", "resultdir",
                             "resultpath", "jsonout", "outfile", "outpath", "evidence", "staging"):
                self.assertNotIn(fragment, lowered, name)
        # The library's injectable helpers are never bound to a script parameter: every
        # -Root argument in the executable path is the fixed canonical root.
        script_text = read_repo_text("probe_script")
        self.assertIn("New-ExpiryProbeTrustedRootLease -Root $script:ExpiryProbeStateRoot", script_text)
        self.assertEqual(script_text.count("New-ExpiryProbeTrustedRootLease"), 1)
        self.assertEqual(script_text.count("-Root $script:ExpiryProbeStateRoot"), 2)
        self.assertEqual(script_text.count("-Root "), 2)
        self.assertNotIn("-MoveAction", script_text)

    def test_single_gated_save_never_in_a_loop(self):
        info = self._inspect()
        self.assertEqual(info["parseErrors"], 0)
        self.assertTrue(info["funcExists"])
        self.assertEqual(info["saveInvokeCount"], 1)
        self.assertTrue(info["saveInsideNarrow"])
        self.assertEqual(info["callCount"], 1)
        self.assertFalse(info["callInLoop"])

    def test_duplicate_check_before_save_and_no_forbidden_mutations(self):
        info = self._inspect()
        self.assertGreaterEqual(info["getMemberInvokeCount"], 1)
        self.assertTrue(info["dupCheckBeforeSave"])
        self.assertEqual(info["forbiddenMutationCount"], 0)

    def test_no_artefact_removal_in_script_or_library(self):
        for target in (self.scratch_script, self.scratch_lib):
            info = self._inspect(target)
            self.assertEqual(info["parseErrors"], 0, str(target))
            self.assertEqual(info["removeItemCount"], 0, "no Remove-Item may exist in %s" % target.name)
        # The library's only System.IO.File mutation is the single no-replace publication
        # move: never a Delete and never a Replace (which would clobber prior evidence).
        lib_info = self._inspect(self.scratch_lib)
        self.assertEqual(lib_info["fileMoveCount"], 0,
                         "publication uses the native write-through move, not System.IO.File")
        self.assertEqual(lib_info["fileDeleteCount"], 0)
        script_info = self._inspect(self.scratch_script)
        self.assertEqual(script_info["fileMoveCount"], 0)
        self.assertEqual(script_info["fileDeleteCount"], 0)


if __name__ == "__main__":
    unittest.main()
