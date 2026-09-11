#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Oscilloscope Test Client
A Python GUI application that connects to the oscilloscope daemon via WebSocket
and provides a CLI-like interface for sending commands with live plotting.
"""

import os
# Suppress macOS Tk deprecation warning
os.environ['TK_SILENCE_DEPRECATION'] = '1'

import asyncio
import json
import websockets
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import threading
import matplotlib.pyplot as plt
import matplotlib.backends.backend_tkagg as tkagg
from matplotlib.figure import Figure
import numpy as np
from typing import Dict, Any, Optional
import queue
import time
import sys
import argparse

class OscilloscopeClient:
    def __init__(self, host: str = "localhost", port: int = 8082):
        self.host = host
        self.port = port
        self.websocket: Optional[websockets.WebSocketServerProtocol] = None
        self.connected = False
        self.streaming_data = {}
        self.is_streaming = False
        self.message_queue = queue.Queue()
        self.loop = None
        self.thread = None
        
    def start_async_loop(self):
        """Start the async event loop in a separate thread"""
        print("Creating async loop thread...")
        
        def run_loop():
            print("Thread: Creating new event loop...")
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            print("Thread: Starting event loop...")
            self.loop.run_forever()
        
        self.thread = threading.Thread(target=run_loop, daemon=True)
        print("Starting thread...")
        self.thread.start()
        print("[OK] Thread started")
        
        # Wait for loop to be ready
        print("Waiting for loop to be ready...")
        while self.loop is None:
            time.sleep(0.01)
        print("[OK] Loop is ready")
    
    async def connect(self):
        """Connect to the daemon"""
        try:
            uri = f"ws://{self.host}:{self.port}"
            self.websocket = await websockets.connect(uri)
            self.connected = True
            self.message_queue.put(("info", f"[OK] Connected to {uri}"))
            return True
        except Exception as e:
            self.message_queue.put(("error", f"[FAIL] Connection failed: {e}"))
            return False
            
    async def disconnect(self):
        """Disconnect from daemon"""
        if self.websocket:
            await self.websocket.close()
            self.connected = False
            self.message_queue.put(("info", "[OK] Disconnected from daemon"))
            
    async def send_command(self, command: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Send command and return response"""
        if not self.connected:
            self.message_queue.put(("error", "[FAIL] Not connected to daemon!"))
            return None
            
        try:
            await self.websocket.send(json.dumps(command))
            response = await self.websocket.recv()
            return json.loads(response)
        except Exception as e:
            self.message_queue.put(("error", f"[FAIL] Error sending command: {e}"))
            return None
            
    async def listen_for_data(self):
        """Listen for streaming data"""
        try:
            async for message in self.websocket:
                try:
                    data = json.loads(message)
                    if "TriggeredData" in data:
                        await self._handle_triggered_data(data["TriggeredData"])
                    else:
                        self.message_queue.put(("response", f">> {message}"))
                except json.JSONDecodeError:
                    self.message_queue.put(("response", f">> {message}"))
        except websockets.exceptions.ConnectionClosed:
            self.message_queue.put(("error", "[FAIL] Connection closed"))
            self.connected = False
        except Exception as e:
            self.message_queue.put(("error", f"[FAIL] WebSocket error: {e}"))
            
    async def _handle_triggered_data(self, data):
        """Handle triggered data from oscilloscope"""
        try:
            samples = data.get("samples", [])
            trigger_position = data.get("trigger_position", 0)
            sample_interval_ns = data.get("sample_interval_ns", 1)
            overflow = data.get("overflow", False)
            
            self.message_queue.put(("data", f"Data: Received {len(samples)} samples, trigger at {trigger_position}, overflow: {overflow}"))
            
            if self.is_streaming and samples:
                self.message_queue.put(("plot", {
                    "samples": samples,
                    "sample_interval_ns": sample_interval_ns
                }))
                
        except Exception as e:
            self.message_queue.put(("error", f"[FAIL] Error processing triggered data: {e}"))

class OscilloscopeGUI:
    def __init__(self, host: str = "localhost", port: int = 8082):
        self.client = OscilloscopeClient(host, port)
        self.root = tk.Tk()
        self.root.title(f"Oscilloscope Test Client - {host}:{port}")
        self.root.geometry("1000x700")
        
        # Start async loop
        self.client.start_async_loop()
        
        self.setup_ui()
        self.setup_plot()
        self.process_queue()
        
    def setup_ui(self):
        """Setup the GUI interface"""
        # Main frame
        main_frame = tk.Frame(self.root, bg="black")
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        # Plot frame (top 70% of window)
        plot_frame = tk.Frame(main_frame, bg="black")
        plot_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.plot_frame = plot_frame
        
        # CLI frame (bottom 30% of window)
        cli_frame = tk.Frame(main_frame, bg="black", height=150)
        cli_frame.pack(fill=tk.X, padx=5, pady=(0, 5))
        cli_frame.pack_propagate(False)  # Maintain fixed height
        
        # CLI output area
        self.cli_output = scrolledtext.ScrolledText(
            cli_frame, 
            height=8, 
            font=("Courier", 11), 
            bg="black", 
            fg="green",
            insertbackground="green",
            selectbackground="darkgreen"
        )
        self.cli_output.pack(fill=tk.BOTH, expand=True, pady=(0, 5))
        
        # CLI input area
        input_frame = tk.Frame(cli_frame, bg="black")
        input_frame.pack(fill=tk.X, pady=(0, 5))
        
        # Prompt label
        prompt_label = tk.Label(input_frame, text="oscilloscope> ", 
                               font=("Courier", 11), bg="black", fg="green")
        prompt_label.pack(side=tk.LEFT)
        
        # Command entry
        self.cmd_entry = tk.Entry(input_frame, font=("Courier", 11), 
                                 bg="black", fg="green", insertbackground="green",
                                 relief="flat", bd=0)
        self.cmd_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.cmd_entry.bind('<Return>', self.send_command)
        self.cmd_entry.bind('<Up>', self.command_history_up)
        self.cmd_entry.bind('<Down>', self.command_history_down)
        
        # Focus on command entry
        self.cmd_entry.focus()
        
        # Command history
        self.command_history = []
        self.history_index = -1
        
        # Welcome message
        self.log_message("Oscilloscope Terminal Client")
        self.log_message("Type 'help' for available commands")
        self.log_message("Type 'connect' to connect to daemon")
        self.log_message("")
        
    def setup_plot(self):
        """Setup the matplotlib plot"""
        try:
            self.fig = Figure(figsize=(10, 6), dpi=100, facecolor='black')
            self.ax = self.fig.add_subplot(111, facecolor='black')
            
            # Oscilloscope-style appearance
            self.ax.set_title("Oscilloscope Display", color='green', fontsize=14, fontweight='bold')
            self.ax.set_xlabel("Time (μs)", color='green')
            self.ax.set_ylabel("Voltage (V)", color='green')
            self.ax.grid(True, color='darkgreen', alpha=0.3)
            self.ax.tick_params(colors='green')
            
            # Set axis colors
            for spine in self.ax.spines.values():
                spine.set_color('green')
            
            self.canvas = tkagg.FigureCanvasTkAgg(self.fig, self.plot_frame)
            self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
            
            # Add a test signal
            import numpy as np
            x = np.linspace(0, 10, 100)
            y = np.sin(x)
            self.ax.plot(x, y, 'lime', linewidth=2, label='Channel A')
            self.ax.legend(loc='upper right', frameon=False, labelcolor='green')
            
            self.canvas.draw()
            
        except Exception as e:
            print(f"[FAIL] Error in setup_plot: {e}")
            import traceback
            traceback.print_exc()
        
    def log_message(self, message: str, msg_type: str = "info"):
        """Add message to CLI output"""
        timestamp = time.strftime("%H:%M:%S")
        if msg_type == "error":
            prefix = "[ERROR]"
        elif msg_type == "response":
            prefix = ">>"
        elif msg_type == "data":
            prefix = "[DATA]"
        else:
            prefix = "[INFO]"
            
        formatted_msg = f"[{timestamp}] {prefix} {message}\n"
        self.cli_output.insert(tk.END, formatted_msg)
        self.cli_output.see(tk.END)
        
    def process_queue(self):
        """Process messages from the async queue"""
        try:
            while True:
                msg_type, data = self.client.message_queue.get_nowait()
                if msg_type == "plot":
                    self.update_plot(data["samples"], data["sample_interval_ns"])
                else:
                    self.log_message(data, msg_type)
        except queue.Empty:
            pass
        
        # Schedule next check
        self.root.after(100, self.process_queue)
        
    def update_plot(self, samples, sample_interval_ns):
        """Update the plot with new data"""
        try:
            # Process samples by channel
            channel_data = {}
            for sample in samples:
                channel = sample.get("channel", "A")
                voltage = sample.get("voltage", 0.0)
                sample_index = sample.get("sample_index", 0)
                
                if channel not in channel_data:
                    channel_data[channel] = {"time": [], "voltage": []}
                
                # Convert sample index to time
                time_ns = sample_index * sample_interval_ns
                time_us = time_ns / 1000.0  # Convert to microseconds
                
                channel_data[channel]["time"].append(time_us)
                channel_data[channel]["voltage"].append(voltage)
            
            # Clear and redraw plot
            self.ax.clear()
            self.ax.set_title("Oscilloscope Data")
            self.ax.set_xlabel("Time (μs)")
            self.ax.set_ylabel("Voltage (V)")
            self.ax.grid(True)
            
            colors = ['blue', 'red', 'green', 'orange']
            for i, (channel, data) in enumerate(channel_data.items()):
                if data["time"] and data["voltage"]:
                    color = colors[i % len(colors)]
                    self.ax.plot(data["time"], data["voltage"], 
                               color=color, label=f"Channel {channel}", linewidth=1)
            
            self.ax.legend()
            self.canvas.draw()
            
        except Exception as e:
            self.log_message(f"[FAIL] Error plotting data: {e}", "error")
    
    def connect(self):
        """Connect to daemon"""
        def _connect():
            asyncio.run_coroutine_threadsafe(self.client.connect(), self.client.loop)
        _connect()
        
    def disconnect(self):
        """Disconnect from daemon"""
        def _disconnect():
            asyncio.run_coroutine_threadsafe(self.client.disconnect(), self.client.loop)
        _disconnect()
        
    def show_status(self):
        """Show connection status"""
        status = "Connected" if self.client.connected else "Disconnected"
        self.log_message(f"Status: {status}")
        if self.client.connected:
            self.log_message(f"Host: {self.client.host}:{self.client.port}")
    
    def command_history_up(self, event=None):
        """Navigate up in command history"""
        if self.history_index < len(self.command_history) - 1:
            self.history_index += 1
            self.cmd_entry.delete(0, tk.END)
            self.cmd_entry.insert(0, self.command_history[-(self.history_index + 1)])
        return "break"
    
    def command_history_down(self, event=None):
        """Navigate down in command history"""
        if self.history_index > 0:
            self.history_index -= 1
            self.cmd_entry.delete(0, tk.END)
            self.cmd_entry.insert(0, self.command_history[-(self.history_index + 1)])
        elif self.history_index == 0:
            self.history_index = -1
            self.cmd_entry.delete(0, tk.END)
        return "break"
    
    def send_command(self, event=None):
        """Send command from input field"""
        cmd = self.cmd_entry.get().strip()
        if not cmd:
            return
            
        # Add to history
        if not self.command_history or self.command_history[-1] != cmd:
            self.command_history.append(cmd)
        self.history_index = -1
        
        self.cmd_entry.delete(0, tk.END)
        self.log_message(f"oscilloscope> {cmd}")
        
        # Parse and execute command
        def _send():
            asyncio.run_coroutine_threadsafe(self._execute_command(cmd), self.client.loop)
        _send()
    
    async def _execute_command(self, cmd: str):
        """Execute a parsed command"""
        parts = cmd.split()
        if not parts:
            return
            
        command_name = parts[0].lower()
        
        try:
            # Built-in commands
            if command_name == "connect":
                await self.client.connect()
                return
            elif command_name == "disconnect":
                await self.client.disconnect()
                return
            elif command_name == "status":
                self.show_status()
                return
            elif command_name == "help":
                self.show_help()
                return
            elif command_name == "clear":
                self.output_text.delete(1.0, tk.END)
                return
            elif command_name == "exit" or command_name == "quit":
                self.root.quit()
                return
            # Oscilloscope commands
            elif command_name == "sv" and len(parts) >= 3:
                channel = parts[1].upper()
                volts = float(parts[2])
                command = {"SetVoltsPerDiv": {"channel": channel, "volts_per_div": volts}}
                response = await self.client.send_command(command)
                if response:
                    self.client.message_queue.put(("response", f"SetVoltsPerDiv: {channel} {volts}V/div -> {response}"))
                    
            elif command_name == "tp" and len(parts) >= 2:
                time_per_div = float(parts[1])
                command = {"SetTimePerDiv": {"time_per_div": time_per_div}}
                response = await self.client.send_command(command)
                if response:
                    self.client.message_queue.put(("response", f"SetTimePerDiv: {time_per_div}s/div -> {response}"))
                    
            elif command_name == "enable" and len(parts) >= 2:
                channel = parts[1].upper()
                command = {"EnableChannel": {"channel": channel}}
                response = await self.client.send_command(command)
                if response:
                    self.client.message_queue.put(("response", f"EnableChannel: {channel} -> {response}"))
                    
            elif command_name == "disable" and len(parts) >= 2:
                channel = parts[1].upper()
                command = {"DisableChannel": {"channel": channel}}
                response = await self.client.send_command(command)
                if response:
                    self.client.message_queue.put(("response", f"DisableChannel: {channel} -> {response}"))
                    
            elif command_name == "coupling" and len(parts) >= 3:
                channel = parts[1].upper()
                coupling = parts[2].upper()
                command = {"SetCoupling": {"channel": channel, "coupling": coupling}}
                response = await self.client.send_command(command)
                if response:
                    self.client.message_queue.put(("response", f"SetCoupling: {channel} {coupling} -> {response}"))
                    
            elif command_name == "trigger_level" and len(parts) >= 2:
                level = float(parts[1])
                command = {"SetTriggerLevel": {"trigger_level": level}}
                response = await self.client.send_command(command)
                if response:
                    self.client.message_queue.put(("response", f"SetTriggerLevel: {level}V -> {response}"))
                    
            elif command_name == "trigger_source" and len(parts) >= 2:
                channel = parts[1].upper()
                command = {"SetTriggerSource": {"trigger_source": channel}}
                response = await self.client.send_command(command)
                if response:
                    self.client.message_queue.put(("response", f"SetTriggerSource: {channel} -> {response}"))
                    
            elif command_name == "trigger_slope" and len(parts) >= 2:
                slope = parts[1].capitalize()
                command = {"SetTriggerSlope": {"trigger_slope": slope}}
                response = await self.client.send_command(command)
                if response:
                    self.client.message_queue.put(("response", f"SetTriggerSlope: {slope} -> {response}"))
                    
            elif command_name == "capture_mode" and len(parts) >= 2:
                mode = parts[1].capitalize()
                command = {"SetCaptureMode": {"capture_mode": mode}}
                response = await self.client.send_command(command)
                if response:
                    self.client.message_queue.put(("response", f"SetCaptureMode: {mode} -> {response}"))
                    
            elif command_name == "start":
                trigger_pos = float(parts[1]) if len(parts) >= 2 else 50.0
                command = {"StartAcquisition": {"trigger_position_percent": trigger_pos}}
                response = await self.client.send_command(command)
                if response:
                    self.client.message_queue.put(("response", f"StartAcquisition: trigger at {trigger_pos}% -> {response}"))
                    
            elif command_name == "stop":
                command = {"StopAcquisition": {}}
                response = await self.client.send_command(command)
                if response:
                    self.client.message_queue.put(("response", f"StopAcquisition -> {response}"))
                    
            elif command_name == "ready":
                command = {"IsReady": {}}
                response = await self.client.send_command(command)
                if response:
                    self.client.message_queue.put(("response", f"IsReady -> {response}"))
                    
            elif command_name == "data":
                command = {"GetTriggeredData": {}}
                response = await self.client.send_command(command)
                if response:
                    self.client.message_queue.put(("response", f"GetTriggeredData -> {response}"))
                    
            elif command_name == "stream":
                if not self.client.connected:
                    self.client.message_queue.put(("error", "[FAIL] Not connected! Use 'connect' first."))
                    return
                    
                self.client.is_streaming = True
                self.client.message_queue.put(("info", "Starting streaming mode..."))
                self.client.message_queue.put(("info", "Tip: Use 'start' command to begin acquisition"))
                
                # Start listening for data
                asyncio.create_task(self.client.listen_for_data())
                
            else:
                self.client.message_queue.put(("error", f"[FAIL] Unknown command: {command_name}"))
                
        except Exception as e:
            self.client.message_queue.put(("error", f"[FAIL] Error executing command: {e}"))
    
    def show_help(self):
        """Show help in terminal"""
        help_text = """Available Commands:

Built-in Commands:
  connect     - Connect to daemon
  disconnect  - Disconnect from daemon
  status      - Show connection status
  help        - Show this help
  clear       - Clear terminal
  exit/quit   - Exit application

Channel Settings:
  sv <channel> <volts>     - Set volts per division (e.g., 'sv A 1.0')
  enable <channel>         - Enable channel (e.g., 'enable A')
  disable <channel>        - Disable channel (e.g., 'disable A')
  coupling <channel> <type> - Set coupling (e.g., 'coupling A DC')

Time Settings:
  tp <time>                - Set time per division (e.g., 'tp 1e-3')

Trigger Settings:
  trigger_level <volts>    - Set trigger level (e.g., 'trigger_level 0.5')
  trigger_source <channel> - Set trigger source (e.g., 'trigger_source A')
  trigger_slope <slope>    - Set trigger slope (e.g., 'trigger_slope Rising')
  capture_mode <mode>      - Set capture mode (e.g., 'capture_mode Auto')

Acquisition:
  start <pos%>   - Start acquisition (e.g., 'start 50')
  stop           - Stop acquisition
  ready          - Check if ready
  data           - Get triggered data
  stream         - Start streaming mode with live plotting

Use ↑/↓ arrows for command history."""
        
        self.log_message(help_text)
    
    def run(self):
        """Start the GUI"""
        self.root.mainloop()

def main():
    """Main function with command line argument parsing"""
    parser = argparse.ArgumentParser(description="Oscilloscope Test Client - Terminal-like GUI")
    parser.add_argument("--host", default="localhost", help="Daemon host (default: localhost)")
    parser.add_argument("--port", type=int, default=8082, help="Daemon port (default: 8082)")
    
    args = parser.parse_args()
    
    app = OscilloscopeGUI(args.host, args.port)
    app.run()

if __name__ == "__main__":
    main()