"""Веб-интерфейс DocSift: серверный рендер на Jinja2 + HTMX.

Подключение к существующему приложению:

    from docsift.web import mount_web
    mount_web(app)
"""

from .app import mount_web, build_templates

__all__ = ["mount_web", "build_templates"]
