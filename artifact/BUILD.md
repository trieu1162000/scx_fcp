# Build pipeline

This document builds everything required to reproduce the `scx_fcp` on Cuttlefish:

1. the unified `scx_fcp` scheduler (`scx_fcp`, `scx_fcp_named`, `scx_fcp_behavioral`) and `sched_ext` baselines (`scx_lavd`, `scx_p2dq`);
2. the synthetic 4-stage frame pipeline benchmark (`fcp_frame_bench`);
3. AOSP `JankBench` and AOSP `UiBench` / `UiBenchJankTests` workload APKs.

The build is currently split between the repository build script and the AOSP
workload trees. The commands below use environment variables rather than fixed
host paths.

## 1. Host prerequisites

Tested on Ubuntu 24.04.1 LTS x86_64 with:

```text
Rust / Cargo 1.82+
Clang / LLVM 19–22+
bpftool, pahole, libelf-dev, libzstd-dev, zlib1g-dev
make, cc, python3, unzip, rsync
adb (Android platform-tools)
Android SDK + JDK for APK builds (optional for building apks without cloning AOSP source tree)
KVM access for Cuttlefish
```

For APK builds, set:

```bash
export ANDROID_HOME=${ANDROID_HOME:-/path/to/Android/Sdk}
export JAVA_HOME=${JAVA_HOME:-/path/to/jdk}
```

## 2. Scheduler and synthetic benchmark

The canonical scheduler source lives at `$SCX_ROOT/scheds/rust/scx_fcp` (mirrored  and core developed in `src/`).

```bash
cd /path/to/scx_fcp
export SCX_ROOT=${SCX_ROOT:-$HOME/workspace/scx}
./artifact/pipeline.sh build
```

This compiles all three static profiles from the unified single-entry source (`src/bpf/main.bpf.c`), optional baselines, and `fcp_frame_bench`:

```text
artifact/build/bin/scx_fcp             (default profile: behavioral + comm/PID roots, ops: fcp)
artifact/build/bin/scx_fcp_named       (named profile: comm/PID + behavioral roots, ops: fcp_nh)
artifact/build/bin/scx_fcp_behavioral  (zero-hint profile: behavioral roots only, ops: fcp_zh)
artifact/build/bin/scx_lavd
artifact/build/bin/scx_p2dq
artifact/build/bin/fcp_frame_bench
```

and writes `artifact/build/bin/SHA256SUMS`.

To build directly via `build_static.sh`:

```bash
export SCX_ROOT=${SCX_ROOT:-$HOME/workspace/scx}
export ARTIFACT=${ARTIFACT:-$PWD/artifact}
SCX_ROOT="$SCX_ROOT" BIN_DIR="$ARTIFACT/build/bin" \
  "$ARTIFACT/scripts/internal/build_static.sh"
```

## 3. JankBench APK

`JankBench` (`com.android.benchmark`) is an intent-driven app, not an instrumentation test module. It must
hold `INJECT_EVENTS`, so the final APK must be platform-signed with the image's
platform test key. it runs all 8 UI benchmarks (`BM_ID=0,1,2,3,4,5,6,7`):
- `0`: List View Fling
- `1`: Image List View Fling
- `2`: Shadow Grid Fling
- `3`: High-Hitrate Text Render
- `4`: Low-Hitrate Text Render
- `5`: Edit Text Input
- `6`: Overdraw Test
- `7`: Bitmap Upload Test

Source location in AOSP: `frameworks/base/tests/JankBench` (or prebuilt under `$AOSP_OUT/testcases/JankBench/x86_64/JankBench.apk`). Place the APK at:

```bash
source build/env_setup.sh
lunch <target>
m JankBench -j64
```

#### (Optional) Lab build tree

```bash
export AOSP_TESTS=${AOSP_TESTS:-$HOME/work/aosp-tests}
cd "$AOSP_TESTS/JankBenchGradle"
./gradlew :app:assembleDebug
```

Then sign the resulting APK with the platform key used by the Cuttlefish image
(for example via `apksigner` or the AOSP signing tools), and place it at:

```text
artifact/workloads/JankBench-platform-signed.apk
```

## 4. UiBench app and test APKs

Source location in AOSP: `frameworks/base/tests/UiBench` (or prebuilt under `$AOSP_OUT/testcases/UiBench/x86_64/UiBench.apk` and `$AOSP_OUT/testcases/UiBenchJankTests/x86_64/UiBenchJankTests.apk`).

**Important note on `testBitmapUploadJank` (`BitmapUploadActivity`)**:
- `BitmapUploadActivity` is driven by two `ObjectAnimator` instances (`mColorValueAnimator` and `mYAnimator`) started in `Activity.onCreate()`.
- If `settings put global animator_duration_scale 0` is set on the device, Android's `ValueAnimator.start()` jumps synchronously to its end value inside `onCreate()` *before* `onAttachedToWindow()` allocates `mBitmap`, throwing a `NullPointerException`.
- Setting `settings put global animator_duration_scale 1` (and `setprop debug.hwui.renderer opengl` under Cuttlefish `guest_swiftshader`) allows `testBitmapUploadJank` and all 5 other `UiBenchJankTests` benchmarks (`6/6` total) to pass cleanly without modifying `UiBench.apk`. Both `run_uibench.sh` and `run_jankbench.sh` configure `animator_duration_scale 1` automatically.

```bash
source build/env_setup.sh
lunch <target>
m UiBench UiBenchJankTests -j64
```

#### (Optional) Lab build tree

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

Place the APKs at:

```text
artifact/workloads/UiBench-app-debug.apk
artifact/workloads/UiBenchJankTests-debug.apk
```

## 5. Expected build artifacts

Before launching the matrix, verify:

```text
artifact/build/bin/scx_fcp
artifact/build/bin/scx_fcp_named
artifact/build/bin/scx_fcp_behavioral
artifact/build/bin/fcp_frame_bench
artifact/build/bin/scx_lavd
artifact/build/bin/scx_p2dq
artifact/workloads/JankBench-platform-signed.apk
artifact/workloads/UiBench-app-debug.apk
artifact/workloads/UiBenchJankTests-debug.apk
```

The pipeline records the built scheduler, baseline binaries, and workload
APKs in the campaign output directory.
