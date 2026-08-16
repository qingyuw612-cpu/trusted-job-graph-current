ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

LABEL org.opencontainers.image.title="Trusted Job Knowledge Graph" \
      org.opencontainers.image.description="Review server for job panorama, emerging roles and ability changes"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOST=0.0.0.0 \
    APP_PORT=8080

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app
COPY --chown=app:app deployment_app.py ./
COPY --chown=app:app demo_data ./demo_data
COPY --chown=app:app trusted_graph_agent/static/panorama.html ./trusted_graph_agent/static/panorama.html
COPY --chown=app:app new_role_discovery/static ./new_role_discovery/static
COPY --chown=app:app trusted_graph_agent/__init__.py trusted_graph_agent/neo4j_repository.py trusted_graph_agent/neo4j_filtered_view.py ./trusted_graph_agent/

USER app
EXPOSE 8080
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
  CMD python -c "import json,urllib.request; d=json.load(urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=2)); assert d['status']=='ok'"

CMD ["python", "deployment_app.py"]
