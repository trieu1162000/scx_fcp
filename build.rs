// SPDX-License-Identifier: GPL-2.0

fn main() {
    let mut bpf_cflags =
        std::env::var("BPF_EXTRA_CFLAGS_POST_INCL").unwrap_or_else(|_| "-mcpu=v3".to_owned());
    if std::env::var_os("SCX_FCP_ANDROID_61").is_some() {
        bpf_cflags.push_str(" -DSCX_FCP_ANDROID_61");
    }
    unsafe {
        std::env::set_var("BPF_EXTRA_CFLAGS_POST_INCL", bpf_cflags);
    }
    scx_cargo::BpfBuilder::new()
        .unwrap()
        .enable_intf("src/bpf/intf.h", "bpf_intf.rs")
        .enable_skel("src/bpf/main.bpf.c", "bpf")
        .build()
        .unwrap();
}
