FROM python:3.11-slim

WORKDIR /app

ARG TG_OUTREACH_BUILD_VERSION=dev
ARG TG_OUTREACH_GIT_SHA=unknown

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV TG_OUTREACH_BUILD_VERSION=${TG_OUTREACH_BUILD_VERSION}
ENV TG_OUTREACH_GIT_SHA=${TG_OUTREACH_GIT_SHA}

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tests ./tests

EXPOSE 8100

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]
