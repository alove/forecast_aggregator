.PHONY: setup collect test validate sources schema clean db-collect db-validate db-stage db-up db-refresh db-status db-down db-smoke

setup:
	./setup.sh

collect:
	./run.sh collect

test:
	PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -v

validate:
	./run.sh validate ./collected_data/election_forecasts_2026_national.csv
	./run.sh validate ./collected_data/election_forecasts_2026_state.csv

sources:
	./run.sh sources

schema:
	./run.sh schema

db-collect:
	./election_forecasts_ecs.sh collect

db-validate:
	./election_forecasts_ecs.sh validate

db-stage:
	./election_forecasts_ecs.sh stage

db-up:
	./election_forecasts_ecs.sh up

db-refresh:
	./election_forecasts_ecs.sh refresh

db-status:
	./election_forecasts_ecs.sh status

db-smoke:
	./election_forecasts_ecs.sh smoke

db-down:
	./election_forecasts_ecs.sh down

clean:
	rm -rf .venv build dist *.egg-info forecast_collector/*.egg-info
	rm -rf forecast_database_ecs/.venv
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -f ./*.lock sample_output/*.lock
	rm -f forecast_database_ecs/image/data/*.part
