# Cuttlefish and kernel setup

## 1. Get or build the Cuttlefish image and kernel

### Option A: prebuilt Android image

Download the matching `aosp_cf_x86_64_only_phone` image and
`cvd-host_package.tar.gz` from the Android build/CI source used by your lab.
Extract the host package:

```bash
export CF_HOME=/path/to/cuttlefish
mkdir -p "$CF_HOME"
tar -xzf cvd-host_package.tar.gz -C "$CF_HOME"
```

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

Use that image initramfs directly, or repack its vendor ramdisk when the 6.18
kernel needs different modules. The network/block/virtio modules must match the
kernel. Set the resulting file as `INITRAMFS`.

