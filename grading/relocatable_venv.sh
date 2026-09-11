#!/usr/bin/env bash
# Build a grading venv that works after it is mounted somewhere else.
#
# A venv bakes absolute paths into bin/python and pyvenv.cfg, and the tree is
# built at one path and read at another, so those baked paths name the second.
# Recipe from hosted-envs/hosted_envs/grading_volume.py.
#
#   relocatable_venv.sh <build_dir> <runtime_dir>
set -euo pipefail

BUILD="${1:?usage: relocatable_venv.sh <build_dir> <runtime_dir>}"
RUNTIME="${2:?usage: relocatable_venv.sh <build_dir> <runtime_dir>}"
# Stripping "/" leaves "", which joins correctly as /x but is not a directory:
# `cd ""` stays put.
BUILD="${BUILD%/}"
RUNTIME="${RUNTIME%/}"
BUILD_DIR="${BUILD:-/}"

# Inside the tree, so the interpreter travels with it.
export UV_PYTHON_INSTALL_DIR="${BUILD}/.python"
uv python install 3.13

# Both a versioned directory (cpython-3.13.15-...) and an alias get written.
CPYTHON_BIN="$(find "${BUILD}/.python" -maxdepth 3 -path '*/cpython-3.13.*/bin/python3.13' | sort | head -1)"

cd "${BUILD_DIR}"
uv venv --relocatable --python "${CPYTHON_BIN}" "${BUILD}/.venv"
# shellcheck disable=SC1091
. "${BUILD}/.venv/bin/activate"
# --no-install-project keeps this layer cacheable; --all-groups brings the
# data-science group llm_code_verifier lets a verifier import.
uv sync --all-extras --all-groups --no-dev --no-cache --frozen --no-install-project
# The toolchain pip-installs these on top of the group. numpy follows its
# 2.0.2, not the group's 2.1.3: the lists disagree and the lane's wins.
uv pip install --no-cache fastapi "numpy==2.0.2"

# LAST: uv sync re-touches bin/python. Symlinks are relative so they survive
# the move; pyvenv.cfg cannot be, so it names RUNTIME.
CPY="$(basename "$(find "${BUILD}/.python" -maxdepth 1 -type d -name 'cpython-3.13.*' | sort | head -1)")"
cd "${BUILD}/.venv/bin"
rm -f python python3 python3.13
ln -s "../../.python/${CPY}/bin/python3.13" python3.13
ln -s python3.13 python3
ln -s python3.13 python
sed -i "s|^home = .*|home = ${RUNTIME}/.python/${CPY}/bin|" "${BUILD}/.venv/pyvenv.cfg"

# Resolve within the tree or it dangles once mounted. Against BUILD, since
# RUNTIME does not exist yet.
RESOLVED="$(realpath "${BUILD}/.venv/bin/python")"
case "${RESOLVED}" in
  "${BUILD}"/*) ;;  # "" for a root build, so this is any absolute path
  *)
    echo "relocatable_venv: bin/python resolves to ${RESOLVED}, outside ${BUILD}" >&2
    exit 1
    ;;
esac
grep -q "^home = ${RUNTIME}/" "${BUILD}/.venv/pyvenv.cfg" || {
  echo "relocatable_venv: pyvenv.cfg home does not name ${RUNTIME}" >&2
  exit 1
}
