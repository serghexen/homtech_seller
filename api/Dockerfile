FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# T-Bank API uses the Russian Trusted CA chain, which is not included in
# Python/certifi by default. Keep the normal Debian public CA bundle and add
# only the two CA certificates required by T-Bank.
COPY certs/russian_trusted_root_ca.crt /usr/local/share/ca-certificates/russian_trusted_root_ca.crt
COPY certs/russian_trusted_sub_ca.crt /usr/local/share/ca-certificates/russian_trusted_sub_ca.crt
RUN update-ca-certificates
ENV TBANK_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

COPY app.py .
COPY worker.py .
COPY notifier.py .
COPY domains ./domains
COPY scripts ./scripts

RUN adduser --disabled-password --gecos "" appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
CMD ["sh", "-lc", "uvicorn app:app --host 0.0.0.0 --port 8000 --workers ${UVICORN_WORKERS:-1}"]
