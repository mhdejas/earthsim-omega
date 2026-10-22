# EARTHSIM-Omega CPU image. NOT BUILT/TESTED in the development sandbox (no Docker available there).
FROM python:3.12-slim
ENV PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1
RUN useradd -m earthsim
WORKDIR /app
COPY pyproject.toml README.md requirements-lock.txt ./
COPY src ./src
COPY configs ./configs
COPY web ./web
COPY tests ./tests
RUN pip install -e ".[all]"
USER earthsim
# data/ is mounted as a volume so acquired data and outputs persist outside the container
VOLUME ["/app/data"]
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
CMD ["earthsim", "--set", "api.host=0.0.0.0", "serve"]
