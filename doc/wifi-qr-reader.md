# Wi-Fi QR reader protocol

The Portal controls palmimo-wifi-qr.service and connects as a client to
/run/palmimo-wifi-qr/control.sock. The reader owns the camera, face display,
and their reservations. It creates the socket in its service runtime
directory, permits socket access, and verifies the client's uid with
SO_PEERCRED against the configured Portal user.

## Framing

Each message is one UTF-8 JSON object followed by LF. The limit is 32 KiB
before LF. Senders discard oversized messages. Receivers discard an
oversized line through its next LF and continue with the following line.
Malformed UTF-8, invalid JSON, invalid message fields, and unknown message
types are ignored. A partial line at EOF is discarded.

Only one authorized client is accepted. Disconnecting it ends the reader
normally after restoring the face and releasing its devices.

## Reader to Portal

| type | Fields | Meaning |
| --- | --- | --- |
| hello | proto: 1, display: frame or expr | Camera and display initialization completed; sent once |
| qr | text: string | Decoded QR contents, without interpretation |

The Portal accepts qr only while its screen state is scanning. On decoding
a QR, the reader immediately stops the live preview and shows checking
before waiting for the Portal. It suppresses the same text until that text
has been absent for 30 consecutive frames.

## Portal to reader

All messages have type: screen.

| state | Additional fields |
| --- | --- |
| scanning | None |
| checking | None |
| rejected | reason |
| connecting | ssid |
| failed | reason |
| connected | hostname, without the .local suffix |

Reasons are not_wifi, unsupported_security, hidden_network, invalid_ssid,
invalid_psk, busy, or connect_failed. The connected screen presents the
hostname and sticker-login guidance without a network name: the connected
LAN can differ from the QR's network.

The Portal owns screen timing: rejected lasts 5 seconds, failed 8 seconds,
and connected 60 seconds. An unresolved QR trial screen fails after 120
seconds; this does not cancel the independent network trial.

## Reader timers and termination

The reader exits normally if no authorized client arrives within 30 seconds
of startup. While checking, 15 seconds without a Portal command returns it
to scanning. While displaying frames, it sends a display PING every 2
seconds. SIGTERM restores the sleeping face and releases devices.

| Exit code | Meaning | Portal response |
| --- | --- | --- |
| 0 | Normal disconnect, termination, or client wait expiry | Normal completion |
| 69 | Required decoding dependencies unavailable | Disable until the Portal restarts |
| 75 | Camera or display reservation unavailable | Retry after 30 seconds |
| 78 | Camera or face display missing | Retry after one hour |
| Other | Abnormal termination | Retry after 30 seconds, doubling to at most 10 minutes |

A hello resets the abnormal retry interval to 30 seconds. A unit missing
from systemd disables setup until the Portal restarts. A stop requested by
the Portal is normal regardless of the resulting exit status.

## Secrets

QR text, passwords, and preview frames must not be logged or written to
files by either protocol endpoint. Errors in decoding JSON, text, or QR
credentials must be reported without input fragments or exception chains
that could contain them. SSID and result logging is permitted.
