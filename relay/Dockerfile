FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chmod=644 server.py utils.py ./

# run as an unprivileged user (8025 needs no root)
RUN useradd --system --uid 10001 --no-create-home relay
USER relay

EXPOSE 8025
CMD ["python", "server.py"]
