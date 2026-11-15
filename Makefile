.PHONY: build up down logs restart shell test

build:
	docker compose build

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f --tail=200

restart:
	docker compose restart

shell:
	docker compose exec stream-monitor sh

test: build
	docker run --rm --entrypoint sh \
		-v "$(CURDIR)/tests:/app/tests" \
		-e PYTHONDONTWRITEBYTECODE=1 \
		stream-monitor:latest \
		-c "pip install --no-cache-dir -q pytest==8.3.4 && python -m pytest -q -p no:cacheprovider"
