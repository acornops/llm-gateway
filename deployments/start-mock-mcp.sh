#!/bin/sh
set -eu

certificate_directory=/certs
certificate_file="${certificate_directory}/mock-mcp-cert.pem"
private_key_file="${certificate_directory}/mock-mcp-key.pem"

mkdir -p "${certificate_directory}"

if [ ! -s "${certificate_file}" ] || [ ! -s "${private_key_file}" ]; then
  openssl req \
    -x509 \
    -newkey rsa:2048 \
    -sha256 \
    -nodes \
    -days 2 \
    -subj "/CN=localhost" \
    -addext "subjectAltName=DNS:localhost,DNS:mock-mcp,IP:127.0.0.1" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign" \
    -keyout "${private_key_file}" \
    -out "${certificate_file}"
  chmod 0600 "${private_key_file}"
fi

exec uvicorn mock_mcp_server:app \
  --host 0.0.0.0 \
  --port 8002 \
  --ssl-keyfile "${private_key_file}" \
  --ssl-certfile "${certificate_file}"
