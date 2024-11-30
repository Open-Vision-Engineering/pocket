import asyncio
from bleak import BleakClient, BleakScanner
import struct
import wave
import time
import os
from datetime import datetime
import requests
import wifi
import subprocess
import platform
from urllib.parse import urljoin
import time
import aiohttp

#for wifi direct
WIFI_SSID = "ESP32_Audio"
WIFI_PASSWORD = "12345678"
ESP32_IP = "192.168.4.1"  # Default AP IP for ESP32

# BLE UUIDs (must match ESP32)
SERVICE_UUID = "4fafc201-1fb5-459e-8fcc-c5c9c331914b"
CHARACTERISTIC_UUID = "beb5483e-36e1-4688-b7f5-ea07361b26a8"

# Device name as advertised
DEVICE_NAME = "ESP32WAV"

# Audio settings
SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2  # 16-bit audio
CHANNELS = 1

class WiFiConnector:
    def __init__(self):
        self.original_wifi = None
    
    def connect_to_esp32(self):
        """Connect to ESP32's WiFi Direct network"""
        system = platform.system()
        
        if system == "Darwin":  # macOS
            # Store current WiFi info
            try:
                result = subprocess.run(['networksetup', '-getairportnetwork', 'en0'], 
                                     capture_output=True, text=True)
                self.original_wifi = result.stdout.strip().split(': ')[-1]
            except:
                self.original_wifi = None
            
            # Connect to ESP32
            try:
                subprocess.run(['networksetup', '-setairportnetwork', 'en0', 
                              WIFI_SSID, WIFI_PASSWORD], check=True)
                time.sleep(2)  # Wait for connection
                return True
            except subprocess.CalledProcessError:
                print("Failed to connect to ESP32 WiFi")
                return False
                
        elif system == "Linux":
            try:
                subprocess.run(['nmcli', 'device', 'wifi', 'connect', WIFI_SSID, 
                              'password', WIFI_PASSWORD], check=True)
                time.sleep(2)
                return True
            except subprocess.CalledProcessError:
                print("Failed to connect to ESP32 WiFi")
                return False
                
        else:
            print(f"Automatic WiFi connection not supported on {system}")
            print(f"Please connect manually to {WIFI_SSID} with password {WIFI_PASSWORD}")
            input("Press Enter when connected...")
            return True

    def restore_original_wifi(self):
        """Restore original WiFi connection"""
        system = platform.system()
        
        if system == "Darwin" and self.original_wifi:  # macOS
            try:
                subprocess.run(['networksetup', '-setairportnetwork', 'en0', 
                              self.original_wifi], check=True)
            except:
                print("Failed to restore original WiFi connection")
                
        elif system == "Linux":
            try:
                subprocess.run(['nmcli', 'device', 'wifi', 'connect', 
                              self.original_wifi], check=True)
            except:
                print("Failed to restore original WiFi connection")

class AudioStreamReceiver:
    def __init__(self):
        self.reset_session()
        self.wifi_ssid = "ESP32_Audio"
        self.wifi_password = "12345678"
        self.original_wifi = None
        
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
        self.current_file_timestamp = None
    
    async def test_server_connection(self):
        """Test connection to ESP32 server with detailed debugging"""
        print("\nTesting connection to ESP32 server...")
        
        try:
            # Try ping first
            print("Testing ping to ESP32...")
            ping_result = subprocess.run(['ping', '-c', '1', '-t', '1', '192.168.4.1'], 
                                      capture_output=True, text=True)
            print(f"Ping result: {ping_result.stdout}")
            
            # Try netcat to test port 80
            print("Testing HTTP port with netcat...")
            try:
                nc_result = subprocess.run(['nc', '-zv', '-G', '1', '192.168.4.1', '80'],
                                         capture_output=True, text=True)
                print(f"Netcat port test result: {nc_result.stderr}")
            except:
                print("Netcat test failed")
            
            # Try HTTP connection
            print("Testing HTTP connection...")
            async with aiohttp.ClientSession() as session:
                async with session.get('http://192.168.4.1', 
                                     timeout=aiohttp.ClientTimeout(total=5)) as response:
                    print(f"HTTP Response status: {response.status}")
                    text = await response.text()
                    print(f"Response content: {text[:200]}...")  # First 200 chars
                    return response.status == 200
                    
        except aiohttp.ClientError as e:
            print(f"HTTP connection error: {e}")
        except Exception as e:
            print(f"Connection test error: {e}")
            print(f"Error type: {type(e)}")
        
        return False

    async def connect_to_esp32_wifi(self):
        """Connect to ESP32's WiFi network"""
        print("\nAttempting to connect to ESP32 WiFi...")
        max_retries = 3
        
        for attempt in range(max_retries):
            try:
                # Store current WiFi
                print("Getting current WiFi network...")
                result = subprocess.run(['networksetup', '-getairportnetwork', 'en0'], 
                                     capture_output=True, text=True)
                self.original_wifi = result.stdout.strip().split(': ')[-1]
                print(f"Current WiFi network: {self.original_wifi}")
                
                # Turn WiFi off and on to refresh networks
                print("Cycling WiFi to refresh networks...")
                subprocess.run(['networksetup', '-setairportpower', 'en0', 'off'])
                await asyncio.sleep(1)
                subprocess.run(['networksetup', '-setairportpower', 'en0', 'on'])
                await asyncio.sleep(2)
                
                # List available networks
                print("Scanning for available networks...")
                result = subprocess.run(['airport', '-s'], 
                                     capture_output=True, text=True)
                print("Available networks:")
                print(result.stdout)
                
                # Connect to ESP32
                print(f"Attempting to connect to {self.wifi_ssid} (Attempt {attempt + 1}/{max_retries})")
                subprocess.run(['networksetup', '-setairportnetwork', 'en0', 
                              self.wifi_ssid, self.wifi_password], check=True)
                
                # Wait longer for connection to establish and stabilize
                print("Waiting for connection to establish and stabilize...")
                await asyncio.sleep(10)  # Increased from 5 to 10 seconds
                
                # Verify connection
                print("Verifying connection...")
                result = subprocess.run(['networksetup', '-getairportnetwork', 'en0'], 
                                     capture_output=True, text=True)
                current_network = result.stdout.strip().split(': ')[-1]
                print(f"Currently connected to: {current_network}")
                
                if current_network == self.wifi_ssid:
                    print(f"Successfully connected to {self.wifi_ssid}")
                    
                    # Extended connection testing
                    if await self.test_server_connection():
                        print("Server connection tests successful!")
                        return True
                    else:
                        print("Connected to WiFi but server tests failed")
                        print("Waiting additional 5 seconds and retrying server test...")
                        await asyncio.sleep(5)
                        if await self.test_server_connection():
                            print("Server connection successful on second attempt!")
                            return True
                else:
                    print(f"Failed to connect. Current network is {current_network}")
                    
            except subprocess.CalledProcessError as e:
                print(f"Connection attempt {attempt + 1} failed: {e}")
                print(f"Error output: {e.stderr}")
                await asyncio.sleep(2)
                
        print(f"Failed to connect to {self.wifi_ssid} after {max_retries} attempts")
        return False
    
    async def restore_wifi(self):
        """Restore original WiFi connection"""
        if not self.original_wifi:
            return

        system = platform.system()
        if system == "Darwin" and self.original_wifi:  # macOS
            try:
                subprocess.run(['networksetup', '-setairportnetwork', 'en0', 
                              self.original_wifi], check=True)
                await asyncio.sleep(2)
                print(f"Restored original WiFi connection to: {self.original_wifi}")
            except subprocess.CalledProcessError as e:
                print(f"Failed to restore original WiFi: {e}")
        
    async def download_wav_file(self):
        """Download the WAV file from ESP32 over WiFi with maximum speed optimizations"""
        print("\nInitiating high-speed WiFi Direct transfer...")
        
        CHUNK_SIZE = 32768  # 32KB to match server's chunk size
        MAX_RETRIES = 3
        STABILITY_WAIT = 2  # Seconds to wait for connection stability
        
        async def verify_connection():
            """Verify connection stability"""
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get('http://192.168.4.1', 
                                        timeout=aiohttp.ClientTimeout(total=5)) as response:
                        return response.status == 200
            except:
                return False
        
        for attempt in range(MAX_RETRIES):
            try:
                print(f"\nAttempt {attempt + 1}/{MAX_RETRIES}")
                
                if not await self.connect_to_esp32_wifi():
                    print(f"Failed to establish WiFi connection on attempt {attempt + 1}")
                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(2)
                    continue
                
                # Wait for connection stability
                print("Waiting for connection stability...")
                await asyncio.sleep(STABILITY_WAIT)
                if not await verify_connection():
                    print("Connection not stable, retrying...")
                    continue
                
                # Configure optimized aiohttp session
                timeout = aiohttp.ClientTimeout(
                    total=300,      # 5 minutes total timeout
                    connect=10,     # 10 seconds connect timeout
                    sock_read=10,   # 10 seconds read timeout
                    sock_connect=10 # 10 seconds socket connect timeout
                )
                
                # TCP optimizations
                connector = aiohttp.TCPConnector(
                    force_close=True,
                    enable_cleanup_closed=True,
                    limit=1,  # Single connection for maximum throughput
                    ttl_dns_cache=300,  # Cache DNS for 5 minutes
                    use_dns_cache=True
                )
                
                async with aiohttp.ClientSession(timeout=timeout, 
                                            connector=connector,
                                            raise_for_status=True) as session:
                    
                    # Test connection before starting download
                    async with session.get('http://192.168.4.1') as response:
                        if response.status != 200:
                            raise aiohttp.ClientError(f"Server returned {response.status}")
                    
                    print("Starting file download...")
                    async with session.get('http://192.168.4.1/file',
                                        timeout=timeout) as response:
                        
                        total_size = int(response.headers.get('Content-Length', 0))
                        if total_size == 0:
                            raise ValueError("Content-Length header missing")
                        
                        filename = f"sdcard_recording_{self.current_file_timestamp}.wav"
                        print(f"\nDownloading {total_size/1024/1024:.1f} MB to {filename}")
                        
                        with open(filename, 'wb') as f:
                            received_size = 0
                            start_time = time.time()
                            last_progress_time = start_time
                            last_speed_update = start_time
                            speed_samples = []  # For calculating average speed
                            
                            async for chunk in response.content.iter_chunked(CHUNK_SIZE):
                                if chunk:  # Filter out empty chunks
                                    f.write(chunk)
                                    received_size += len(chunk)
                                    
                                    current_time = time.time()
                                    elapsed = current_time - start_time
                                    
                                    # Update progress and speed every 0.5 seconds
                                    if current_time - last_speed_update >= 0.5:
                                        speed = received_size / elapsed / 1024 / 1024  # MB/s
                                        speed_samples.append(speed)
                                        if len(speed_samples) > 5:  # Keep last 5 samples
                                            speed_samples.pop(0)
                                        avg_speed = sum(speed_samples) / len(speed_samples)
                                        
                                        progress = (received_size / total_size) * 100
                                        mbps = avg_speed * 8  # Convert MB/s to Mbps
                                        
                                        # Calculate ETA
                                        remaining_bytes = total_size - received_size
                                        eta_seconds = remaining_bytes / (received_size / elapsed)
                                        
                                        print(f"Progress: {progress:.1f}% "
                                            f"Speed: {mbps:.1f} Mbps "
                                            f"ETA: {eta_seconds:.1f}s", end='\r')
                                        
                                        last_speed_update = current_time
                            
                            # Final statistics
                            total_time = time.time() - start_time
                            avg_speed_mbps = (received_size / total_time / 1024 / 1024) * 8
                            
                            print(f"\n\nTransfer Complete:")
                            print(f"Total size: {received_size/1024/1024:.2f} MB")
                            print(f"Time: {total_time:.2f} seconds")
                            print(f"Average speed: {avg_speed_mbps:.2f} Mbps")
                            
                            if received_size == total_size:
                                print(f"\nFile saved successfully: {filename}")
                                return True
                            else:
                                print(f"\nSize mismatch: {received_size}/{total_size}")
                                if os.path.exists(filename):
                                    os.remove(filename)
                                
            except aiohttp.ClientError as e:
                print(f"Network error during attempt {attempt + 1}: {e}")
            except Exception as e:
                print(f"Error during attempt {attempt + 1}: {e}")
            finally:
                if attempt == MAX_RETRIES - 1:
                    await self.restore_wifi()
        
        return False
        
    def notification_handler(self, sender, data):
        """Handle incoming BLE notifications"""
        current_time = time.time()
        
        # If we're getting data after a pause, this is a new session
        if self.last_data_time and (current_time - self.last_data_time) > 1.0:
            if self.is_receiving:
                # Previous session ended, save it
                self.save_wav_file()
                # Create task to download the SD card file
                asyncio.create_task(self.download_wav_file())
                self.reset_session()
        
        self.last_data_time = current_time
        self.is_receiving = True
        
        if not self.start_time:
            self.start_time = current_time
            self.last_progress_time = current_time
            self.current_file_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            
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
                print("\nStream stopped, saving recording...")
                self.save_wav_file()
                # Create task to download the SD card file
                asyncio.create_task(self.download_wav_file())
                self.reset_session()

    def save_wav_file(self):
        """Save the collected audio data as a WAV file"""
        if not self.audio_data:
            print("No audio data collected!")
            return
            
        # Use the timestamp from when we started recording
        filename = f"ble_recording_{self.current_file_timestamp}.wav"
            
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
        
        print("\nAttempting to download SD card file...")

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
