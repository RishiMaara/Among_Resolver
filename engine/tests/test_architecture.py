"""
The architecture, as tests: the layers are rules the build enforces, not a
diagram that drifts.

    interface     main, auth, api/*            HTTP: read requests, shape responses
    application   settlement_run, pipeline,    what a run does around the solve
                  investigation_agent, ...
    money path    orchestrator and every       which payments make a settlement
                  module it reaches

1. The money path never reaches a model, HTTP or the API layer, not even
   through a lazy import inside a function. "No model decides which payments
   make a settlement" is a property of the import graph, checked here, and of
   behaviour, checked in test_no_model_decides.
2. Only the interface layer imports FastAPI or Starlette.
3. Nothing imports main.
4. Only the interface layer, and settlement_run (which builds the response
   body with api.presentation), import from api/.
5. No import cycles between modules at load time.
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

MONEY_PATH = [
    "orchestrator", "tiered_solve", "candidate_filters", "recon_report", "recon_gates",
    "subset_sum", "subset_sum_nm", "linkage", "linkage_em", "fee_decomposition",
    "fuzzy_match", "exception_diagnosis", "schema", "ingestion", "pipeline",
]
MODEL_CODE = {"llm_provider", "llm_header_mapper", "narration_reader", "settlement_qa",
              "investigation_agent", "statement_ocr", "grounding_check", "model_budget",
              "google"}
WEB = {"fastapi", "starlette"}


def _modules() -> dict[str, Path]:
    out = {}
    for p in SRC.rglob("*.py"):
        name = p.relative_to(SRC).with_suffix("").as_posix().replace("/", ".")
        out[name.removesuffix(".__init__")] = p
    return out


def _imports(p: Path, *, module_level_only: bool) -> set[str]:
    """Imported module names. Module level skips `if TYPE_CHECKING:` blocks."""
    tree = ast.parse(p.read_text(encoding="utf-8"))
    if module_level_only:
        nodes = []
        for node in tree.body:
            if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.dump(node.test):
                continue
            nodes.extend(ast.walk(node) if isinstance(node, (ast.Try, ast.If)) else [node])
    else:
        nodes = list(ast.walk(tree))
    found = set()
    for n in nodes:
        if isinstance(n, ast.Import):
            found |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            found.add(n.module)
    return found


MODULES = _modules()
ALL_IMPORTS = {m: _imports(p, module_level_only=False) for m, p in MODULES.items()}


def _interface(m: str) -> bool:
    return m in ("main", "auth") or m == "api" or m.startswith("api.")


def _top(name: str) -> str:
    return name if name.startswith("api") else name.split(".")[0]


def test_the_money_path_never_reaches_a_model_or_http():
    for start in MONEY_PATH:
        seen, stack = set(), [start]
        while stack:
            for dep in ALL_IMPORTS.get(stack.pop(), ()):
                top = _top(dep)
                if top not in seen:
                    seen.add(top)
                    stack.append(top)
        forbidden = sorted(d for d in seen
                           if d in MODEL_CODE or d in WEB or d == "main" or d.startswith("api"))
        assert not forbidden, f"{start} reaches {forbidden}"


def test_only_the_interface_layer_speaks_http():
    offenders = sorted(m for m, deps in ALL_IMPORTS.items()
                       if not _interface(m) and any(_top(d) in WEB for d in deps))
    assert not offenders, f"HTTP imported outside the interface layer: {offenders}"


def test_nothing_imports_main():
    offenders = sorted(m for m, deps in ALL_IMPORTS.items() if "main" in deps)
    assert not offenders, offenders


def test_the_api_layer_is_reached_only_from_the_interface_and_settlement_run():
    offenders = sorted(m for m, deps in ALL_IMPORTS.items()
                       if not _interface(m) and m != "settlement_run"
                       and any(d == "api" or d.startswith("api.") for d in deps))
    assert not offenders, offenders


def test_no_import_cycles_at_load_time():
    graph = {m: {_top(d) for d in _imports(p, module_level_only=True)} & set(MODULES)
             for m, p in MODULES.items()}
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    cycles: list[list[str]] = []

    def visit(v: str) -> None:
        index[v] = low[v] = len(index)
        stack.append(v)
        on_stack.add(v)
        for w in graph[v]:
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                cycles.append(sorted(comp))

    for v in graph:
        if v not in index:
            visit(v)
    assert not cycles, f"import cycles: {cycles}"
