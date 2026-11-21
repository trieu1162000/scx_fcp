# Build pipeline

This document builds everything required to reproduce `scx_fcp` on Cuttlefish:

1. the unified `scx_fcp` scheduler (`scx_fcp`, `scx_fcp_named`, `scx_fcp_behavioral`) and `sched_ext` baselines (`scx_lavd`, `scx_p2dq`);
2. the synthetic 4-stage frame pipeline benchmark (`fcp_frame_bench`);
3. AOSP `JankBench` and AOSP `UiBench` / `UiBenchJankTests` workload APKs (via either an AOSP tree or standalone Android SDK + JDK + Gradle).

## 1. Host prerequisites

Tested on Ubuntu 24.04.1 LTS x86_64. Install required host packages:

```bash
sudo apt-get update
sudo apt-get install -y \
  build-essential pkg-config libelf-dev libzstd-dev zlib1g-dev \
  clang llvm lld bpftool pahole python3 unzip rsync adb \
  gcc-aarch64-linux-gnu libc6-dev-arm64-cross
```

Additional toolchains:
- Rust / Cargo `1.82+`
- Clang / LLVM `19–22+` (required for BPF arena / `may_goto` in `scx_lavd` and `scx_p2dq`)
- Android SDK + JDK (optional: for building the workload APKs with Gradle without cloning a full AOSP tree):

Set `ANDROID_HOME`, `JAVA_HOME`, and `AOSP_TESTS` in [`artifact/config.env`](config.env) (or in your shell environment).

## 2. Scheduler and synthetic benchmark

The canonical scheduler source lives at `$SCX_ROOT/scheds/rust/scx_fcp` (mirrored and developed in `src/`).

Set `SCX_ROOT` in [`artifact/config.env`](config.env), then run from the repository root:

```bash
./artifact/pipeline.sh build
```

This compiles all three static profiles from `src/bpf/main.bpf.c`, the baseline schedulers, and `fcp_frame_bench`:

```text
artifact/build/bin/scx_fcp             (default profile: behavioral + comm/PID roots, ops: fcp)
artifact/build/bin/scx_fcp_named       (named profile: comm/PID + behavioral roots, ops: fcp_nh)
artifact/build/bin/scx_fcp_behavioral  (zero-hint profile: behavioral roots only, ops: fcp_zh)
artifact/build/bin/scx_lavd
artifact/build/bin/scx_p2dq
artifact/build/bin/fcp_frame_bench
```

and writes `artifact/build/bin/SHA256SUMS`.

To invoke `build_static.sh` directly:

```bash
./scripts/build_static.sh
```

### SA8155P / SCX 6.1 ARM64

Build from the existing SCX 6.1 workspace and historical cross-compiled static archives:

```bash
source artifact/sa8155.env
./artifact/pipeline.sh build
```

`ARM64_LIBS` defaults to `$HOME/opt/arm64-libs`. Install host packages `gcc-aarch64-linux-gnu` and `libc6-dev-arm64-cross` for the cross linker and sysroot. The build requires `libelf.a`, `libz.a`, and `libzstd.a` under `$ARM64_LIBS/lib`; it adds that directory to the Rust linker search path and links `libzstd.a`. Do not install host `libelf-dev:arm64` just for this build.

The default ARM64 build stages `scx_fcp`, `scx_fcp_behavioral`, `scx_lavd`, and `scx_p2dq` in `artifact/build/bin/arm64/`. Device uses `scx_fcp_behavioral` for `scx_fcp_v_a_depth3_tgid1ms` and runs `scx_p2dq` with PELT disabled for `scx_p2dq_pelt_off`. It builds the baseline packages `scx_lavd_k61` and `scx_p2dq_k61`; set `BUILD_BASELINES=0` to skip them.

## 3. JankBench APK (`com.android.benchmark`)

`JankBench` (`frameworks/base/tests/JankBench`) is an intent-driven app that runs all 8 UI benchmarks (`BM_ID=0,1,2,3,4,5,6,7`):
- `0`: List View Fling
- `1`: Image List View Fling
- `2`: Shadow Grid Fling
- `3`: High-Hitrate Text Render
- `4`: Low-Hitrate Text Render
- `5`: Edit Text Input
- `6`: Overdraw Test
- `7`: Bitmap Upload Test

### Source changes in `JankBench`

1. **`Automator.java` — fallback touch/key dispatch when `INJECT_EVENTS` is unavailable**:
   In `app/src/main/java/com/android/benchmark/ui/automation/Automator.java`, `mInstrumentation.sendPointerSync(event)` and `mInstrumentation.sendKeyDownUpSync(code)` throw `SecurityException` unless the APK is signed with the target image's platform key. Wrapping both calls in `try { ... } catch (SecurityException e)` and dispatching directly to `mWindow.getDecorView().post(() -> mWindow.superDispatchTouchEvent(event))` (plus guarding `if (mResults != null)` in `writeResults()`) allows `JankBench` to run even when built with a standard debug key via standalone Gradle:

   ```java
   // In Automator.java (AutomatorHandler.handleMessage):
   try {
       mInstrumentation.sendPointerSync(event);
   } catch (SecurityException e) {
       dispatchTouchSync(event);
   }
   ```

2. **`TextScrollActivity.java` — respect `EXTRA_HIT_RATE` for `BM_ID=3` vs `BM_ID=4`**:
   In `app/src/main/java/com/android/benchmark/ui/TextScrollActivity.java` line 111, pass `mHitPercentage` instead of hardcoded `80`:

   ```java
   return new ArrayAdapter<>(this, android.R.layout.simple_list_item_1,
           Utils.buildParagraphListWithHitPercentage(PARAGRAPH_COUNT, mHitPercentage));
   ```

### Build Option A: AOSP build tree

```bash
source build/envsetup.sh
lunch <target>
m JankBench -j64
cp -f "$ANDROID_PRODUCT_OUT/testcases/JankBench/x86_64/JankBench.apk" \
  artifact/workloads/JankBench-platform-signed.apk
```

### Build Option B: Standalone Android SDK + JDK + Gradle (no AOSP tree required)

```bash
cd "$AOSP_TESTS/JankBenchGradle"
./gradlew :app:assembleDebug
cp -f app/build/outputs/apk/debug/app-debug.apk \
  /path/to/scx_fcp/artifact/workloads/JankBench-platform-signed.apk
```

## 4. UiBench and UiBenchJankTests APKs (`com.android.test.uibench`)

Source location in AOSP: `frameworks/base/tests/UiBench`.

### Source change and runtime requirement for `testBitmapUploadJank` (`BitmapUploadActivity`)

In `src/com/android/test/uibench/BitmapUploadActivity.java`, `onCreate()` starts `mColorValueAnimator` (`ObjectAnimator.ofInt(uploadView, "colorValue", 0, 255)`) before `UploadView.onAttachedToWindow()` allocates `mBitmap`:

1. **Runtime setting**: Keep `settings put global animator_duration_scale 1` (and `setprop debug.hwui.renderer opengl` on Cuttlefish `guest_swiftshader`). If `animator_duration_scale` is `0`, `ValueAnimator.start()` jumps to its end value synchronously inside `onCreate()` while `mBitmap` is `null`. Both `launch_cuttlefish.sh` and `run_uibench.sh` set `animator_duration_scale 1` automatically.
2. **Source null-guard in `BitmapUploadActivity.java` (`UploadView.setColorValue`)**: Even with `animator_duration_scale 1`, if the first layout traversal (`onAttachedToWindow()`) is delayed by a VSYNC frame under heavy CPU contention, `Choreographer`'s animation callback invokes `setColorValue()` before `mBitmap` is allocated, causing a `NullPointerException` at `mBitmap.eraseColor(...)`. Guard `mBitmap` in `setColorValue()`:

   ```java
   public void setColorValue(int colorValue) {
       if (colorValue == mColorValue || mBitmap == null) return;
       mColorValue = colorValue;
       mBitmap.eraseColor(Color.rgb(mColorValue, 255 - mColorValue, 255));
       invalidate();
   }
   ```

### Build Option A: AOSP build tree

```bash
source build/envsetup.sh
lunch <target>
m UiBench UiBenchJankTests -j64
cp -f "$ANDROID_PRODUCT_OUT/testcases/UiBench/x86_64/UiBench.apk" \
  artifact/workloads/UiBench-app-debug.apk
cp -f "$ANDROID_PRODUCT_OUT/testcases/UiBenchJankTests/x86_64/UiBenchJankTests.apk" \
  artifact/workloads/UiBenchJankTests-debug.apk
```

### Build Option B: Standalone Android SDK + JDK + Gradle (no AOSP tree required)

```bash
cd "$AOSP_TESTS/UiBenchSuiteGradle"
./gradlew :app:assembleDebug :janktests:assembleDebug
cp -f app/build/outputs/apk/debug/app-debug.apk \
  /path/to/scx_fcp/artifact/workloads/UiBench-app-debug.apk
cp -f janktests/build/outputs/apk/debug/janktests-debug.apk \
  /path/to/scx_fcp/artifact/workloads/UiBenchJankTests-debug.apk
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
