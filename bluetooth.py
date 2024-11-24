import asyncio
from bleak import BleakClient, BleakScanner
import struct
import wave
import time
import os
from datetime import datetime

# BLE UUIDs (must match ESP32)
SERVICE_UUID = "4fafc201-1fb5-459e-8fcc-c5c9c331914b"
CHARACTERISTIC_UUID = "beb5483e-36e1-4688-b7f5-ea07361b26a8"

# Device name as advertised
DEVICE_NAME = "ESP32WAV"

# Audio settings
SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2  # 16-bit audio
CHANNELS = 1

class AudioStreamReceiver:
    def __init__(self):
        self.reset_session()
        
    def reset_session(self):
        """Reset all session variables for a new recording"""
        self.audio_data = bytearray()
        self.start_time = None
        self.last_progress_time = None
        self.frames_received = 0
        self.last_frame_count = None
        self.last_data_time = None
        self.is_receiving = False
        self.samples_per_frame = 160  # Match ESP32's FRAME_SIZE
        self.frame_stats = {
            'drops': 0,
            'out_of_order': 0,
            'invalid_size': 0
        }
        
    def notification_handler(self, sender, data):
        """Handle incoming BLE notifications"""
        current_time = time.time()
        
        # If we're getting data after a pause, this is a new session
        if self.last_data_time and (current_time - self.last_data_time) > 1.0:
            if self.is_receiving:
                # Previous session ended, save it
                self.save_wav_file()
                self.reset_session()
        
        self.last_data_time = current_time
        self.is_receiving = True
        
        if not self.start_time:
            self.start_time = current_time
            self.last_progress_time = current_time
            
        if len(data) < 3:  # Ensure we have at least the header
            print(f"Warning: Received incomplete frame (length: {len(data)})")
            return
            
        # Parse the 3-byte header from ESP32
        frame_count = data[0] | (data[1] << 8)
        
        # Check for frame sequence continuity
        if self.last_frame_count is not None:
            expected_frame = (self.last_frame_count + 1) & 0xFFFF
            if frame_count != expected_frame:
                self.frame_stats['drops'] += 1
                if frame_count < self.last_frame_count:
                    self.frame_stats['out_of_order'] += 1
                print(f"Frame discontinuity: Expected {expected_frame}, Got {frame_count}")
        
        self.last_frame_count = frame_count
        
        # Extract the actual audio data (skip header)
        audio_data = data[3:]
        
        # Verify the data length matches our expectations
        expected_length = self.samples_per_frame * 2  # 16-bit samples = 2 bytes per sample
        if len(audio_data) != expected_length:
            self.frame_stats['invalid_size'] += 1
            print(f"Warning: Unexpected data length. Expected: {expected_length}, Got: {len(audio_data)}")
        
        self.audio_data.extend(audio_data)
        self.frames_received += 1
        
        # Print progress every second
        if self.last_progress_time and (current_time - self.last_progress_time) >= 1.0:
            kb_received = len(self.audio_data) / 1024
            duration = len(self.audio_data) / (SAMPLE_RATE * SAMPLE_WIDTH)
            frames_per_second = self.frames_received / (current_time - self.start_time)
            print(f"\nStatus Update:")
            print(f"Received: {kb_received:.2f} KB ({duration:.1f} seconds)")
            print(f"Current frame: {frame_count}")
            print(f"Average frames/second: {frames_per_second:.1f}")
            print(f"Frame drops: {self.frame_stats['drops']}")
            print(f"Out of order frames: {self.frame_stats['out_of_order']}")
            print(f"Invalid sized frames: {self.frame_stats['invalid_size']}")
            self.last_progress_time = current_time

    async def check_stream_status(self):
        """Periodically check if we've stopped receiving data"""
        while True:
            await asyncio.sleep(1)
            current_time = time.time()
            
            if (self.is_receiving and self.last_data_time and 
                (current_time - self.last_data_time) > 1.0):
                # We've stopped receiving data, save the file
                print("\nStream stopped, saving recording...")
                self.save_wav_file()
                self.reset_session()

    def save_wav_file(self):
        """Save the collected audio data as a WAV file"""
        if not self.audio_data:
            print("No audio data collected!")
            return
            
        # Generate filename with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"recording_{timestamp}.wav"
            
        with wave.open(filename, 'wb') as wav_file:
            wav_file.setnchannels(CHANNELS)
            wav_file.setsampwidth(SAMPLE_WIDTH)
            wav_file.setframerate(SAMPLE_RATE)
            wav_file.writeframes(self.audio_data)
            
        duration = time.time() - self.start_time if self.start_time else 0
        expected_frames = duration * SAMPLE_RATE / self.samples_per_frame
        
        print("\n=== Recording Summary ===")
        print(f"Audio saved to: {filename}")
        print(f"Total frames received: {self.frames_received}")
        print(f"Expected frames: {expected_frames:.0f}")
        print(f"Recording duration: {duration:.1f} seconds")
        print(f"File size: {len(self.audio_data)/1024:.1f} KB")
        print(f"Average frame rate: {self.frames_received/duration:.1f} frames/second")
        print("\nFrame Statistics:")
        print(f"- Dropped frames: {self.frame_stats['drops']}")
        print(f"- Out of order frames: {self.frame_stats['out_of_order']}")
        print(f"- Invalid sized frames: {self.frame_stats['invalid_size']}")
        
        # Calculate actual vs expected data rate
        expected_bytes = duration * SAMPLE_RATE * SAMPLE_WIDTH
        actual_bytes = len(self.audio_data)
        data_ratio = actual_bytes / expected_bytes if expected_bytes > 0 else 0
        print(f"\nData completeness: {data_ratio:.2%}")
        if abs(1 - data_ratio) > 0.1:  # More than 10% off
            print("Warning: Significant difference between expected and actual data rate")
            print("This might explain any speed issues in the recording")
        
        print("\nReady for next recording session...")

async def find_device():
    """Scan for esp32 device"""
    print("Scanning for esp32 device...")
    try:
        devices = await BleakScanner.discover()
        for device in devices:
            if device.name and DEVICE_NAME in device.name:
                print(f"Found {DEVICE_NAME} device: {device.address}")
                return device.address
            elif device.name:
                print(f"Found device: {device.name} ({device.address})")
    except Exception as e:
        print(f"Error during scanning: {e}")
    return None

async def main():
    # Find OpenGlass device
    address = await find_device()
    if not address:
        print(f"{DEVICE_NAME} device not found!")
        return

    # Create receiver instance
    receiver = AudioStreamReceiver()
    
    try:
        print(f"Attempting to connect to {DEVICE_NAME} at {address}...")
        async with BleakClient(address, timeout=20.0) as client:
            print(f"Connected to {DEVICE_NAME}")
            
            # Get services (for debugging)
            services = await client.get_services()
            print("\nAvailable services and characteristics:")
            for service in services:
                print(f"Service: {service.uuid}")
                for char in service.characteristics:
                    print(f"  Characteristic: {char.uuid}")
                    print(f"    Properties: {char.properties}")
            
            # Start notification handler
            print(f"\nStarting notifications for characteristic: {CHARACTERISTIC_UUID}")
            await client.start_notify(
                CHARACTERISTIC_UUID, 
                receiver.notification_handler
            )
            
            # Start the stream status checker
            status_checker = asyncio.create_task(receiver.check_stream_status())
            
            print("\nReady for recording... Use serial monitor to start/stop")
            try:
                while True:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                status_checker.cancel()
                
    except KeyboardInterrupt:
        print("\nConnection terminated by user")
    except Exception as e:
        print(f"\nError: {e}")
        print("If you're seeing a timeout error, try running the script again.")
    finally:
        # Save any remaining audio data
        if receiver.audio_data:
            receiver.save_wav_file()

if __name__ == "__main__":
    # For macOS, you might need to run with sudo
    asyncio.run(main())
