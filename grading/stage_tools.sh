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

# The loader, copied and named. ldd lists it WITHOUT a "=>", so no closure ever
# picks it up, and every wrapper below invokes it by path. One function because
# staging LibreOffice separately got this wrong and shipped a wrapper pointing
# at a loader that was not there.
stage_loader() {
  local tool_dir=$1 entry=$2 interp
  interp=$(patchelf --print-interpreter "$entry")
  cp -Ln "$interp" "$tool_dir/lib/"
  echo "${interp##*/}"
}

stage() {
  local name=$1 tool_dir interp
  shift
  tool_dir="$PREFIX/tools/$name"
  mkdir -p "$tool_dir/bin" "$tool_dir/lib" "$tool_dir/libexec"
  for binary in "$@"; do
    stage_one "$tool_dir" "$binary"
  done

  interp=$(stage_loader "$tool_dir" "$tool_dir/libexec/$1")
  patchelf --set-rpath '$ORIGIN/../lib' "$tool_dir"/libexec/*
  # A staged library can pull in another, and looks for it beside itself.
  for lib in "$tool_dir"/lib/*; do
    if [ "$lib" != "$tool_dir/lib/$interp" ]; then
      patchelf --set-rpath '$ORIGIN' "$lib"
    fi
  done

  for binary in "$@"; do
    write_wrapper "$tool_dir" "$binary" "$interp"
  done
  LOADER="$interp"  # read by the closure check
}

# The font config a rendering tool reads from a mount. Written per tool and
# from one definition, because the paths are relative to the file's own
# directory and every caller sits at the same depth: <prefix>/tools/<tool>/etc.
#
# The conf.d include is load-bearing. Without it Arial resolves to DejaVu Sans
# instead of Liberation Sans, so a chart or a page the lane rendered comes back
# in another typeface. The cachedir has to be writable or fontconfig finds no
# fonts at all.
write_fonts_conf() {
  local etc_dir=$1
  mkdir -p "$etc_dir"
  cat > "$etc_dir/fonts.conf" <<'FONTS'
<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">
<fontconfig>
  <dir prefix="relative">../../../../usr/share/fonts</dir>
  <include prefix="relative" ignore_missing="yes">../../../../etc/fonts/conf.d</include>
  <cachedir>/tmp/.grading-fontconfig</cachedir>
</fontconfig>
FONTS
}

# ffmpeg, for video_ffmpeg_verifier. Poppler's exact shape: a binary with a
# shared-library closure and nothing that finds itself by path, so `stage`
# handles it unchanged. It reads 127 from a mount unstaged (spike #20977).
stage ffmpeg ffmpeg ffprobe

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
  local binary=$1 flag=$2 out
  if [ "$(head -c 2 "$binary")" != "#!" ]; then
    echo "BUILD FAIL: $binary is not a wrapper, so it carries a baked path" >&2
    exit 1
  fi
  # The flag differs per tool: poppler answers -v, ffmpeg reads -v as a
  # loglevel and needs -version.
  if ! out=$("$binary" "$flag" 2>&1); then
    echo "BUILD FAIL: $binary does not run from $PREFIX" >&2
    echo "$out" >&2
    exit 1
  fi
  echo "$out" | head -1
}

for tool_binary in pdfinfo pdftoppm pdftocairo; do
  verify "$PREFIX/tools/poppler/bin/$tool_binary" -v
done
for tool_binary in ffmpeg ffprobe; do
  verify "$PREFIX/tools/ffmpeg/bin/$tool_binary" -version
done

# A version banner proves the binary starts and its direct libraries resolve.
# It does not prove the tool can do its job, and for poppler that difference is
# the CMaps: they live outside the binary and a missing one renders a CJK PDF
# differently with no error. Same shape as LibreOffice's filters below.
work=$(mktemp -d)
{
  printf '%%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n'
  printf '2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n'
  printf '3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 99 99]>>endobj\n'
  printf 'trailer<</Root 1 0 R>>\n'
} > "$work/p.pdf"
if ! "$PREFIX/tools/poppler/bin/pdftoppm" -png -r 10 "$work/p.pdf" "$work/out" \
  2>"$work/err"; then
  echo "BUILD FAIL: staged pdftoppm starts but cannot rasterise a PDF:" >&2
  cat "$work/err" >&2
  exit 1
fi
# ffmpeg's encoders are separate objects it loads, so encoding is the check
# that reaches them.
if ! "$PREFIX/tools/ffmpeg/bin/ffmpeg" -f lavfi -i testsrc=duration=1:size=32x32:rate=1 \
  -y "$work/v.mp4" >"$work/err" 2>&1; then
  echo "BUILD FAIL: staged ffmpeg starts but cannot encode:" >&2
  cat "$work/err" >&2
  exit 1
fi
if [ ! -s "$work/v.mp4" ]; then
  echo "BUILD FAIL: staged ffmpeg reported success and wrote nothing" >&2
  exit 1
fi
rm -rf "$work"


# LibreOffice: made path-independent where apt put it, NOT relocated.
#
# Spike #20977 measured every other option. Moving the install gives
# DeploymentException, because it resolves its registry against the install
# path. Linking the path back at mount time fixes that and leaves it loading
# the world's libraries. And naming the whole of our usr/lib/x86_64-linux-gnu
# on LD_LIBRARY_PATH broke the sandbox: our libc is 2.36, the world is trixie,
# and /bin/sh died with "version GLIBC_2.38 not found".
#
# So: leave the tree, make its internals relative, and give it a library
# directory holding ONLY its own dependencies. The world's loader keeps serving
# libc, which is the pairing that must not be broken.
LO=/usr/lib/libreoffice
# Every ${ORIGIN} and ${BRAND_BASE_DIR} below is written into a config file for
# LibreOffice to expand, so the shell must not expand it. That is SC2016's
# whole subject, and here it is the intent.
# shellcheck disable=SC2016
if [ -d "$LO" ]; then
  lo_rc="$LO/program/fundamentalrc"

  # 1. Every path this install resolves against itself. BRAND_BASE_DIR is the
  #    anchor the rest of fundamentalrc hangs off, and ${ORIGIN} is program/,
  #    so the install root is one above it.
  #
  #    CONFIGURATION_LAYERS is the one that decides whether any of this works.
  #    7.4.7 names the registry outright where 25.2 anchors it on
  #    BRAND_BASE_DIR, so rewriting only the anchor leaves configmgr reading
  #    the WORLD's /etc/libreoffice/registry, which is not there.
  for lo_key in BRAND_BASE_DIR CONFIGURATION_LAYERS; do
    grep -q "^$lo_key=" "$lo_rc" && continue
    echo "BUILD FAIL: no $lo_key in fundamentalrc, so this LibreOffice anchors" \
      "its paths some other way and the rewrites below are wrong" >&2
    exit 1
  done
  sed -i 's|^BRAND_BASE_DIR=.*|BRAND_BASE_DIR=${ORIGIN}/..|' "$lo_rc"
  sed -i 's|file:///etc/libreoffice/registry|${BRAND_BASE_DIR}/share/registry|g' \
    "$lo_rc"
  # Bootstrap values and the python bridge. Neither is load-bearing for a
  # conversion, and both read out of the world's filesystem if left alone.
  sed -i 's|^FHS_CONFIG_FILE=file:///etc/|FHS_CONFIG_FILE=${ORIGIN}/../../../../etc/|' \
    "$LO/program/sofficerc"
  sed -i 's|^PYTHONHOME=file:///usr/lib/|PYTHONHOME=$ORIGIN/../../../lib/|' \
    "$LO/program/pythonloader.unorc"
  # The assert that matters, and the one the first attempt did not have: any
  # install path still pointing at the world. hsqldb is a java classpath entry
  # and this image installs no java.
  lo_absolute=$(grep -n 'file:///usr\|file:///etc' "$LO"/program/*rc \
    | grep -v hsqldb || true)
  if [ -n "$lo_absolute" ]; then
    echo "BUILD FAIL: LibreOffice still resolves these against the world," \
      "so a mounted copy reads a path it does not own:" >&2
    echo "$lo_absolute" >&2
    exit 1
  fi

  # 2. An absolute symlink leaves the tree once the tree is read at <mount>/...
  #    On 7.4.7 share/registry is itself a directory symlink into /etc, so this
  #    is what carries the registry into the mount. -m because some targets are
  #    not present at build time and a resolving realpath refuses those.
  find "$LO" -type l | while read -r lo_link; do
    case "$(readlink "$lo_link")" in
      /*) ln -sfn "$(realpath -m --relative-to="$(dirname "$lo_link")" \
            "$(readlink "$lo_link")")" "$lo_link" ;;
    esac
  done

  # 3. Its dependencies, as relative symlinks, which costs kilobytes instead of
  #    the 116MB a copy costs.
  #
  #    MEASURED, not predicted. ldd reports what an object declares, and NSS
  #    dlopens its softoken module: without libsoftokn3 the PDF export fails
  #    while --version still exits 0, so no version probe catches it. One real
  #    conversion under LD_DEBUG names every object that actually loads.
  lo_lib="$PREFIX/tools/libreoffice/lib"
  mkdir -p "$lo_lib"
  lo_work=$(mktemp -d)
  printf 'a,b\n1,2\n' > "$lo_work/probe.csv"
  LD_DEBUG=libs LD_DEBUG_OUTPUT="$lo_work/trace" \
    "$LO/program/soffice" --headless \
    -env:UserInstallation="file://$lo_work/profile" \
    --convert-to pdf --outdir "$lo_work" "$lo_work/probe.csv" \
    >"$lo_work/out" 2>&1 || true
  if [ ! -s "$lo_work/probe.pdf" ]; then
    echo "BUILD FAIL: LibreOffice cannot convert inside its own image, so the" \
      "closure below would be measured from a run that did nothing:" >&2
    cat "$lo_work/out" >&2
    exit 1
  fi
  {
    awk '/calling init:/{print $NF}' "$lo_work"/trace.* 2>/dev/null || true
    find "$LO/program" -name '*.so*' -o -name 'soffice.bin' -o -name 'oosplash' \
      | while read -r lo_obj; do
          ldd "$lo_obj" 2>/dev/null | awk '/=> \//{print $3}' || true
        done
  } | sort -u | while read -r lo_dep; do
    case "$lo_dep" in
      # program/ has to keep resolving through its own RPATH. A symlink to it
      # here moves $ORIGIN to this directory and the UNO bootstrap stops
      # finding unorc beside itself, which spike #20977 read as
      # soffice_starts rc=134 with DeploymentException.
      "$LO"/*) continue ;;
      /*) ;;
      *) continue ;;
    esac
    case "$(basename "$lo_dep")" in
      libc.so.*|libm.so.*|libpthread.so.*|libdl.so.*|librt.so.*|\
      libresolv.so.*|ld-linux*|libgcc_s.so.*) continue ;;
    esac
    [ -e "$lo_dep" ] || continue
    ln -sfn "$(realpath -m --relative-to="$lo_lib" "$lo_dep")" \
      "$lo_lib/$(basename "$lo_dep")"
  done
  rm -rf "$lo_work"
  # NSS is the whole reason the closure is measured rather than declared. A
  # trace that missed it ships a mount whose PDF export fails and whose probe
  # passes, which is the failure this file exists to make impossible.
  for lo_dlopened in libsoftokn3.so libfreeblpriv3.so; do
    [ -e "$lo_lib/$lo_dlopened" ] && continue
    echo "BUILD FAIL: $lo_dlopened is not in the measured closure, so a" \
      "mounted PDF export fails while --version still exits 0" >&2
    exit 1
  done

  # 4. Fonts change rendering, and the world's set is not ours. The conf.d
  #    include is load-bearing: without it Arial resolves to DejaVu Sans
  #    instead of Liberation Sans, so every chart the lane rendered comes back
  #    in a different typeface. The cachedir is writable because fontconfig
  #    finds no fonts at all without one.
  write_fonts_conf "$PREFIX/tools/libreoffice/etc"
fi


# Chromium, the same shape as LibreOffice: left where playwright put it, and
# given a directory of its own dependencies.
#
# Spike #20977 measured it. The mounted binary runs in a world that ships
# chromium and returns 127 in one that does not, which is the loader and not a
# path: `playwright install --with-deps` put the OS libraries in THIS image and
# the world has no reason to carry them. bua_verifier drives a live headless
# browser, so without this those grades belong on the lane.
#
CR=""
for cr_candidate in /root/.cache/ms-playwright/chromium-*/chrome-linux*/chrome; do
  [ -x "$cr_candidate" ] || continue
  CR=$cr_candidate
  break
done
if [ -n "$CR" ]; then
  cr_dir="$PREFIX/tools/chromium"
  mkdir -p "$cr_dir/bin" "$cr_dir/lib"

  # A stable path, because playwright keys its directory on a build number and
  # nothing at runtime should have to glob for it. Relative, so it survives the
  # move like every other link here.
  ln -sfn "$(realpath -m --relative-to="$cr_dir/bin" "$CR")" "$cr_dir/bin/chrome"

  # MEASURED, like LibreOffice's. ldd covers what the binary declares, and one
  # real page load covers what it opens after that.
  cr_work=$(mktemp -d)
  LD_DEBUG=libs LD_DEBUG_OUTPUT="$cr_work/trace" \
    "$CR" --headless --no-sandbox --disable-gpu --dump-dom about:blank \
    >"$cr_work/dom" 2>"$cr_work/err" || true
  if ! grep -q "</html>" "$cr_work/dom" 2>/dev/null; then
    echo "BUILD FAIL: chromium cannot load a page in its own image, so the" \
      "closure below would be measured from a run that rendered nothing:" >&2
    tail -5 "$cr_work/err" >&2
    exit 1
  fi
  {
    awk '/calling init:/{print $NF}' "$cr_work"/trace.* 2>/dev/null || true
    ldd "$CR" 2>/dev/null | awk '/=> \//{print $3}' || true
    for cr_obj in "$(dirname "$CR")"/*.so*; do
      [ -e "$cr_obj" ] || continue
      ldd "$cr_obj" 2>/dev/null | awk '/=> \//{print $3}' || true
    done
  } | sort -u | while read -r cr_dep; do
    case "$cr_dep" in
      # Chromium ships libEGL, libGLESv2, libvk_swiftshader and libffmpeg
      # beside its own binary, and they find each other from there. Shadowing
      # them here is the mistake that cost LibreOffice a build.
      /root/.cache/ms-playwright/*) continue ;;
      /*) ;;
      *) continue ;;
    esac
    case "$(basename "$cr_dep")" in
      libc.so.*|libm.so.*|libpthread.so.*|libdl.so.*|librt.so.*|\
      libresolv.so.*|ld-linux*|libgcc_s.so.*) continue ;;
    esac
    [ -e "$cr_dep" ] || continue
    ln -sfn "$(realpath -m --relative-to="$cr_dir/lib" "$cr_dep")" \
      "$cr_dir/lib/$(basename "$cr_dep")"
  done
  rm -rf "$cr_work"

  # Fonts, because a page the judge reads is a page chromium rendered, and the
  # world's font set is not the lane's.
  write_fonts_conf "$cr_dir/etc"

  # The assert: the staged tree, and only the staged tree, loads a page. Run
  # with the closure alone on the path, which is what the grade does.
  if ! LD_LIBRARY_PATH="$cr_dir/lib" FONTCONFIG_FILE="$cr_dir/etc/fonts.conf" \
    "$cr_dir/bin/chrome" --headless --no-sandbox --disable-gpu \
    --dump-dom about:blank 2>/dev/null | grep -q "</html>"; then
    echo "BUILD FAIL: chromium does not load a page from the staged closure," \
      "so a mounted bua_verifier would get 127" >&2
    exit 1
  fi
  echo "staged chromium: $(find "$cr_dir/lib" -type l | wc -l) libraries named"
else
  echo "BUILD FAIL: no chromium under /root/.cache/ms-playwright, so" \
    "playwright install did not run or moved its registry" >&2
  exit 1
fi


# The assert that matters, twice over.
#
# First the library closure. `--library-path` is additive, so a library ldd
# missed still resolves from this host's ld.so.cache and the tree only dies on
# a world image. Ask the staged loader what it would load and fail on anything
# outside the staged lib/.
for spec in "poppler pdfinfo pdftoppm pdftocairo" "ffmpeg ffmpeg ffprobe"; do
 # shellcheck disable=SC2086
 set -- $spec
 tool=$1
 shift
 for tool_binary in "$@"; do
  tool_dir="$PREFIX/tools/$tool"
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
done

# The paths are NOT rehearsed here. The closure check above is what proves the
# tree carries what it needs; whether it works at the mount point is answered a
# few seconds into a trajectory, by unusable_mounted_tools(), which falls back
# to the lane. A build-time dress rehearsal guarded a failure that already
# recovers on its own.


echo "staged tools under $PREFIX/tools, and they run from anywhere"
