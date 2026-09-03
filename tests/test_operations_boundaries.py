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


def test_services_only_use_repository_package_api() -> None:
    for path in SERVICES.glob("*.py"):
        assert not {
            name
            for name in imports(path)
            if name.startswith("core.operations.repository._")
        }, path


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
        Path("core/mcp_tools.py"),
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
