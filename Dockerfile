# Imagen del worker: `handoff worker` lee Slack, encola e investiga en bucle.
# Sin puerto ni servidor web. Las claves llegan como variables de entorno del
# servicio: .dockerignore deja fuera .env y cualquier dato de personas reales.

FROM ghcr.io/astral-sh/uv:0.11.28 AS uv

FROM python:3.13-slim AS build
# python-jobspy fija numpy 1.26.3, que no tiene wheel para Python 3.13: hay que
# compilarla. El compilador vive solo en esta etapa; la imagen final no lo lleva.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Primero solo las dependencias: mientras no cambie uv.lock, esta capa se
# reutiliza y un cambio de código no vuelve a descargar todo.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim
RUN useradd --create-home --uid 10001 handoff
COPY --from=build --chown=handoff:handoff /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
USER handoff
WORKDIR /app
# Forma exec, sin shell de por medio: Python es el PID 1 y recibe el SIGTERM
# de Railway directamente. Con un shell delante la señal no llegaría y el
# worker moriría por SIGKILL a mitad de una investigación.
CMD ["handoff", "worker"]
