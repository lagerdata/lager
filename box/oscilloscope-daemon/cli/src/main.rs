// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

use anyhow::Result;
use clap::Parser;
use protocol::{CaptureMode, ChannelId, Command, Coupling, Response, TriggerSlope};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader, BufWriter};
use tokio::net::TcpStream;
use tokio::net::tcp::OwnedReadHalf;
use tokio::net::tcp::OwnedWriteHalf;

#[derive(Parser)]
#[command(name = "oscilloscope-cli")]
#[command(about = "Command-line interface for oscillscope control")]
struct Cli {
    #[arg(long, default_value = "127.0.0.1:8081")]
    server: String,

    #[command(subcommand)]
    command: Command,
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();

    match cli.command {
        Command::GetBandwidth => {
            handle_get_bandwidth(&cli.server).await?;
        }
        Command::GetChannelCount => {
            handle_get_channel_count(&cli.server).await?;
        }
        Command::GetMemoryDepth => {
            handle_get_memory_depth(&cli.server).await?;
        }
        Command::GetSampleRate => {
            handle_get_sample_rate(&cli.server).await?;
        }
        Command::SetVoltsPerDiv {
            channel,
            volts_per_div,
        } => {
            handle_set_volts_per_div(&cli.server, channel, volts_per_div).await?;
        }
        Command::GetVoltsPerDiv { channel } => {
            handle_get_volts_per_div(&cli.server, channel).await?;
        }
        Command::SetCoupling { channel, coupling } => {
            handle_set_coupling(&cli.server, channel, coupling).await?;
        }
        Command::GetCoupling { channel } => {
            handle_get_coupling(&cli.server, channel).await?;
        }
        Command::SetTriggerLevel { trigger_level } => {
            handle_set_trigger_level(&cli.server, trigger_level).await?;
        }
        Command::GetTriggerLevel => {
            handle_get_trigger_level(&cli.server).await?;
        }
        Command::SetTriggerSource { trigger_source } => {
            handle_set_trigger_source(&cli.server, trigger_source).await?;
        }
        Command::GetTriggerSource => {
            handle_get_trigger_source(&cli.server).await?;
        }
        Command::SetTriggerSlope { trigger_slope } => {
            handle_set_trigger_slope(&cli.server, trigger_slope).await?;
        }
        Command::GetTriggerSlope => {
            handle_get_trigger_slope(&cli.server).await?;
        }
        Command::SetCaptureMode { capture_mode } => {
            handle_set_capture_mode(&cli.server, capture_mode).await?;
        }
        Command::GetCaptureMode => {
            handle_get_capture_mode(&cli.server).await?;
        }
        _ => {
            return Err(anyhow::anyhow!("Unexpected command: {:?}", cli.command));
        }
    }

    Ok(())
}

async fn handle_get_bandwidth(server: &str) -> Result<()> {
    println!("Getting bandwidth");
    let (mut reader, mut writer) = create_networking_components(server.to_string()).await?;

    let json_command = serde_json::to_string(&Command::GetBandwidth)?;
    writer.write_all(json_command.as_bytes()).await?;
    writer.write_all(b"\n").await?;
    writer.flush().await?;

    let mut buffer = String::new();
    reader.read_line(&mut buffer).await?;
    let response: Response = serde_json::from_str(&buffer)?;
    match response {
        Response::GetBandwidth { bandwidth } => {
            println!("Bandwidth: {}", bandwidth);
        }
        Response::Error { message } => {
            println!("Error: {}", message);
        }
        _ => {
            return Err(anyhow::anyhow!("Unexpected response: {:?}", response));
        }
    }
    Ok(())
}

async fn handle_get_channel_count(server: &str) -> Result<()> {
    println!("Getting bandwidth");
    let (mut reader, mut writer) = create_networking_components(server.to_string()).await?;

    let json_command = serde_json::to_string(&Command::GetChannelCount)?;
    writer.write_all(json_command.as_bytes()).await?;
    writer.write_all(b"\n").await?;
    writer.flush().await?;

    let mut buffer = String::new();
    reader.read_line(&mut buffer).await?;
    let response: Response = serde_json::from_str(&buffer)?;
    match response {
        Response::GetChannelCount { channel_count } => {
            println!("Channel count: {}", channel_count);
        }
        Response::Error { message } => {
            println!("Error: {}", message);
        }
        _ => {
            return Err(anyhow::anyhow!("Unexpected response: {:?}", response));
        }
    }
    Ok(())
}

async fn handle_get_memory_depth(server: &str) -> Result<()> {
    println!("Getting memory depth");
    let (mut reader, mut writer) = create_networking_components(server.to_string()).await?;

    let json_command = serde_json::to_string(&Command::GetMemoryDepth)?;
    writer.write_all(json_command.as_bytes()).await?;
    writer.write_all(b"\n").await?;
    writer.flush().await?;

    let mut buffer = String::new();
    reader.read_line(&mut buffer).await?;
    let response: Response = serde_json::from_str(&buffer)?;
    match response {
        Response::GetMemoryDepth { memory_depth } => {
            println!("Memory depth: {}", memory_depth);
        }
        _ => {
            return Err(anyhow::anyhow!("Unexpected response: {:?}", response));
        }
    }
    Ok(())
}

async fn handle_get_sample_rate(server: &str) -> Result<()> {
    println!("Getting sample rate");
    let (mut reader, mut writer) = create_networking_components(server.to_string()).await?;

    let json_command = serde_json::to_string(&Command::GetSampleRate)?;
    writer.write_all(json_command.as_bytes()).await?;
    writer.write_all(b"\n").await?;
    writer.flush().await?;

    let mut buffer = String::new();
    reader.read_line(&mut buffer).await?;
    let response: Response = serde_json::from_str(&buffer)?;
    match response {
        Response::GetSampleRate { sample_rate } => {
            println!("Sample rate: {}", sample_rate);
        }
        Response::Error { message } => {
            println!("Error: {}", message);
        }
        _ => {
            return Err(anyhow::anyhow!("Unexpected response: {:?}", response));
        }
    }
    Ok(())
}

async fn handle_get_volts_per_div(server: &str, channel: ChannelId) -> Result<()> {
    let (mut reader, mut writer) = create_networking_components(server.to_string()).await?;

    let json_command = serde_json::to_string(&Command::GetVoltsPerDiv { channel })?;
    writer.write_all(json_command.as_bytes()).await?;
    writer.write_all(b"\n").await?;
    writer.flush().await?;

    let mut buffer = String::new();
    reader.read_line(&mut buffer).await?;
    let response: Response = serde_json::from_str(&buffer)?;
    match response {
        Response::GetVoltsPerDiv {
            channel,
            volts_per_div,
        } => {
            println!(
                "Volts per div for channel {}: {}",
                channel.as_str(),
                volts_per_div
            );
        }
        Response::Error { message } => {
            println!("Error: {}", message);
        }
        _ => {
            return Err(anyhow::anyhow!("Unexpected response: {:?}", response));
        }
    }
    Ok(())
}

async fn handle_set_volts_per_div(
    server: &str,
    channel: ChannelId,
    volts_per_div: f64,
) -> Result<()> {
    println!("Setting volts per div for channel {}", channel.as_str());
    let (mut reader, mut writer) = create_networking_components(server.to_string()).await?;

    let json_command = serde_json::to_string(&Command::SetVoltsPerDiv {
        channel,
        volts_per_div,
    })?;
    writer.write_all(json_command.as_bytes()).await?;
    writer.write_all(b"\n").await?;
    writer.flush().await?;

    let mut buffer = String::new();
    reader.read_line(&mut buffer).await?;
    let response: Response = serde_json::from_str(&buffer)?;
    match response {
        Response::ConfigureChannelVoltsPerDiv => {
            println!("Success");
        }
        Response::Error { message } => {
            println!("Error: {}", message);
        }
        _ => {
            return Err(anyhow::anyhow!("Unexpected response: {:?}", response));
        }
    }
    Ok(())
}

async fn handle_set_coupling(server: &str, channel: ChannelId, coupling: Coupling) -> Result<()> {
    println!("Setting coupling for channel {}", channel.as_str());
    let (mut reader, mut writer) = create_networking_components(server.to_string()).await?;

    let json_command = serde_json::to_string(&Command::SetCoupling { channel, coupling })?;
    writer.write_all(json_command.as_bytes()).await?;
    writer.write_all(b"\n").await?;
    writer.flush().await?;

    let mut buffer = String::new();
    reader.read_line(&mut buffer).await?;
    let response: Response = serde_json::from_str(&buffer)?;
    match response {
        Response::ConfigureChannelCoupling => {
            println!("Success");
        }
        Response::Error { message } => {
            println!("Error: {}", message);
        }
        _ => {
            return Err(anyhow::anyhow!("Unexpected response: {:?}", response));
        }
    }
    Ok(())
}

async fn handle_get_coupling(server: &str, channel: ChannelId) -> Result<()> {
    let (mut reader, mut writer) = create_networking_components(server.to_string()).await?;

    let json_command = serde_json::to_string(&Command::GetCoupling { channel })?;
    writer.write_all(json_command.as_bytes()).await?;
    writer.write_all(b"\n").await?;
    writer.flush().await?;

    let mut buffer = String::new();
    reader.read_line(&mut buffer).await?;
    let response: Response = serde_json::from_str(&buffer)?;
    match response {
        Response::GetCoupling { channel, coupling } => {
            println!("Coupling for channel {}: {}", channel.as_str(), coupling);
        }
        Response::Error { message } => {
            println!("Error: {}", message);
        }
        _ => {
            return Err(anyhow::anyhow!("Unexpected response: {:?}", response));
        }
    }
    Ok(())
}

async fn handle_set_trigger_level(server: &str, trigger_level: f64) -> Result<()> {
    let (mut reader, mut writer) = create_networking_components(server.to_string()).await?;

    let json_command = serde_json::to_string(&Command::SetTriggerLevel { trigger_level })?;
    writer.write_all(json_command.as_bytes()).await?;
    writer.write_all(b"\n").await?;
    writer.flush().await?;

    let mut buffer = String::new();
    reader.read_line(&mut buffer).await?;
    let response: Response = serde_json::from_str(&buffer)?;
    match response {
        Response::ConfigureTriggerLevel => {
            println!("Success");
        }
        Response::Error { message } => {
            println!("Error: {}", message);
        }
        _ => {
            return Err(anyhow::anyhow!("Unexpected response: {:?}", response));
        }
    }
    Ok(())
}

async fn handle_get_trigger_level(server: &str) -> Result<()> {
    let (mut reader, mut writer) = create_networking_components(server.to_string()).await?;

    let json_command = serde_json::to_string(&Command::GetTriggerLevel)?;
    writer.write_all(json_command.as_bytes()).await?;
    writer.write_all(b"\n").await?;
    writer.flush().await?;

    let mut buffer = String::new();
    reader.read_line(&mut buffer).await?;
    let response: Response = serde_json::from_str(&buffer)?;
    match response {
        Response::GetTriggerLevel { trigger_level } => {
            println!("Trigger level: {}", trigger_level);
        }
        _ => {
            return Err(anyhow::anyhow!("Unexpected response: {:?}", response));
        }
    }
    Ok(())
}

async fn handle_set_trigger_source(server: &str, trigger_source: ChannelId) -> Result<()> {
    let (mut reader, mut writer) = create_networking_components(server.to_string()).await?;

    let json_command = serde_json::to_string(&Command::SetTriggerSource { trigger_source })?;
    writer.write_all(json_command.as_bytes()).await?;
    writer.write_all(b"\n").await?;
    writer.flush().await?;

    let mut buffer = String::new();
    reader.read_line(&mut buffer).await?;
    let response: Response = serde_json::from_str(&buffer)?;
    match response {
        Response::ConfigureTriggerSource => {
            println!("Success");
        }
        Response::Error { message } => {
            println!("Error: {}", message);
        }
        _ => {
            return Err(anyhow::anyhow!("Unexpected response: {:?}", response));
        }
    }
    Ok(())
}

async fn handle_get_trigger_source(server: &str) -> Result<()> {
    let (mut reader, mut writer) = create_networking_components(server.to_string()).await?;

    let json_command = serde_json::to_string(&Command::GetTriggerSource)?;
    writer.write_all(json_command.as_bytes()).await?;
    writer.write_all(b"\n").await?;
    writer.flush().await?;

    let mut buffer = String::new();
    reader.read_line(&mut buffer).await?;
    let response: Response = serde_json::from_str(&buffer)?;
    match response {
        Response::GetTriggerSource { trigger_source } => {
            println!("Trigger source: {}", trigger_source);
        }
        Response::Error { message } => {
            println!("Error: {}", message);
        }
        _ => {
            return Err(anyhow::anyhow!("Unexpected response: {:?}", response));
        }
    }
    Ok(())
}

async fn handle_set_trigger_slope(server: &str, trigger_slope: TriggerSlope) -> Result<()> {
    let (mut reader, mut writer) = create_networking_components(server.to_string()).await?;

    let json_command = serde_json::to_string(&Command::SetTriggerSlope { trigger_slope })?;
    writer.write_all(json_command.as_bytes()).await?;
    writer.write_all(b"\n").await?;
    writer.flush().await?;

    let mut buffer = String::new();
    reader.read_line(&mut buffer).await?;
    let response: Response = serde_json::from_str(&buffer)?;
    match response {
        Response::ConfigureTriggerSlope => {
            println!("Success");
        }
        Response::Error { message } => {
            println!("Error: {}", message);
        }
        _ => {
            return Err(anyhow::anyhow!("Unexpected response: {:?}", response));
        }
    }
    Ok(())
}

async fn handle_get_trigger_slope(server: &str) -> Result<()> {
    let (mut reader, mut writer) = create_networking_components(server.to_string()).await?;

    let json_command = serde_json::to_string(&Command::GetTriggerSlope)?;
    writer.write_all(json_command.as_bytes()).await?;
    writer.write_all(b"\n").await?;
    writer.flush().await?;

    let mut buffer = String::new();
    reader.read_line(&mut buffer).await?;
    let response: Response = serde_json::from_str(&buffer)?;
    match response {
        Response::GetTriggerSlope { trigger_slope } => {
            println!("Trigger slope: {}", trigger_slope);
        }
        Response::Error { message } => {
            println!("Error: {}", message);
        }
        _ => {
            return Err(anyhow::anyhow!("Unexpected response: {:?}", response));
        }
    }
    Ok(())
}

async fn handle_set_capture_mode(server: &str, capture_mode: CaptureMode) -> Result<()> {
    let (mut reader, mut writer) = create_networking_components(server.to_string()).await?;

    let json_command = serde_json::to_string(&Command::SetCaptureMode { capture_mode })?;
    writer.write_all(json_command.as_bytes()).await?;
    writer.write_all(b"\n").await?;
    writer.flush().await?;

    let mut buffer = String::new();
    reader.read_line(&mut buffer).await?;
    let response: Response = serde_json::from_str(&buffer)?;
    match response {
        Response::ConfigureCaptureMode => {
            println!("Success");
        }
        Response::Error { message } => {
            println!("Error: {}", message);
        }
        _ => {
            return Err(anyhow::anyhow!("Unexpected response: {:?}", response));
        }
    }
    Ok(())
}

async fn handle_get_capture_mode(server: &str) -> Result<()> {
    let (mut reader, mut writer) = create_networking_components(server.to_string()).await?;

    let json_command = serde_json::to_string(&Command::GetCaptureMode)?;
    writer.write_all(json_command.as_bytes()).await?;
    writer.write_all(b"\n").await?;
    writer.flush().await?;

    let mut buffer = String::new();
    reader.read_line(&mut buffer).await?;
    let response: Response = serde_json::from_str(&buffer)?;
    match response {
        Response::GetCaptureMode { capture_mode } => {
            println!("Capture mode: {}", capture_mode);
        }
        Response::Error { message } => {
            println!("Error: {}", message);
        }
        _ => {
            return Err(anyhow::anyhow!("Unexpected response: {:?}", response));
        }
    }
    Ok(())
}

async fn create_networking_components(
    server: String,
) -> Result<(BufReader<OwnedReadHalf>, BufWriter<OwnedWriteHalf>)> {
    let stream = TcpStream::connect(server).await?;
    let (read_half, write_half) = stream.into_split();
    let reader = BufReader::new(read_half);
    let writer = BufWriter::new(write_half);
    Ok((reader, writer))
}
