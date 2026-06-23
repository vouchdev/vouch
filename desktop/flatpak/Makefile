.PHONY: help icons validate requirements build install run clean

ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
PY ?= python3
MANIFEST := $(ROOT)/com.vouchdev.vouch.yaml
BUILD_DIR := $(ROOT)/build-dir
REPO_DIR := $(ROOT)/repo

help:
	@echo "vouch flatpak targets (#211)"
	@echo ""
	@echo "  make icons         regenerate hicolor PNGs from SVG"
	@echo "  make validate      run packaging validators (strict)"
	@echo "  make requirements  sync requirements-flatpak.txt from pyproject.toml"
	@echo "  make build         flatpak-builder (no install)"
	@echo "  make install       build + --user --install"
	@echo "  make run           flatpak run com.vouchdev.vouch"
	@echo "  make clean         remove build-dir/ and repo/"

icons:
	$(PY) $(ROOT)/scripts/generate-icons.py

validate: icons
	$(PY) $(ROOT)/scripts/validate-manifest.py --strict

requirements:
	$(PY) $(ROOT)/scripts/generate-requirements.py

build: validate
	flatpak-builder --force-clean --repo=$(REPO_DIR) $(BUILD_DIR) $(MANIFEST)

install: validate
	flatpak-builder --user --install --force-clean --repo=$(REPO_DIR) $(BUILD_DIR) $(MANIFEST)

run:
	flatpak run com.vouchdev.vouch

clean:
	rm -rf $(BUILD_DIR) $(REPO_DIR)
