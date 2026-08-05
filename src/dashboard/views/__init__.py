"""
The dashboard's three views.

DELIBERATELY NOT NAMED `pages/`. Streamlit treats a `pages/` directory beside
the entrypoint script as a magic multipage source and auto-registers every file
in it as a navigable page — which would have produced a duplicate navigation
alongside the sidebar in app.py, and would have executed each module as a
standalone script rather than through its `render()` function.
"""
