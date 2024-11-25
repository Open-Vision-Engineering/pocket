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
        """Download the WAV file from ESP32 over WiFi Direct with robust error handling and retries"""
        print("\nInitiating WiFi Direct transfer...")
        
        MAX_RETRIES = 3
        RETRY_DELAY = 5  # seconds
        WIFI_CONNECT_TIMEOUT = 30  # seconds
        DOWNLOAD_TIMEOUT = 60  # seconds
        
        for attempt in range(MAX_RETRIES):
            try:
                print(f"\nAttempt {attempt + 1}/{MAX_RETRIES}")
                
                # 1. Connect to ESP32's WiFi
                wifi_connect_start = time.time()
                if not await self.connect_to_esp32_wifi():
                    print(f"Failed to establish WiFi connection on attempt {attempt + 1}")
                    if attempt < MAX_RETRIES - 1:
                        print(f"Waiting {RETRY_DELAY} seconds before retry...")
                        await asyncio.sleep(RETRY_DELAY)
                    continue

                # 2. Verify WiFi Connection
                wifi_connect_time = time.time() - wifi_connect_start
                print(f"WiFi connection established in {wifi_connect_time:.1f} seconds")
                
                # 3. Initial connection stabilization delay
                print("Waiting for connection to stabilize...")
                await asyncio.sleep(2)
                
                try:
                    async with aiohttp.ClientSession() as session:
                        # 4. First check server availability
                        print("\nChecking server availability...")
                        try:
                            async with session.get('http://192.168.4.1', 
                                                timeout=aiohttp.ClientTimeout(total=10)) as response:
                                root_content = await response.text()
                                print(f"Server is available. Root page content: {root_content[:200]}...")
                        except Exception as e:
                            print(f"Error accessing server root page: {e}")
                            raise
                        
                        # 5. Attempt file download
                        print("\nInitiating file download...")
                        url = "http://192.168.4.1/file"
                        
                        async with session.get(url, 
                                            timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)) as response:
                            print(f"Server response status: {response.status}")
                            print(f"Response headers: {response.headers}")
                            
                            if response.status == 200:
                                # Get content length and type
                                total_size = int(response.headers.get('Content-Length', 0))
                                content_type = response.headers.get('Content-Type', '')
                                
                                if total_size == 0:
                                    print("Warning: Content-Length is 0, server might not be sending file correctly")
                                    if attempt < MAX_RETRIES - 1:
                                        continue
                                    else:
                                        raise Exception("Server returned zero Content-Length")
                                
                                # Generate filename with timestamp
                                filename = f"sdcard_recording_{self.current_file_timestamp}.wav"
                                print(f"\nStarting download of {total_size/1024:.1f} KB to {filename}")
                                print(f"Content-Type: {content_type}")
                                
                                received_size = 0
                                last_progress_update = time.time()
                                download_start_time = time.time()
                                
                                with open(filename, 'wb') as f:
                                    async for chunk in response.content.iter_chunked(8192):
                                        if chunk:
                                            f.write(chunk)
                                            received_size += len(chunk)
                                            
                                            # Update progress every second
                                            current_time = time.time()
                                            if current_time - last_progress_update >= 1.0:
                                                elapsed_time = current_time - download_start_time
                                                speed = received_size / (1024 * elapsed_time)  # KB/s
                                                progress = (received_size / total_size) * 100
                                                print(f"Progress: {progress:.1f}% ({received_size}/{total_size} bytes) "
                                                    f"Speed: {speed:.1f} KB/s", end='\r')
                                                last_progress_update = current_time
                                
                                # Verify downloaded file
                                final_size = os.path.getsize(filename)
                                download_time = time.time() - download_start_time
                                
                                print(f"\n\nDownload completed in {download_time:.1f} seconds")
                                print(f"Final file size: {final_size/1024:.1f} KB")
                                
                                if final_size != total_size:
                                    print(f"Warning: Size mismatch. Expected {total_size}, got {final_size}")
                                    if attempt < MAX_RETRIES - 1:
                                        print("Retrying download...")
                                        if os.path.exists(filename):
                                            os.remove(filename)
                                        continue
                                
                                print(f"File downloaded successfully: {filename}")
                                return True
                                
                            else:
                                error_text = await response.text()
                                print(f"Failed to download file. Status: {response.status}")
                                print(f"Error message: {error_text}")
                                
                                if attempt < MAX_RETRIES - 1:
                                    print(f"Retrying in {RETRY_DELAY} seconds...")
                                    await asyncio.sleep(RETRY_DELAY)
                                continue
                                
                except aiohttp.ClientError as e:
                    print(f"HTTP client error: {e}")
                    if attempt < MAX_RETRIES - 1:
                        print(f"Retrying in {RETRY_DELAY} seconds...")
                        await asyncio.sleep(RETRY_DELAY)
                    continue
                    
                except Exception as e:
                    print(f"Unexpected error during download: {e}")
                    print(f"Error type: {type(e)}")
                    if attempt < MAX_RETRIES - 1:
                        print(f"Retrying in {RETRY_DELAY} seconds...")
                        await asyncio.sleep(RETRY_DELAY)
                    continue
                    
            except Exception as e:
                print(f"Critical error in attempt {attempt + 1}: {e}")
                if attempt < MAX_RETRIES - 1:
                    print(f"Retrying entire process in {RETRY_DELAY} seconds...")
                    await asyncio.sleep(RETRY_DELAY)
                else:
                    print("All retry attempts exhausted")
            
            finally:
                # For each attempt, try to restore WiFi
                if attempt == MAX_RETRIES - 1:  # Only on last attempt
                    print("\nAttempting to restore original WiFi connection...")
                    try:
                        await self.restore_wifi()
                        print("Original WiFi connection restored")
                    except Exception as e:
                        print(f"Error restoring WiFi: {e}")
        
        return False  # If we get here, all attempts failed
        
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
