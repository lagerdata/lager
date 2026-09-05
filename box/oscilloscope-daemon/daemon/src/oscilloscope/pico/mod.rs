// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! PicoScope backends.
//!
//! `ps2000` is the legacy snake_case driver, kept behind a Cargo feature
//! because it links against `libps2000` at build time. The 2000a/3000a/4000a/
//! 5000a families load at runtime instead, so one binary can serve any of
//! them without the SDK present at build time.

pub mod detect;
pub mod loader;
pub mod modern;
pub mod modern_scope;
pub mod ps2000;
pub mod status;
pub mod types;

pub use detect::{detect, DetectedScope};
pub use loader::installed_families;
pub use modern::{api_for, PicoModernApi};
pub use modern_scope::PicoScopeModern;
pub use ps2000::PicoScope2000;
pub use types::{Coupling, DeviceResolution, Range, ThresholdDirection};
