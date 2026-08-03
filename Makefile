# Convenience wrappers. Everything here also works as a plain command.
.PHONY: help build test test-contracts test-checker test-e2e fixture clean deps

help:
	@echo "build           compile contracts and regenerate the ABI bundle"
	@echo "test            every suite (177 checks)"
	@echo "test-contracts  contract suite only, on an in-process EVM"
	@echo "test-checker    checker suite only, against live local servers"
	@echo "test-e2e        the real page in a headless DOM, real contracts"
	@echo "fixture         serve a populated register at :8646, no chain needed"
	@echo "deps            install Python and Node development dependencies"
	@echo "clean           remove build output and local checker state"

deps:
	pip install -r requirements-dev.txt -r bot/requirements.txt
	npm ci

build:
	./scripts/build.sh

test: build
	./test/run-all.sh

test-contracts:
	python3 test/test_registry.py

test-checker:
	python3 test/test_checker.py

test-e2e: build
	node test/e2e.js

fixture:
	python3 test/e2e_fixture.py --hold

clean:
	rm -rf contract/build contract/abi __pycache__ */__pycache__ test/e2e-manifest.json
	rm -f bot/*.sqlite3* web/data/status-*.json web/data/status-*.json.sig
	find . -name '*.pyc' -delete
