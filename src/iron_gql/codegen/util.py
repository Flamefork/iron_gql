from collections.abc import Callable
from collections.abc import Iterable
from pathlib import Path


def reachable[T](roots: Iterable[T], neighbors: Callable[[T], Iterable[T]]) -> set[T]:
    # Everything reached from `roots` by following one edge or more. A root is
    # in the result only when an edge leads back to it, so a caller that wants
    # the roots counted says so itself -- the two walks over the artifact graph
    # want opposite answers there, and both walk the same edges otherwise.
    reached: set[T] = set()
    queue = list(roots)
    while queue:
        for neighbor in neighbors(queue.pop()):
            if neighbor not in reached:
                reached.add(neighbor)
                queue.append(neighbor)
    return reached


def capitalize_first(name: str) -> str:
    return name[0].upper() + name[1:]


def indent_block(block: str, indent: str) -> str:
    return "\n".join(
        indent + line if i > 0 and line.strip() else line
        for i, line in enumerate(block.split("\n"))
    )


def write_if_changed(path: Path, new_content: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_content = path.read_text(encoding="utf-8") if path.exists() else None
    if existing_content == new_content:
        return False
    path.write_text(new_content, encoding="utf-8")
    return True
