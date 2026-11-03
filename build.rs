// SPDX-License-Identifier: GPL-2.0

fn main() {
    if std::env::var_os("BPF_EXTRA_CFLAGS_POST_INCL").is_none() {
        unsafe {
            std::env::set_var("BPF_EXTRA_CFLAGS_POST_INCL", "-mcpu=v3");
        }
    }
    scx_cargo::BpfBuilder::new()
        .unwrap()
        .enable_intf("src/bpf/intf.h", "bpf_intf.rs")
        .enable_skel("src/bpf/main.bpf.c", "bpf")
        .build()
        .unwrap();
}
