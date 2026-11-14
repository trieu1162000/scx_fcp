// SPDX-License-Identifier: GPL-2.0

fn main() {
    let mut bpf_cflags =
        std::env::var("BPF_EXTRA_CFLAGS_POST_INCL").unwrap_or_else(|_| "-mcpu=v3".to_owned());

    let profile = std::env::var("SCX_FCP_BUILD").unwrap_or_default();
    println!("cargo:rerun-if-env-changed=SCX_FCP_BUILD");
    println!("cargo:rerun-if-env-changed=SCX_FCP_NO_TOKEN");
    match profile.as_str() {
        "zero-hint" => bpf_cflags.push_str(" -DSCX_FCP_ZERO_HINT"),
        "named" => bpf_cflags.push_str(" -DSCX_FCP_NAMED"),
        "oracle" => bpf_cflags.push_str(" -DSCX_FCP_ORACLE"),
        _ => {}
    }
    if std::env::var_os("SCX_FCP_NO_TOKEN").is_some() {
        bpf_cflags.push_str(" -DSCX_FCP_NO_TOKEN");
    }
    if std::env::var_os("SCX_FCP_NO_FANOUT_CAP").is_some() {
        bpf_cflags.push_str(" -DSCX_FCP_NO_FANOUT_CAP");
    }
    let profile_name = if profile.is_empty() {
        "default".to_owned()
    } else {
        profile
    };
    println!("cargo:rustc-env=SCX_FCP_BUILD_PROFILE={profile_name}");

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
