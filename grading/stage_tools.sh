#!/usr/bin/env bash
# Copy poppler, with its libraries and loader, under PREFIX so it runs inside a
# container that has no poppler of its own.
#
# poppler and not ffmpeg. A missing poppler is silent: pdf_to_base64_images
# catches Exception and returns no images, so the judge scores a document it
# could not see. A missing ffmpeg is already loud, because video_ffmpeg_verifier
# reports it as ERROR. One is a wrong score, the other is an ungradeable that
# falls back to the lane, and ffmpeg's closure is 240 MB against poppler's 35.
#
# Nothing here may bake an absolute path. `mount_image` puts an image's root at
# the mount point, so a tree staged at PREFIX in this image is read at
# <mount>/PREFIX once mounted, and any path compiled in would point at nothing. RPATH uses $ORIGIN and each bin/ entry is a wrapper that resolves its
# own directory, so the tree runs wherever it is read from.
set -euo pipefail

PREFIX="${1:?usage: stage_tools.sh <prefix>}"

# Set by stage(), read by the closure check below. One tool, so one loader.
LOADER=""

stage_one() {
  local tool_dir=$1 binary=$2 resolved
  resolved=$(command -v "$binary")
  cp -L "$resolved" "$tool_dir/libexec/$binary"
  # Only "=>" lines name a real file; the rest are the loader and linux-vdso.
  ldd "$resolved" | awk '/=> \//{print $3}' | sort -u | while read -r lib; do
    cp -Ln "$lib" "$tool_dir/lib/"
  done
}

# The loader cannot be found through RPATH, because PT_INTERP is read before the
# process exists. Invoking it explicitly is what keeps the tree relocatable, and
# it has to be the staged one: it and libc are a matched pair, and a host loader
# against these libraries aborts with "stack smashing detected".
write_wrapper() {
  local tool_dir=$1 binary=$2 interp=$3
  cat > "$tool_dir/bin/$binary" <<WRAPPER
#!/bin/sh
root=\$(cd "\$(dirname "\$0")/.." && pwd)
exec "\$root/lib/$interp" --library-path "\$root/lib" "\$root/libexec/$binary" "\$@"
WRAPPER
  chmod +x "$tool_dir/bin/$binary"
}

stage() {
  local name=$1 tool_dir interp
  shift
  tool_dir="$PREFIX/tools/$name"
  mkdir -p "$tool_dir/bin" "$tool_dir/lib" "$tool_dir/libexec"
  for binary in "$@"; do
    stage_one "$tool_dir" "$binary"
  done

  interp=$(patchelf --print-interpreter "$tool_dir/libexec/$1")
  cp -Ln "$interp" "$tool_dir/lib/"
  patchelf --set-rpath '$ORIGIN/../lib' "$tool_dir"/libexec/*
  # A staged library can pull in another, and looks for it beside itself.
  for lib in "$tool_dir"/lib/*; do
    if [ "$lib" != "$tool_dir/lib/${interp##*/}" ]; then
      patchelf --set-rpath '$ORIGIN' "$lib"
    fi
  done

  for binary in "$@"; do
    write_wrapper "$tool_dir" "$binary" "${interp##*/}"
  done
  LOADER="${interp##*/}"  # read by the closure check
}

# pdf2image calls pdfinfo for the page count and pdftoppm or pdftocairo to raster.
stage poppler pdfinfo pdftoppm pdftocairo

# poppler-data, which apt pulls in as a Recommends. It holds the CMaps and
# CID-to-Unicode tables a PDF with a predefined CJK encoding needs, and a world
# image has no such directory, so the same PDF would render differently here and
# on the lane.
#
# It travels here, and only the consumer can place it. poppler takes the path
# from a compile-time /usr/share/poppler and reads no environment variable for
# it: the string POPPLER_DATADIR does not appear in libpoppler, while
# /usr/share/poppler does. So whoever mounts this tree also has to present
# share/poppler at /usr/share/poppler, which `mount_image` can do directly.
if [ ! -d /usr/share/poppler ]; then
  echo "BUILD FAIL: /usr/share/poppler is absent, so poppler-data is not" \
    "installed and CJK PDFs would render differently here and on the lane" >&2
  exit 1
fi
mkdir -p "$PREFIX/tools/poppler/share"
rm -rf "$PREFIX/tools/poppler/share/poppler"
cp -a /usr/share/poppler "$PREFIX/tools/poppler/share/poppler"

# Fail the build if a staged binary does not run. poppler takes `-v`, prints to
# stderr, and reads `-version` as a filename: pdfinfo and pdftoppm exit 1 on it
# and pdftocairo exits 99.
#
# Running it is not enough on its own. This build is the same distro it staged
# from, so a binary that still reaches the host's loader or libraries runs fine
# here and aborts only on a world image. The assert below is what pins that, and
# it holds wherever the build runs.
verify() {
  local binary=$1 out
  if [ "$(head -c 2 "$binary")" != "#!" ]; then
    echo "BUILD FAIL: $binary is not a wrapper, so it carries a baked path" >&2
    exit 1
  fi
  if ! out=$("$binary" -v 2>&1); then
    echo "BUILD FAIL: $binary does not run from $PREFIX" >&2
    echo "$out" >&2
    exit 1
  fi
  echo "$out" | head -1
}

for tool_binary in pdfinfo pdftoppm pdftocairo; do
  verify "$PREFIX/tools/poppler/bin/$tool_binary"
done

# The assert that matters, twice over.
#
# First the library closure. `--library-path` is additive, so a library ldd
# missed still resolves from this host's ld.so.cache and the tree only dies on
# a world image. Ask the staged loader what it would load and fail on anything
# outside the staged lib/.
for tool_binary in pdfinfo pdftoppm pdftocairo; do
  tool_dir="$PREFIX/tools/poppler"
  outside=$(LD_TRACE_LOADED_OBJECTS=1 "$tool_dir/lib/$LOADER" \
    --library-path "$tool_dir/lib" "$tool_dir/libexec/$tool_binary" \
    | awk '/=> \//{print $3}' | grep -v "^$tool_dir/lib/" || true)
  if [ -n "$outside" ]; then
    echo "BUILD FAIL: $tool_binary loads libraries from outside the staged tree," \
      "so it only works on a host that already has them:" >&2
    echo "$outside" >&2
    exit 1
  fi
done

# Then the paths. A consumer reads this tree at the mount point, not at PREFIX,
# so running it here proves only that it works where it was built. Copy it
# somewhere else and run it again.
elsewhere=$(mktemp -d)
cp -a "$PREFIX/tools" "$elsewhere/"
for tool_binary in pdfinfo pdftoppm pdftocairo; do
  if ! "$elsewhere/tools/poppler/bin/$tool_binary" -v >/dev/null 2>&1; then
    echo "BUILD FAIL: $tool_binary does not run from $elsewhere, so the tree is" \
      "not relocatable and a mount at any other path will not work" >&2
    exit 1
  fi
done
# The data moves with it, and no version banner would have noticed if it did not.
if [ ! -d "$elsewhere/tools/poppler/share/poppler/cMap" ]; then
  echo "BUILD FAIL: the CMaps did not survive relocation, so a CJK PDF would" \
    "render differently from the lane" >&2
  exit 1
fi
rm -rf "$elsewhere"

echo "staged tools under $PREFIX/tools, and they run from anywhere"
