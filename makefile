.PHONY: up down test ingest transform lint typecheck clean

# --- Infrastructure ---
up:
	docker-compose up -d

down:
	docker-compose down

down-volumes:
	docker-compose down -v   # WARNING: destroys the Postgres data volume

# --- Development ---
test:
	pytest tests/unit/ -v

test-all:
	pytest tests/ -v

lint:
	ruff check src/ tests/

typecheck:
	mypy src/

# --- Pipeline ---
ingest:
	@echo "TODO: python -m src.ingestion"

transform:
	@echo "TODO: dbt run --project-dir dbt/"

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true
	find . -name "*.pyc" -delete 2>/dev/null; true