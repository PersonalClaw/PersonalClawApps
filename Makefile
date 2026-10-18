# The apps gate, as CI runs it: the static checks, the repository's own tests, every bundle's suite,
# then the quality claims judged from that suite's results.
# PYTHON is the environment holding core (the commit core.lock pins), pytest, pytest-asyncio and
# pytest-xdist (CONTRIBUTING.md shows how to make one). The gate warns when its core is another.
PYTHON ?= .venv/bin/python

.PHONY: gate static rails bundles quality

# GATE_ARGS goes to the gate as written, for example GATE_ARGS="--jobs 4" (the default runs half
# the CPUs' worth of bundles at once, at most 8).
gate:
	$(PYTHON) scripts/gate.py $(GATE_ARGS)

static rails bundles:
	$(PYTHON) scripts/gate.py $@ $(GATE_ARGS)

# The quality part judges the bundle results of its own run, so it runs with the bundles part: on
# its own it would find none and fail, by design.
quality:
	$(PYTHON) scripts/gate.py bundles quality $(GATE_ARGS)
