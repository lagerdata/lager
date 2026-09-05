// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! Generates PicoScope bindings from the PicoTech headers.
//!
//! The headers are *not* in this repository. Pico licenses them rather than
//! selling them and limits who may be given access, which a public repo
//! cannot honour, so they have to come from the machine doing the build.
//! Two places are searched, in order:
//!
//! * `picoscope/include/<family>/` at the repo root, where you can unpack the
//!   SDK to build without installing the PicoTech packages system-wide.
//! * `/opt/picoscope/include/<family>/`, where those packages install them --
//!   which is how the box images get them.
//!
//! One consequence worth knowing: a checkout with neither will fail this
//! build script by design, with a message saying where to put them, rather
//! than silently producing a daemon that cannot talk to any scope.
//!
//! The other change relative to linking against an installed SDK:
//!
//! * `dynamic_library_name` makes bindgen emit a `dlopen` wrapper instead of
//!   `extern "C"` declarations, so there is no link-time dependency on
//!   `libps2000` and one binary can serve whichever driver families are
//!   actually installed on a given box. Linking meant a build for a box with
//!   a 2000-series scope could not talk to a 5000-series one.

use std::{env, fs, path::Path, path::PathBuf};

/// Locate one family's headers: repo checkout first, installed SDK second.
fn sdk_include(family: &str) -> PathBuf {
    let manifest = PathBuf::from(env::var("CARGO_MANIFEST_DIR").unwrap());
    // daemon -> oscilloscope-daemon -> box -> repo root
    manifest
        .join("../../../picoscope/include")
        .join(family)
        .canonicalize()
        .unwrap_or_else(|_| {
            // The repo copy is gitignored, so this is the usual path on a
            // box and in any fresh clone.
            PathBuf::from("/opt/picoscope/include").join(family)
        })
}

fn main() {
    let out_dir = PathBuf::from(env::var("OUT_DIR").unwrap());

    // The legacy snake_case API. Kept separate because its calling
    // convention differs enough that it gets its own driver rather than a
    // row in the modern vtable (see pico/ps2000.rs).
    generate("libps2000", "ps2000.h", "Ps2000", &["ps2000.*", "PS2000.*"], &out_dir);

    // The four modern "a" APIs. Same call shapes apart from `oversample`
    // (2000a/3000a only) and `resolution` (5000a only), which is what makes
    // the macro-generated vtable in pico/modern/ possible.
    generate(
        "libps2000a",
        "ps2000aApi.h",
        "Ps2000a",
        &["ps2000a.*", "PS2000A.*"],
        &out_dir,
    );
    generate(
        "libps3000a",
        "ps3000aApi.h",
        "Ps3000a",
        &["ps3000a.*", "PS3000A.*"],
        &out_dir,
    );
    generate(
        "libps4000a",
        "ps4000aApi.h",
        "Ps4000a",
        &["ps4000a.*", "PS4000A.*", "PICO_CONNECT.*", "PICO_X1.*"],
        &out_dir,
    );
    generate(
        "libps5000a",
        "ps5000aApi.h",
        "Ps5000a",
        &["ps5000a.*", "PS5000A.*"],
        &out_dir,
    );

    println!("cargo:rerun-if-changed=build.rs");
}

fn generate(family: &str, header: &str, struct_name: &str, patterns: &[&str], out_dir: &Path) {
    let include = sdk_include(family);
    let header_path = include.join(header);

    if !header_path.exists() {
        panic!(
            "missing PicoScope header {}.\n\
             The PicoTech headers are not redistributed in this repo. Either \
             install the PicoTech packages (which put them in \
             /opt/picoscope/include/{family}/), or unpack the SDK into \
             picoscope/include/{family}/ at the repo root.",
            header_path.display()
        );
    }

    println!("cargo:rerun-if-changed={}", header_path.display());

    let mut builder = bindgen::Builder::default()
        .header(header_path.to_string_lossy())
        .clang_arg(format!("-I{}", include.display()));

    // PicoTech's per-family header sets are not self-contained: libps3000a's
    // PicoDeviceStructs.h includes PicoConnectProbes.h, which only ships
    // under libps4000a. Adding the sibling directories lets those resolve.
    // The family's own directory is first, so a name that exists in more than
    // one place still resolves to that family's version.
    for sibling in ["libps4000a", "libps5000a", "libps6000a", "libpsospa"] {
        let dir = sdk_include(sibling);
        if dir.exists() {
            builder = builder.clang_arg(format!("-I{}", dir.display()));
        }
    }

    let mut builder = builder
        // Emits a struct that loads the library at runtime rather than
        // extern "C" blocks that must be satisfied at link time.
        .dynamic_library_name(struct_name)
        // Tolerate a driver build that is missing a newer entry point; the
        // wrapper reports the absence per symbol instead of failing to load.
        .dynamic_link_require_all(false)
        .generate_inline_functions(false)
        .layout_tests(false)
        // The PicoTech headers document some functions with prose that is
        // not valid Rust ("Example: AQ005 / 139, ..."). Carried through as
        // doc comments, rustdoc treats those as doctests and `cargo test`
        // fails trying to compile them. The prose is still in the vendored
        // headers, which is where anyone would read it anyway.
        .generate_comments(false);

    for pattern in patterns {
        builder = builder
            .allowlist_function(pattern)
            .allowlist_type(pattern)
            .allowlist_var(pattern);
    }
    // Enum constants are declared with an `en` prefix in these headers, and
    // PICO_STATUS / PICO_INFO codes come from the shared PicoStatus.h that
    // every family ships its own copy of.
    builder = builder
        .allowlist_type("en.*")
        .allowlist_var("en.*")
        .allowlist_var("PICO_.*")
        .allowlist_type("PICO_.*");

    let bindings = builder
        .parse_callbacks(Box::new(bindgen::CargoCallbacks::new()))
        .generate()
        .unwrap_or_else(|e| panic!("could not generate {family} bindings: {e}"));

    // This crate is edition 2024, which rejects a safe `extern "C" {` block.
    //
    // bindgen 0.70 and earlier emit the safe form, so it has to be patched to
    // `unsafe extern "C" {`. bindgen 0.71+ emits the unsafe form already, and
    // an unconditional prepend turned that into `unsafe unsafe extern "C" {` --
    // one parse error, which (because the module is glob-imported) evaporated
    // every bindgen symbol and produced ~70 errors.
    //
    // Normalising to the safe form first makes the substitution idempotent, so
    // this works on either generation and a future bindgen bump cannot
    // reintroduce the doubling.
    let source = bindings
        .to_string()
        .replace("unsafe extern \"C\" {", "extern \"C\" {")
        .replace("extern \"C\" {", "unsafe extern \"C\" {");

    let out_path = out_dir.join(format!("{family}_bindings.rs"));
    fs::write(&out_path, source)
        .unwrap_or_else(|e| panic!("could not write {}: {e}", out_path.display()));
}
