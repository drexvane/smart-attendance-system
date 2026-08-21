"""Request handlers, grouped by area.

Each blueprint parses input and renders output; the rules live in ``app.services``.
Route *paths* are unchanged from the original single-module app so that templates and
front-end fetch calls keep working -- only the ``url_for`` endpoint names gained a
blueprint prefix.
"""
