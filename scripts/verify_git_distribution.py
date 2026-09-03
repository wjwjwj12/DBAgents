import os
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_PATHS = {
    ".env.example",
    "requirements.txt",
    "pytest.ini",
    "start.py",
    "frontend/package.json",
    "frontend/package-lock.json",
    "frontend/.env.example",
    "deploy/opensandbox/Dockerfile",
    "deploy/opensandbox/requirements.txt",
    "skills/ppt-master/scripts/pptx_shapes/data/presetShapeDefinitions.xml",
    "skills/ppt-master/scripts/pptx_shapes/data/shape_type_values.txt",
}
DISTRIBUTION_ROOTS = ("backend", "deploy", "frontend", "scripts", "skills", "tests")
IGNORED_DIRECTORY_NAMES = {
    ".next",
    ".pytest_cache",
    "__pycache__",
    "build",
    "coverage",
    "node_modules",
    "out",
}
IGNORED_LOCAL_FILES = {
    ".env",
    ".env.development",
    ".env.local",
    ".env.production",
    ".env.test",
    ".DS_Store",
    "next-env.d.ts",
}
IGNORED_SUFFIXES = {".pyc", ".pyo", ".tsbuildinfo"}
PPT_REQUIREMENTS = ROOT / "skills" / "ppt-master" / "requirements.txt"
SANDBOX_REQUIREMENTS = ROOT / "deploy" / "opensandbox" / "requirements.txt"
GIT_LFS_HEADER = b"version https://git-lfs.github.com/spec/v1"
MAX_GIT_FILE_BYTES = 100 * 1024 * 1024


def _tracked_paths() -> set[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return {
        item.decode("utf-8").replace("\\", "/")
        for item in result.stdout.split(b"\0")
        if item
    }


def _distribution_files():
    for root_name in DISTRIBUTION_ROOTS:
        root = ROOT / root_name
        if not root.exists():
            continue
        for current, directories, filenames in os.walk(root):
            directories[:] = [
                name for name in directories if name not in IGNORED_DIRECTORY_NAMES
            ]
            current_path = Path(current)
            for filename in filenames:
                path = current_path / filename
                if filename in IGNORED_LOCAL_FILES or path.suffix.lower() in IGNORED_SUFFIXES:
                    continue
                yield path


def _requirement_names(path: Path) -> set[str]:
    names = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.partition("#")[0].strip()
        match = re.match(r"([A-Za-z0-9_.-]+)", line)
        if match:
            names.add(match.group(1).casefold().replace("_", "-"))
    return names


def main() -> int:
    tracked = _tracked_paths()
    errors = []

    missing = sorted(REQUIRED_PATHS - tracked)
    if missing:
        errors.extend(f"required file is not tracked: {path}" for path in missing)

    for relative in sorted(tracked):
        path = ROOT / relative
        if not path.exists():
            errors.append(f"tracked file is missing from the working tree: {relative}")

    for path in _distribution_files():
        relative = path.relative_to(ROOT).as_posix()
        if relative not in tracked:
            errors.append(f"distribution file is not tracked: {relative}")

    missing_packages = sorted(
        _requirement_names(PPT_REQUIREMENTS) - _requirement_names(SANDBOX_REQUIREMENTS)
    )
    if missing_packages:
        errors.append(
            "OpenSandbox image is missing PPT dependencies: "
            + ", ".join(missing_packages)
        )

    for relative in sorted(tracked):
        path = ROOT / relative
        if not path.is_file():
            continue
        size = path.stat().st_size
        if size >= MAX_GIT_FILE_BYTES:
            errors.append(f"tracked file reaches GitHub's 100 MiB limit: {relative}")
        if size <= 1024 and path.read_bytes().startswith(GIT_LFS_HEADER):
            errors.append(f"Git LFS pointer is not self-contained in an offline archive: {relative}")

    case_groups: dict[str, list[str]] = {}
    for path in tracked:
        case_groups.setdefault(path.casefold(), []).append(path)
    for paths in case_groups.values():
        if len(paths) > 1:
            errors.append(f"case-colliding paths are not portable: {', '.join(sorted(paths))}")

    if errors:
        print("Git distribution validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"Git distribution validation passed: {len(tracked)} tracked files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
