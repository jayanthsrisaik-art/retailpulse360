PY ?= python

.PHONY: install data build report serve test clean

install:
	$(PY) -m pip install -r requirements.txt

data:
	$(PY) -m retailpulse generate

build: data
	$(PY) -m retailpulse build

report:
	$(PY) -m retailpulse report

serve:
	$(PY) -m retailpulse serve

test:
	$(PY) -m pytest -q

clean:
	rm -rf data .pytest_cache
