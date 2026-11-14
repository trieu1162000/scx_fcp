# Cuttlefish image and kernel setup

## 1. Android Cuttlefish image and kernel (`6.12` / `6.18` / `7.2`)

Use an `aosp_cf_x86_64_only_phone` (or `aosp_cf_x86_64_phone`) image from an
AOSP build or Android CI:

- Build grid: <https://ci.android.com/builds/branches/aosp-main/grid>
- Cuttlefish getting started: <https://source.android.com/docs/devices/cuttlefish/get-started>

Download and unpack the image and host package:

```bash
export CF_HOME=/path/to/cuttlefish
mkdir -p "$CF_HOME"
unzip aosp_cf_x86_64_only_phone-img-<build-id>.zip -d "$CF_HOME"
tar -xzf cvd-host_package.tar.gz -C "$CF_HOME"
```

Required kernel configuration options for `sched_ext` + BTF:

```text
CONFIG_BPF=y
CONFIG_BPF_SYSCALL=y
CONFIG_BPF_JIT=y
CONFIG_DEBUG_INFO_BTF=y
CONFIG_SCHED_CLASS_EXT=y
CONFIG_EXT_GROUP_SCHED=y
# CONFIG_SCHED_PROXY_EXEC is not set
```

*(To evaluate stock `EEVDF + Proxy Execution`, boot the companion kernel built
with `CONFIG_SCHED_PROXY_EXEC=y` and `CONFIG_SCHED_CLASS_EXT=n`.)*

## 2. Building a custom kernel and repacking `initramfs.img`

When booting a custom Android mainline/GKI kernel (`bzImage`) against a
Cuttlefish userspace image, repack the `virtual_device_x86_64` vendor ramdisk
modules into `initramfs.img`:

```bash
# 1. Build common + virtual-device modules with Bazel/Kleaf
tools/bazel run //common-modules/virtual-device:virtual_device_x86_64_dist \
  -- --dist_dir=out/virtual_device_x86_64/dist

# 2. Repack initramfs.img with the newly built kernel modules
WORKDIR=$(mktemp -d)
lz4 -d -c "$CF_HOME/initramfs.img" | (cd "$WORKDIR" && cpio -idm)
rm -rf "$WORKDIR/lib/modules/"*
MODDIR="$WORKDIR/lib/modules/$(strings out/virtual_device_x86_64/dist/bzImage | grep -E '^[0-9]+\.[0-9]+\.[0-9]+' | head -1 | awk '{print $1}')"
mkdir -p "$MODDIR"
cp -f out/virtual_device_x86_64/dist/*.ko "$MODDIR/"
depmod -b "$WORKDIR" "$(basename "$MODDIR")"
(cd "$WORKDIR" && find . | sort | cpio -o -H newc --quiet) | lz4 -l -12 > out/virtual_device_x86_64/dist/initramfs.img
rm -rf "$WORKDIR"
```

## 3. Launching Cuttlefish across 4, 8, 16, and 32 vCPUs

```bash
export CF_HOME=/path/to/cuttlefish
export KERNEL=/path/to/bzImage
export INITRAMFS=/path/to/initramfs.img
export SERIAL=127.0.0.1:6520

CPUS=32 MEMORY_MB=8192 artifact/scripts/launch_cuttlefish.sh
```

For Cuttlefish instances using `guest_swiftshader`, configure HWUI to use OpenGL
ES and keep `ValueAnimator` duration scaling enabled (`1`):

```bash
adb -s "$SERIAL" shell setprop debug.hwui.renderer opengl
adb -s "$SERIAL" shell settings put global animator_duration_scale 1
```
