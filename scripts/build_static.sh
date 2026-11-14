#!/usr/bin/env bash
# Build statically linked scx_fcp profiles, optional baseline schedulers, and
# the synthetic frame benchmark.
#
# Environment:
#   SCX_ROOT        scx workspace                 (default: $HOME/workspace/scx)
#   SCX_SUB         scheduler source directory    (default: $SCX_ROOT/scheds/rust/scx_fcp)
#   BIN_DIR         output binary directory       (default: <repo>/build/bin)
#   LOG_DIR         build log directory           (default: <repo>/build/logs)
#   TARGET          cargo target triple           (default: x86_64-unknown-linux-gnu)
#   PROFILES        default named zero-hint       (space separated)
#   BUILD_BASELINES 1 to also build scx_lavd, scx_p2dq, scx_bpfland
#   JOBS            cargo parallelism             (default: nproc)
#   ZSTD_A          path to libzstd.a             (auto-detected)
#   STRICT_SOURCE   1 to fail when the mirror differs from SCX_SUB
#   SYNC_SOURCE     1 to sync mirror into SCX_SUB (default: 1)
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
SCX_ROOT=${SCX_ROOT:-$HOME/workspace/scx}
SCX_SUB=${SCX_SUB:-$SCX_ROOT/scheds/rust/scx_fcp}
BIN_DIR=${BIN_DIR:-$REPO_ROOT/build/bin}
LOG_DIR=${LOG_DIR:-$REPO_ROOT/build/logs}
TARGET=${TARGET:-x86_64-unknown-linux-gnu}
PROFILES=${PROFILES:-"default named zero-hint"}
BUILD_BASELINES=${BUILD_BASELINES:-0}
JOBS=${JOBS:-$(nproc 2>/dev/null || echo 4)}
STRICT_SOURCE=${STRICT_SOURCE:-0}
SYNC_SOURCE=${SYNC_SOURCE:-1}

die() { echo "build_static: $*" >&2; exit 1; }
warn() { echo "build_static: warning: $*" >&2; }

[ -f "$SCX_ROOT/Cargo.toml" ] || die "SCX_ROOT is not an scx workspace: $SCX_ROOT"
[ -d "$SCX_SUB" ] || die "missing scheduler source: $SCX_SUB"
[ -f "$SCX_SUB/Cargo.toml" ] || die "missing $SCX_SUB/Cargo.toml"

if [ -z "${ZSTD_A:-}" ]; then
    for candidate in \
        /usr/lib/x86_64-linux-gnu/libzstd.a \
        /usr/lib64/libzstd.a \
        /usr/local/lib/libzstd.a \
        /usr/lib/libzstd.a
    do
        if [ -f "$candidate" ]; then
            ZSTD_A=$candidate
            break
        fi
    done
fi
[ -n "${ZSTD_A:-}" ] || die "libzstd.a not found; install libzstd-dev or set ZSTD_A"

mkdir -p "$BIN_DIR" "$LOG_DIR"

MIRROR_FILES=(
    build.rs
    src/bpf/intf.h
    src/bpf/main.bpf.c
    src/main.rs
    tools/scx_fcp_bench/Makefile
    tools/scx_fcp_bench/fcp_frame_bench.c
)
MISMATCH=0
for rel in "${MIRROR_FILES[@]}"; do
    if [ -f "$REPO_ROOT/$rel" ] && [ -f "$SCX_SUB/$rel" ] && ! cmp -s "$REPO_ROOT/$rel" "$SCX_SUB/$rel"; then
        if [ "$SYNC_SOURCE" = 1 ] && [ "$REPO_ROOT" != "$SCX_SUB" ]; then
            mkdir -p "$(dirname "$SCX_SUB/$rel")"
            cp -f "$REPO_ROOT/$rel" "$SCX_SUB/$rel"
        else
            warn "mirror differs from SCX_SUB: $rel"
            MISMATCH=1
        fi
    fi
done
if [ "$MISMATCH" = 1 ] && [ "$STRICT_SOURCE" = 1 ]; then
    die "source mirror does not match SCX_SUB; sync it or set STRICT_SOURCE=0"
fi

# Prefer Clang 22+ when available so BPF arena / may_goto code in scx_lavd and scx_p2dq passes verifier.
CLANG_PREBUILT=${CLANG_DIR:-"$HOME/workspace/aosp_research/cf/kernel/common-android-mainline/prebuilts/clang/host/linux-x86/clang-r596125"}
if [ -d "$CLANG_PREBUILT/bin" ]; then
    export PATH=${SCX_PATH:-$HOME/.cargo/bin:$CLANG_PREBUILT/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}
    export LIBCLANG_PATH="$CLANG_PREBUILT/lib"
else
    export PATH=${SCX_PATH:-$HOME/.cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}
fi
export RUSTFLAGS="-C target-feature=+crt-static -C link-arg=$ZSTD_A"
export CARGO_TERM_COLOR=never

run_logged() {
    local log=$1
    shift
    echo "build_static: $*"
    if ! "$@" >"$log" 2>&1; then
        echo "build_static: failed, tail of $log" >&2
        tail -n 60 "$log" >&2 || true
        exit 1
    fi
}

install_bin() {
    local src=$1
    local dst=$2
    [ -x "$src" ] || die "missing built binary $src"
    install -m 0755 "$src" "$BIN_DIR/$dst"
}

build_profile() {
    local profile=$1
    local name=$2
    local env_profile=$profile
    [ "$profile" = default ] && env_profile=""
    echo "build_static: profile $profile -> $BIN_DIR/$name"
    touch "$SCX_SUB/src/bpf/main.bpf.c"
    run_logged "$LOG_DIR/build-fcp-$profile.log" \
        env SCX_FCP_BUILD="$env_profile" \
        cargo build --locked --manifest-path "$SCX_ROOT/Cargo.toml" \
        --release --target "$TARGET" -j "$JOBS" -p scx_fcp
    install_bin "$SCX_ROOT/target/$TARGET/release/scx_fcp" "$name"
}

if [ "$BUILD_BASELINES" = 1 ]; then
    run_logged "$LOG_DIR/build-baselines.log" \
        cargo build --locked --manifest-path "$SCX_ROOT/Cargo.toml" \
        --release --target "$TARGET" -j "$JOBS" \
        -p scx_lavd -p scx_p2dq -p scx_bpfland
    install_bin "$SCX_ROOT/target/$TARGET/release/scx_lavd" scx_lavd
    install_bin "$SCX_ROOT/target/$TARGET/release/scx_p2dq" scx_p2dq
    install_bin "$SCX_ROOT/target/$TARGET/release/scx_bpfland" scx_bpfland
fi

for profile in $PROFILES; do
    case "$profile" in
        default) build_profile default scx_fcp ;;
        named) build_profile named scx_fcp_named ;;
        zero-hint|behavioral) build_profile zero-hint scx_fcp_behavioral ;;
        *) die "unknown profile $profile" ;;
    esac
done

BENCH_DIR="$SCX_SUB/tools/scx_fcp_bench"
[ -d "$BENCH_DIR" ] || BENCH_DIR="$SCX_ROOT/tools/scx_fcp_bench"
echo "build_static: building fcp_frame_bench in $BENCH_DIR"
make -C "$BENCH_DIR" clean >/dev/null 2>&1 || true
run_logged "$LOG_DIR/build-bench.log" \
    make -C "$BENCH_DIR" \
    CC="${CC:-cc}" \
    CFLAGS="-O2 -g -Wall -Wextra -Werror -std=c11 -static" \
    LDFLAGS="-pthread -lm"
install_bin "$BENCH_DIR/fcp_frame_bench" fcp_frame_bench

for bin in "$BIN_DIR"/*; do
    [ -f "$bin" ] || continue
    case "$(basename "$bin")" in
        SHA256SUMS) continue ;;
    esac
    file "$bin" | grep -Eq 'statically linked|static-pie linked' || \
        die "$(basename "$bin") is not statically linked"
    if readelf -d "$bin" 2>/dev/null | grep -q '(NEEDED)'; then
        die "$(basename "$bin") has a dynamic NEEDED entry"
    fi
done

(
    cd "$BIN_DIR"
    find . -maxdepth 1 -type f ! -name SHA256SUMS -printf '%f\n' | sort | xargs sha256sum > SHA256SUMS
)
echo "build_static: wrote $BIN_DIR/SHA256SUMS"
