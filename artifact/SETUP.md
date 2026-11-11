# Cuttlefish image setup

## 1. Download the Android 17 image

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

## 2. Use the image as-is

The image contains its kernel and ramdisk. Rebuilding that kernel after
changing Kconfig options produces a custom kernel; use the matching ramdisk
for that kernel. If you do not rebuild the kernel, no custom-kernel step is
needed.

Launch Cuttlefish with the image directory:

```bash
export CF_HOME=/path/to/cuttlefish
export SERIAL=0.0.0.0:6520

CPUS=16 MEMORY_MB=6144 artifact/scripts/launch_cuttlefish.sh
```

No separate kernel build or initramfs repack is required for the paper setup.

## 3. Build the image from AOSP (optional)

```bash
mkdir -p "$HOME/aosp"
cd "$HOME/aosp"
repo init -u https://android.googlesource.com/platform/manifest \
  -b android-latest-release
repo sync -j"$(nproc)"
source build/envsetup.sh
lunch aosp_cf_x86_64_only_phone-userdebug
m -j"$(nproc)"
```

The image and host package are under:

```text
out/target/product/vsoc_x86_64_only/
  cvd-host_package.tar.gz
  system.img
  vendor_boot.img
  ...
```

Use that directory as `CF_HOME`.
