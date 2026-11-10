# Build pipeline

This document builds everything required to reproduce the paper on Cuttlefish:

1. the frozen `scx_fcp` scheduler and baselines;
2. the synthetic frame benchmark;
3. JankBench and UiBench workload APKs.

The build is currently split between the repository build script and the AOSP
workload trees. The commands below are the lab-tested path; use environment
variables instead of copying author-specific absolute paths.

## 1. Host prerequisites

Tested on Ubuntu 26.04 x86_64 with:

```text
Rust / Cargo 1.91+
Clang / LLVM 17+
bpftool, pahole, libelf-dev, libzstd-dev, zlib1g-dev
make, cc, python3, unzip, rsync
adb (Android platform-tools)
Android SDK + JDK for APK builds
KVM access for Cuttlefish
```

For APK builds, set:

```bash
export ANDROID_HOME=${ANDROID_HOME:-/path/to/Android/Sdk}
export JAVA_HOME=${JAVA_HOME:-/path/to/jdk}
```

## 2. Scheduler and synthetic benchmark

The canonical scheduler source is the `scx` workspace at
`$SCX_ROOT/scheds/rust/scx_fcp`; this repository mirrors it.

```bash
cd /path/to/scx_fcp

export SCX_ROOT=${SCX_ROOT:-$HOME/workspace/scx}
export BIN_DIR=${BIN_DIR:-$PWD/build/bin}

scripts/build_static.sh
```

This builds:

```text
scx_fcp
scx_fcp_behavioral
scx_fcp_named
fcp_frame_bench
```

and writes `SHA256SUMS`. Set `BUILD_BASELINES=1` to also build `scx_lavd`,
`scx_p2dq`, and `scx_bpfland`.

For the frozen paper scheduler, the evaluated binary is **not** the default
build:

```text
file:      scx_fcp
SHA-256:   5a2e7a7e5bca9a8cd0f9d25426e9766dae4ff4aaecfe22579a6a4d0371aab4c4
source:    artifact/old_5a2e_src
commit:    ab428c5c4dce30159e74837079522e8a2a566ff2
```

Use the frozen binary for the paper matrix. Do not silently replace it with a
newer development build.

The current lab build source is:

```bash
export OLD=${OLD:-$HOME/workspace/scx_tune_target/v_a_depth3_tgid1ms/x86_64-unknown-linux-gnu/release/scx_fcp}
sha256sum "$OLD"
```

The full-matrix driver checks this hash before it starts.

## 3. JankBench APK

JankBench is an intent-driven app, not an instrumentation test module. It must
hold `INJECT_EVENTS`, so the final APK must be platform-signed with the image's
platform test key.

Source location in AOSP:

```text
frameworks/base/tests/JankBench
```

Lab source / Gradle tree used for the evaluation:

```bash
export AOSP_TESTS=${AOSP_TESTS:-$HOME/work/aosp-tests}
cd "$AOSP_TESTS/JankBenchGradle"
./gradlew :app:assembleDebug
```

Then sign the resulting APK with the platform key used by the Cuttlefish image
(for example via `apksigner` or the AOSP signing tools), and place it at:

```text
paper/scx_fcp_v2/results/device/JankBench-platform-signed.apk
```

Record the SHA-256. The evaluated build used:

```text
package: com.android.benchmark
version: 2.2
sha256:  e4e521a3a6fa5c6a754fac9d4dcd9a5238c3c915af1dd84f1f086b12887974af
```

If the APK is missing or not platform-signed, the JankBench `INJECT_EVENTS`
grant fails and the matrix must stop; do not substitute `dumpsys gfxinfo`.

## 4. UiBench app and test APKs

Source locations in AOSP:

```text
frameworks/base/tests/UiBench
```

The evaluated source contains a bug in
`BitmapUploadActivity$UploadView.setColorValue`: the color animator can run
before `onAttachedToWindow()` allocates `mBitmap`, causing an NPE. Apply the
patch in `plan/85_AOSP_BUG_REPORTS.md` before building the final APK, or use
the already-built fixed APK.

Lab build tree:

```bash
export AOSP_TESTS=${AOSP_TESTS:-$HOME/work/aosp-tests}
cd "$AOSP_TESTS/UiBenchSuiteGradle"
./gradlew :app:assembleDebug :janktests:assembleDebug
```

Outputs:

```text
app/build/outputs/apk/debug/app-debug.apk
janktests/build/outputs/apk/debug/janktests-debug.apk
```

Copy them to:

```text
paper/scx_fcp_v2/results/device/UiBench-app-debug.apk
paper/scx_fcp_v2/results/device/UiBenchJankTests-debug.apk
```

The fixed app APK used in the lab is:

```text
work/aosp-tests/uibench-dl/UiBench-app-fixed-debug.apk
sha256: f8a6405f156861cfe4409b6001b97b37eb389293d0e5375fffae422fe34c5db2
```

Do not use the old UiBench app with the NPE if the run is expected to validate
`bitmap-upload`.

## 5. Expected build artifacts

Before launching the matrix, these must exist:

```text
$OLD                                                   # frozen scx_fcp
$BIN_DIR/fcp_frame_bench                              # synthetic bench
$BIN_DIR/scx_lavd
$BIN_DIR/scx_p2dq
paper/scx_fcp_v2/results/device/JankBench-platform-signed.apk
paper/scx_fcp_v2/results/device/UiBench-app-debug.apk
paper/scx_fcp_v2/results/device/UiBenchJankTests-debug.apk
```

Every APK and scheduler hash is recorded in the campaign `FREEZE.json` and
`device_bins.sha256`. A mismatch is a hard failure, not a warning.
