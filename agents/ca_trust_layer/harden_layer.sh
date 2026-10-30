#!/bin/sh
# Distrust the emSign roots and Sectigo AAA in a built image's own trust stores (MKT-3922).
# Idempotent POSIX sh for any distro: a missing tool is logged and skipped, never fatal.
set -u

HERE=$(dirname "$0")
DISTRUST='emSign|AAA Certificate Services'
OS_BUNDLES="/etc/ssl/certs/ca-certificates.crt
/etc/pki/tls/certs/ca-bundle.crt
/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem
/etc/ssl/ca-bundle.pem
/etc/pki/tls/cacert.pem
/etc/ssl/cert.pem"
log() { printf 'harden_layer: %s\n' "$*" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }

# The subject of the one certificate in PEM file $1, from whichever reader the image has.
subject() {
  if have openssl; then openssl x509 -noout -subject -in "$1" 2>/dev/null
  elif have python3; then python3 -c 'import ssl, sys; print(ssl._ssl._test_decode_cert(sys.argv[1])["subject"])' "$1" 2>/dev/null
  else return 2
  fi
}
have openssl || have python3 || log "WARNING: no openssl or python3; only the distro tool can distrust here"

# 1. Debian/Ubuntu/Alpine: disable the roots and regenerate, as the built images do (Alpine has no --fresh).
if [ -f /etc/ca-certificates.conf ] && have update-ca-certificates; then
  sed -i -E 's#^(mozilla/(emSign|Comodo_AAA_Services_root))#!\1#' /etc/ca-certificates.conf
  update-ca-certificates --fresh >/dev/null 2>&1 || update-ca-certificates >/dev/null 2>&1 \
    || log "warning: update-ca-certificates failed"
fi

# 2. Every OS bundle, filtered by subject: covers distros without the tool and CAs added locally.
work=$(mktemp -d)
echo "$OS_BUNDLES" | while IFS= read -r p; do
  [ -f "$p" ] || continue
  rm -f "$work"/c.*
  awk -v d="$work" '/-----BEGIN CERTIFICATE-----/ { f = sprintf("%s/c.%05d", d, ++n) } f { print > f } /-----END CERTIFICATE-----/ { close(f); f = "" }' "$p"
  : > "$work/kept"
  dropped=0
  for c in "$work"/c.*; do
    [ -f "$c" ] || continue
    if subject "$c" | grep -Eq "$DISTRUST"; then dropped=$((dropped + 1)); else cat "$c" >> "$work/kept"; fi
  done
  # cat > keeps the inode, so the links onto the bundle see the new contents.
  [ "$dropped" = 0 ] || { cat "$work/kept" > "$(readlink -f "$p")" && log "bundle: $p dropped $dropped"; }
done
rm -rf "$work"
OS_BUNDLE=$(echo "$OS_BUNDLES" | while IFS= read -r p; do [ -f "$p" ] && echo "$p" && break; done)

# 3. The hashed dirs OpenSSL CApath, Go and Node read one file at a time.
for dir in /etc/ssl/certs /etc/pki/tls/certs; do
  for f in "$dir"/*; do
    [ -f "$f" ] || continue
    [ "$(grep -c 'BEGIN CERTIFICATE' "$f" 2>/dev/null)" = 1 ] || continue
    subject "$f" | grep -Eq "$DISTRUST" && rm -f "$f" && log "hashed: removed $f"
  done
done

# 4. Vendored Python bundles point at the OS bundle; ln -sf never writes through a uv-cache hardlink.
if [ -n "$OS_BUNDLE" ]; then
  find / -xdev \( -path /proc -o -path /sys \) -prune -o \
    \( -path '*/certifi/cacert.pem' -o -path '*/botocore/cacert.pem' -o -path '*/requests/cacert.pem' \
       -o -path '*/grpc/_cython/_credentials/roots.pem' -o -path '*/httplib2/cacerts.txt' \) -print 2>/dev/null |
  while IFS= read -r f; do
    [ "$(readlink "$f" 2>/dev/null)" = "$OS_BUNDLE" ] || ln -sf "$OS_BUNDLE" "$f" || log "warning: could not relink $f"
  done
fi

# 5. Chromium-family browsers keep their own root store.
if find / -xdev \( -path /proc -o -path /sys \) -prune -o -type f \
  \( -name chromium -o -name chrome -o -name chrome-headless-shell -o -name headless_shell \) -print 2>/dev/null | grep -q .; then
  have certutil || { have apt-get && apt-get update -qq >/dev/null 2>&1 \
      && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends libnss3-tools >/dev/null 2>&1 \
      && rm -rf /var/lib/apt/lists/*; } || { have apk && apk add --no-cache nss-tools >/dev/null 2>&1; } \
    || log "warning: could not install certutil; Chromium gets policy only"
  sh "$HERE/chromium-emsign-distrust.sh" || log "warning: chromium-emsign-distrust.sh failed"
fi
exit 0
