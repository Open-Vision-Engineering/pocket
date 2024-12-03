// lib/main.dart
import 'package:flutter/material.dart';
import 'package:flutter_blue_plus/flutter_blue_plus.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:network_info_plus/network_info_plus.dart';
import 'package:path_provider/path_provider.dart';
import 'package:http/http.dart' as http;
import 'dart:async';
import 'dart:io';
import 'dart:typed_data';
import 'package:wifi_iot/wifi_iot.dart';

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
      title: 'BLE Audio Recorder',
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
  BluetoothDevice? device;
  BluetoothCharacteristic? audioCharacteristic;
  bool isScanning = false;
  bool isConnected = false;
  bool isRecording = false;
  String statusMessage = "Ready to scan";
  List<int> audioBuffer = [];
  String? currentWifiSSID;
  DateTime? recordingStartTime;
  int framesReceived = 0;
  Timer? statusTimer;

  // Recording statistics
  int frameDrops = 0;
  int outOfOrderFrames = 0;
  int invalidSizeFrames = 0;
  int lastFrameCount = -1;

  @override
  void initState() {
    super.initState();
    _initBluetooth();
  }

  @override
  void dispose() {
    statusTimer?.cancel();
    device?.disconnect();
    super.dispose();
  }

  Future<void> _initBluetooth() async {
    // Request necessary permissions
    await Permission.bluetooth.request();
    await Permission.bluetoothScan.request();
    await Permission.bluetoothConnect.request();
    await Permission.location.request();

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
    await FlutterBluePlus.startScan(timeout: const Duration(seconds: 10));

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
    Future.delayed(const Duration(seconds: 10), () {
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
        statusMessage = "Connected to $DEVICE_NAME";
      });
    } catch (e) {
      setState(() {
        statusMessage = "Connection failed: $e";
        isScanning = false;
      });
    }
  }

  Future<void> setupNotifications() async {
    await audioCharacteristic?.setNotifyValue(true);
    audioCharacteristic?.lastValueStream.listen((value) {
      if (isRecording) {
        processAudioData(value);
      }
    });
  }

  void processAudioData(List<int> data) {
    if (data.length < 3) return; // Ensure we have header

    // Parse frame count from header (first 2 bytes)
    int frameCount = data[0] | (data[1] << 8);

    // Check for frame sequence continuity
    if (lastFrameCount != -1) {
      int expectedFrame = (lastFrameCount + 1) & 0xFFFF;
      if (frameCount != expectedFrame) {
        frameDrops++;
        if (frameCount < lastFrameCount) {
          outOfOrderFrames++;
        }
      }
    }
    lastFrameCount = frameCount;

    // Extract audio data (skip 3-byte header)
    List<int> audioData = data.sublist(3);
    if (audioData.length != 320) {
      // 160 samples * 2 bytes per sample
      invalidSizeFrames++;
      return;
    }

    audioBuffer.addAll(audioData);
    framesReceived++;
  }

  Future<void> startRecording() async {
    setState(() {
      isRecording = true;
      statusMessage = "Recording...";
      audioBuffer.clear();
      framesReceived = 0;
      frameDrops = 0;
      outOfOrderFrames = 0;
      invalidSizeFrames = 0;
      lastFrameCount = -1;
      recordingStartTime = DateTime.now();
    });

    // Start periodic status updates
    statusTimer = Timer.periodic(const Duration(seconds: 1), (timer) {
      if (mounted) {
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

  Future<void> stopRecording() async {
    setState(() {
      isRecording = false;
      statusMessage = "Stopped recording. Saving file...";
    });

    statusTimer?.cancel();

    // Save the recorded audio buffer
    if (audioBuffer.isNotEmpty) {
      await saveWavFile();
      await downloadFromESP32();
    }
  }

  Future<void> saveWavFile() async {
    final directory = await getApplicationDocumentsDirectory();
    final timestamp =
        DateTime.now().toString().replaceAll(RegExp(r'[^0-9]'), '');
    final path = '${directory.path}/ble_recording_$timestamp.wav';

    final file = File(path);
    final sink = file.openWrite();

    // Write WAV header
    sink.add(generateWavHeader(audioBuffer.length));
    sink.add(audioBuffer);

    await sink.close();
    setState(() => statusMessage = "Saved BLE recording to: $path");
  }

  Uint8List generateWavHeader(int dataSize) {
    final header = ByteData(44); // WAV header is 44 bytes

    // RIFF chunk descriptor
    header.setUint32(0, 0x46464952, Endian.big); // 'RIFF'
    header.setUint32(4, 36 + dataSize, Endian.little);
    header.setUint32(8, 0x45564157, Endian.big); // 'WAVE'

    // fmt sub-chunk
    header.setUint32(12, 0x20746D66, Endian.big); // 'fmt '
    header.setUint32(16, 16, Endian.little); // subchunk size
    header.setUint16(20, 1, Endian.little); // PCM = 1
    header.setUint16(22, 1, Endian.little); // Mono = 1 channel
    header.setUint32(24, 16000, Endian.little); // Sample rate
    header.setUint32(28, 32000, Endian.little); // Byte rate
    header.setUint16(32, 2, Endian.little); // Block align
    header.setUint16(34, 16, Endian.little); // Bits per sample

    // data sub-chunk
    header.setUint32(36, 0x61746164, Endian.big); // 'data'
    header.setUint32(40, dataSize, Endian.little);

    return header.buffer.asUint8List();
  }

  Future<void> downloadFromESP32() async {
    setState(() => statusMessage = "Connecting to ESP32 WiFi...");

    // Store current WiFi info
    currentWifiSSID = await NetworkInfo().getWifiName();

    try {
      // Connect to ESP32 WiFi
      if (Platform.isAndroid) {
        await WiFiForIoTPlugin.connect(ESP_WIFI_SSID,
            password: ESP_WIFI_PASSWORD, security: NetworkSecurity.WPA);
      } else if (Platform.isIOS) {
        // For iOS, user needs to connect manually
        showDialog(
          context: context,
          builder: (context) => AlertDialog(
            title: Text("WiFi Connection Required"),
            content: Text("Please connect to '$ESP_WIFI_SSID' "
                "with password '$ESP_WIFI_PASSWORD' "
                "in your WiFi settings."),
            actions: [
              TextButton(
                child: Text("OK"),
                onPressed: () => Navigator.pop(context),
              ),
            ],
          ),
        );
        await Future.delayed(Duration(seconds: 15)); // Wait for user
      }

      // Download file
      setState(() => statusMessage = "Downloading from ESP32...");
      final response = await http.get(Uri.parse('http://$ESP_IP/file'));

      if (response.statusCode == 200) {
        final directory = await getApplicationDocumentsDirectory();
        final timestamp =
            DateTime.now().toString().replaceAll(RegExp(r'[^0-9]'), '');
        final path = '${directory.path}/sdcard_recording_$timestamp.wav';

        await File(path).writeAsBytes(response.bodyBytes);
        setState(
            () => statusMessage = "Downloaded SD card recording to: $path");
      } else {
        setState(
            () => statusMessage = "Download failed: ${response.statusCode}");
      }
    } catch (e) {
      setState(() => statusMessage = "Download error: $e");
    } finally {
      // Restore original WiFi
      if (Platform.isAndroid && currentWifiSSID != null) {
        await WiFiForIoTPlugin.connect(currentWifiSSID!);
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text('BLE Audio Recorder'),
        backgroundColor: Theme.of(context).colorScheme.inversePrimary,
      ),
      body: Center(
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: <Widget>[
            Text(
              statusMessage,
              textAlign: TextAlign.center,
              style: Theme.of(context).textTheme.bodyLarge,
            ),
            SizedBox(height: 20),
            if (!isConnected && !isScanning)
              ElevatedButton(
                onPressed: startScan,
                child: Text('Scan for Device'),
              ),
            if (isConnected && !isRecording)
              ElevatedButton(
                onPressed: startRecording,
                child: Text('Start Recording'),
              ),
            if (isRecording)
              ElevatedButton(
                onPressed: stopRecording,
                child: Text('Stop Recording'),
              ),
          ],
        ),
      ),
    );
  }
}
