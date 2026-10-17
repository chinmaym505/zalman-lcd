# How the protocol was recovered

These notes are for anyone who wants to re-check the protocol, support another Zalman display, or follow a future OZ-Wide release. The driver itself doesn't need any of this.

## 1. Identify the device

```bash
lsusb                                   # 1c57:3ed7 Zalman Tech Co., Ltd. Zalman USB Device
lsusb -v -d 1c57:3ed7                   # one HID interface, 1024-byte interrupt IN/OUT
sudo xxd /sys/bus/hid/devices/0003:1C57:3ED7.*/report_descriptor
```

The report descriptor is a single vendor-defined collection (`06 00 FF 09 01 A1 01 …`) with one 1024-byte input report and one 1024-byte output report, and no report IDs. Linux binds `hid-generic`, so `/dev/hidrawN` can be used straight away. No kernel driver is needed.

## 2. Unpack OZ-Wide

The `OZ_Wide_Setup.exe` installer is NSIS, so `7z x` unpacks it. `Zalman_OZ_Wide.exe` and `ZM_Service.exe` are 32-bit .NET assemblies, and USB access goes through `HidSharp.dll`. Decompile with ILSpy:

```bash
dotnet tool install -g ilspycmd
ilspycmd -p -o dec_app Zalman_OZ_Wide.exe
grep -rnE "HidStream|16087|7255" dec_app      # 7255 = 0x1C57, 16087 = 0x3ED7 (P40_DS in Define.cs)
```

The protocol lives in `WideDisplay.cs` (command building, framing, checksum, escaping, `RealTimeWrite`) and `WideDisplay_HidSharp.cs` (the write path and render loop).

## 3. Deal with the obfuscation

The assembly is protected with ConfuserEx: identifiers are invisible Unicode, control flow is flattened into `switch` state machines, and string constants are encrypted behind calls like `<Module>.X<string>(177472558)`.

**Strings.** On Linux, the decrypter can't run. Modern .NET rejects the deliberately corrupted metadata, and Mono crashes in the module initializer. On Windows with .NET Framework the initializer runs fine, but the decrypters return `null` unless the caller is code inside the app's own assembly. `tools/decrypt_strings.cs` gets around that by calling each decrypter through a `DynamicMethod` owned by the app's module:

```
csc.exe -platform:x86 -out:decrypt_strings.exe tools\decrypt_strings.cs
decrypt_strings.exe Zalman_OZ_Wide.exe keys.txt strings_b64.txt
```

- **Building `keys.txt`:** `grep -rhoE '<string>\(-?[0-9]+\)' dec_app | grep -oE -- '-?[0-9]+' | sort -u` (about 1,335 keys).
- **Output:** base64, so `\r\n` inside protocol strings survives.
- **Next step:** substitute the decrypted strings back into the decompiled source to make it readable.

**Control flow.** In the flattened methods, the order in which `case` blocks run is decided by arithmetic on the state variable. When order matters, for example to tell whether a chunk index starts at 0 or 1, the reliable method is to re-run that arithmetic in Python with the same constants and print the sequence of cases. That's how these were confirmed:

- `RealTimeWrite`: the header is filled in before the write, and the index increments after it, so it starts at 0.
- The HID render loop: one write, then `Thread.Sleep(1000)`.
- The image encoder: realtime mode encodes JPEG; OSD and media modes encode PNG.

## 4. Verify on the wire

`usbmon` shows exactly what reaches the device:

```bash
sudo modprobe usbmon
sudo cat /sys/kernel/debug/usb/usbmon/<bus>u     # bus from lsusb; look for Io/Ii on the device number
```

Each OUT URB should be 1024 bytes, starting with `5a…` for commands or `5c03fd01…` for full JPEG chunks.

## What turned out to matter

| Symptom | Cause |
|---|---|
| Every JSON command returned `1 400` | Guessed bodies were wrong (`{"brightness":80}` instead of `{"value":80}`, `{"realtimeDisplay":1}` instead of `{"enable":true}`) |
| Screen dark | Brightness was 0 and the panel was suspended. Fixed by `power resume` and `brightness` |
| Commands all `1 200`, frames ignored, backlight on but black even on the panel's own screen | The panel's display process had hung. Fixed by the `reboot` command |

## Not explored

OSD mode (the app encodes PNG in this mode and uploads it with `transport`), media/video upload, and firmware update. All three write to the panel's storage, so the driver leaves them alone.
