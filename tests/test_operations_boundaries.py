"""Static import boundaries for the refactored operations package."""

from __future__ import annotations

import ast
from pathlib import Path


OPERATIONS = Path("core/operations")
RULES = OPERATIONS / "rules"
DEPENDENCIES = OPERATIONS / "dependencies"
REPOSITORY = OPERATIONS / "repository"
SERVICES = OPERATIONS / "services"
EXECUTING = Path("core/executing")
FORBIDDEN_PREFIXES = (
    "sqlite3",
    "core.storage",
    "core.executing",
    "core.jira_client",
    "core.operations.container",
    "core.operations.repository",
    "core.operations.services",
    "web",
)


def imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    values: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            values.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            values.add(node.module)
    return values


def imported_names(path: Path, module: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == module
        for alias in node.names
    }


def test_models_and_rules_do_not_depend_on_outer_layers() -> None:
    pure_modules = (
        OPERATIONS / "models.py",
        *sorted(RULES.glob("*.py")),
        *sorted(DEPENDENCIES.glob("*.py")),
    )
    for path in pure_modules:
        imported = imports(path)
        assert not {
            name
            for name in imported
            if name.startswith(FORBIDDEN_PREFIXES)
        }, path


def test_only_repository_package_imports_sqlite3() -> None:
    sqlite_modules = {
        path.relative_to(OPERATIONS).as_posix()
        for path in OPERATIONS.rglob("*.py")
        if "sqlite3" in imports(path)
    }
    assert sqlite_modules
    assert all(Path(name).parent == Path("repository") for name in sqlite_modules)


def test_dependency_private_modules_do_not_leak_outside_package() -> None:
    for path in OPERATIONS.rglob("*.py"):
        if path.is_relative_to(DEPENDENCIES):
            continue
        assert not {
            name
            for name in imports(path)
            if name.startswith("core.operations.dependencies._")
        }, path


def test_services_use_public_repository_capability_modules() -> None:
    allowed_names = {
        "DatabaseConnection",
        "dependencies",
        "execution",
        "metadata",
        "queue",
    }
    for path in SERVICES.glob("*.py"):
        assert not {
            name
            for name in imports(path)
            if name.startswith("core.operations.repository._")
        }, path
        assert imported_names(
            path, "core.operations.repository"
        ) <= allowed_names, path
        assert "from core.operations import repository" not in path.read_text(
            encoding="utf-8"
        )


def test_repository_public_modules_and_private_records_match_real_boundaries() -> None:
    for module_name in ("queue", "execution", "dependencies", "metadata"):
        assert (REPOSITORY / f"{module_name}.py").is_file()
        assert not (REPOSITORY / f"_{module_name}.py").exists()

    records_importers = {
        path.relative_to(OPERATIONS).as_posix()
        for path in OPERATIONS.rglob("*.py")
        if "core.operations.repository._records" in imports(path)
    }
    assert records_importers
    assert all(name.startswith("repository/") for name in records_importers)


def test_dependency_orchestration_is_the_only_public_rule_module() -> None:
    assert (DEPENDENCIES / "orchestration.py").is_file()
    assert not (DEPENDENCIES / "_orchestration.py").exists()
    assert not (DEPENDENCIES / "_types.py").exists()
    for path in OPERATIONS.rglob("*.py"):
        if path.is_relative_to(DEPENDENCIES):
            continue
        dependency_imports = {
            name
            for name in imports(path)
            if name.startswith("core.operations.dependencies")
        }
        assert dependency_imports <= {"core.operations.dependencies"}, path


def test_services_do_not_branch_on_concrete_dependency_types() -> None:
    forbidden_terms = (
        "ISSUE_PRODUCER_OPERATION_TYPES",
        "ATTACHMENT_PRODUCER_OPERATION_TYPES",
        "IssueDependencyResolution",
        "AttachmentDependencyResolution",
        "broadcast_issue_resolution",
        "broadcast_attachment_resolution",
    )
    for filename in ("execution.py", "recovery.py"):
        source = (SERVICES / filename).read_text(encoding="utf-8")
        assert not [term for term in forbidden_terms if term in source]


def test_repository_package_has_no_stateless_compatibility_class() -> None:
    from core.operations import repository

    assert not hasattr(repository, "OperationRepository")
    assert not hasattr(repository, "insert_queue")
    assert not hasattr(repository, "staging_records")
    assert not hasattr(repository, "broadcast_dependency_resolution")


def test_services_do_not_call_or_import_sibling_services() -> None:
    service_modules = {
        path
        for path in SERVICES.glob("*.py")
        if path.name != "__init__.py"
    }
    service_imports = {
        f"core.operations.services.{path.stem}" for path in service_modules
    }
    for path in service_modules:
        imported = imports(path)
        assert not (imported & service_imports), path
        assert "core.operations.container" not in imported, path


def test_container_is_the_only_service_aggregator() -> None:
    consumers = {
        path.relative_to(OPERATIONS).as_posix()
        for path in OPERATIONS.rglob("*.py")
        if any(
            name.startswith("core.operations.services.")
            for name in imports(path)
        )
    }
    assert consumers == {"container.py"}


def test_production_adapters_use_operations_package_api() -> None:
    adapters = [
        Path("server.py"),
        Path("web/app.py"),
        *sorted(Path("core/mcp_tools").glob("*.py")),
        *sorted(EXECUTING.glob("*.py")),
    ]
    for path in adapters:
        assert not {
            name
            for name in imports(path)
            if name.startswith("core.operations.")
        }, path


def test_removed_internal_modules_and_old_runtime_do_not_exist() -> None:
    for filename in (
        "dependencies.py",
        "execution.py",
        "store.py",
        "queue_store.py",
        "execution_store.py",
        "issue_references.py",
        "persistence.py",
        "ordering.py",
        "query.py",
        "queue.py",
        "safety.py",
        "services.py",
        "submission.py",
        "views.py",
        "field_references.py",
        "issue_key_references.py",
        "public_views.py",
    ):
        assert not (OPERATIONS / filename).exists()
    assert not Path("core/execution").exists()
    assert not (OPERATIONS / "repository.py").exists()


def test_public_packages_import_without_cycles() -> None:
    from core import operations
    from core.executing import jira_executor, lock, worker

    assert callable(operations.get_services)
    assert callable(jira_executor.execute_operation)
    assert callable(lock.execution_slot)
    assert callable(worker.process_once)
