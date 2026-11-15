# Cuttlefish image and kernel setup

## 0. Host Cuttlefish packages and KVM / vhost permissions

Ensure `cuttlefish-base`, `cuttlefish-user`, and KVM/`vhost_vsock` device
permissions are active on the host before launching Cuttlefish:

```bash
sudo apt-get install -y cuttlefish-base cuttlefish-user iproute2
sudo modprobe kvm_intel vhost_net vhost_vsock
sudo usermod -aG kvm,cvdnetwork,render "$USER"
sudo service cuttlefish-host-resources start
sudo chmod 666 /dev/kvm /dev/vhost-vsock /dev/vhost-net /dev/net/tun
```

## 1. Prebuilt Cuttlefish image and `6.12.74-android16` kernel (No Kernel Rebuild Required)

Download a prebuilt `aosp_cf_x86_64_only_phone` (or `aosp_cf_x86_64_phone`)
image and `cvd-host_package.tar.gz` from Android CI:

- Build grid: <https://ci.android.com/builds/branches/aosp-main/grid>
- Cuttlefish getting started: <https://source.android.com/docs/devices/cuttlefish/get-started>

Unpack into `CF_HOME`:

```bash
export CF_HOME=/path/to/cuttlefish
mkdir -p "$CF_HOME"
unzip aosp_cf_x86_64_only_phone-img-<build-id>.zip -d "$CF_HOME"
tar -xzf cvd-host_package.tar.gz -C "$CF_HOME"
```

### Why the prebuilt `boot.img` (`6.12.74-android16`) works out of the box

The prebuilt Cuttlefish `boot.img` carries Google's Android 16 GKI `6.12`
kernel (`Linux 6.12.74-android16-6-g3ec022196c4e-ab15076761`), which **already
compiles in `sched_ext` and BTF**:

```text
CONFIG_BPF=y
CONFIG_BPF_SYSCALL=y
CONFIG_BPF_JIT=y
CONFIG_DEBUG_INFO_BTF=y
CONFIG_DEBUG_INFO_BTF_MODULES=y
CONFIG_SCHED_CLASS_EXT=y
CONFIG_SCHED_PROXY_EXEC=y
```

Because that prebuilt kernel also enables `CONFIG_SCHED_PROXY_EXEC=y`, booting
it with `-extra_kernel_cmdline "sched_proxy_exec=0"` (which
`artifact/scripts/launch_cuttlefish.sh` passes automatically) disables Proxy
Execution at boot and allows `sched_ext` schedulers (`scx_fcp`, `scx_lavd`,
`scx_p2dq`) and stock `EEVDF` to run directly on the prebuilt `boot.img`
**without rebuilding the kernel**.

*(To evaluate `EEVDF w/ ProxyExec`, boot the same prebuilt `boot.img` with
`sched_proxy_exec=1`.)*

## 2. Optional: Building a custom Android mainline (`7.2.0-rc9`) kernel

Rebuilding the kernel is only required if you want to evaluate against a custom
Android mainline (`7.2.0-rc9-mainline`) kernel tree where
`CONFIG_SCHED_CLASS_EXT=y` is not enabled by default:

```bash
# 1. Enable CONFIG_SCHED_CLASS_EXT=y, CONFIG_DEBUG_INFO_BTF=y, CONFIG_SCHED_PROXY_EXEC=n
#    and build common + virtual-device modules with Bazel/Kleaf:
tools/bazel run //common-modules/virtual-device:virtual_device_x86_64_dist \
  -- --dist_dir=out/virtual_device_x86_64/dist

# 2. Repack initramfs.img with the newly built kernel modules:
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

Set `CF_HOME`, `SERIAL`, `CPUS` (and optional `KERNEL` / `INITRAMFS`) in
[`artifact/config.env`](config.env), then launch via `./artifact/pipeline.sh launch`
(or override `CPUS` inline):

```bash
# Default (reads CF_HOME, SERIAL, CPUS=32 from artifact/config.env):
./artifact/pipeline.sh launch

# Or override vCPU count / custom kernel inline:
CPUS=16 ./artifact/pipeline.sh launch
KERNEL=/path/to/bzImage INITRAMFS=/path/to/initramfs.img ./artifact/pipeline.sh launch
```
