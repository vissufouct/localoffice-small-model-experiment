from pathlib import Path
from .errors import HarnessError


def project_path(root: Path, relative: str, *, must_exist: bool = False) -> Path:
    """No absolute paths, traversal, state directory or symlink traversal."""
    path = Path(relative)
    if not relative or path.is_absolute() or any(p in ('..', '.localoffice') for p in path.parts):
        raise HarnessError('path_denied', 'Use a project-relative path outside .localoffice.')
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise HarnessError('path_denied', 'Symbolic links are not permitted for project tools.')
    resolved = current.resolve()
    if not resolved.is_relative_to(root) or resolved == root:
        raise HarnessError('path_denied', 'Path must be a file inside this project.')
    if must_exist and not resolved.is_file():
        raise HarnessError('not_found', 'Project file does not exist.')
    return resolved
