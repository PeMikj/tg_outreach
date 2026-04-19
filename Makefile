.PHONY: astrixa-up astrixa-down poc-up poc-down demo eval-replay health status smoke

astrixa-up:
	docker compose --env-file .env -f vendor/astrixa/docker-compose.yml up -d --build

astrixa-down:
	docker compose --env-file .env -f vendor/astrixa/docker-compose.yml down

poc-up:
	TG_OUTREACH_GIT_SHA=$$(git rev-parse --short HEAD) TG_OUTREACH_BUILD_VERSION=$$(git rev-parse --short HEAD) docker compose --env-file .env -f docker-compose.poc.yml up -d --build

poc-down:
	docker compose --env-file .env -f docker-compose.poc.yml down

demo:
	curl -sS -X POST http://127.0.0.1:18100/api/v1/vacancies/ingest \
		-H 'content-type: application/json' \
		-d '{"source_channel":"@jobs","recruiter_handle":"@recruiter","vacancy_text":"Senior Python Backend Engineer. Docker, FastAPI, PostgreSQL, observability, LLM integrations. Remote."}'

eval-replay:
	docker compose --env-file .env -f docker-compose.poc.yml exec -T outreach-api python -m app.replay_eval

health:
	curl -sS http://127.0.0.1:18080/healthz
	curl -sS http://127.0.0.1:18100/healthz
	curl -sS http://127.0.0.1:18100/readyz

status:
	docker ps --format '{{.Names}}\t{{.Status}}'

smoke:
	curl -sS http://127.0.0.1:18080/healthz
	curl -sS http://127.0.0.1:18100/readyz
	curl -sS -X POST http://127.0.0.1:18100/api/v1/vacancies/ingest \
		-H 'content-type: application/json' \
		-d '{"source_channel":"@smoke","recruiter_handle":"@smoke_hr","vacancy_text":"Senior Python Backend Engineer. Docker, FastAPI, PostgreSQL, observability, LLM integrations. Remote."}'
