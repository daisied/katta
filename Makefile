.PHONY: bootstrap lint test ci up down logs clean-data

bootstrap:
	python -m pip install --upgrade pip
	python -m pip install -r requirements.txt -r requirements-dev.txt

lint:
	ruff check .

test:
	pytest

ci: lint test

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f katta

clean-data:
	rm -rf app/data/history app/data/logs
	find app/data -maxdepth 1 -type f \
		! -name '.gitkeep' \
		! -name 'README.md' \
		! -name '*.example.json' \
		! -name '*.template.md' \
		! -name '*.example.sh' \
		-delete
