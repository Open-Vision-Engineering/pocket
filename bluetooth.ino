#define CAMERA_MODEL_XIAO_ESP32S3

// Include all necessary libraries
#include <driver/i2s.h>  // Main I2S driver
#include "FS.h"
#include "SD.h"
#include "SPI.h"
#include "mulaw.h"

// BLE includes
#include <BLE2902.h>
#include <BLEDevice.h>
#include <BLEUtils.h>
#include <BLEScan.h>
#include <BLEAdvertisedDevice.h>

// WiFi and time includes
#include <WiFi.h>
#include <WebServer.h>
#include <ESPmDNS.h>
#include <esp_wifi.h>
#include "time.h"
#include "ESP32Time.h"

// Hardware Pin Definitions
#define I2S_WS      D0    // I2S Word Select
#define I2S_SCK     D1    // I2S Serial Clock
#define I2S_SD      D2    // I2S Serial Data

#define SD_CS_PIN   D3    // SD Card CS
#define SD_MOSI_PIN D4    // SD Card MOSI
#define SD_MISO_PIN D5    // SD Card MISO
#define SD_SCK_PIN  D6    // SD Card SCK

const int buttonPin1 = D7;  // First pushbutton
const int buttonPin2 = D8;  // Second pushbutton
const int motorPin = D9;    // Motor control

// Audio Configuration
#define SAMPLE_RATE 48000          // Sample rate in Hz (for recording)
#define BLE_SAMPLE_RATE 16000      // Sample rate for BLE streaming
#define SAMPLE_BITS 16             // Bits per sample
#define WAV_HEADER_SIZE 44         // WAV header size in bytes
#define VOLUME_GAIN 2              // Audio volume multiplier

// Frame and Buffer Configuration
#define RECORDING_FRAME_SIZE (SAMPLE_RATE / 100)     // 480 samples for 48kHz
#define BLE_FRAME_SIZE 160                           // Fixed size for BLE streaming
#define BLE_MTU_SIZE 512                            // Maximum BLE packet size
#define DOWNSAMPLE_FACTOR 3                         // 48kHz to 16kHz conversion

// Buffer Sizes
#define TRANSFER_BUFFER_SIZE 65536  // 64KB buffer for file transfers
#define CHUNK_SIZE 32768           // 32KB chunks for file transfers
const int BufferSizeInBytes = 1024;  // Recording buffer size for crystal clear audio
static size_t recording_buffer_size = BufferSizeInBytes;
static size_t compressed_buffer_size = BLE_FRAME_SIZE * 2 + 3;  // BLE frame + header

// WiFi and Time Configuration
const char* ssid = "mango";
const char* password = "peterpeel";
const char* ntpServer = "pool.ntp.org";
const long gmtOffset_sec = -28800;      // PST offset
const int daylightOffset_sec = 0;      // No daylight savings

// WiFi Server Configuration
const char* ap_ssid = "ESP32_Audio";
const char* ap_password = "12345678";
#define MAX_CLIENTS 1               // Maximum simultaneous clients

// BLE Configuration
#define DEVICE_NAME "ESP32WAV"
static BLEUUID serviceUUID("4fafc201-1fb5-459e-8fcc-c5c9c331914b");
static BLEUUID audioCharacteristicUUID("beb5483e-36e1-4688-b7f5-ea07361b26a8");

// Timing Constants
const unsigned long FRAME_INTERVAL = 10;    // 10ms between frames
const unsigned long STATS_INTERVAL = 1000;  // 1 second between stats

// Global Objects
ESP32Time rtc;
WebServer server(80);
BLECharacteristic *audioCharacteristic;
File wavFile;

// Buffer Pointers
static uint8_t *s_recording_buffer = nullptr;
static uint8_t *s_compressed_frame = nullptr;

// State Variables
bool timeInitialized = false;
bool wifi_active = false;
bool connected = false;        // BLE connection state
bool isRecording = false;     // Recording state
bool isStreaming = false;     // Streaming state
bool stringComplete = false;  // Serial input state

// Button States
int buttonState = 0;
int lastButtonState = 1;
int buttonState2 = 0;

// Counters and Statistics
uint16_t audio_frame_count = 0;
unsigned long frames_sent = 0;
unsigned long last_frames_sent = 0;
unsigned long bytes_read = 0;
unsigned long bytes_sent = 0;
unsigned long frame_overruns = 0;
unsigned long frame_underruns = 0;
unsigned long last_frame_time = 0;
unsigned long last_stats_time = 0;

// String Storage
String inputString = "";       // Serial input buffer
String currentWavFile = "";    // Current recording filename

void setupWiFiDirect() {
    Serial.println("\nInitializing WiFi Direct...");
    
    // Complete WiFi disconnect and cleanup
    WiFi.disconnect(true);
    WiFi.mode(WIFI_OFF);
    delay(100);
    
    // Configure AP with optimized settings
    WiFi.mode(WIFI_AP);
    WiFi.setTxPower(WIFI_POWER_19_5dBm);
    
    // Configure for static IP - do this before starting AP
    IPAddress local_IP(192,168,4,1);
    IPAddress gateway(192,168,4,1);
    IPAddress subnet(255,255,255,0);
    
    Serial.println("Configuring soft-AP...");
    if (!WiFi.softAPConfig(local_IP, gateway, subnet)) {
        Serial.println("Soft-AP configuration failed");
        return;
    }
    
    // Start AP
    Serial.println("Starting soft-AP...");
    if(!WiFi.softAP(ap_ssid, ap_password, 1, 0, 1)) {
        Serial.println("Soft-AP start failed");
        return;
    }
    
    delay(500); // Give AP more time to start up
    
    Serial.println("Configuring HTTP server...");
    // Define server routes BEFORE begin()
    server.on("/", HTTP_GET, []() {
        Serial.println("Root page accessed");
        server.send(200, "text/html", "<h1>ESP32 Audio Server</h1>");
    });
    
    server.on("/test", HTTP_GET, []() {
        Serial.println("Test endpoint accessed");
        server.send(200, "text/plain", "Server is running");
    });
    
    server.on("/file", HTTP_GET, handleFileDownload);
    
    // Start server
    Serial.println("Starting HTTP server...");
    server.begin();
    
    // Verify server status
    Serial.printf("Server running: %s\n", server.client() ? "Yes" : "No");
    Serial.printf("Soft-AP IP: %s\n", WiFi.softAPIP().toString().c_str());
    
    wifi_active = true;
}

// Call this in your loop
void checkServerStatus() {
    static unsigned long lastCheck = 0;
    unsigned long currentTime = millis();
    
    if (currentTime - lastCheck >= 5000) {  // Check every 5 seconds
        lastCheck = currentTime;
        Serial.printf("Server Status:\n");
        Serial.printf("- WiFi Mode: %d\n", WiFi.getMode());
        Serial.printf("- AP IP: %s\n", WiFi.softAPIP().toString().c_str());
        Serial.printf("- Connected clients: %d\n", WiFi.softAPgetStationNum());
        Serial.printf("- wifi_active flag: %d\n", wifi_active);
        
        // Try to create a test client connection
        WiFiClient testClient;
        if (testClient.connect(WiFi.softAPIP(), 80)) {
            Serial.println("- Local server test: SUCCESS");
            testClient.stop();
        } else {
            Serial.println("- Local server test: FAILED");
        }
    }
}



// Handle root page
void handleRoot() {
    Serial.println("Client accessed root page");
    String html = "<html><body>";
    html += "<h1>ESP32 Audio File Server</h1>";
    if(currentWavFile.length() > 0) {
        html += "<p>Current file: " + currentWavFile + "</p>";
        html += "<a href='/file'>Download WAV File</a>";
    } else {
        html += "<p>No file available</p>";
    }
    html += "</body></html>";
    server.send(200, "text/html", html);
}

// Improved file download handler with better buffering and error handling
void handleFileDownload() {
    Serial.println("\nInitiating high-speed file transfer...");
    
    if(currentWavFile.length() == 0) {
        server.send(404, "text/plain", "No file available");
        return;
    }
    
    File file = SD.open(currentWavFile.c_str(), FILE_READ);
    if(!file) {
        server.send(404, "text/plain", "File not found");
        return;
    }
    
    size_t fileSize = file.size();
    
    WiFiClient client = server.client();
    client.setNoDelay(true);     // Disable Nagle's algorithm
    
    // TCP optimization
    int tcp_mss = 1460;
    
    // Minimal headers for reduced overhead
    String headers = "HTTP/1.1 200 OK\r\n"
                    "Content-Type: audio/wav\r\n"
                    "Content-Length: " + String(fileSize) + "\r\n"
                    "Connection: keep-alive\r\n\r\n";
    
    client.print(headers);
    
    // Allocate maximum buffer in PSRAM if available
    uint8_t *buffer = NULL;
    if(psramFound()) {
        buffer = (uint8_t*)ps_malloc(TRANSFER_BUFFER_SIZE);
    }
    if (!buffer) {
        buffer = (uint8_t*)malloc(CHUNK_SIZE);  // Fallback to smaller size
        if (!buffer) {
            file.close();
            return;
        }
    }
    
    size_t bytesSent = 0;
    unsigned long startTime = millis();
    unsigned long lastProgressTime = startTime;
    
    // Pre-calculate TCP segment size for optimal network packets
    size_t optimalChunkSize = (CHUNK_SIZE / tcp_mss) * tcp_mss;
    
    while(file.available() && client.connected()) {
        size_t bytesRead = file.read(buffer, TRANSFER_BUFFER_SIZE);
        if(bytesRead == 0) break;
        
        size_t bytesRemaining = bytesRead;
        uint8_t* bufPtr = buffer;
        
        while(bytesRemaining > 0 && client.connected()) {
            size_t chunkSize = (bytesRemaining < optimalChunkSize) ? bytesRemaining : optimalChunkSize;
            size_t bytesWritten = client.write(bufPtr, chunkSize);
            
            if(bytesWritten == 0) {
                Serial.println("Write failed");
                break;
            }
            
            bytesSent += bytesWritten;
            bytesRemaining -= bytesWritten;
            bufPtr += bytesWritten;
        }
        
        // Progress monitoring with speed calculation
        unsigned long currentTime = millis();
        if(currentTime - lastProgressTime >= 1000) {  // Update every second
            float elapsedSecs = (currentTime - startTime) / 1000.0;
            float speedMbps = (bytesSent * 8.0) / (elapsedSecs * 1000000.0);  // Convert to Mbps
            float progress = (bytesSent * 100.0) / fileSize;
            
            Serial.printf("Progress: %.1f%% Speed: %.2f Mbps\n", progress, speedMbps);
            lastProgressTime = currentTime;
            
            // Calculate estimated completion time
            float remainingBytes = fileSize - bytesSent;
            float estimatedSeconds = remainingBytes / (bytesSent / elapsedSecs);
            Serial.printf("Estimated completion in: %.1f seconds\n", estimatedSeconds);
        }
    }
    
    client.flush();
    free(buffer);
    file.close();
    
    float totalTime = (millis() - startTime) / 1000.0;
    float averageSpeedMbps = (bytesSent * 8.0) / (totalTime * 1000000.0);
    Serial.printf("\nTransfer complete:\n");
    Serial.printf("Total bytes: %u\n", bytesSent);
    Serial.printf("Time: %.1f seconds\n", totalTime);
    Serial.printf("Average speed: %.2f Mbps\n", averageSpeedMbps);
}

void generate_wav_header(uint8_t *wav_header, uint32_t wav_size, uint32_t sample_rate) {
    uint32_t file_size = wav_size + WAV_HEADER_SIZE - 8;
    uint32_t byte_rate = sample_rate * SAMPLE_BITS / 8;
    const uint8_t set_wav_header[] = {
        'R', 'I', 'F', 'F',
        file_size, file_size >> 8, file_size >> 16, file_size >> 24,
        'W', 'A', 'V', 'E',
        'f', 'm', 't', ' ',
        0x10, 0x00, 0x00, 0x00,
        0x01, 0x00,
        0x01, 0x00,
        sample_rate, sample_rate >> 8, sample_rate >> 16, sample_rate >> 24,
        byte_rate, byte_rate >> 8, byte_rate >> 16, byte_rate >> 24,
        0x02, 0x00,
        0x10, 0x00,
        'd', 'a', 't', 'a',
        wav_size, wav_size >> 8, wav_size >> 16, wav_size >> 24,
    };
    memcpy(wav_header, set_wav_header, sizeof(set_wav_header));
}

void setupTime() {
    Serial.println("Connecting to WiFi for time sync...");
    WiFi.begin(ssid, password);
    
    // Wait for connection with timeout
    int attempts = 0;
    while (WiFi.status() != WL_CONNECTED && attempts < 20) {  // 10 second timeout
        delay(500);
        Serial.print(".");
        attempts++;
    }
    
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("\nWiFi connection failed!");
        return;
    }
    
    Serial.println("\nWiFi connected");

    // Init and get the time
    configTime(gmtOffset_sec, daylightOffset_sec, ntpServer);
    
    // Verify time was set
    struct tm timeinfo;
    if(!getLocalTime(&timeinfo)){
        Serial.println("Failed to obtain time");
    } else {
        Serial.println("Time successfully obtained!");
        char timeStr[64];
        strftime(timeStr, sizeof(timeStr), "%Y-%m-%d %H:%M:%S", &timeinfo);
        Serial.printf("Current time: %s\n", timeStr);
        timeInitialized = true;
    }
    
    // Disconnect WiFi
    WiFi.disconnect(true);
    WiFi.mode(WIFI_OFF);
    Serial.println("WiFi disconnected");
}

void startRecording() {
    if (isStreaming || isRecording) return;
    
    // Reset all counters
    audio_frame_count = 0;
    last_frame_time = millis();
    last_stats_time = millis();
    frames_sent = 0;
    last_frames_sent = 0;
    bytes_read = 0;
    bytes_sent = 0;
    frame_overruns = 0;
    frame_underruns = 0;
    
    // Create filename using RTC time
    String timeStr = rtc.getTime("%Y-%m-%d_%H-%M-%S");
    String filename = "/" + timeStr + ".wav";
    
    Serial.printf("Creating file: %s\n", filename.c_str());
    
    wavFile = SD.open(filename.c_str(), FILE_WRITE);
    if (!wavFile) {
        Serial.println("Failed to open file for writing");
        return;
    }

    // Write WAV header with placeholder size
    uint8_t header[WAV_HEADER_SIZE];
    generate_wav_header(header, 0, SAMPLE_RATE);
    wavFile.write(header, WAV_HEADER_SIZE);

    isRecording = true;
    isStreaming = true;  // Start streaming when recording starts
    Serial.printf("Started recording to %s\n", filename.c_str());
}

void stopRecording() {
    isStreaming = false;  // Stop streaming first
    
    if (isRecording) {
        // Make sure all data is written
        wavFile.flush();
        
        // Get the current file size (subtract header size to get actual audio data size)
        uint32_t totalFileSize = wavFile.size();
        uint32_t audioDataSize = (totalFileSize > WAV_HEADER_SIZE) ? (totalFileSize - WAV_HEADER_SIZE) : 0;
        
        Serial.printf("Total file size: %u bytes\n", totalFileSize);
        Serial.printf("Audio data size: %u bytes\n", audioDataSize);
        
        // Update WAV header with final size
        wavFile.seek(0);
        uint8_t header[WAV_HEADER_SIZE];
        generate_wav_header(header, audioDataSize, SAMPLE_RATE);
        wavFile.write(header, WAV_HEADER_SIZE);
        
        // Store the current file path
        String filename = wavFile.name();
        currentWavFile = filename;
        if (!filename.startsWith("/")) {
            currentWavFile = "/" + filename;
        }
        
        // Debug output
        Serial.printf("Current WAV file path: %s\n", currentWavFile.c_str());
        
        // Verify file exists
        if (SD.exists(currentWavFile.c_str())) {
            Serial.println("File verified on SD card");
            // Additional size check
            File testFile = SD.open(currentWavFile.c_str());
            if (testFile) {
                Serial.printf("Verified file size: %u bytes\n", testFile.size());
                testFile.close();
            }
        } else if (SD.exists(currentWavFile.substring(1).c_str())) {
            // Try without leading slash
            currentWavFile = currentWavFile.substring(1);
            Serial.println("File verified on SD card (without leading slash)");
        } else {
            Serial.println("Warning: File not found on SD card");
        }
        
        // Make sure to flush and close the file
        wavFile.flush();
        wavFile.close();
        
        isRecording = false;
        Serial.printf("Recording stopped. File saved as: %s\n", currentWavFile.c_str());
        
        if(wifi_active) {
            Serial.println("File ready for WiFi transfer at: http://192.168.4.1/file");
            Serial.printf("AP Status: %s\n", WiFi.softAPIP().toString().c_str());
            Serial.printf("Current Connections: %d\n", WiFi.softAPgetStationNum());
        } else {
            Serial.println("Warning: WiFi is not active, attempting to restart...");
            setupWiFiDirect();
        }
    }
}

void printStats() {
    unsigned long current_time = millis();
    float elapsed_time = (current_time - last_stats_time) / 1000.0;
    unsigned long frames_this_period = frames_sent - last_frames_sent;
    float current_frame_rate = frames_this_period / elapsed_time;
    
    Serial.println("\n=== Audio Streaming Stats ===");
    Serial.printf("Time elapsed: %.1f seconds\n", elapsed_time);
    Serial.printf("Current frame rate: %.1f fps\n", current_frame_rate);
    Serial.printf("Total frames sent: %lu\n", frames_sent);
    Serial.printf("Bytes read: %lu\n", bytes_read);
    Serial.printf("Bytes sent: %lu\n", bytes_sent);
    Serial.printf("Frame overruns: %lu\n", frame_overruns);
    Serial.printf("Frame underruns: %lu\n", frame_underruns);
    Serial.printf("Buffer sizes - Recording: %u, Compressed: %u\n", recording_buffer_size, compressed_buffer_size);
    Serial.printf("Last frame interval: %lu ms\n", current_time - last_frame_time);
    Serial.println("============================\n");

    last_frames_sent = frames_sent;
    last_stats_time = current_time;
}
void processSerialCommand(String command) {
    command.trim();  // Remove any whitespace/newlines
    
    if (command.equalsIgnoreCase("start")) {
        if (!connected) {
            Serial.println("Error: Cannot start - No BLE client connected");
            return;
        }
        if (!isStreaming && !isRecording) {
            startRecording();  // This will start both streaming and recording
            Serial.println("Streaming and recording started");
            Serial.println("Type 'stop' to end");
        } else {
            Serial.println("Already streaming/recording");
        }
    }
    else if (command.equalsIgnoreCase("stop")) {
        if (isStreaming || isRecording) {
            stopRecording();  // This will stop both streaming and recording
            Serial.println("Streaming and recording stopped");
            Serial.println("Type 'start' to begin a new session");
        } else {
            Serial.println("Not currently streaming/recording");
        }
    }
    else if (command.equalsIgnoreCase("status")) {
        Serial.println("\n=== Status ===");
        Serial.println("BLE Connected: " + String(connected ? "Yes" : "No"));
        Serial.println("Streaming: " + String(isStreaming ? "Yes" : "No"));
        Serial.println("Recording: " + String(isRecording ? "Yes" : "No"));
        if (isStreaming) {
            printStats();
        }
    }
    else {
        Serial.println("Unknown command. Available commands:");
        Serial.println("  start  - Start streaming and recording");
        Serial.println("  stop   - Stop streaming and recording");
        Serial.println("  status - Show current status");
    }
}

class ServerCallbacks: public BLEServerCallbacks {
    void onConnect(BLEServer* server) {
        connected = true;
        Serial.println("\n=== Client Connected ===");
        Serial.println("Type 'start' to begin streaming/recording or 'stop' to end");
    }

    void onDisconnect(BLEServer* server) {
        connected = false;
        Serial.println("\n=== Client Disconnected ===");
        if (isRecording) {
            stopRecording();
        }
        isStreaming = false;  // Stop streaming on disconnect
        BLEDevice::startAdvertising();
    }
};

void configure_ble() {
    // Initialize BLE with the correct name
    BLEDevice::init(DEVICE_NAME);
    BLEServer *server = BLEDevice::createServer();
    server->setCallbacks(new ServerCallbacks());

    // Create main service
    BLEService *service = server->createService(serviceUUID);

    // Create audio characteristic
    audioCharacteristic = service->createCharacteristic(
        audioCharacteristicUUID,
        BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_NOTIFY
    );
    
    BLE2902 *ccc = new BLE2902();
    ccc->setNotifications(true);
    audioCharacteristic->addDescriptor(ccc);

    // Start the service
    service->start();

    // Modified advertising configuration
    BLEAdvertising *advertising = BLEDevice::getAdvertising();
    advertising->addServiceUUID(serviceUUID);
    advertising->setScanResponse(true);
    advertising->setMinPreferred(0x06);
    advertising->setMaxPreferred(0x12);
    
    // Add these lines to ensure the name is set correctly
    BLEAdvertisementData scanResponse;
    scanResponse.setName(DEVICE_NAME);
    advertising->setScanResponseData(scanResponse);
    
    BLEDevice::startAdvertising();
    Serial.println("BLE service started");
    Serial.printf("Device Name: %s\n", DEVICE_NAME);
    Serial.printf("Audio configuration: %dHz, %d-bit, Frame size: %d samples\n", 
                 BLE_SAMPLE_RATE, SAMPLE_BITS, BLE_FRAME_SIZE);  // Changed from FRAME_SIZE to BLE_FRAME_SIZE
}

void motorFeedback() {
    for (int i = 0; i < 2; i++) {
        analogWrite(motorPin, 200);
        delay(150);
        analogWrite(motorPin, 0);
        delay(100);
    }
}

void configure_microphone() {
    Serial.println("Configuring microphone...");
    
    i2s_config_t i2s_config = {
        .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
        .sample_rate = SAMPLE_RATE,
        .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
        .channel_format = I2S_CHANNEL_FMT_ONLY_RIGHT,
        .communication_format = I2S_COMM_FORMAT_STAND_I2S,
        .intr_alloc_flags = 0,
        .dma_buf_count = 8,         // From working code
        .dma_buf_len = 512,         // From working code
        .use_apll = true
    };

    i2s_driver_install(I2S_NUM_0, &i2s_config, 0, NULL);

    const i2s_pin_config_t pin_config = {
        .bck_io_num = I2S_SCK,
        .ws_io_num = I2S_WS,
        .data_out_num = I2S_PIN_NO_CHANGE,
        .data_in_num = I2S_SD
    };

    i2s_set_pin(I2S_NUM_0, &pin_config);

    // Allocate using the correct buffer size
    s_recording_buffer = (uint8_t *) ps_calloc(BufferSizeInBytes, sizeof(uint8_t));
    s_compressed_frame = (uint8_t *) ps_calloc(compressed_buffer_size, sizeof(uint8_t));
    
    if (!s_recording_buffer || !s_compressed_frame) {
        Serial.println("Failed to allocate audio buffers!");
        while (1);
    }
    
    Serial.printf("Microphone configured - Buffer sizes: Recording=%u bytes, Compressed=%u bytes\n", 
                 BufferSizeInBytes, compressed_buffer_size);
}

// Updated read_microphone function using proper I2S driver calls
size_t read_microphone() {
    size_t bytes_recorded = 0;
    i2s_read(I2S_NUM_0, (void*)s_recording_buffer, BufferSizeInBytes, &bytes_recorded, portMAX_DELAY);
    bytes_read += bytes_recorded;
    return bytes_recorded;
}

void setup() {
    Serial.begin(921600);
    Serial.println("\n=== Starting BLE Audio Streaming ===");
    inputString.reserve(200);
    
    // Initialize pins
    pinMode(buttonPin1, INPUT_PULLUP);
    pinMode(buttonPin2, INPUT_PULLUP);
    pinMode(motorPin, OUTPUT);
    
    // Initialize SPI for SD card
    SPI.begin(SD_SCK_PIN, SD_MISO_PIN, SD_MOSI_PIN, SD_CS_PIN);
    
    // Set RTC time to compilation time
    rtc.setTime(0, 0, 14, 22, 11, 2024);
    Serial.printf("RTC set to: %s\n", rtc.getTime("%Y-%m-%d_%H-%M-%S").c_str());
    
    if (!SD.begin(SD_CS_PIN)) {
        Serial.println("Failed to mount SD Card!");
        while (1);
    }
    Serial.println("SD Card mounted successfully");
    
    setupWiFiDirect();
    configure_ble();
    configure_microphone();
    
    Serial.println("\nSetup complete - Ready for commands:");
    Serial.println("  start  - Start recording");
    Serial.println("  stop   - Stop recording");
    Serial.println("  status - Show current status");
    
    delay(100);
}

void loop() {
    static uint8_t lastStationNum = 0;
    static unsigned long lastServerCheck = 0;
    unsigned long current_time = millis();
    
    // Read button states at the start of each loop
    buttonState = digitalRead(buttonPin1);
    buttonState2 = digitalRead(buttonPin2);

    // Handle button press for recording control (non-blocking)
    if (buttonState == LOW && lastButtonState == HIGH) {
        motorFeedback();
        if (!isStreaming && !isRecording) {
            startRecording();
        } else {
            stopRecording();
        }
    }
    lastButtonState = buttonState;
    
    // Check if we're actively transferring a file
    bool isTransferringFile = server.client() && server.client().connected();
    
    if (isTransferringFile) {
        // During file transfer, only handle client requests and yield
        server.handleClient();
        delay(1);  // Minimal delay to prevent WiFi stack issues
        return;    // Skip all other processing during file transfer
    }
    
    // Process serial commands with non-blocking approach
    while (Serial.available()) {
        char inChar = (char)Serial.read();
        if (inChar == '\n') {
            processSerialCommand(inputString);
            inputString = "";
        } else {
            inputString += inChar;
        }
    }

    // Monitor WiFi connections (only check every 100ms)
    static unsigned long lastWifiCheck = 0;
    if (current_time - lastWifiCheck >= 100) {
        uint8_t currentStationNum = WiFi.softAPgetStationNum();
        if (currentStationNum != lastStationNum) {
            Serial.printf("WiFi client connection changed. Connected clients: %d\n", currentStationNum);
            if (currentStationNum > lastStationNum) {
                Serial.printf("New client connected. IP: %s\n", WiFi.softAPIP().toString().c_str());
            }
            lastStationNum = currentStationNum;
        }
        lastWifiCheck = current_time;
    }

    // Handle client requests
    server.handleClient();
    
    // Monitor server status every 5 seconds
    if (current_time - lastServerCheck >= 5000) {
        Serial.printf("Server status - WiFi active: %d, Connected clients: %d\n", 
                     wifi_active, WiFi.softAPgetStationNum());
        lastServerCheck = current_time;
    }

    // If not connected or not streaming, wait with reduced delay
    if (!connected || !isStreaming) {
        delay(50);  // Reduced from 100ms to improve responsiveness
        return;
    }

    // Audio streaming section
    
    // Check if it's time to print stats
    if (current_time - last_stats_time >= STATS_INTERVAL) {
        printStats();
    }

    // Check frame timing with improved accuracy
    long frame_delay = current_time - last_frame_time;
    if (frame_delay < FRAME_INTERVAL) {
        frame_underruns++;
        delayMicroseconds((FRAME_INTERVAL - frame_delay) * 1000);  // More precise delay
        return;
    }

    if (frame_delay > FRAME_INTERVAL + 1) {
        frame_overruns++;
    }

    // Read from mic
    size_t bytes_recorded = read_microphone();
    if (bytes_recorded != BufferSizeInBytes) {  // Changed to match the new buffer size
        Serial.printf("Unexpected bytes recorded: %d\n", bytes_recorded);
        return;
    }

    // Write raw audio to SD card if recording is active
    if (isRecording) {
        // Write to SD card
        if (wavFile.write((const byte*)s_recording_buffer, bytes_recorded) != bytes_recorded) {
            Serial.println("Failed to write to SD card");
        }

        // Process for BLE streaming - downsample to 16kHz for BLE
        int16_t* samples = (int16_t*)s_recording_buffer;
        uint8_t* output = s_compressed_frame + 3;
        
        // Take every third sample to downsample from 48kHz to 16kHz
        for (size_t i = 0; i < BLE_FRAME_SIZE; i++) {
            int16_t sample = samples[i * 3] << VOLUME_GAIN;  // Take every 3rd sample
            *output++ = sample & 0xFF;         // Low byte
            *output++ = (sample >> 8) & 0xFF;  // High byte
        }

        // Add frame header
        s_compressed_frame[0] = audio_frame_count & 0xFF;
        s_compressed_frame[1] = (audio_frame_count >> 8) & 0xFF;
        s_compressed_frame[2] = 0;

        // Send the audio data
        audioCharacteristic->setValue(s_compressed_frame, compressed_buffer_size);
        audioCharacteristic->notify();
        
        audio_frame_count++;
        frames_sent++;
        bytes_sent += compressed_buffer_size;
        last_frame_time = current_time;
    }

    if (wifi_active) {
        server.handleClient();
        checkServerStatus();
    }

    // Small delay to prevent watchdog issues
    delay(1);
}
