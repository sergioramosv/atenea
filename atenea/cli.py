"""Atajo de compatibilidad: Atenea Código vive ahora en atenea.codigo.cli (las instalaciones antiguas llaman aquí)."""
import sys

from .codigo.cli import main  # noqa: F401

if __name__ == "__main__":
    sys.exit(main() or 0)
