.PHONY: astrixa-up astrixa-down poc-up poc-down demo eval-replay health status smoke security-check verify migrate preflight test cleanup-demo-data

astrixa-up:
	docker compose --env-file .env -f vendor/astrixa/docker-compose.yml up -d --build

astrixa-down:
	docker compose --env-file .env -f vendor/astrixa/docker-compose.yml down

poc-up:
	TG_OUTREACH_GIT_SHA=$$(git rev-parse --short HEAD) TG_OUTREACH_BUILD_VERSION=$$(git rev-parse --short HEAD) docker compose --env-file .env -f docker-compose.poc.yml up -d --build

poc-down:
	docker compose --env-file .env -f docker-compose.poc.yml down

demo:
	DEMO_ID=$$(date +%s); \
	curl -sS -X POST http://127.0.0.1:18100/api/v1/vacancies/ingest \
		-H 'content-type: application/json' \
		-d "{\"source_channel\":\"@jobs-$$DEMO_ID\",\"recruiter_handle\":\"@recruiter_$$DEMO_ID\",\"vacancy_text\":\"Senior Python Backend Engineer. Docker, FastAPI, PostgreSQL, observability, LLM integrations. Remote. Demo $$DEMO_ID.\"}"

eval-replay:
	docker compose --env-file .env -f docker-compose.poc.yml exec -T outreach-api python -m app.replay_eval

migrate:
	docker compose --env-file .env -f docker-compose.poc.yml exec -T outreach-api python -m app.migrate

cleanup-demo-data:
	docker compose --env-file .env -f docker-compose.poc.yml exec -T outreach-api python -m app.cleanup

health:
	curl -sS http://127.0.0.1:18080/healthz
	curl -sS http://127.0.0.1:18100/healthz
	curl -sS http://127.0.0.1:18100/readyz

status:
	docker ps --format '{{.Names}}\t{{.Status}}'

smoke:
	curl -sS http://127.0.0.1:18080/healthz
	curl -sS http://127.0.0.1:18100/readyz
	SMOKE_ID=$$(date +%s); \
	RESPONSE=$$(curl -sS -X POST http://127.0.0.1:18100/api/v1/vacancies/ingest \
		-H 'content-type: application/json' \
		-d "{\"source_channel\":\"@smoke-$$SMOKE_ID\",\"recruiter_handle\":\"@smoke_hr_$$SMOKE_ID\",\"vacancy_text\":\"Senior Python Backend Engineer. Docker, FastAPI, PostgreSQL, observability, LLM integrations. Remote. Smoke $$SMOKE_ID.\"}"); \
	echo "$$RESPONSE"; \
	echo "$$RESPONSE" | grep -q '"created_count":[1-9]'

security-check:
	python3 scripts/check_secret_hygiene.py

verify:
	python3 -m py_compile scripts/check_secret_hygiene.py services/outreach-api/app/main.py services/outreach-api/app/worker.py services/outreach-api/app/replay_eval.py services/outreach-api/tests/test_runtime_contracts.py
	$(MAKE) security-check
	$(MAKE) smoke

preflight:
	$(MAKE) verify
	$(MAKE) migrate
	curl -sS http://127.0.0.1:18100/version
	curl -sS http://127.0.0.1:18100/api/v1/admin/runtime
	curl -sS http://127.0.0.1:18100/api/v1/admin/dependencies

test:
	docker compose --env-file .env -f docker-compose.poc.yml exec -T outreach-api python -m unittest discover -s tests -v
