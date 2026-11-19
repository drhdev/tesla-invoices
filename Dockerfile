FROM mcr.microsoft.com/playwright/python:v1.58.0-noble
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && install -d -m 0700 -o pwuser -g pwuser /data
COPY tesla_invoice ./tesla_invoice
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER pwuser
ENTRYPOINT ["python", "-m", "tesla_invoice"]
