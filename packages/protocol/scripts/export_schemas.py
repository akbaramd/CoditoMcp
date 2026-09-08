import sys
from pathlib import Path

from codito_protocol.schemas import export_schemas

if __name__ == "__main__":
    repository_root = Path(__file__).resolve().parents[3]
    paths = export_schemas(repository_root / "packages" / "protocol" / "schemas")
    rendered = "\n".join(path.relative_to(repository_root).as_posix() for path in paths)
    sys.stdout.write(f"{rendered}\n")
