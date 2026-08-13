VERSION := $(shell PYTHONPATH=src python3 -c 'from mujterm import __version__; print(__version__)')
DEB := $(abspath dist/mujterm_$(VERSION)_all.deb)

.PHONY: run test compatibility check deb reinstall clean

run:
	./bin/mujterm

test:
	PYTHONPATH=src python3 -m unittest discover -s tests -v

compatibility:
	PYTHONPATH=src python3 -m unittest tests.test_terminal_compatibility -v

check:
	python3 -m compileall -q src tests
	PYTHONPATH=src python3 -m unittest discover -s tests -v

deb:
	./scripts/build-deb.sh

reinstall: check deb
	pkexec dpkg -i "$(DEB)"

clean:
	find src tests -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf build dist
