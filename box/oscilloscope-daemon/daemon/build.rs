// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! Generates PicoScope bindings from the PicoTech headers.
//!
//! The headers are *not* in this repository. Pico licenses them rather than
//! selling them and limits who may be given access, which a public repo
//! cannot honour, so they have to come from the machine doing the build.
//! Each family's headers are looked for in:
//!
//! * `$LAGER_PICOSCOPE_INCLUDE/<family>/` when that is set, and nowhere
//!   else, so a build can be tried against exactly the families a box has.
//! * Otherwise `picoscope/include/<family>/` at the repo root, where you can
//!   unpack the SDK to build without installing the PicoTech packages
//!   system-wide, then `/opt/picoscope/include/<family>/`, where those
//!   packages install them -- which is how the boxes get them.
//!
//! A family whose headers are missing is left out rather than failing the
//! build. PicoTech packages each family separately, and a box set up for one
//! scope can have only that scope's, such as just `libps2000` for a 2204A.
//! Each family that is built sets a `pico_<family>` cfg
//! (`pico_ps2000`, `pico_ps5000a`, ...) that its code is compiled under. Each
//! one left out gets a cargo warning saying why, and the daemon gives the
//! same reason when it meets that family's driver on a box.
//!
//! With no family at all the build still fails, with a message saying where
//! to put the headers, rather than silently producing a daemon that cannot
//! talk to any scope.
//!
//! The other change relative to linking against an installed SDK:
//!
//! * `dynamic_library_name` makes bindgen emit a `dlopen` wrapper instead of
//!   `extern "C"` declarations, so there is no link-time dependency on
//!   `libps2000` and one binary can serve whichever driver families are
//!   actually installed on a given box. Linking meant a build for a box with
//!   a 2000-series scope could not talk to a 5000-series one.

use std::{env, fs, path::Path, path::PathBuf};

/// Names one directory to take every family's headers from; see the module
/// docs.
const INCLUDE_ENV: &str = "LAGER_PICOSCOPE_INCLUDE";

/// Where the PicoTech packages install the headers.
const SDK_INCLUDE: &str = "/opt/picoscope/include";

/// One PicoTech driver family.
struct Family {
    /// The directory its headers are in, which is also its library's name.
    dir: &'static str,
    header: &'static str,
    /// The `dlopen` wrapper bindgen generates.
    struct_name: &'static str,
    patterns: &'static [&'static str],
    /// Headers this family's own include but PicoTech ships only with others.
    borrowed: &'static [Borrowed],
}

/// A header one family's headers include, which ships with other families.
struct Borrowed {
    header: &'static str,
    /// The families that ship it, for the message when none is installed.
    shipped_with: &'static str,
}

const FAMILIES: &[Family] = &[
    // The legacy snake_case API. Kept separate because its calling
    // convention differs enough that it gets its own driver rather than a
    // row in the modern vtable (see pico/ps2000.rs).
    Family {
        dir: "libps2000",
        header: "ps2000.h",
        struct_name: "Ps2000",
        patterns: &["ps2000.*", "PS2000.*"],
        borrowed: &[],
    },
    // The four modern "a" APIs. Same call shapes apart from `oversample`
    // (2000a/3000a only) and `resolution` (5000a only), which is what makes
    // the macro-generated vtable in pico/modern.rs possible.
    Family {
        dir: "libps2000a",
        header: "ps2000aApi.h",
        struct_name: "Ps2000a",
        patterns: &["ps2000a.*", "PS2000A.*"],
        borrowed: &[],
    },
    Family {
        dir: "libps3000a",
        header: "ps3000aApi.h",
        struct_name: "Ps3000a",
        patterns: &["ps3000a.*", "PS3000A.*"],
        // Its PicoDeviceStructs.h includes this and declares fields of the
        // probe-range type it defines, so it cannot be stubbed out.
        borrowed: &[Borrowed {
            header: "PicoConnectProbes.h",
            shipped_with: "libps4000a, libps5000a and libps6000a",
        }],
    },
    Family {
        dir: "libps4000a",
        header: "ps4000aApi.h",
        struct_name: "Ps4000a",
        patterns: &["ps4000a.*", "PS4000A.*", "PICO_CONNECT.*", "PICO_X1.*"],
        borrowed: &[],
    },
    Family {
        dir: "libps5000a",
        header: "ps5000aApi.h",
        struct_name: "Ps5000a",
        patterns: &["ps5000a.*", "PS5000A.*"],
        borrowed: &[],
    },
];

/// Families whose headers other families include.
///
/// PicoTech's per-family header sets are not self-contained: libps3000a's
/// PicoDeviceStructs.h includes PicoConnectProbes.h, which ships with some of
/// these but not with libps3000a. They go on every family's include path,
/// after the family's own directory, so a name that exists in more than one
/// place still resolves to that family's version.
const SHARED_DIRS: &[&str] = &["libps4000a", "libps5000a", "libps6000a", "libpsospa"];

/// `picoscope/include` at the repo root.
fn repo_include() -> PathBuf {
    let manifest = PathBuf::from(env::var("CARGO_MANIFEST_DIR").unwrap());
    // daemon -> oscilloscope-daemon -> box -> repo root
    manifest.join("../../../picoscope/include")
}

/// Locate one family's headers: the override if set, otherwise the repo
/// checkout first and the installed SDK second.
fn family_dir(family: &str) -> PathBuf {
    if let Some(root) = env::var_os(INCLUDE_ENV) {
        return PathBuf::from(root).join(family);
    }
    repo_include()
        .join(family)
        .canonicalize()
        .unwrap_or_else(|_| {
            // The repo copy is gitignored, so this is the usual path on a
            // box and in any fresh clone.
            PathBuf::from(SDK_INCLUDE).join(family)
        })
}

/// Every directory `family_dir` can pick from.
fn include_roots() -> Vec<PathBuf> {
    match env::var_os(INCLUDE_ENV) {
        Some(root) => vec![PathBuf::from(root)],
        None => vec![repo_include(), PathBuf::from(SDK_INCLUDE)],
    }
}

/// The family's name as the daemon spells it: `libps2000a` is `ps2000a`.
fn short_name(family: &Family) -> &'static str {
    family.dir.trim_start_matches("lib")
}

/// The cfg that `family`'s code is compiled under, such as `pico_ps2000a`.
fn cfg_name(family: &Family) -> String {
    format!("pico_{}", short_name(family))
}

/// Why `family` cannot be built here, or `None` when it can.
fn missing(family: &Family) -> Option<String> {
    let header = family_dir(family.dir).join(family.header);
    if !header.is_file() {
        return Some(format!(
            "{} is not installed. Install PicoTech's {} package",
            header.display(),
            family.dir
        ));
    }
    for borrowed in family.borrowed {
        let found = std::iter::once(&family.dir)
            .chain(SHARED_DIRS)
            .any(|dir| family_dir(dir).join(borrowed.header).is_file());
        if !found {
            return Some(format!(
                "its headers include {}, which PicoTech ships with {} rather than \
                 with {}. Install one of those packages",
                borrowed.header, borrowed.shipped_with, family.dir
            ));
        }
    }
    None
}

fn main() {
    let out_dir = PathBuf::from(env::var("OUT_DIR").unwrap());

    println!("cargo::rerun-if-changed=build.rs");
    println!("cargo::rerun-if-env-changed={INCLUDE_ENV}");
    // The directories, not just the headers bindgen reads: the PicoTech
    // packages install files with the timestamps they were packaged with, so
    // a family installed since the last build can be older than that build,
    // and cargo would never look at it. Adding or removing a family's
    // directory changes its parent's timestamp, which cargo does see. A root
    // that does not exist is not watched, because cargo treats a watched
    // path that is missing as changed and would rerun this on every build.
    for root in include_roots() {
        if let Ok(root) = root.canonicalize() {
            println!("cargo::rerun-if-changed={}", root.display());
        }
    }

    let cfgs: Vec<String> = FAMILIES.iter().map(cfg_name).collect();
    println!("cargo::rustc-check-cfg=cfg({})", cfgs.join(", "));

    let mut left_out = Vec::new();
    for family in FAMILIES {
        let cfg = cfg_name(family);
        match missing(family) {
            None => {
                generate(family, &out_dir);
                println!("cargo::rustc-cfg={cfg}");
            }
            Some(reason) => {
                println!(
                    "cargo::warning=PicoScope {} support left out: {reason}.",
                    short_name(family)
                );
                // Read back by loader.rs, so the daemon can say why it does
                // not drive a scope whose driver is installed.
                println!("cargo::rustc-env={}_LEFT_OUT={reason}", cfg.to_uppercase());
                left_out.push(format!("  {}: {reason}.", family.dir));
            }
        }
    }

    if left_out.len() == FAMILIES.len() {
        panic!(
            "no PicoScope family can be built, so the daemon could not talk to any scope:\n\
             {}\n\
             The PicoTech headers are not redistributed in this repo. Either install the \
             PicoTech packages (which put them in {SDK_INCLUDE}/<family>/), unpack the SDK \
             into picoscope/include/<family>/ at the repo root, or set {INCLUDE_ENV} to a \
             directory holding the <family>/ directories. Any one family is enough.",
            left_out.join("\n")
        );
    }
}

fn generate(family: &Family, out_dir: &Path) {
    let include = family_dir(family.dir);
    let header_path = include.join(family.header);

    let mut builder = bindgen::Builder::default()
        .header(header_path.to_string_lossy())
        .clang_arg(format!("-I{}", include.display()));

    for shared in SHARED_DIRS {
        let dir = family_dir(shared);
        if dir.exists() {
            builder = builder.clang_arg(format!("-I{}", dir.display()));
        }
    }

    let mut builder = builder
        // Emits a struct that loads the library at runtime rather than
        // extern "C" blocks that must be satisfied at link time.
        .dynamic_library_name(family.struct_name)
        // Tolerate a driver build that is missing a newer entry point; the
        // wrapper reports the absence per symbol instead of failing to load.
        .dynamic_link_require_all(false)
        .generate_inline_functions(false)
        .layout_tests(false)
        // The PicoTech headers document some functions with prose that is
        // not valid Rust ("Example: AQ005 / 139, ..."). Carried through as
        // doc comments, rustdoc treats those as doctests and `cargo test`
        // fails trying to compile them. The prose is still in the headers,
        // which is where anyone would read it anyway.
        .generate_comments(false);

    for pattern in family.patterns {
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
        .unwrap_or_else(|e| panic!("could not generate {} bindings: {e}", family.dir));

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

    let out_path = out_dir.join(format!("{}_bindings.rs", family.dir));
    fs::write(&out_path, source)
        .unwrap_or_else(|e| panic!("could not write {}: {e}", out_path.display()));
}
