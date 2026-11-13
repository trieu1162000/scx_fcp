# Cuttlefish image and kernel setup

## 1. Android 17 Cuttlefish image and kernel (`6.18` / `7.2`)

Use a prebuilt `aosp_cf_x86_64_only_phone` image from Android CI:

- Build grid: <https://ci.android.com/builds/branches/aosp-main/grid>
- Cuttlefish getting started: <https://source.android.com/docs/devices/cuttlefish/get-started>

Download from the selected build:

```text
aosp_cf_x86_64_only_phone-img-<build-id>.zip
cvd-host_package.tar.gz
```

Unpack:

```bash
export CF_HOME=/path/to/cuttlefish
mkdir -p "$CF_HOME"
unzip aosp_cf_x86_64_only_phone-img-<build-id>.zip -d "$CF_HOME"
tar -xzf cvd-host_package.tar.gz -C "$CF_HOME"
```

The current kernel against the cf prebuilt image is 6.12 AFAIUC, so if you wish to boot with an Android mainline kernel (`6.18` or `7.2`), need to manually clone, see #2, then the simply compile with:

```text
CONFIG_BPF=y
CONFIG_BPF_SYSCALL=y
CONFIG_BPF_JIT=y
CONFIG_DEBUG_INFO_BTF=y
CONFIG_SCHED_CLASS_EXT=y
CONFIG_SCHED_PROXY_EXEC=n
```

(To evaluate stock `EEVDF + Proxy Execution`, boot the companion (eg. `6.18.21`) kernel built with `CONFIG_SCHED_PROXY_EXEC=y` and `CONFIG_SCHED_CLASS_EXT=n`.)

## 2. Building the kernel and repacking for vendor modules
TODO: update steps

## 3. Launching Cuttlefish across 4, 8, 16, and 32 vCPUs

```bash
export CF_HOME=/path/to/cuttlefish
export KERNEL=/path/to/bzImage
export INITRAMFS=/path/to/initramfs.img
export SERIAL=127.0.0.1:6520 # or 0.0.0.0:6520

CPUS=16 MEMORY_MB=8192 artifact/scripts/launch_cuttlefish.sh
```

For Cuttlefish instances using `guest_swiftshader`, ensure HWUI uses the OpenGL ES pipeline and keep `ValueAnimator` duration scaling enabled:

```bash
adb -s "$SERIAL" shell setprop debug.hwui.renderer opengl
adb -s "$SERIAL" shell settings put global animator_duration_scale 1
```
