.PHONY: demo test eval lint reset ui graph

demo:   ## reset, run every sample, show the VP queue
	uv run acme-ap run --all --reset && uv run acme-ap queue

test:
	uv run pytest -q

eval:   ## scorecard: every sample + stress file vs data/expected_outcomes.csv
	uv run acme-ap eval

lint:
	uv run ruff check . && uv run ruff format --check .

reset:
	uv run acme-ap setup-db --reset

ui:     ## the VP inbox in a browser
	uv sync --group ui && uv run streamlit run app.py

graph:  ## Mermaid source of the compiled workflow
	uv run acme-ap graph
