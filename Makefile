.PHONY: setup collect test validate sources schema clean

setup:
	./setup.sh

collect:
	./run.sh collect

test:
	PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -v

validate:
	./run.sh validate ./election_forecasts_2026.csv

sources:
	./run.sh sources

schema:
	./run.sh schema

clean:
	rm -rf .venv build dist *.egg-info forecast_collector/*.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -f ./*.lock sample_output/*.lock
