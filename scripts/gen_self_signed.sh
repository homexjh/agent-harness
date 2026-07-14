#!/usr/bin/env bash
# 生成本地自签 TLS 证书，用于 uvicorn HTTPS（开发 / 演示）。
# 生产环境请改用 Caddy（自动签发受信任证书）或真实证书。
#
# 用法：
#   bash scripts/gen_self_signed.sh
# 生成：deploy/tls/{cert.pem,key.pem}
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/deploy/tls"
mkdir -p "$OUT"
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout "$OUT/key.pem" -out "$OUT/cert.pem" \
  -days 365 -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
chmod 600 "$OUT/key.pem"
echo "✅ 生成完成：$OUT/cert.pem / $OUT/key.pem"
echo ""
echo "启动带 TLS 的后端（前端 VITE_API_URL 改 https://127.0.0.1:8443）："
echo "  PYTHONPATH=. python -m uvicorn src.server.app:app \\"
echo "    --host 0.0.0.0 --port 8443 \\"
echo "    --ssl-keyfile deploy/tls/key.pem --ssl-certfile deploy/tls/cert.pem"
echo ""
echo "浏览器访问自签证书会报“不安全”，属正常；可用 curl -k 验证。"
