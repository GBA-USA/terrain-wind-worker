#!/usr/bin/env bash
# Install the mass-conserving WindNinja CLI (no OpenFOAM) into WINDNINJA_PREFIX.
# Pinned to firelab/windninja 4.0.0. A warm actions/cache hit skips the build.
set -euo pipefail

PREFIX="${WINDNINJA_PREFIX:-$HOME/windninja}"
REF="4.0.0"

if [[ -x "$PREFIX/bin/WindNinja_cli" ]]; then
  exit 0
fi

export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq
sudo apt-get install -y -qq \
  build-essential cmake ninja-build pkg-config git ca-certificates \
  gdal-bin libgdal-dev libnetcdf-dev \
  libboost-date-time-dev libboost-program-options-dev libboost-test-dev \
  libshp-dev

SRC="${RUNNER_TEMP:-/tmp}/windninja-src"
BUILD="${RUNNER_TEMP:-/tmp}/windninja-build"
rm -rf "$SRC" "$BUILD"
git clone --depth 1 --branch "$REF" https://github.com/firelab/windninja.git "$SRC"

export CC=gcc CXX=g++
cmake -S "$SRC" -B "$BUILD" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX="$PREFIX" \
  -DNINJA_CLI=ON \
  -DNINJA_GUI=OFF \
  -DNINJAFOAM=OFF \
  -DWITH_LCP_CLIENT=ON \
  -DENABLE_GMTED=OFF \
  -DPHONE_HOME_QUERIES=OFF \
  -DDISABLE_THREDDS_UPDATE=ON \
  -DBUILD_TESTING=OFF \
  -DBUILD_SURFACE_INPUT_NODATA_FILLER=OFF \
  -DBUILD_FETCH_DEM=OFF \
  -DBUILD_STL_CONVERTER=OFF \
  -DBUILD_CONVERT_OUTPUT=OFF \
  -DBUILD_WRF_TO_KMZ=OFF \
  -DBUILD_HRRR_TO_KMZ=OFF \
  -DBUILD_SLOPE_ASPECT_GRID=OFF \
  -DBUILD_FLOW_SEPARATION_GRID=OFF \
  -DBUILD_SOLAR_GRID=OFF \
  -DOPENMP_SUPPORT=ON \
  -DSUPRESS_WARNINGS=ON \
  -DWITH_NOMADS_SUPPORT=ON \
  -DSKIP_GETDATE=ON

cmake --build "$BUILD" --target WindNinja_cli --parallel "$(nproc)"
cmake --install "$BUILD"
test -x "$PREFIX/bin/WindNinja_cli"
test -f "$PREFIX/share/windninja/date_time_zonespec.csv"
