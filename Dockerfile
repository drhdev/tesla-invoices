FROM mcr.microsoft.com/playwright/python:v1.65.2-noble@sha256:38dd67fc654b56b3cba611e4c8eb71cd33750e1cd29eea2a5dc00b484817a75f
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && install -d -m 0700 -o pwuser -g pwuser /data
COPY tesla_invoice ./tesla_invoice
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER pwuser
ENTRYPOINT ["python", "-m", "tesla_invoice"]
