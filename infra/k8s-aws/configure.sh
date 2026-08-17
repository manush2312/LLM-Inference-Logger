#!/usr/bin/env bash
#
# Fills in the two values that cannot be committed: the hostname the certificate
# is issued for, and the address Let's Encrypt sends expiry notices to.
#
#   ./configure.sh 3-109-10-125.sslip.io you@example.com
#
# Idempotent: it rewrites whatever is currently there, so running it again with a
# new hostname is the supported way to move the deployment to a different address.
set -euo pipefail

HOST="${1:-}"
EMAIL="${2:-}"

if [[ -z "$HOST" || -z "$EMAIL" ]]; then
  echo "usage: $0 <hostname> <email>" >&2
  echo "example: $0 3-109-10-125.sslip.io you@example.com" >&2
  exit 1
fi

# A bare IP cannot hold a public certificate, and this is a cheap mistake to make
# with an sslip.io hostname sitting right next to one.
if [[ "$HOST" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "ERROR: '$HOST' is an IP address, not a hostname." >&2
  echo "Let's Encrypt cannot issue a certificate for an IP." >&2
  echo "Use the sslip.io form instead: ${HOST//./-}.sslip.io" >&2
  exit 1
fi

if [[ "$HOST" == *.amazonaws.com ]]; then
  echo "ERROR: Let's Encrypt refuses to issue for *.amazonaws.com," >&2
  echo "so the EC2 public DNS name cannot be used. Use an sslip.io" >&2
  echo "hostname or a domain you control." >&2
  exit 1
fi

cd "$(dirname "$0")"

# In-place across both files. -i.bak then remove, because GNU and BSD sed
# disagree about bare -i and this runs on a server and on a laptop.
for f in ingress.yaml issuer.yaml; do
  sed -i.bak "s|HOSTNAME_PLACEHOLDER|${HOST}|g; s|EMAIL_PLACEHOLDER|${EMAIL}|g" "$f"
  rm -f "$f.bak"
done

echo "configured:"
echo "  hostname : $HOST"
echo "  email    : $EMAIL"
echo
grep -n "host:\|email:" ingress.yaml issuer.yaml | sed 's/^/  /'
echo
echo "Next: kubectl apply -k infra/k8s-aws"
