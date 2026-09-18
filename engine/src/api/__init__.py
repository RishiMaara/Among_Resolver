"""
HTTP surface for the reconciliation engine.

main.py was 2,062 lines: app construction, middleware, request models,
twenty-one endpoints, and six hundred lines of presentation logic that
has nothing to do with HTTP. Splitting it is not cosmetic — the upload
endpoint alone is three hundred lines, and finding the one function that
decides whether a payment is interchangeable meant scrolling past all of
it.

The seam is deliberate: `models` and `presentation` know nothing about
FastAPI beyond pydantic, so they can be imported and tested without a
running app, and the route modules stay thin enough to read.
"""
