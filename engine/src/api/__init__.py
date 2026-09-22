"""
HTTP surface for the reconciliation engine. `models` and `presentation` know
nothing about FastAPI beyond pydantic, so they can be tested without an app;
the route modules stay thin.
"""
