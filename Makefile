.PHONY: install lint format test bench eval serve
install:  ## editable install with dev, postgres and api extras
	pip install -e ".[dev,postgres,api]"
lint:
	ruff check . && ruff format --check .
format:
	ruff check --fix . && ruff format .
test:
	pytest -q
bench:    ## build the bundled retail benchmark database
	python evals/bench/build_db.py
eval:     ## execution-accuracy eval (needs a model; see README)
	python evals/run_eval.py --max-repairs 2
serve:
	TEXT2SQL_DATABASE=evals/bench/retail.sqlite uvicorn text2sql.api:app --reload
