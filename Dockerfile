FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 BIND_HOST=0.0.0.0 PORT=8080 CACHE_DIR=/data/cache
RUN groupadd --gid 10001 cndecky && useradd --uid 10001 --gid cndecky --no-create-home cndecky && mkdir -p /data/cache && chown -R cndecky:cndecky /data
COPY --chown=cndecky:cndecky cndecky/ /app/cndecky/
COPY --chown=cndecky:cndecky scripts/ /app/scripts/
COPY --chown=cndecky:cndecky web/ /app/web/
USER cndecky
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"
CMD ["python", "-m", "cndecky.server"]
