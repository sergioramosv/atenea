"""Atajo de compatibilidad: el panel vive ahora en atenea.app.web."""
from .app.web import *  # noqa: F401,F403
from .app.web import app, main  # noqa: F401

if __name__ == "__main__":
    main()
