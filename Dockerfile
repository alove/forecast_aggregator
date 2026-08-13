FROM python:3.12-slim

WORKDIR /app
COPY forecast_collector ./forecast_collector
COPY README.md LICENSE ./

RUN useradd --create-home --uid 10001 collector \
    && mkdir -p /data \
    && chown -R collector:collector /data
USER collector
WORKDIR /data
ENV PYTHONPATH=/app

ENTRYPOINT ["python", "-m", "forecast_collector"]
CMD ["collect", "--output", "/data/election_forecasts_2026.csv"]
