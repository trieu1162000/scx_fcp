# Cuttlefish and kernel setup

## 1. Get or build the Cuttlefish image and kernel

### Option A: prebuilt Android image

Download a `aosp_cf_x86_64_only_phone` build from Android CI:

- Build grid: <https://ci.android.com/builds/branches/aosp-main/grid>
- Cuttlefish getting started: <https://source.android.com/docs/devices/cuttlefish/get-started>

Select a build and download:

```text
aosp_cf_x86_64_only_phone-img-<build-id>.zip
cvd-host_package.tar.gz
```

Unpack the image and host package:

```bash
export CF_HOME=/path/to/cuttlefish
mkdir -p "$CF_HOME"
unzip aosp_cf_x86_64_only_phone-img-<build-id>.zip -d "$CF_HOME"
tar -xzf cvd-host_package.tar.gz -C "$CF_HOME"
```

The image directory contains `system.img`, `vendor_boot.img`, and the other
Cuttlefish images. Use it as `CF_HOME` for launch.

The system image directory contains `system.img`, `vendor_boot.img`, and the
other Cuttlefish images. Use that directory as `CF_HOME` for launch.

### Option B: build AOSP Cuttlefish

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

The host tools and image are under:

```text
out/target/product/vsoc_x86_64_only/
  cvd-host_package.tar.gz
  system.img
  vendor_boot.img
  ...
```

### Kernel

Use a Linux 6.18 kernel with:

```text
CONFIG_SCHED_CLASS_EXT=y
CONFIG_DEBUG_INFO_BTF=y
CONFIG_BPF=y
CONFIG_BPF_SYSCALL=y
CONFIG_BPF_JIT=y
# CONFIG_SCHED_PROXY_EXEC is not set
```

The kernel source is the Android common kernel:

```bash
git clone https://android.googlesource.com/kernel/common
cd common
# select the 6.18 branch/tag required by the image
# configure the required options above
make -j"$(nproc)" bzImage
```

The output is `arch/x86/boot/bzImage`; export it as `KERNEL`.

If using a prebuilt image, use its matching kernel instead of rebuilding.

### Initramfs

`make bzImage` produces only the kernel image; it does not create the
Cuttlefish initramfs. The Android/Cuttlefish image build produces the image's
`initramfs.img` / vendor ramdisk.

The downloaded image carries a vendor ramdisk whose modules match the image
kernel (the lab image has `6.12.74-android16` modules). When booting a
custom 6.18 kernel, extract that vendor ramdisk, replace its modules with the
modules produced by the 6.18 kernel build, filter `modules.load` to the
modules that exist, and repack the ramdisk with cpio + lz4.

If you use the image's own kernel, use its matching ramdisk unchanged. Set the
final file as `INITRAMFS`.

