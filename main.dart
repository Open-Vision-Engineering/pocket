// lib/main.dart
import 'package:flutter/material.dart';
import 'package:flutter_blue_plus/flutter_blue_plus.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:network_info_plus/network_info_plus.dart';
import 'package:path_provider/path_provider.dart';
import 'package:http/http.dart' as http;
import 'package:just_audio/just_audio.dart';
import 'package:share_plus/share_plus.dart';
import 'dart:async';
import 'dart:io';
import 'dart:typed_data';
import 'package:wifi_iot/wifi_iot.dart';
import 'dart:math' show min, max;

// Constants matching ESP32 configuration
const String DEVICE_NAME = "ESP32WAV";
const String SERVICE_UUID = "4fafc201-1fb5-459e-8fcc-c5c9c331914b";
const String CHARACTERISTIC_UUID = "beb5483e-36e1-4688-b7f5-ea07361b26a8";
const String ESP_WIFI_SSID = "ESP32_Audio";
const String ESP_WIFI_PASSWORD = "12345678";
const String ESP_IP = "192.168.4.1";

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const MyApp());
}

class MyApp extends StatelessWidget {
  const MyApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'BLE Audio Receiver',
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(seedColor: Colors.blue),
        useMaterial3: true,
      ),
      home: const HomePage(),
    );
  }
}

class HomePage extends StatefulWidget {
  const HomePage({super.key});

  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> {
  bool isScanning = false;
  bool isConnected = false;
  bool isRecording = false;
  String statusMessage = "Ready to scan";
  List<int> audioBuffer = [];
  String? currentWifiSSID;
  DateTime? recordingStartTime;
  int framesReceived = 0;
  Timer? statusTimer;
  BluetoothDevice? device;
  BluetoothCharacteristic? audioCharacteristic;

  // Audio player
  final AudioPlayer _audioPlayer = AudioPlayer();
  bool isPlaying = false;
  Duration? audioDuration;
  Duration currentPosition = Duration.zero;
  String? currentAudioFile;
  Timer? positionTimer;

  // Recording statistics
  int frameDrops = 0;
  int outOfOrderFrames = 0;
  int invalidSizeFrames = 0;
  int lastFrameCount = -1;
  bool dataReceived = false;

  @override
  void initState() {
    super.initState();
    _initBluetooth();

    // Setup position updates for audio player
    positionTimer = Timer.periodic(Duration(milliseconds: 200), (timer) async {
      if (isPlaying) {
        final position = await _audioPlayer.position;
        setState(() => currentPosition = position);
      }
    });
  }

  @override
  void dispose() {
    statusTimer?.cancel();
    positionTimer?.cancel();
    _audioPlayer.dispose();
    device?.disconnect();
    super.dispose();
  }

  Future<void> _initBluetooth() async {
    // Request necessary permissions
    await Permission.bluetooth.request();
    await Permission.bluetoothScan.request();
    await Permission.bluetoothConnect.request();
    await Permission.location.request();
    await Permission.storage.request();
    await Permission.audio.request();

    if (Platform.isAndroid) {
      await Permission.bluetoothAdvertise.request();
    }

    // Initialize FlutterBlue
    FlutterBluePlus.adapterState.listen((state) {
      if (state == BluetoothAdapterState.on) {
        setState(() => statusMessage = "Bluetooth ready");
      } else {
        setState(() => statusMessage = "Bluetooth not available");
      }
    });
  }

  Future<void> startScan() async {
    setState(() {
      isScanning = true;
      statusMessage = "Scanning for $DEVICE_NAME...";
    });

    // Start scanning
    await FlutterBluePlus.startScan(timeout: Duration(seconds: 10));

    // Listen to scan results
    FlutterBluePlus.scanResults.listen((results) async {
      for (ScanResult result in results) {
        if (result.device.name == DEVICE_NAME) {
          await FlutterBluePlus.stopScan();
          await connectToDevice(result.device);
          break;
        }
      }
    });

    // After timeout
    Future.delayed(Duration(seconds: 10), () {
      if (mounted && isScanning) {
        setState(() {
          isScanning = false;
          statusMessage = "Device not found";
        });
      }
    });
  }

  Future<void> connectToDevice(BluetoothDevice bluetoothDevice) async {
    setState(() => statusMessage = "Connecting...");

    try {
      await bluetoothDevice.connect();
      device = bluetoothDevice;

      // Discover services
      List<BluetoothService> services = await device!.discoverServices();
      for (var service in services) {
        if (service.uuid.toString() == SERVICE_UUID) {
          for (var characteristic in service.characteristics) {
            if (characteristic.uuid.toString() == CHARACTERISTIC_UUID) {
              audioCharacteristic = characteristic;
              await setupNotifications();
              break;
            }
          }
        }
      }

      setState(() {
        isConnected = true;
        isScanning = false;
        statusMessage =
            "Connected to $DEVICE_NAME\nWaiting for recording to start...";
      });
    } catch (e) {
      setState(() {
        statusMessage = "Connection failed: $e";
        isScanning = false;
      });
    }
  }

  Future<void> setupNotifications() async {
    Timer? noDataTimer;
    bool isFirstPacket = true;
    DateTime? lastPacketTime;

    await audioCharacteristic?.setNotifyValue(true);
    audioCharacteristic?.value.listen(
      (value) async {
        // Update last packet time
        lastPacketTime = DateTime.now();

        // Cancel existing timer if any
        noDataTimer?.cancel();

        // Start new timer to detect end of stream
        noDataTimer = Timer(Duration(milliseconds: 500), () async {
          if (isRecording) {
            print("No data received for 500ms, stopping recording");
            await stopRecording();
          }
        });

        // Handle first data packet
        if (isFirstPacket && value.length >= 3) {
          isFirstPacket = false;
          setState(() {
            isRecording = true;
            dataReceived = true;
            recordingStartTime = lastPacketTime;
            statusMessage = "Recording started from ESP32...";
            audioBuffer.clear();
            framesReceived = 0;
            frameDrops = 0;
            outOfOrderFrames = 0;
            invalidSizeFrames = 0;
            lastFrameCount = -1;
          });
          startStatusUpdates();
        }

        // Process audio data if valid
        if (value.length >= 3) {
          processAudioData(value);
        }
      },
      onError: (error) {
        print("BLE notification error: $error");
        if (isRecording) {
          stopRecording();
        }
      },
      cancelOnError: false,
    );
  }

  void startStatusUpdates() {
    statusTimer?.cancel();
    statusTimer = Timer.periodic(Duration(seconds: 1), (timer) {
      if (mounted && isRecording) {
        setState(() {
          Duration duration = DateTime.now().difference(recordingStartTime!);
          double kbReceived = audioBuffer.length / 1024;
          double fps = framesReceived / duration.inSeconds;
          statusMessage = "Recording: ${duration.inSeconds}s\n"
              "Received: ${kbReceived.toStringAsFixed(1)} KB\n"
              "FPS: ${fps.toStringAsFixed(1)}\n"
              "Drops: $frameDrops\n"
              "Out of order: $outOfOrderFrames";
        });
      }
    });
  }

  void processAudioData(List<int> data) {
    if (data.length < 3) {
      print("Received invalid data length: ${data.length}");
      return;
    }

    // Parse frame count from header (first 2 bytes)
    int frameCount = data[0] | (data[1] << 8);
    print("Processing frame #$frameCount, data length: ${data.length}");

    // Check for frame sequence continuity
    if (lastFrameCount != -1) {
      int expectedFrame = (lastFrameCount + 1) & 0xFFFF;
      if (frameCount != expectedFrame) {
        frameDrops++;
        if (frameCount < lastFrameCount) {
          outOfOrderFrames++;
        }
        print("Frame discontinuity: Expected $expectedFrame, Got $frameCount");
      }
    }
    lastFrameCount = frameCount;

    // Extract audio data (skip 3-byte header)
    List<int> audioData = data.sublist(3);
    if (audioData.length != 320) {
      // 160 samples * 2 bytes per sample
      invalidSizeFrames++;
      print("Invalid audio data size: ${audioData.length}");
      return;
    }

    audioBuffer.addAll(audioData);
    framesReceived++;

    // Print buffer size periodically
    if (framesReceived % 100 == 0) {
      print("Current audio buffer size: ${audioBuffer.length} bytes");
    }
  }

  Future<void> stopRecording() async {
    if (!isRecording) return; // Prevent multiple stops

    print("Stopping recording...");
    setState(() {
      isRecording = false;
      dataReceived = false;
      statusMessage = "Recording stopped. Saving files...";
    });

    statusTimer?.cancel();

    // Add a small delay to ensure all data is processed
    await Future.delayed(Duration(milliseconds: 100));

    // Save the recorded audio buffer
    if (audioBuffer.isNotEmpty) {
      print("Saving ${audioBuffer.length} bytes of audio data");
      await saveWavFile();
      await downloadFromESP32();
    } else {
      print("No audio data to save");
    }
  }

  Future<void> saveWavFile() async {
    final directory = await getApplicationDocumentsDirectory();
    final timestamp =
        DateTime.now().toString().replaceAll(RegExp(r'[^0-9]'), '');
    final path = '${directory.path}/Recordings/ble_recording_$timestamp.wav';

    try {
      // Create Recordings directory if it doesn't exist
      final recordingsDir = Directory('${directory.path}/Recordings');
      if (!await recordingsDir.exists()) {
        await recordingsDir.create(recursive: true);
      }

      print("Saving WAV file to: $path");
      print("Audio buffer size: ${audioBuffer.length} bytes");

      final file = File(path);
      final sink = file.openWrite();

      // Write WAV header
      final header = generateWavHeader(audioBuffer.length);
      sink.add(header);
      print("Wrote WAV header: ${header.length} bytes");

      // Write audio data
      sink.add(audioBuffer);
      print("Wrote audio data: ${audioBuffer.length} bytes");

      await sink.flush();
      await sink.close();

      // Verify file was created
      if (await file.exists()) {
        final size = await file.length();
        print("WAV file saved successfully. Size: $size bytes");
        currentAudioFile = path;
        setState(() => statusMessage = "Saved BLE recording ($size bytes)");
      } else {
        print("Error: WAV file was not created");
        setState(() => statusMessage = "Error saving WAV file");
      }
    } catch (e) {
      print("Error saving WAV file: $e");
      setState(() => statusMessage = "Error saving WAV file: $e");
    }
  }

  Uint8List generateWavHeader(int dataSize) {
    print("Generating WAV header for data size: $dataSize bytes");
    final header = ByteData(44); // WAV header is 44 bytes
    final totalSize = 36 + dataSize;

    try {
      // RIFF chunk descriptor
      header.setUint32(0, 0x52494646, Endian.big); // 'RIFF' in ASCII
      header.setUint32(4, totalSize, Endian.little); // total file size - 8
      header.setUint32(8, 0x57415645, Endian.big); // 'WAVE' in ASCII

      // fmt sub-chunk
      header.setUint32(12, 0x666D7420, Endian.big); // 'fmt ' in ASCII
      header.setUint32(16, 16, Endian.little); // subchunk size (16 for PCM)
      header.setUint16(20, 1, Endian.little); // PCM = 1
      header.setUint16(22, 1, Endian.little); // Mono = 1 channel
      header.setUint32(24, 16000, Endian.little); // Sample rate: 16000 Hz
      header.setUint32(
          28,
          32000,
          Endian
              .little); // Byte rate = SampleRate * NumChannels * BitsPerSample/8
      header.setUint16(
          32, 2, Endian.little); // Block align = NumChannels * BitsPerSample/8
      header.setUint16(34, 16, Endian.little); // Bits per sample = 16

      // data sub-chunk
      header.setUint32(36, 0x64617461, Endian.big); // 'data' in ASCII
      header.setUint32(40, dataSize, Endian.little); // data size

      print("WAV header generated successfully");
      return header.buffer.asUint8List();
    } catch (e) {
      print("Error generating WAV header: $e");
      throw e;
    }
  }

  Future<bool> _verifyESP32Connection() async {
    try {
      // Try to connect to ESP32's root page first
      final testResponse = await http.get(
        Uri.parse('http://$ESP_IP'),
        headers: {'Connection': 'keep-alive'},
      ).timeout(Duration(seconds: 5));

      return testResponse.statusCode == 200;
    } catch (e) {
      print("Connection verification failed: $e");
      return false;
    }
  }

  Future<bool> _waitForWiFiConnection() async {
    bool connected = false;
    int attempts = 0;
    const maxAttempts = 10;

    while (!connected && attempts < maxAttempts) {
      attempts++;
      print("Checking WiFi connection attempt $attempts/$maxAttempts");

      try {
        final currentSSID = await NetworkInfo().getWifiName();
        print("Current WiFi network: $currentSSID");

        if (currentSSID?.contains(ESP_WIFI_SSID) == true) {
          // Give the connection a moment to stabilize
          await Future.delayed(Duration(seconds: 2));

          if (await _verifyESP32Connection()) {
            connected = true;
            print("Successfully connected to ESP32");
            break;
          }
        }
      } catch (e) {
        print("Error checking WiFi connection: $e");
      }

      await Future.delayed(Duration(seconds: 1));
    }

    return connected;
  }

  Future<void> downloadFromESP32() async {
    setState(() => statusMessage = "Preparing WiFi connection...");

    try {
      if (Platform.isIOS) {
        bool shouldContinue = true;

        await showDialog(
          context: context,
          barrierDismissible: false,
          builder: (context) => AlertDialog(
            title: Text("WiFi Connection Required"),
            content: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Text("1. Open iOS Settings and connect to:"),
                SizedBox(height: 8),
                Text("Network: $ESP_WIFI_SSID",
                    style: TextStyle(fontWeight: FontWeight.bold)),
                Text("Password: $ESP_WIFI_PASSWORD",
                    style: TextStyle(fontWeight: FontWeight.bold)),
                SizedBox(height: 16),
                Text("2. Dismiss the 'No Internet' warning"),
                SizedBox(height: 16),
                Text("3. Return to this app and tap 'Continue'"),
              ],
            ),
            actions: [
              TextButton(
                child: Text("Cancel"),
                onPressed: () {
                  shouldContinue = false;
                  Navigator.pop(context);
                },
              ),
              TextButton(
                child: Text("Continue"),
                onPressed: () => Navigator.pop(context),
              ),
            ],
          ),
        );

        if (!shouldContinue) {
          setState(() => statusMessage = "Download cancelled");
          return;
        }

        setState(() => statusMessage = "Verifying WiFi connection...");

        if (!await _waitForWiFiConnection()) {
          setState(() => statusMessage = "Could not connect to ESP32");
          return;
        }
      }

      setState(() => statusMessage = "Downloading from ESP32...");

      final response = await _retryDownload();

      if (response != null) {
        final directory = await getApplicationDocumentsDirectory();
        final timestamp =
            DateTime.now().toString().replaceAll(RegExp(r'[^0-9]'), '');
        final path =
            '${directory.path}/Recordings/sdcard_recording_$timestamp.wav';

        // Ensure Recordings directory exists
        final recordingsDir = Directory('${directory.path}/Recordings');
        if (!await recordingsDir.exists()) {
          await recordingsDir.create(recursive: true);
        }

        await File(path).writeAsBytes(response.bodyBytes);
        setState(
            () => statusMessage = "Downloaded SD card recording to: $path");

        // Restore original WiFi connection
        await restoreOriginalWifi();
      }
    } catch (e) {
      print("Download error: $e");
      setState(() => statusMessage = "Download error: $e");
    }
  }

  Future<void> restoreOriginalWifi() async {
    if (Platform.isIOS) {
      showDialog(
        context: context,
        builder: (context) => AlertDialog(
          title: Text("Reconnect to Original Network"),
          content: Text(
              "Please reconnect to your original WiFi network in Settings."),
          actions: [
            TextButton(
              child: Text("OK"),
              onPressed: () => Navigator.pop(context),
            ),
          ],
        ),
      );
    } else if (Platform.isAndroid) {
      if (currentWifiSSID != null) {
        await WiFiForIoTPlugin.connect(currentWifiSSID!);
      }
    }
  }

  Future<http.Response?> _retryDownload() async {
    for (int i = 0; i < 3; i++) {
      try {
        print("Download attempt ${i + 1}/3");
        final response = await http.get(
          Uri.parse('http://$ESP_IP/file'),
          headers: {'Connection': 'keep-alive', 'Cache-Control': 'no-cache'},
        ).timeout(Duration(seconds: 30));

        if (response.statusCode == 200 && response.bodyBytes.length > 44) {
          // Minimum WAV file size
          print(
              "Download successful, received ${response.bodyBytes.length} bytes");
          return response;
        }
        print(
            "Download attempt ${i + 1} failed with status: ${response.statusCode}");
      } catch (e) {
        print("Download attempt ${i + 1} failed: $e");
        await Future.delayed(Duration(seconds: 2));
      }
    }
    setState(() => statusMessage = "Download failed after 3 attempts");
    return null;
  }

  Future<void> playAudio(String filePath) async {
    try {
      print("Attempting to play audio file: $filePath");

      // Verify file exists and has content
      final file = File(filePath);
      if (!await file.exists()) {
        print("Error: Audio file does not exist");
        return;
      }

      final size = await file.length();
      print("Audio file size: $size bytes");

      if (size < 44) {
        // Minimum size for a valid WAV file
        print("Error: Audio file is too small to be valid");
        return;
      }

      // Try to load and play the file
      await _audioPlayer.setFilePath(filePath);
      audioDuration = await _audioPlayer.duration;
      print("Audio duration: $audioDuration");

      if (audioDuration == null || audioDuration!.inMilliseconds == 0) {
        print("Error: Invalid audio duration");
        return;
      }

      await _audioPlayer.play();
      setState(() {
        isPlaying = true;
        currentPosition = Duration.zero;
      });
    } catch (e, stackTrace) {
      print("Error playing audio: $e");
      print("Stack trace: $stackTrace");
      setState(() => statusMessage = "Error playing audio: $e");
    }
  }

  Future<void> togglePlayPause() async {
    if (currentAudioFile == null) return;

    if (isPlaying) {
      await _audioPlayer.pause();
    } else {
      await _audioPlayer.play();
    }
    setState(() => isPlaying = !isPlaying);
  }

  Future<void> stopPlaying() async {
    await _audioPlayer.stop();
    setState(() {
      isPlaying = false;
      currentPosition = Duration.zero;
    });
  }

  Future<bool> hasRecordings() async {
    final directory = await getApplicationDocumentsDirectory();
    final recordingsDir = Directory('${directory.path}/Recordings');
    if (!await recordingsDir.exists()) return false;

    final files = await recordingsDir
        .list()
        .where((entity) => entity.path.endsWith('.wav'))
        .toList();
    return files.isNotEmpty;
  }

  Future<String?> getLatestRecording() async {
    final directory = await getApplicationDocumentsDirectory();
    final recordingsDir = Directory('${directory.path}/Recordings');
    if (!await recordingsDir.exists()) return null;

    final files = await recordingsDir
        .list()
        .where((entity) => entity.path.endsWith('.wav'))
        .toList();

    if (files.isEmpty) return null;

    return files
        .map((f) => f.path)
        .reduce((a, b) => a.compareTo(b) > 0 ? a : b);
  }

  String formatDuration(Duration duration) {
    String twoDigits(int n) => n.toString().padLeft(2, '0');
    String twoDigitMinutes = twoDigits(duration.inMinutes.remainder(60));
    String twoDigitSeconds = twoDigits(duration.inSeconds.remainder(60));
    return "$twoDigitMinutes:$twoDigitSeconds";
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text('BLE Audio Receiver'),
        backgroundColor: Theme.of(context).colorScheme.inversePrimary,
      ),
      body: Center(
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: <Widget>[
            Padding(
              padding: const EdgeInsets.all(16.0),
              child: Text(
                statusMessage,
                textAlign: TextAlign.center,
                style: Theme.of(context).textTheme.bodyLarge,
              ),
            ),
            SizedBox(height: 20),

            // Connection button
            if (!isConnected && !isScanning)
              ElevatedButton(
                onPressed: startScan,
                child: Text('Connect to Device'),
              ),

            // Audio player controls
            if (currentAudioFile != null && !isRecording) ...[
              SizedBox(height: 20),
              Text('Audio Player',
                  style: Theme.of(context).textTheme.titleLarge),
              SizedBox(height: 10),

              // Progress bar
              Padding(
                padding: const EdgeInsets.symmetric(horizontal: 32),
                child: Column(
                  children: [
                    Slider(
                      value: currentPosition.inSeconds.toDouble(),
                      max: audioDuration?.inSeconds.toDouble() ?? 0,
                      onChanged: (value) async {
                        final position = Duration(seconds: value.toInt());
                        await _audioPlayer.seek(position);
                        setState(() => currentPosition = position);
                      },
                    ),
                    Padding(
                      padding: const EdgeInsets.symmetric(horizontal: 16),
                      child: Row(
                        mainAxisAlignment: MainAxisAlignment.spaceBetween,
                        children: [
                          Text(formatDuration(currentPosition)),
                          Text(formatDuration(audioDuration ?? Duration.zero)),
                        ],
                      ),
                    ),
                  ],
                ),
              ),

              // Playback controls
              Row(
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  IconButton(
                    icon: Icon(Icons.replay_10),
                    onPressed: () async {
                      final newPosition = Duration(
                          seconds: max(0, currentPosition.inSeconds - 10));
                      await _audioPlayer.seek(newPosition);
                    },
                  ),
                  IconButton(
                    iconSize: 48,
                    icon: Icon(
                        isPlaying ? Icons.pause_circle : Icons.play_circle),
                    onPressed: togglePlayPause,
                  ),
                  IconButton(
                    icon: Icon(Icons.forward_10),
                    onPressed: () async {
                      final newPosition = Duration(
                          seconds: min((audioDuration?.inSeconds ?? 0),
                              currentPosition.inSeconds + 10));
                      await _audioPlayer.seek(newPosition);
                    },
                  ),
                ],
              ),

              // Recording selection buttons
              Row(
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  ElevatedButton.icon(
                    icon: Icon(Icons.bluetooth),
                    label: Text('Play BLE Recording'),
                    onPressed: () async {
                      await stopPlaying();
                      await playAudio(currentAudioFile!);
                    },
                  ),
                  SizedBox(width: 16),
                  ElevatedButton.icon(
                    icon: Icon(Icons.sd_card),
                    label: Text('Play SD Recording'),
                    onPressed: () async {
                      final latestFile = await getLatestRecording();
                      if (latestFile != null) {
                        await stopPlaying();
                        await playAudio(latestFile);
                      }
                    },
                  ),
                ],
              ),

              // Share button
              FutureBuilder<bool>(
                future: hasRecordings(),
                builder: (context, snapshot) {
                  if (snapshot.data == true) {
                    return Column(
                      children: [
                        SizedBox(height: 20),
                        ElevatedButton.icon(
                          icon: Icon(Icons.share),
                          label: Text('Share Recording'),
                          onPressed: () async {
                            final filePath =
                                currentAudioFile ?? await getLatestRecording();
                            if (filePath != null) {
                              await Share.shareXFiles([XFile(filePath)],
                                  text: 'Audio Recording from ESP32');
                            }
                          },
                        ),
                      ],
                    );
                  }
                  return SizedBox.shrink();
                },
              ),
            ],
          ],
        ),
      ),
    );
  }
}
