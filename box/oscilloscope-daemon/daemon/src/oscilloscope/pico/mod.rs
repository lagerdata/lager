// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! PicoScope backends.
//!
//! `ps2000` is the legacy snake_case driver, with an implementation of its
//! own. The 2000a/3000a/4000a/5000a families share one, over the vtable in
//! `modern`. Every family's driver is loaded at runtime, so one binary can
//! serve any of them; each is compiled in only when build.rs found its
//! headers.

pub mod detect;
pub mod loader;
pub mod modern;
pub mod modern_scope;
#[cfg(pico_ps2000)]
pub mod ps2000;
pub mod status;
pub mod types;

pub use detect::{detect, DetectedScope, NoUnitFound};
pub use loader::{installed_families, LeftOut};
pub use modern::{api_for, PicoModernApi};
pub use modern_scope::PicoScopeModern;
#[cfg(pico_ps2000)]
pub use ps2000::PicoScope2000;
pub use types::{Coupling, DeviceResolution, Range, ThresholdDirection};
