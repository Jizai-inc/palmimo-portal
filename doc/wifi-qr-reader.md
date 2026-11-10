# Wi-Fi QR reader protocol

The Portal controls palmimo-wifi-qr.service and connects as a client to
/run/palmimo-wifi-qr/control.sock. The reader owns the camera, face display,
and their reservations. It creates the socket in its service runtime
directory, permits socket access, and verifies the client's uid with
SO_PEERCRED against the configured Portal user.

After five observed non-LAN seconds, the Portal starts the reader in waiting
when no running app uses the camera or face display and no Wi-Fi attempt is in
progress. The reader draws waiting before hello; the Portal records that state
without resending it. After five observed AP seconds, the Portal sends scanning.
If known Wi-Fi networks exist and an app must be stopped, the Portal instead
waits for 60 observed AP seconds before stopping the app and entering scanning.
Returning to LAN without a pending QR attempt stops the reader without a success
screen, including while waiting for hello.

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
| hello | proto: 1 | Reservations, dependencies, camera open, and face preparation completed; streaming has not started; sent once |
| close | None | Connected screen Close button pressed |
| qr | text: string | Decoded QR contents, without interpretation |

Any hello display field is ignored. The Portal accepts close only on the
connected screen, then stops the reader normally and restores the app.
It accepts qr only while its screen state is scanning. On decoding
a QR, the reader immediately stops the live preview and shows checking
before waiting for the Portal. It suppresses each sent text until it has
been absent for 30 consecutive frames, retaining at most 16 texts and
evicting the oldest sent first.

## Portal to reader

All messages have type: screen.

| state | Additional fields |
| --- | --- |
| waiting | None; static Wi-Fi search notice, without starting the camera stream |
| scanning | None; start the stream on first use, then show live preview |
| checking | None |
| rejected | reason |
| connecting | ssid |
| failed | reason |
| connected | url: http://LAN-IPv4; qr: {size: n, rows: [binary strings]}; name (optional): hostname.local |

Reasons are not_wifi, unsupported_security, hidden_network, invalid_ssid,
invalid_psk, busy, or connect_failed. The connected screen presents the URL,
the name below it, the URL's QR code, a Close button, and an automatic-close
notice without a network name: the connected LAN can differ from the QR's
network. The url and name are printable ASCII.

The Portal encodes the URL with error correction M. The square matrix has
size rows of size characters each; 1 is dark and 0 is light. It excludes the
quiet zone, which the reader adds when rendering. The URL has no trailing
slash. It names the LAN IPv4 address rather than hostname.local, because
Android Chrome does not resolve mDNS names; the name stays on screen as the
stable address for browsers that do. The reader sends close when the
connected screen's button is tapped.

The Portal owns screen timing: rejected lasts 5 seconds, failed 8 seconds,
and connected 300 seconds unless closed earlier. An unresolved QR trial screen fails after 120
seconds; this does not cancel the independent network trial.

## Startup and camera streaming

Startup creates the socket, reserves camera then display, imports hardware
dependencies, opens and configures the camera, and initializes the face before
sending hello. Camera setup includes S_FMT, REQBUFS, QUERYBUF/mmap, and QBUF,
but no STREAMON. A missing or busy camera exits with 78 or 75 before any face
communication.

The first image is the RGB565 waiting screen. The reader remains in waiting
without decoding QR codes until the Portal sends a screen command. The first
scanning command starts STREAMON. Until the first frame arrives, the face shows
a GRAY8 camera preparation notice; it then shows live preview. No blank frame
is sent during startup or a return to scanning. Once started, the stream stays
on until reader termination. Returning to scanning after live preview resumes
with the next camera frame without another preparation notice.

## Reader timers and termination

The reader exits normally if no authorized client arrives within 30 seconds
of startup. While checking, 15 seconds without a Portal command returns it
to scanning. If no camera frame arrives within 15 seconds of STREAMON, the
reader exits abnormally with 1, even if the Portal changes screens. These deadlines
use a monotonic clock. While displaying frames, including waiting and camera
preparation, it sends a display PING every 2 seconds. SIGTERM restores the face to its power state from startup (`IDLE` if it was on, `EXPR SLEEPING` if it was off) and releases devices.

| Exit code | Meaning | Portal response |
| --- | --- | --- |
| 0 | Normal disconnect, termination, or client wait expiry | Normal completion |
| 69 | Required decoding dependencies unavailable | Disable until the Portal restarts |
| 75 | Camera or display reservation unavailable | Retry after 30 seconds |
| 76 | Face firmware does not support FRAME | Disable until the Portal restarts; expose firmware_too_old in setup status |
| 78 | Camera or face display unavailable: missing, not answering, or the reservation directory is missing or unsafe | Retry after one hour |
| Other | Abnormal termination | Retry after 30 seconds, doubling to at most 10 minutes |

A hello resets the abnormal retry interval to 30 seconds. A unit missing
from systemd disables setup until the Portal restarts. A stop requested by
the Portal is normal regardless of the resulting exit status.

## Secrets

QR text, passwords, and preview frames must not be logged or written to
files by either protocol endpoint. Errors in decoding JSON, text, or QR
credentials must be reported without input fragments or exception chains
that could contain them. SSID and result logging is permitted.
