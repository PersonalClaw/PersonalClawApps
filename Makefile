# The apps gate, as CI runs it: the static checks, the repository's own tests, every bundle's suite.
# PYTHON is the environment holding core (the commit core.lock pins), pytest, pytest-asyncio and
# pytest-xdist (CONTRIBUTING.md shows how to make one). The gate warns when its core is another.
PYTHON ?= .venv/bin/python

.PHONY: gate static rails bundles

# GATE_ARGS goes to the gate as written, for example GATE_ARGS="--jobs 4" (the default runs half
# the CPUs' worth of bundles at once, at most 8).
gate:
	$(PYTHON) scripts/gate.py $(GATE_ARGS)

static rails bundles:
	$(PYTHON) scripts/gate.py $@ $(GATE_ARGS)
