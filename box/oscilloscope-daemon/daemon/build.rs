// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

use std::{env, fs, path::Path};

fn main() {
    let pico_include = "/opt/picoscope/include/libps2000";
    let pico_lib = "/opt/picoscope/lib";

    println!("cargo:rustc-link-search=native={}", pico_lib);
    println!("cargo:rustc-link-lib=dylib=ps2000");
    println!("cargo:rerun-if-changed=wrapper.h");

    let bindings = bindgen::Builder::default()
        .header("wrapper.h")
        .clang_arg(format!("-I{}", pico_include))
        // Allow all PS2000 functions (including advanced trigger functions)
        .allowlist_function("ps2000_.*")
        .allowlist_function("ps2000SetAdvTrigger.*")
        .allowlist_function("ps2000SetPulseWidthQualifier")
        .allowlist_function("ps2000PingUnit")
        // Allow all PS2000 types
        .allowlist_type("enPS2000.*")
        .allowlist_type("PS2000_.*")
        .allowlist_type("tPS2000.*")
        // Allow all PS2000 constants
        .allowlist_var("enPS2000.*")
        .allowlist_var("PS2000_.*")
        .generate_inline_functions(true)
        .layout_tests(false)
        .parse_callbacks(Box::new(bindgen::CargoCallbacks::new()))
        .generate()
        .expect("Unable to generate bindings");

    // This crate is edition 2024, which rejects a safe `extern "C" {` block.
    //
    // bindgen 0.70 and earlier emit the safe form, so it has to be patched to
    // `unsafe extern "C" {`. bindgen 0.71+ emits the unsafe form already, and
    // an unconditional prepend turned that into `unsafe unsafe extern "C" {` --
    // one parse error, which (because ps2000.rs glob-imports the generated
    // module) evaporated all 43 bindgen symbols and produced ~70 errors.
    //
    // Normalising to the safe form first makes the substitution idempotent, so
    // this works on either generation and a future bindgen bump cannot
    // reintroduce the doubling.
    let raw = bindings
        .to_string()
        .replace("unsafe extern \"C\" {", "extern \"C\" {")
        .replace("extern \"C\" {", "unsafe extern \"C\" {");

    let out_path = Path::new(&env::var("OUT_DIR").unwrap()).join("ps2000_bindings.rs");
    fs::write(out_path, raw).expect("Couldn't write ps2000_bindings.rs");
}
