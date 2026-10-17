# Zalman P40 DS / ZM-MF916 USB protocol

This was recovered from Zalman OZ-Wide 3.1 (`WideDisplay.cs` and `WideDisplay_HidSharp.cs`) and confirmed against a panel running firmware V1.2.3. Everything here is implemented in `zalman_lcd.py`.

## Transport

- **USB device:** `1c57:3ed7`, one HID interface, vendor usage page `0xFF00`.
- **Endpoints:** interrupt OUT `0x01` and interrupt IN `0x81`, both 1024 bytes.
- **No report IDs:** the report descriptor declares one 1024-byte input report and one 1024-byte output report. On Linux `hidraw`, write `0x00` (report ID 0) followed by exactly 1024 bytes. Shorter messages are zero-padded.
- **Messages:** every message, whether a command or a frame chunk, is exactly one report.
- **Replies:** they arrive as 1024-byte input reports. The panel never sends anything unprompted.
- **No touch input:** the panel isn't a touchscreen. With a listener on the input endpoint, taps, holds and swipes produced no reports, while a command reply sent during the same window was captured. OZ-Wide has no touch handling either.

## Command frames

```
5A | len_hi len_lo | payload ... | checksum | 5A
```

- `len` is a big-endian u16 equal to `len(payload) + 5`.
- `checksum` is the low byte of the sum of every byte between the opening `5A` and the checksum: the two length bytes plus the payload.
- **Escaping:** between the two `5A` markers (length, payload and checksum), byte `5A` is sent as `5B 01` and byte `5B` as `5B 02`. Unescape replies the same way. The panel's serial number contains a `Z` (`0x5A`), which is a handy check.

### Payloads

Requests are ASCII and look like HTTP:

```
POST conn 1\r\nSeqNumber=2\r\nDate=<anything>\r\n\r\n
POST <name> 1\r\nSeqNumber=100\r\nContentType=json\r\nContentLength=<n>\r\n\r\n<json body>
STATE heartbeat 1\r\nSeqNumber=100\r\n\r\n
```

`ContentLength` is the byte length of the JSON body. The date format in `conn` doesn't matter.

Replies:

```
1 200\r\nAckNumber=101\r\n\r\n                         accepted
1 400\r\nAckNumber=101\r\n\r\n                         parsed, but the body was rejected
1 200\r\nAckNumber=3\r\nContentType=json\r\nContentLength=255\r\n\r\n{...}   reply to conn
```

### Connect reply

```json
{"attribute":["Status","Fan","Pump"],"OS":"Linux","productId":"1",
 "version":{"app":"V3.0.0","firmware":"V1.2.3","hardware":"V2.0"},
 "space":69548,"brightness":80,"degree":90,"sn":"BYZL…","osdState":0,
 "mode":0,"logo":3,"timeout":5,"suspend":0}
```

### Commands

| Name | Body | Effect |
|---|---|---|
| `conn` | (none, see above) | Handshake; returns device info |
| `brightness` | `{"value":10..100}` | Backlight level (the app's slider range is 10–100) |
| `realtimeDisplay` | `{"enable":true\|false}` | Enter/leave streaming mode. `false` returns to the panel's own screen |
| `power` | `{"event":"resume"\|"suspend"}` | The app sends `resume` after connecting and `suspend` on PC sleep |
| `osdState` | `{"enable":true\|false}` | On-device overlay; the app turns it off on connect |
| `displayInSleep` | `{"enable":true\|false}` | Keep showing while the PC sleeps |
| `timeout` | `{"value":N}` | Panel timeout setting |
| `rotate` | `{"degree":N}` | Orientation (the app echoes the device's `degree`) |
| `reboot` | `{"enable":true}` | Reboots the panel firmware; it re-enumerates after about 6 s |
| `recovery` | `{"enable":true}` | **Not used.** Factory/recovery path |
| `transport` / `transported` | file name, size, md5 | **Not used.** Uploads media or firmware to the panel's storage |

### Heartbeat

The app sends `STATE heartbeat 1` every **500 ms** from connection onwards, including while streaming. The panel doesn't reply to it.

## Realtime frames

After `realtimeDisplay {"enable":true}` returns `1 200`, send JPEG frames.

- **Image:** a 1920 × 462 baseline JPEG (the app uses GDI+ at quality 85, 4:2:0). Pillow's default output is structurally identical.
- **Chunks:** split the JPEG into pieces of at most 1000 bytes. Each chunk is one report:

```
offset  size  value
0       1     5C
1       2     chunk_len + 21                (u16 BE)
3       1     01
4       2     jpeg_len // 1000 + 1          (u16 BE, chunk count as the app computes it;
                                              one too many when jpeg_len is a multiple of 1000)
6       2     chunk index, starting at 0    (u16 BE)
8       1     01
9..23         00
24      ≤1000 JPEG bytes                    (no escaping)
```

- **No acknowledgements:** the panel doesn't acknowledge chunks or frames.
- **Pacing:** the app sends one frame per second. This driver defaults to 2 fps, which works. The app's settings mention a 30 fps / 1.5 Mbit/s ceiling; higher rates are untested.

File uploads (`transport`) use the same 24-byte chunk header.

## Startup sequence used by the driver

1. `conn`
2. `power {"event":"resume"}`, then poll `conn` once a second until the reply shows `"suspend":0` three times in a row (the panel may not answer while it wakes; allow up to 30 s)
3. `osdState {"enable":false}`
4. `displayInSleep {"enable":false}`. The app never sends this command; the driver sends it to undo an earlier manual `true`.
5. `brightness {"value":N}` (optional)
6. `realtimeDisplay {"enable":true}`. Retry for a few seconds if there's no reply; the panel can be busy right after `resume`.
7. Loop: one JPEG frame per period, plus a heartbeat every 500 ms. Every 10 s, between frames, send `conn`. If it reports `"suspend":1` or brightness 0, repeat steps 2–6.
8. On exit: `realtimeDisplay {"enable":false}`. If the whole system is shutting down (`systemctl is-system-running` reports `stopping`), also send `power {"event":"suspend"}`, as OZ-Wide does when Windows shuts down or the display turns off.

## Known quirks

- **Stuck display:** the panel's display process can hang while its command handler keeps answering `1 200`. The symptom is a backlit but black screen, even outside realtime mode. `reboot` fixes it.
- **Suspended at power-on:** the panel can come up with `"suspend":1` and `"brightness":0`. Send `resume` and set a brightness.
- **`resume` takes effect late:** it's acknowledged at once, but the panel then spends several seconds waking and may ignore commands meanwhile. Right after the PC powers on, a panel that was suspended at shutdown has been seen accepting `resume`, `brightness` and `realtimeDisplay`, showing frames for a few seconds, then returning to `"suspend":1, "brightness":0`. Confirm the wake with `conn`, and keep checking.
- **Commands between chunks get dropped:** a command that arrives in the middle of a frame's chunk sequence (for example from a second process) is often lost. Send commands between frames.
- **`suspend` zeroes brightness:** after `power {"event":"suspend"}` the panel reports `"brightness":0`, so set brightness again after every `resume`.
- **Standby power:** many motherboards keep 5 V on the USB headers while the PC is off. Left awake with no host (`displayInSleep` on, no `suspend` at shutdown), the panel was seen rebooting in a loop.
