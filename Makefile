.PHONY: help install dev test test-cov lint format type check build clean examples-screenshots desktop-typecheck

PY ?= python
PIP ?= $(PY) -m pip

help:
	@echo "vouch developer targets"
	@echo ""
	@echo "  make install       editable install with dev extras"
	@echo "  make test          run pytest"
	@echo "  make test-cov      run pytest with coverage"
	@echo "  make lint          ruff check"
	@echo "  make format        ruff format (writes)"
	@echo "  make type          mypy"
	@echo "  make check         lint + type + test"
	@echo "  make build         build sdist + wheel"
	@echo "  make clean         remove caches, build artifacts, *.egg-info"
	@echo "  make examples-screenshots  re-render docs/img/examples/*.svg"
	@echo "  make desktop-typecheck  npm typecheck in desktop/app (Tauri shell)"

install:
	$(PIP) install -e '.[dev]'

dev: install

test:
	$(PY) -m pytest

test-cov:
	$(PY) -m pytest --cov=vouch --cov-report=term-missing --cov-report=xml

lint:
	$(PY) -m ruff check src tests

format:
	$(PY) -m ruff format src tests

type:
	$(PY) -m mypy src

check: lint type test

examples-screenshots:
	$(PY) docs/img/examples/render.py

build:
	$(PY) -m pip install --upgrade build
	$(PY) -m build

clean:
	rm -rf build dist *.egg-info src/*.egg-info \
	       .pytest_cache .ruff_cache .mypy_cache \
	       coverage.xml .coverage htmlcov
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

desktop-typecheck:
	cd desktop/app && npm ci && npm run typecheck
