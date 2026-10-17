# The apps gate, as CI runs it: the static checks, the repository's own tests, every bundle's suite.
# PYTHON is the environment holding core, pytest, pytest-asyncio and pytest-xdist
# (CONTRIBUTING.md shows how to make one).
PYTHON ?= .venv/bin/python

.PHONY: gate static rails bundles

gate:
	$(PYTHON) scripts/gate.py

static rails bundles:
	$(PYTHON) scripts/gate.py $@
