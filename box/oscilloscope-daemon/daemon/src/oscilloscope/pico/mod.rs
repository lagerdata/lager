// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

#[cfg(feature = "ps2000")]
pub mod ps2000;

#[cfg(feature = "ps2000a")]
pub mod ps2000a;

#[cfg(feature = "ps2000")]
pub use ps2000::PicoScope2000;

#[cfg(feature = "ps2000a")]
pub use ps2000a::PicoScope2000A;
