# OTi-2108 Clean-Room Specification — Part 2: Host-to-Host Wire Protocol

## 1. 64 KB Bulk Block Framing (`0x00443D69`, `0x0040B7E7`, `0x0043CA00`)

In **Transfer Line Mode** (`mode = 0x007F`), every bulk read (`0xD9 0x28`) and bulk write (`0xD9 0x2A`) transfers a fixed **`65,536`-byte (`0x10000`) block**.

### 1.1 20-Byte (`0x14`) Little-Endian Block Header
Every `0x10000`-byte block starts with a 20-byte header followed by up to **`65,516` bytes (`0xFFEC`)** of message payload:

```text
Offset  0x00              0x04              0x08              0x0C              0x10              0x14
       +-----------------+-----------------+-----------------+-----------------+-----------------+-------------------...
       |  block_seq_id   |   msg_seq_id    |   chunk_index   |  total_chunks   |chunk_payload_len| payload[0..0xFFEB]
       |   (uint32_le)   |   (uint32_le)   |   (uint32_le)   |   (uint32_le)   |   (uint32_le)   | (up to 65,516 B)
       +-----------------+-----------------+-----------------+-----------------+-----------------+-------------------...
```

| Offset | Size | Type | Field | Description |
| :--- | ---: | :--- | :--- | :--- |
| `+0x00` | 4 | `uint32_le` | `block_seq_id` | Monotonically incrementing counter incremented for **every** `0x10000` block (`0x0041D7EE`: `inc dword ptr [ecx+0x608]`). |
| `+0x04` | 4 | `uint32_le` | `msg_seq_id` | Monotonically incrementing counter incremented once per **logical message** (`0x0041CCFF`: `inc dword ptr [ecx+0x60C]`). All chunks of a multi-block message share the same `msg_seq_id`. |
| `+0x08` | 4 | `uint32_le` | `chunk_index` | Zero-based index of this block within the logical message (`0 .. total_chunks - 1`). |
| `+0x0C` | 4 | `uint32_le` | `total_chunks` | Total number of `0x10000` blocks required to carry the logical message ($\lceil \text{message\_len} / \text{0xFFEC} \rceil$, minimum `1`). |
| `+0x10` | 4 | `uint32_le` | `chunk_payload_len` | Number of valid payload bytes at `+0x14` in this block (`1 .. 0xFFEC`). |
| `+0x14` | `0xFFEC` | `bytes` | `payload` | Message bytes (`chunk_payload_len` valid bytes; trailing bytes in the `0x10000` buffer are zero/ignored). In **chunk 0** (`chunk_index == 0`), `payload[0]` (offset `+0x14`) is the **Message Opcode Byte**. |

---

## 2. Multi-Block Packet Splitter & Reassembler

### 2.1 Packet Splitter (`0x0040B7E7` & `0x00443D69`)
Given a logical message buffer `msg` of length `L` bytes (where `msg[0]` is the opcode byte):
1. Allocate a new `msg_seq_id = next_msg_seq_id()`.
2. If `L <= 0xFFEC` (`65,516` bytes):
   - Allocate one `0x10000`-byte block.
   - Set `block_seq_id = next_block_seq_id()`, `msg_seq_id = msg_seq_id`, `chunk_index = 0`, `total_chunks = 1`, `chunk_payload_len = L`.
   - Copy `msg[0..L]` to `block[0x14 .. 0x14 + L]`.
   - Enqueue block to the TX queue (`0x0041B983`).
3. If `L > 0xFFEC`:
   - Compute `full_chunks = L // 0xFFEC`, `rem = L % 0xFFEC`, and `total_chunks = full_chunks + (1 if rem > 0 else 0)`.
   - For `i` in `0 .. full_chunks - 1`:
     + Build block with `chunk_index = i`, `total_chunks = total_chunks`, `chunk_payload_len = 0xFFEC`, copying `msg[i*0xFFEC : (i+1)*0xFFEC]` to `+0x14`.
   - If `rem > 0`:
     + Build final block with `chunk_index = full_chunks`, `total_chunks = total_chunks`, `chunk_payload_len = rem`, copying the final `rem` bytes to `+0x14`.

### 2.2 Packet Reassembler State Machine (`0x0043CA00..0x0043D180`)
The RX reassembler thread dequeues `0x10000`-byte blocks from the RX queue and reassembles logical messages before dispatching them:
1. **Single-Block Fast Path (`total_chunks == 1` and `chunk_index == 0`):**
   - Extracts `msg = block[0x14 : 0x14 + chunk_payload_len]`.
   - Inspects `opcode = msg[0]`:
     + If `0xA1 <= opcode <= 0xAC`: dispatches to **Server Reply Dispatcher (`0x0040B93F`)**.
     + Otherwise (`0x01 <= opcode <= 0x39`): dispatches to **Client Command Dispatcher (`0x0043D181`)**.
2. **Multi-Block Reassembly (`total_chunks > 1`):**
   - For **in-memory commands/replies** (e.g., large directory listings `0xA2`, batch pre-check lists `0x14`/`0xA8`/`0xAA`):
     + On `chunk_index == 0`, allocates an accumulation buffer of `total_chunks * 0xFFEC` bytes and records `expected_msg_seq_id = msg_seq_id`.
     + For each subsequent chunk (`chunk_index == 1 .. total_chunks - 1`), verifies `msg_seq_id == expected_msg_seq_id` and appends `block[0x14 : 0x14 + chunk_payload_len]`.
     + When `chunk_index == total_chunks - 1`, dispatches the complete reassembled buffer to `0x0040B93F` or `0x0043D181`.
   - For **streaming file data (`opcode == 0xA3` Download File Stream or `opcode == 0x0C` Upload File Stream):**
     + Streams payload chunks directly to the open destination file handle on disk (`WriteFile`), updating the transfer progress bar after each 64 KB block so multi-gigabyte files do not buffer in RAM.

---

## 3. Cross-Platform Endianness & Peer Mode Rules (`flags & 0x10` / `0x20`)

`EasySuite.exe` supports connecting a Windows x86 host (little-endian) to PowerPC Mac OS or little-endian POSIX/Unix peers via a global platform mode variable at `[0x00480000]`, configured when Client Command `0x01` (**Query Peer Root / Drives**) is received (`0x0043D492..0x0043D4B5`):
- **Default (`[0x00480000] == 0`, Windows/x86/ARM little-endian peers):** All multi-byte integers (`uint16`, `uint32`, `int64`) inside wire command/reply payloads (and inside the 552-byte directory entry struct) are **little-endian**, paths use `\`, and error codes are formatted with `KERNEL32.DLL!FormatMessageA` (`0x00402372`).
- **Mac Peer Mode (`[0x00480000] == 0x10`, `flags & 0x10 != 0`):** Multi-byte integers in command headers, UTF-16 characters, and directory entries are byte-swapped (`0x004029C1`, `0x004029CC`, `0x004029F4`, `0x00402AC6`, `0x00442525`). Paths are normalized to `/` (`0x00402B88`), error codes are formatted with `MSVCRT.dll!strerror` (`0x004023AE`), abort/cancel commands (`0x0D`/`0x0F`/`0x11`) append `"ErrStr"`, and handlers gate on `[0x7ADBF4] != 0`.
- **Little-Endian POSIX/Unix Peer Mode (`[0x00480000] == 0x20`, `flags & 0x20 != 0`):** Keeps all multi-byte integers and UTF-16LE strings **little-endian** (no byte-swapping), formats error codes via `MSVCRT.dll!strerror` (`0x004023AE`), appends the `"ErrStr"` suffix on `0x0D`/`0x0F`/`0x11`, gates server/client handlers on `[0x45CB00] != 0` (statically initialized to `1`), and enables extended root-name/path records in `0xA4` (`0x0040F239` / `0x00435DAF`). Note that client command senders (`0x0040DB30`, `0x0040DBF4`, `0x0040F511`, etc.) only invoke `0x00402B88` (`\` $\rightarrow$ `/`) when `[0x00480000] == 0x10`, **not** when `[0x00480000] == 0x20`.

---

## 4. Complete Client Command Catalog (`0x01..0x39`)

Client Commands are sent by the initiating host (`0x0040D9C9..0x0040F800`) and handled by the receiving peer's Client Command Dispatcher (`0x0043D181..0x00441F02`).

### 4.1 Master Client Opcode Table

| Opcode | Name | Client Sender VA | Server Handler VA | Expected Reply Opcode(s) | Payload Format (after `opcode` byte at `+0x00`) |
| :---: | :--- | :--- | :--- | :---: | :--- |
| **`0x01`** | **Query Peer Root / Drives** | `0x0040D9C9` | `0x0043D479` | `0xA4` (and `0xA1`) | `[0x01][flags:u8][code_page:u16_le][name_len:u8][computer_name:bytes]` (11 bytes total for host query; Section 4.2.1) |
| **`0x02`** | **Notify / Set Active Remote Directory** | `0x0040DA90` | `0x0043D577` | `0xA1` (`status=0`) | `[0x02][path_len:u8][dir_path:bytes]` (folder-change notification; Section 4.2.2) |
| **`0x03`** | **Refresh Current Directory** | — | `0x0043D64D` | `0xA1` (`status=0`), then `0xA2`; or `0xA1` (`status=3`) | `[0x03]` (1 byte; re-lists active directory on peer, returns 24B 0xA1 `status=0` followed by 0xA2 listing, or `status=3` on error) |
| **`0x04`** | **Change / List Remote Directory** | `0x0040DB30` | `0x0043D741` | `0xA1` (`status=0`), then `0xA2`; or `0xA1` (`status=1`) | `[0x04][path_len:u16_le][dir_path:wchar_t]` (emits 24B 0xA1 status=0 followed by 552B `0xA2` listing, or 24B 0xA1 status=1 on error; Section 4.2.3) |
| **`0x05`** | **Select Remote Drive (Legacy/No-op)** | `0x0040DC28` | `0x0043D394` | — | `[0x05]` (1 byte; legacy/no-op, jumps to exit `0x0043D42A`) |
| **`0x06`** | **Remove Remote Directory** | `0x0040EE3A` | `0x0043D90F` | `0xA1` (`status=0` or `1`) | `[0x06][req_id:u32_le][path_len:u16_le][dir_path:wchar_t]` (calls `RemoveDirectoryW`, replies 24B `0xA1`) |
| **`0x07`** | **Create Remote Directory** | `0x0040EF0A` | `0x0043DA61` | `0xA1` (`status=0` or `1`) | `[0x07][req_id:u32_le][path_len:u16_le][dir_path:wchar_t]` (calls `CreateDirectoryW`, replies 24B `0xA1`) |
| **`0x08`** | **Delete Remote File** | `0x0040EC14` | `0x0043DBD1` | `0xA1` (`status=0` or `1`) | `[0x08][req_id:u32_le][path_len:u16_le][file_path:wchar_t]` (calls `DeleteFileW`, replies 24B `0xA1`) |
| **`0x09`** | **Rename Remote File or Directory** | `0x0040ECE4` | `0x0043DD10` | `0xA1` (`status=0` or `1`) | `[0x09][req_id:u32_le][old_len:u16_le][new_len:u16_le][old_path:wchar_t][new_path:wchar_t]` (calls `MoveFileW`, replies 24B `0xA1`) |
| **`0x0A`** | **Single File Upload Request** | `0x0040E2A7` | `0x0043DF17` | `0xA1` (`status=0..7`) | `[0x0A][flags:u8][req_id:u32_le][file_size:u64_le][dest_path_len:u16_le][dest_path:wchar_t]` (16B header + UTF-16LE path; Section 4.2.4) |
| **`0x0B`** | **Single File Download Request** | `0x0040DBF4` | `0x0043E4DA` | `0xA1` (`status=9` inline, `7` empty, `1` error); or `0xA1` (`status=0`) then `0xA3` | `[0x0B][req_id:u32_le][src_len:u16_le][src_path:wchar_t][dest_len:u16_le][dest_path:wchar_t]` (9B header/lengths + UTF-16LE paths; Section 4.2.5) |
| **`0x0C`** | **Upload File Data Chunk** | `0x004450C1` / `0x00443EC9` | `0x00440E41` | None (on success); `0xA5` or `0xA9` (on write error) | `[0x0C][item_id:u32_le][total_chunks:u32_le][chunk_index:u32_le][chunk_len:u32_le][data:bytes]` (Unacknowledged streaming; Section 4.2.6) |
| **`0x0D`** | **Abort Single File Upload (Delete Partial)** | `0x0040E9F8` | `0x0043E31E` | — | `[0x0D][item_id:u32_le][win32_error:u32_le]` (9 bytes in Windows mode; appends `"ErrStr"` + `err_str_len:u32_le` + `err_str:wchar_t[]` in `0x10`/`0x20` mode; closes handle and deletes partial destination file) |
| **`0x0E`** | **Acknowledge File Transfer Completion** | `0x0040CB5F` | `0x0043FFA6` | — | `[0x0E][item_id:u32_le][status:u16_le]` (7 bytes; removes transfer job from `0x7adce0` after streaming completes) |
| **`0x0F`** | **Cancel Single File Transfer (Keep Partial)** | `0x0040EB06` | `0x0043E3B4` | — | `[0x0F][item_id:u32_le][win32_error:u32_le]` (9 bytes in Windows mode; appends `"ErrStr"` + `err_str_len:u32_le` + `err_str:wchar_t[]` in `0x10`/`0x20` mode; closes handle without deleting destination file) |
| **`0x10`** | **Batch File Download Request (16-bit Count)** | `0x0040DD56` | `0x0043E781` | `0xA3`, `0xA6`, `0xA7`, `0xAB` | `[0x10][req_id:u32_le][item_count:u16_le][records: (item_id:u32_le, path_len:u16_le, path:wchar_t)...]` (replies with `0xA3` chunks, `0xA6` zero-len, `0xAB` errors, ended by `0xA7`) |
| **`0x11`** | **Abort Batch File Transfer** | `0x0040E19F` | `0x0043E45C` | — | `[0x11][req_id:u32_le][win32_error:u32_le]` (9 bytes in Windows mode; `ErrStr` extension in `0x10`/`0x20` mode; cancels batch job without reply) |
| **`0x12`** | **Batch Prepare / Clear Read-Only Attributes** | `0x0040E628` | `0x0043F981` | — | `[0x12][req_id:u32_le][item_count:u16_le][records: (item_id:u32_le, path_len:u16_le, path:wchar_t)...]` (fire-and-forget; no reply; clears `0x07` attributes and registers items in `0x7adce0`) |
| **`0x13`** | **Batch Upload Pre-Check (Space & Conflicts)** | `0x0040E468` | `0x0043F269` | `0xA8` | `[0x13][req_id:u32_le][total_size:u64_le][flags:u16_le][item_count:u16_le][records: (item_id:u32_le, path_len:u16_le, path:wchar_t)...]` (replies `0xA8`) |
| **`0x14`** | **Cancel Active Packet Splitter Job** | `0x0040E60A` | `0x0043FCF8` | — | `[0x14][job_id:u32_le]` (5 bytes; cancels active job with flag `1` without reply) |
| **`0x15`** | **Create Batch Zero-Length Files** | `0x0040E7B3` | `0x0043FB55` | None (on success); `0xA9` (on error) | `[0x15][req_id:u32_le][item_count:u16_le][records: (item_id:u32_le, path_len:u16_le, path:wchar_t)...]` (no reply on success; sends `0xA9` via `0x004417A0` on error) |
| **`0x16`** | **Extended Batch Upload Pre-Check** | `0x0043F5F3` | `0x0043F5F3` | `0xAA` | `[0x16][req_id:u32_le][total_size:u64_le][flags:u16_le][item_count:u16_le][records: (item_id:u32_le, path_len:u16_le, path:wchar_t)...]` (Section 4.2.7) |
| **`0x17`** | **UI Notification Command** | `0x0040F6B1` | `0x0043D2E8` | — | `[0x17][code:int8]` (2 bytes; posts Win32 message `0x0FC0` with `wParam = (int8)code`, `lParam = 0`) |
| **`0x32`** | **Keep-Alive / No-Op Ack 0x32** | — | `0x0043D2C1` | — | `[0x32]` (1 byte; immediate jump to exit `0x0043D42A`) |
| **`0x33`** | **Keep-Alive / No-Op Ack 0x33** | — | `0x0043D374` | — | `[0x33]` (1 byte; immediate jump to exit `0x0043D42A`) |
| **`0x34`** | **Dynamic Library Plugin RPC XML Tunnel (`<OTIMSG>`)** | `0x0040EFDF` | `0x0043F13D` | `0x34` | `[0x34][dll_name_len:u8][dll_name:bytes][xml_len:u32_le][xml_bytes]` (Section 7.1) |
| **`0x35`** | **Multi-ID Batch Download Request (16-bit Count)** | `0x0040DF00` | `0x0043EB3F` | `0xA3`, `0xA6`, `0xA7`, `0xAB` | `[0x35][flags_or_cp:u16_le][req_id:u32_le][has_extra:u8][item_count:u16_le][records: (item_id:u32_le, path_len:u16_le, path:wchar_t)...]` (delegates to `0x36` if `item_count > 0xFFFF`) |
| **`0x36`** | **Multi-ID Batch Download Request (32-bit Count)** | `0x0040E05D` | `0x0043EE3E` | `0xA3`, `0xA6`, `0xA7`, `0xAB` | `[0x36][flags_or_cp:u16_le][req_id:u32_le][has_extra:u8][item_count:u32_le][records: (item_id:u32_le, path_len:u16_le, path:wchar_t)...]` |
| **`0x37`** | **Set Remote File Attributes and Timestamps** | `0x0040F511` | `0x00441A47` | — | `[0x37][attributes:u32_le][ftCreationTime:8B][ftLastAccessTime:8B][ftLastWriteTime:8B][path_len:u32_le][path:wchar_t]` (fire-and-forget; no reply) |
| **`0x38`** | **Format Win32 Error Codes Request** | — | `0x00441B9F` | `0xAC` | `[0x38][count:u32_le][codes:u32_le[count]]` (replies `0xAC` via `0x00441CC4`) |
| **`0x39`** | **Extra XML Control Command (`<ExtraXmlCommand>`)** | `0x0040F635` | `0x00441DB1` | `0x39` / Mode Switch | `[0x39][xml_len:u32_le][xml_bytes]` (Section 7.2) |

---

### 4.2 Detailed Client Command Payload Specifications

#### 4.2.1 `0x01` — Query Peer Root / Drives (`0x0040D9C9` / `0x0043D479`)
Queries the remote peer's logical drives and volumes while exchanging host identification and code page. Constructed by the client sender at `0x0040D9C9` (formatting at `0x0040D9F8`) and handled by `0x0043D479`:

```text
Offset  Size        Type          Field
+0x00   1           uint8         opcode = 0x01
+0x01   1           uint8         flags (Platform / OS flags; e.g. 0x07 for WinXP, bit 4: 0x10 = Mac mode)
+0x02   2           uint16_le     code_page (Initiator Windows ANSI Code Page, e.g. 0x04E4 = 1252)
+0x04   1           uint8         name_len (N, length of computer_name string)
+0x05   N           bytes         computer_name (ASCII NetBIOS computer name of initiator, e.g. "TXLIN1")
```

- **Host Identification Framing:** As verified in binary captures (`dumps/usb-capture-01.pcap` through `08.pcap`), the root query is 11 bytes total:
  `[0x01][flags:u8][code_page:u16_le][name_len:u8][computer_name:bytes]` (e.g. `01 07 e4 04 06 54 58 4c 49 4e 31`).
- **Server Behavior (`0x0043D479`):**
  1. Reads `flags` (`edi[0]`) and `code_page` (`*(u16*)&edi[1]`). If `flags & 0x10 != 0`, sets `bMac = 1`.
  2. Reads `name_len` (`edi[3]`) and `computer_name`.
  3. Sends Server Reply `0xA1` (24-byte structured ACK, `req_opcode = 0x01`, `status = 0`, `req_id = 0`, `win32_error = 0`).
  4. Calls `0x00440FDC` to enumerate logical drives (`GetLogicalDriveStringsA` / `GetDriveTypeA`), which formats and sends Server Reply **`0xA4`** (**Remote Logical Drive List**).
- **Critical Implementation Pitfall:**
  > [!WARNING]
  > Opcode `0x01` is **not a 1-byte command**. Sending only 1 byte (`b"\x01"`) results in an immediate buffer underrun: the handler reads `flags`, `code_page`, and `name_len` out-of-bounds from whatever uninitialized bytes follow the buffer in memory, corrupting state and causing communication desynchronization.

#### 4.2.2 `0x02` — Notify / Set Active Remote Directory (`0x0040DA90` / `0x0043D577`)

Notifies the peer host of the currently selected or active local directory path (folder-change notification). Constructed by the client sender at `0x0040DA90` (called when navigating folders, passing active directory CString at `0x0041DD7D`) and handled by the peer at `0x0043D577`:

```text
Offset  Size        Type          Field
+0x00   1           uint8         opcode = 0x02
+0x01   1           uint8         path_len (N)
+0x02   N           bytes         dir_path (ASCII/MBCS active directory path, without NUL)
```

- **Framing Specification:**
    - Length prefix `path_len` is strictly a single byte (`uint8`), **not** a 4-byte `uint32_le` or 2-byte `uint16_le`.
    - The path string starts immediately at offset `+0x02`.
- **Server Behavior (`0x0043D577`):**
  1. Reads `path_len` (`cl = byte ptr [eax]`, zero-extending into `edi`).
  2. Copies `path_len` bytes into a buffer via `memcpy(buf, eax, edi)`.
  3. At `0x0043D5D7`, compares the path string against the active directory stored at `[esi + 0x20]` using `MSVCRT!_mbscmp`.
  4. If changed, calls `0x0041DD84` (`0x0043D60E`), posting internal Win32 UI message `0xFBD` via `PostMessageA` to update the GUI active navigation state.
  5. At `0x0043D5F6` / `0x0043D622`, calls `0x0043FFF9` to format and transmit Server Reply **`0xA1`** (24-byte structured ACK, `req_opcode = 0x02`, `status = 0`, `req_id = 0`, `win32_error = 0`).
- **Clarification:**
  > [!NOTE]
  > Opcode `0x02` is strictly a notification command; it does **not** call `FindFirstFileA` or enumerate directory contents. Directory listing and enumeration is handled separately by Opcode `0x04` (`0x0040DB44` / `0x0043D741`) or refreshed via Opcode `0x03` (`0x0043D64D`), which return 552-byte structures via Server Reply **`0xA2`**.

#### 4.2.3 `0x04` — Change / List Remote Directory (`0x0040DB30` / `0x0043D741`)
Requests the remote host to change to the target directory and return its full directory listing. Constructed at `0x0040DB30` (called by UI navigation at `0x00435E8E` $\rightarrow$ `0x00435FE7`) and handled at `0x0043D741`:

```text
Offset  Size        Type          Field
+0x00   1           uint8         opcode = 0x04
+0x01   2           uint16_le     path_len (P, byte length of dir_path)
+0x03   P           bytes         dir_path (UTF-16LE wchar_t target directory path on peer, e.g. "C:\\")
```

- **Client Behavior (`0x00435E8E` / `0x0040DB30`):**
  1. `0x00435E8E` takes `(display_name, remote_path)` for the selected item. If `display_name` is empty (`[eax - 8] == 0` at `0x00435EA9`), it aborts immediately (`0x0043659B`) **without sending `0x04`**.
  2. `0x00435F5B..0x00435F7C` checks whether the last `wchar_t` of `remote_path` equals `0x005C` (`L'\\'`); if not, it appends `L'\\'`.
  3. `0x0040DB30` only calls `0x00402B88` (`\` $\rightarrow$ `/`) when `[0x00480000] == 0x10` (Mac mode). In `[0x00480000] == 0x20` (POSIX mode), `0x0040DB67` branches to `0x0040DBC6` and transmits `remote_path` verbatim—meaning a POSIX root path such as `"/"` is sent on the wire as `"/\\"`. POSIX servers must accept/strip trailing `\` (or treat `"/\\"` as `"/"`).

- **Server Behavior (`0x0043D741`):**
  1. Reads `path_len` as a 16-bit little-endian integer (`dx = word ptr [ecx]`).
  2. Copies `path_len` bytes from offset `+0x03` as UTF-16LE wchar_t.
  3. Appends wildcard (`\*.*`) and calls directory enumeration routines (`FindFirstFileW` / `FindNextFileW` via `0x004401AD`).
  4. If `FindFirstFileW` succeeds, transmits Server Reply **`0xA1`** (24-byte structured ACK, `req_opcode = 0x04`, `status = 0`, `req_id = 0`, `win32_error = 0`).
  5. Formats directory entries into fixed-size 552-byte structures (`0x00440C2D` / `0x00440C66`) and transmits Server Reply **`0xA2`** (**Directory Listing Reply**; Section 6).
  6. If `FindFirstFileW` fails (e.g. empty optical/removable drive returning `21` `ERROR_NOT_READY`, path not found, or access denied), transmits Server Reply **`0xA1`** (`status = 1`, `win32_error = GetLastError()`) and **terminates processing without sending `0xA2`**. Clients must inspect `status` in `0xA1` and abort waiting for `0xA2` if `status != 0`.

#### 4.2.4 `0x0A` — Single File Upload Request (`0x0040E2A7` / `0x0043DF17`)
Initiates the upload of a single file along with its destination path and target size. The client transmits a 16-byte fixed header followed by the UTF-16LE destination path:

```text
Offset  Size        Type          Field
+0x00   1           uint8         opcode = 0x0A
+0x01   1           uint8         flags (0x00 = normal / prompt on conflict, 0x01 = overwrite)
+0x02   4           uint32_le     req_id (Client transaction sequence ID)
+0x06   8           uint64_le     file_size (Contiguous 64-bit integer: low 32 bits at +0x06, high 32 bits at +0x0A)
+0x0E   2           uint16_le     dest_path_len (N, byte length of dest_path in UTF-16LE, i.e. 2 * wchar count)
+0x10   N           wchar_t[]     dest_path (UTF-16LE wchar_t destination path INCLUDING target filename)
```

- **Wire Layout Details:**
  Total packet length is `16 + dest_path_len` bytes. There is **no** embedded `WIN32_FIND_DATAA` structure in this command.
- **Server Behavior (`0x0043DF17`):**
  1. Unpacks `flags` (`payload[0]`), `req_id` (`payload[1..4]`), `file_size` (`payload[5..12]`), `dest_path_len` (`payload[13..14]`), and copies `dest_path` starting at `payload[15]` (`+0x10` from packet start).
  2. Converts `dest_path` to local character encoding via `WideCharToMultiByte` and normalizes separators.
  3. Inspects destination file state via `GetFileAttributesEx`:
        + If the destination path already exists and `flags == 0` (no overwrite), immediately transmits Server Reply **`0xA1`** (`req_opcode = 0x0A`, `status = 2`, `req_id = req_id`) and terminates transfer initiation.
  4. Checks target volume available free space via `GetDiskFreeSpaceEx`:
        + If available free bytes are less than `file_size`, immediately transmits Server Reply **`0xA1`** (`req_opcode = 0x0A`, `status = 6`, `req_id = req_id`) and aborts transfer.
  5. If `file_size == 0` (zero-length file), creates the file directly, applies empty sizing, and transmits Server Reply **`0xA1`** (`req_opcode = 0x0A`, `status = 7`, `req_id = req_id`). The file transfer is complete; no `0x0C` chunks are required.
  6. If `file_size > 0`, creates or opens the destination file handle for writing (`CFile::Open` with `modeCreate | modeWrite`):
        + If creation fails (e.g. invalid path, path is a directory, access denied), transmits Server Reply **`0xA1`** (`req_opcode = 0x0A`, `status = 1`, `win32_error = GetLastError()`).
        + If creation succeeds, transmits Server Reply **`0xA1`** (`req_opcode = 0x0A`, `status = 0`, `req_id = req_id`) and registers the open file context to await incoming streaming chunks via opcode **`0x0C`** (`Upload File Data Chunk`).

> [!CAUTION]
> **Stack Buffer Overflow in EasySuite (`0x0043DF17`):**
> In `EasySuite.exe`, `0x0043DF17` unpacks `dest_path` into a fixed 1024-byte (`0x400`) stack buffer using an unbounded `memcpy` of `dest_path_len` bytes.
> Transmitting an oversized packet (such as bundling file contents directly into `0x0A`) or sending a corrupted packet where `dest_path_len > 1024` overflows the stack buffer and corrupts the SEH exception handler, instantly crashing `EasySuite.exe` with an Access Violation (`0xC0000005`). Because the server process terminates, no `0xA1` reply is sent, and the OTi-2108 link falls idle (`action_code = 1`). Clean-room implementations MUST ensure `dest_path_len` does not exceed 1024 bytes (512 WCHARs).
>
> In addition, opcode `0x0A` requires the full destination file path including the target filename (e.g. `C:\Windows\Temp\README.md`). Supplying only a directory path (e.g. `C:\Windows\Temp\`) causes `CFile::Open` to fail with Win32 error `5` (`ERROR_ACCESS_DENIED`), resulting in `0xA1` failure (`status = 1`).

#### 4.2.5 `0x0B` — Single File Download Request (`0x0040DBF4` / `0x0043E4DA`)
Requests the remote peer to transmit a single file back to the initiator:

```text
Offset  Size        Type          Field
+0x00   1           uint8         opcode = 0x0B
+0x01   4           uint32_le     req_id (Transaction sequence ID)
+0x05   2           uint16_le     src_path_len (N, byte length of src_path)
+0x07   N           bytes         src_path (UTF-16LE wchar_t source path on peer)
+0x07+N 2           uint16_le     dest_path_len (M, byte length of dest_path; 0 if omitted)
+0x09+N M           bytes         dest_path (UTF-16LE wchar_t destination path on client)
```

- **Wire Layout Characteristics:**
    - Base header and length fields comprise exactly 9 bytes (`1 + 4 + 2 + 2`).
    - Total packet length is `9 + src_path_len + dest_path_len` bytes.
    - `src_path`: full remote path to the file to be read (e.g. `C:\MSDOS.SYS`), UTF-16LE encoded without null terminator.
    - `dest_path_len`: byte count of the optional client destination path string. If omitted, `dest_path_len` MUST be set to `0x0000` (2 bytes); omitting this field causes offset desynchronization and server crashes.

- **Server Behavior (`0x0043E4DA`):**
  1. Unpacks `req_id` (`+0x01`) and `src_path_len` (`+0x05`). Copies `src_path` (`+0x07`) into an internal stack buffer.
  2. Unpacks `dest_path_len` (`+0x07+N`). If `dest_path_len > 0`, copies `dest_path` into an internal buffer.
  3. Opens the requested source file via `CreateFileW(src_path, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, ...)`.
  4. If opening fails (e.g. file not found, access denied), immediately emits a 24-byte Server Reply **`0xA1`** (`req_opcode = 0x0B`, `status = 1`, `win32_error = GetLastError()`) and aborts transfer.
  5. If opening succeeds, queries 64-bit file size and dispatches according to file length:
        + **Mode 1: Zero-Length File (`file_size == 0`):**
          Emits a 24-byte Server Reply **`0xA1`** (`req_opcode = 0x0B`, `status = 7`, `req_id = req_id`). The client touches/creates a 0-byte local file and completes the transfer immediately without any streaming chunks.
        + **Mode 2: Small File (`file_size <= 64,512` bytes / `0xFC00`):**
          Reads up to 64,512 bytes into memory and emits a single inline Server Reply **`0xA1`** of size `24 + file_size` bytes (`req_opcode = 0x0B`, `status = 9`, `req_id = req_id`). The raw file payload starts directly at offset `+0x18`:

          ```text
          Offset  Size        Type          Field
          +0x00   1           uint8         opcode = 0xA1
          +0x01   1           uint8         req_opcode = 0x0B
          +0x02   1           uint8         status = 9 (Inline Small File Delivery)
          +0x03   4           uint32_le     req_id
          +0x07   4           uint32_le     win32_error = 0
          +0x0B   4           uint32_le     file_size_low
          +0x0F   4           uint32_le     file_size_high
          +0x13   1           uint8         flags = 2
          +0x14   4           uint32_le     data_len (equal to file_size_low)
          +0x18   data_len    bytes         file_data (Raw file contents)
          ```

          The client directly writes the embedded `file_data` to disk and completes the transfer. No `0xA3` chunks are transmitted.
        + **Mode 3: Large File (`file_size > 64,512` bytes):**
          Emits an initial 24-byte Server Reply **`0xA1`** (`req_opcode = 0x0B`, `status = 0`, `req_id = req_id`, `file_size_low`, `file_size_high`), then streams the file content in chunks using Server Reply **`0xA3`** (**File Download Data Chunk**; Section 5.1).

> [!CAUTION]
> **Stack Buffer Overflow in EasySuite (`0x0043E4DA`):**
> In `EasySuite.exe`, `0x0043E4DA` copies `src_path` into a fixed 1024-byte (`0x400`) stack buffer using an unbounded `memcpy` of `src_path_len` bytes at `0x0043E566`.
> Transmitting a malformed packet where path length fields are misaligned (such as omitting length fields or sending ASCII characters that land in the length offset, e.g. `'C:'` = `0x3A43` = 14,915 bytes) overflows the stack buffer and corrupts the SEH exception handler at `fs:[0]`, instantly crashing `EasySuite.exe` with an Access Violation (`0xC0000005`). Because the server process dies, no reply is ever sent, and the OTi-2108 link falls idle. Clean-room implementations MUST ensure `src_path_len` and `dest_path_len` do not exceed 1024 bytes.
>
> Even if a client does not supply a local destination path string, the 2-byte `dest_path_len` field (`0x0000`) **MUST** be present immediately after `src_path`. `EasySuite.exe` unconditionally reads `dest_path_len` from `[esi + src_path_len]`. Omitting this field shifts subsequent reads and leads to memory corruption.

#### 4.2.6 `0x0C` — Upload File Data Chunk (`0x004450C1` / `0x00440E41`)
Streams a chunk of binary data for an in-flight file upload:

```text
Offset  Size        Type          Field
+0x00   1           uint8         opcode = 0x0C
+0x01   4           uint32_le     item_id (Transfer item identifier)
+0x05   4           uint32_le     total_chunks (Total chunks in file)
+0x09   4           uint32_le     chunk_index (1-based chunk sequence index)
+0x0D   4           uint32_le     chunk_len (C, byte length of chunk payload)
+0x11   C           bytes         chunk_data (Binary file slice)
```

- **Streaming Transmission Model:**
  1. File chunks are transmitted **unacknowledged** during normal transfer. The client pumps chunks continuously across bulk blocks without synchronously awaiting a per-chunk reply.
  2. The maximum chunk payload size per bulk block is $65,536 - 20 - 17 = 65,499$ bytes (or up to `0xFFEC` = 65,516 bytes when spanning multiple transport blocks).
- **Server Behavior (`0x00440E41`):**
  1. Checks `chunk_index == 1` (offset `+0x09`) to decide file open mode in `0x00441195`: when `chunk_index == 1`, creates/truncates the destination file (`CREATE_ALWAYS` / `modeCreate`); when `chunk_index != 1`, opens the existing file (`OPEN_EXISTING` / `modeWrite` without `modeCreate`) and seeks to `FILE_END`. (On NT/2000/XP+ via `0x0044137C`, if `CreateFileW(..., OPEN_EXISTING)` returns `INVALID_HANDLE_VALUE` at `0x004415C2`, it skips `WriteFile` and returns `0` without calling `GetLastError()`, so no `0xA5` error is emitted.)
  2. Writes `chunk_len` bytes via `WriteFile` at sequential file offset.
  3. When `chunk_index == total_chunks`, automatically flushes/closes the file handle and removes `item_id` from `0x7adce0` (`0x00443AEA`).
  4. **Success Path:** If the write succeeds (`win32_error == 0`), the server returns immediately without sending any acknowledgment reply.
  5. **Error Notification:** If a write error occurs (such as disk full or I/O failure):
        + In single-file upload mode: sends 9-byte Server Reply **`0xA5`** (`[0xA5][item_id:u32_le][win32_error:u32_le]`).
        + In batch upload mode: sends Server Reply **`0xA9`** (**Batch Upload Error Notification**).
- **Transfer Finalization:**
  After transmitting all `0x0C` chunks, the client issues Opcode **`0x0E`** (`Acknowledge File Transfer Completion`, 7 bytes) to commit file timestamps and attributes. Opcode `0x0E` also emits no reply.

#### 4.2.7 `0x16` — Extended Batch Upload Pre-Check (`0x0043F5F3`)
Performs a pre-transfer space and metadata collision check for a batch of files against the peer's existing files before upload (extended counterpart of `0x13`):

```text
Offset  Size        Type          Field
+0x00   1           uint8         opcode = 0x16
+0x01   4           uint32_le     req_id (Client transaction sequence ID)
+0x05   8           uint64_le     total_size (Total byte count of batch)
+0x0D   2           uint16_le     flags (Pre-check option flags)
+0x0F   2           uint16_le     item_count (K)
+0x11   ...         records[K]    Back-to-back file pre-check records
```

Each of the `K` records has the layout:

```text
Record Offset  Size        Type          Field
+0x00          4           uint32_le     item_id (Batch item identifier)
+0x04          2           uint16_le     path_len (P, byte length of UTF-16LE path)
+0x06          P           wchar_t       dest_path (UTF-16LE destination file path on peer)
```

- **Server Behavior (`0x0043F5F3`):**
  1. Dispatched from Client Command Dispatcher `0x0043D181` via `0x0043D309..0x0043D310`.
  2. Reads `req_id`, `total_size`, `flags`, and iterates through all `item_count` records.
  3. Checks volume preconditions via `0x00402884` (`space_ok`, `ntfs_ok`, `is_readonly`) and tests each `dest_path` with `GetFileAttributesExA`/`GetFileAttributesExW`:
        + Existing files (`GetFileAttributesEx` succeeds) are added to `conflict_item_ids`.
        + Non-existence (`GetLastError() == ERROR_FILE_NOT_FOUND (2)`) is ignored.
        + Any other Win32 error (e.g., `ERROR_PATH_NOT_FOUND (3)`) is recorded as a `(win32_error:u32_le, item_id:u32_le)` pair in `error_pairs`.
  4. Generates Server Reply **`0xAA`** (**Extended Batch Pre-Check Reply** at `0x0044196E`) containing the volume flags, colliding `item_id`s, and `(win32_error, item_id)` error pairs (subject to the 4-byte wire truncation bug documented in Section 5.3).
- **Critical Opcode 0x16 Crash Pitfalls:**
  > [!CAUTION]
  > 1. **Heap Buffer Overflow in `0xAA` Reply Construction (`0x0044196E`):** Whenever `EasySuite.exe` handles `0x16`, `0x0044196E` allocates `13 + 4 * conflict_count + 8 * error_count + 1` bytes and writes `17 + 4 * conflict_count + 8 * error_count` bytes, corrupting its own Windows heap before the reply is sent (Section 5.3). Clean-room clients **must use `0x13` (`0xA8` reply)** instead of `0x16` to avoid crashing the remote `EasySuite.exe` peer.
  > 2. **Not an XML Channel:** Opcode `0x16` is **strictly a binary batch pre-check command**; it is **NOT** an XML channel. If a client transmits `<OTIMSG>` ASCII XML preceded by opcode `0x16`, `EasySuite.exe` interprets the ASCII bytes at `+0x0F` (`"SG"` / `"<O"`) as a large 16-bit `item_count`, reads out-of-bounds past the end of the packet, and crashes.

#### 4.2.8 `0x08` — Delete Remote File (`0x0040EC14` / `0x0043DBD1`)
Deletes a single file on the remote peer:

```text
Offset  Size        Type          Field
+0x00   1           uint8         opcode = 0x08
+0x01   4           uint32_le     req_id (Client transaction sequence identifier)
+0x05   2           uint16_le     path_len (P, byte count of UTF-16LE path: 2 * wchar count)
+0x07   P           wchar_t       file_path (Full remote file path, UTF-16LE encoded)
```

- **Server Behavior (`0x0043DBD1`):**
  1. Unpacks `req_id` (`[ebx]`) and `path_len` (`[ebx + 4]`).
  2. Copies `path_len` bytes into a 1,024-byte stack buffer via `memcpy`.
  3. Invokes `DeleteFileW` on the target path.
  4. Returns standard 24-byte Server Reply **`0xA1`** (`req_opcode = 0x08`, `req_id` echoed):
        + `status = 0`, `win32_error = 0`: File successfully deleted.
        + `status = 1`, `win32_error = GetLastError()`: Deletion failed (e.g. `ERROR_FILE_NOT_FOUND` = 2, `ERROR_ACCESS_DENIED` = 5).
- **Critical Opcode 0x08 Crash Pitfall:**
  > [!CAUTION]
  > Opcode `0x08` requires the 2-byte `path_len` field and UTF-16LE encoding.
  > If a client sends an unpadded ASCII path or omits `path_len` (e.g. sending 18 bytes: `[0x08][req_id:4B]["C:\REUSE.toml":13B]`), the server interprets the first two ASCII characters (`'C:'` = `0x3A43` = 14,915 bytes) as `path_len`.
  > `memcpy` immediately overflows the server's 1,024-byte stack buffer and corrupts the SEH frame, causing an unhandled Access Violation (`0xC0000005`) that crashes `EasySuite.exe`. The bridge controller drops to `action_code = 1` (peer disconnected), all subsequent writes fail with timeout errors, and the client hangs waiting for a reply.

#### 4.2.9 `0x12` — Batch Prepare / Clear Read-Only Attributes (`0x0040E628` / `0x0043F981`)
Clears read-only/hidden/system attributes on existing destination items and registers batch upload items in `0x7adce0` prior to streaming `0x0C` chunks:

```text
Offset  Size        Type          Field
+0x00   1           uint8         opcode = 0x12
+0x01   4           uint32_le     req_id (Client transaction sequence ID)
+0x05   2           uint16_le     item_count (K)
+0x07   ...         records[K]    Back-to-back item records: (item_id:u32_le, path_len:u16_le, path:wchar_t)
```

- **Fire-and-Forget Semantics (No Reply):**
  1. **Server Behavior (`0x0043F981`):** For each of the `item_count` records, converts `path` to Windows separators, queries `GetFileAttributesExW` / `GetFileAttributesA`, and if `(attrs != -1)` and `(attrs & 0x07) != 0` (`READONLY | HIDDEN | SYSTEM`), clears those three bits (`and al, 0xf8`) via `SetFileAttributesW` / `SetFileAttributesA`. It then calls `0x0044367D` on `0x7adce0` to register the item and returns immediately **without sending `0xA1` or any other reply**.
  2. **Client Behavior (`0x004395F6`):** `EasySuite.exe` transmits `0x12` via `0x0040E628` and immediately proceeds to `0x00445474` to stream `0x0C` file data chunks without awaiting a reply. Clean-room clients **must not** block waiting for `0xA1` after sending `0x12`.

---

## 5. Complete Server Reply Catalog (`0xA1..0xAC`)

Server Replies are sent by `0x0043FD2D..0x00441DA8` in response to Client Commands and dispatched on the initiating host by `0x0040B93F` (which computes `idx = opcode - 0xA1` and uses the 11-entry jump table at `0x0040BA6A` plus `0x0040BA16` for `0xAC`).

### 5.1 Master Server Reply Opcode Table

| Opcode | Name | Server Sender VA | Client Handler VA | Payload Format (after `opcode` byte at `+0x00`) |
| :---: | :--- | :--- | :--- | :--- |
| **`0xA1`** | **Command Status / ACK / Inline Data** | `0x0043FD2D` | `0x0040BA96` | `[0xA1][req_opcode:u8][status:u8][req_id:u32_le][win32_error:u32_le][reserved:13B][optional_data:bytes]`:<br/>- Standard 24-byte ACK (`status = 0` success, `1` error, `2` conflict, `6` disk full, `7` empty file).<br/>- Small File Inline Delivery (`status = 9`, `file_size <= 64,512` bytes): 24-byte header followed by raw file payload starting at offset `+0x18`, total packet size `24 + file_size` (Section 4.2.5). |
| **`0xA2`** | **Directory Listing Reply** | `0x00440C2D` | `0x0040C251` | `[0xA2][entry_count:u16_le][entries: Entry552[entry_count]]` (`3 + 552 * entry_count` bytes; see Section 6) |
| **`0xA3`** | **File Download Data Chunk** | `0x004450C1` | `0x0040CF68` | `[0xA3][item_id:u32_le][total_chunks:u32_le][chunk_index:u32_le][data_len:u32_le][file_data:bytes]` (Streaming chunks for files > 64,512 bytes via `0x0B` or batch downloads via `0x10`) |
| **`0xA4`** | **Remote Logical Drives List Reply** | `0x00440FDC` | `0x0040F093` | `[0xA4][drive_count:u16_le][records: (drive_letter:char, drive_type:u8)...]` (plus `[len1:u16][display_name:wchar_t[]][len2:u16][root_path:wchar_t[]]` per record when `drive_type` sets `0x10`/`0x20` mode; Section 5.2) |
| **`0xA5`** | **Single-File Upload Error Notification** | `0x0043FE85` | `0x0040D7D2` | `[0xA5][item_id:u32_le][win32_error:u32_le]` (9 bytes total; emitted only when a file chunk write fails during `0x0C`; Section 4.2.6) |
| **`0xA6`** | **Batch Zero-Length Files / Empty Dirs** | `0x00441602` | `0x0040CFEF` | `[0xA6][req_id:u32_le][item_count:u16_le][item_ids:u32_le[item_count]]` (`7 + 4 * item_count` bytes; batch items with 0 data bytes) |
| **`0xA7`** | **Batch Download Complete ACK** | `0x0044175A` | `0x0040D210` | `[0xA7][req_id:u32_le][status:u16_le]` (7 bytes total; signals end of `0x10`/`0x35`/`0x36` batch download) |
| **`0xA8`** | **Batch Upload Pre-Check Reply** | `0x004418C9` | `0x0040CC41` | `[0xA8][peer_error:u8][space_ok:u8][ntfs_ok:u8][is_readonly:u8][req_id:u32_le][conflict_count:u32_le][conflict_item_ids:u32_le[conflict_count]]` (`13 + 4 * conflict_count` bytes; response to `0x13`; Section 5.3) |
| **`0xA9`** | **Batch Upload Error Notification** | `0x004417A0` | `0x0040D420` | `[0xA9][item_id:u32_le][win32_error:u32_le][status:u32_le]` (13 bytes in Windows mode; appends `"ErrStr"` + `err_str_len:u32_le` + `err_str:wchar_t[]` in `0x10` mode) |
| **`0xAA`** | **Extended Batch Pre-Check Reply** | `0x0044196E` | `0x0040CD5F` | `[0xAA][peer_error:u8][space_ok:u8][ntfs_ok:u8][is_readonly:u8][req_id:u32_le][conflict_count:u32_le][error_count:u32_le][conflict_item_ids:u32_le[conflict_count]][error_pairs: (win32_error:u32_le, item_id:u32_le)[error_count]]` (intended `17 + 4 * conflict_count + 8 * error_count` bytes; **`EasySuite.exe` transmits `13 + 4 * conflict_count + 8 * error_count` bytes due to a 4-byte length-calculation bug**; response to `0x16`; Section 5.3) |
| **`0xAB`** | **Batch Unreadable Files Notification** | `0x004416AE` | `0x0040D840` | `[0xAB][req_id:u32_le][failed_count:u16_le][item_ids:u32_le[failed_count]]` (`7 + 4 * failed_count` bytes; items that failed `FindFirstFile`/`CreateFile` during batch download) |
| **`0xAC`** | **Formatted Win32 Error Strings Reply** | `0x00441CC4` | `0x0040BA16` | `[0xAC][count:u32_le][records: (err_code:u32_le, msg_len:u32_le, formatted_msg:wchar_t)...]` (response to `0x38` via `FormatMessage`; `formatted_msg` is UTF-16LE) |

> [!CAUTION]
> **Opcode `0xA2` Protocol Trap:**
> Do **not** send opcode `0xA2` as an error reply. The peer dispatcher at `0x0040C251` treats byte `+0x01` as a 16-bit `entry_count` (`word ptr [edi]`) and allocates heap to unpack fixed 552-byte structures. Emitting a short error reply with opcode `0xA2` (such as `0xA2 [win32_err:u32]`) causes the peer to read 552 bytes out-of-bounds, fatally corrupting memory and terminating communication.

### 5.2 `0xA4` — Remote Logical Drives / Roots List Reply (`0x00440FDC` / `0x0040F093`)

Sent in response to Client Command `0x01` (**Query Peer Root / Drives**). The client handler at `0x0040F093` parses the payload into 12-byte drive records (`+0x00`: `drive_letter`, `+0x01`: `drive_type`, `+0x04`: `display_name` `wchar_t*`, `+0x08`: `root_path` `wchar_t*`), updates `[this + 0x70]`, and posts Win32 message `0x0FBA` (`0x0040F3CD`) to refresh the Remote Host UI pane (`0x004193BD` $\rightarrow$ `0x0042A580` $\rightarrow$ `0x0042A5DD` $\rightarrow$ `0x00435C77`).

#### Wire Framing by Peer Mode

1. **Default Windows Mode (`[0x00480000] == 0`, no `0x10`/`0x20` bit in `drive_type`):**

    ```text
    [0xA4][drive_count:u16_le][records: (drive_letter:u8, drive_type:u8)[drive_count]]
    ```

    - `drive_type = GetDriveTypeA - 1` (`1`=Removable, `2`=Fixed / Hard Disk, `3`=Remote, `4`=CDROM, `5`=RAMDisk).
    - `0x00435C77` formats the UI display name (`[item + 0x08]`) as `"%s:"` (e.g. `"C:"`) and the wire path (`[item + 0x0c]`) as `L"C:"`.

2. **Mac (`0x10`) and Little-Endian POSIX/Unix (`0x20`) Modes (`0x0040F0DE..0x0040F2F1`):**
    - While reading the `drive_count` 2-byte `(drive_letter, drive_type)` headers, `0x0040F093` inspects `drive_type`:
        + If `drive_type & 0x10 != 0`: sets `[0x00480000] = 0x10`.
        + If `drive_type & 0x20 != 0`: sets `[0x00480000] = 0x20` (e.g. `0x22` = `0x20 | 2` for a POSIX Fixed/Hard Disk root).
        + Masks `drive_type &= 0x0F`.
    - **Immediately after** the `2 * drive_count` header bytes, `0x0040F093` reads **two length-prefixed UTF-16 strings for each of the `drive_count` records** (`0x0040F155` for `0x10` using `u16_be` byte lengths + UTF-16BE byte-swapped via `0x00402AC6`; `0x0040F239` for `0x20` using `u16_le` byte lengths + UTF-16LE):

        ```text
        [0xA4][drive_count:u16_le]
        [(drive_letter:u8, drive_type:u8) * drive_count]
        [(name_byte_len:u16, display_name:wchar_t[], path_byte_len:u16, root_path:wchar_t[]) * drive_count]
        ```

    - In `0x20` mode, `0x0040F34E` checks `[0x0045CB00] != 0` (statically `1` in `EasySuite.exe`) before committing the list.
    - `0x00435C77` (`0x00435DAF..0x00435E45`) then **overwrites** `[item + 0x08]` (the UI "Name" column, converted to ANSI `CString` via `0x00447CD2`) with `display_name` (`record + 0x04`) and `[item + 0x0c]` (the `wchar_t*` path passed to `0x04`) with `root_path` (`record + 0x08`).

> [!IMPORTANT]
> **Why Omitting Trailing Strings in `0x20` Mode Renders an Unnamed Hard Disk and Stalls:**
> If a peer sends `drive_type = 0x22` (`0x20 | 2`) without the two trailing UTF-16LE strings, `0x0040F239` reads past the 5-byte payload into zero-filled buffer memory (`name_byte_len = 0`, `path_byte_len = 0`), setting `display_name = L""` and `root_path = L""`. `0x00435C77` still maps `drive_type & 0x0F = 2` to `DRIVE_FIXED` (`Type = Hard Disk`), but overwrites the item's Name with `""`. Because `0x00435EA9` aborts without sending `0x04` whenever `display_name` is empty, `EasySuite.exe` displays `My Computer` $\rightarrow$ unnamed `Hard Disk` and never issues `0x04`.

### 5.3 `0xA8` / `0xAA` — Batch Upload Pre-Check Reply Flags & Wire Truncation Quirk

Both `0xA8` (sent by `0x004418C9`, handled at `0x0040CC41` via `0x0040B9FA`) and `0xAA` (sent by `0x0044196E`, handled at `0x0040CD5F` via `0x0040BA08`) populate their four status bytes (`+0x01..+0x04`) identically using the volume precondition helper `0x00402884`:

| Offset | Field | Polarity | Producer (`0x00402884` / `0x004418C9` / `0x0044196E`) | Client Consumer (`0x0040CC41` / `0x0040CD5F`) |
| :---: | :--- | :--- | :--- | :--- |
| `+0x01` | `peer_error` (`flag0`) | Active-high error | `1` if `[0x004800C4] != 0`, else `0`. | If `1`, aborts transfer via `0x00402139(5)`. |
| `+0x02` | `space_ok` (`flag1`) | **Active-low error** | Calls `GetDiskFreeSpaceExA("%c:\\")`. `1` if call succeeds and `total_size < free_bytes_avail`; `0` otherwise. | If **`0`**, sets `[0x0045F530] = [0x0045F534] = 0x70` (`ERROR_DISK_FULL`) and calls `0x00402139(1)`. |
| `+0x03` | `ntfs_ok` (`flag2`) | **Active-low error** | Initialized to `1`. If `0x13`/`0x16` `flags != 0`, calls `GetVolumeInformationA("%c:\\")` and sets `0` if filesystem name `!= "NTFS"`. | If **`0`**, sets `[0x0045F534] = 0`, `[0x0045F530] = 0x13` (`ERROR_WRITE_PROTECT`) and calls `0x00402139(1)`. |
| `+0x04` | `is_readonly` (`flag3`) | Active-high error | Calls `GetFileAttributesA("%c:\\")` and stores `attrs & 1` (`FILE_ATTRIBUTE_READONLY`; also `1` if `INVALID_FILE_ATTRIBUTES`). | If `1`, sets `[0x0045F530] = [0x0045F534] = 0x13` (`ERROR_WRITE_PROTECT`) and calls `0x00402139(1)`. |

#### `0xAA` `error_pairs` and `EasySuite.exe` 4-Byte Wire Truncation Bug (`0x00441987`)

- **Per-Item Error Pairs:** In `0x0043F5F3` (`0x0043F801..0x0043F830`), when `GetFileAttributesExA`/`GetFileAttributesExW` fails with `err = GetLastError()` and `err != ERROR_FILE_NOT_FOUND (2)`, the server appends an 8-byte `(win32_error:u32_le, item_id:u32_le)` entry to `error_pairs` (`error_count` at `+0x0D`).
- **4-Byte Truncation Bug in `0x0044196E`:** At `0x00441984..0x0044198E`, `EasySuite.exe` computes the allocation and transmit byte length as:
  ```text
  lea eax, [ecx + eax*2]        ; ecx = conflict_count, eax = error_count
  lea eax, [eax*4 + 0xd]        ; 13 + 4 * conflict_count + 8 * error_count  (BUG: should be + 0x11 = 17)
  ```
  Although `0x004419AA..0x00441A28` writes `17 + 4 * conflict_count + 8 * error_count` bytes (overflowing the `size + 1` heap allocation by 3 bytes), `0x00441A2A` passes the `13 + ...` length to `0x0043FD2D`, **truncating the final 4 bytes on the wire**:
  - For example, in `openeasysync-09.log` line 33 (`conflict_count = 0`, `error_count = 1`), `EasySuite.exe` sends `21` bytes (`aa 00 01 01 00 03000000 00000000 01000000 03000000`) instead of `25`, containing `win32_error = 3` (`ERROR_PATH_NOT_FOUND`) with its trailing `item_id` truncated.
  - **Clean-room client guidance:**
    + **Preventing the `EasySuite.exe` crash (use `0x13` $\rightarrow$ `0xA8` instead of `0x16`):** Because `0x0044196E` allocates `13 + 4 * conflict_count + 8 * error_count + 1` bytes and writes `17 + 4 * conflict_count + 8 * error_count` bytes while constructing the `0xAA` reply *before* the packet is ever sent over USB, client-side `0xAA` parsing cannot prevent the Windows heap corruption in `EasySuite.exe`. Clean-room clients must avoid triggering `0x0044196E` by sending **`0x13`** (`0xA8` reply) instead of `0x16` (`0xA8` still provides `peer_error`, `space_ok`, `ntfs_ok`, `is_readonly`, and `conflict_item_ids` without the heap overflow bug).
    + **Safe `0xAA` decoding (for any received `0xAA` packet):** Retain a truncation-tolerant `0xAA` parser so any `0xAA` packet received from `EasySuite.exe` is decoded safely without silently discarding a trailing 4-byte `win32_error`: when `len(payload)` is `13 + 4 * conflict_count + 8 * error_count` (or `offset + 8 > len(payload)` with 4 bytes remaining), parse the final `win32_error:u32_le` with `item_id = None`, while also supporting full `17 + 4 * conflict_count + 8 * error_count` packets.

---

## 6. 552-Byte (`0x228`) Directory Entry Structure (`0xA2`)

Server Reply `0xA2` (`0x00440C2D`, handled at `0x0040C251`) returns an array of `entry_count` fixed-size **552-byte (`0x228`) structures** representing files and subdirectories (in response to `0x04` or `0x03`).

Framing:
`[0xA2][entry_count:uint16_le][entries: Entry552[entry_count]]` (Total length = `3 + 552 * entry_count` bytes).

```text
Offset   Size   Type          Field Name              Description
+0x000   4      uint32_le     dwFileAttributes        Win32 file attributes (0x10 = DIRECTORY, 0x20 = ARCHIVE, 0x06 = HIDDEN|SYSTEM)
+0x004   8      uint64_le     ftCreationTime          Win32 FILETIME creation timestamp
+0x00C   8      uint64_le     ftLastAccessTime        Win32 FILETIME last access timestamp
+0x014   8      uint64_le     ftLastWriteTime         Win32 FILETIME last modification timestamp
+0x01C   4      uint32_le     nFileSizeLow            Low 32 bits of file size (contiguous 8-byte uint64_le nFileSize at +0x01C)
+0x020   4      uint32_le     nFileSizeHigh           High 32 bits of file size
+0x024   4      uint32_le     cFileNameLength         Length of filename in bytes (2 * WCHAR count)
+0x028   520    wchar_t[260]  cFileName               UTF-16LE wide-character filename (zero-padded to 520 bytes / 260 WCHARs)
+0x228   ---    (End of 552-byte structure)
```

> [!NOTE]
> **Endianness and Offset of File Size:**
> In EasySuite's 552-byte directory entry layout, the 64-bit file size is stored contiguously in standard little-endian order starting at offset `+0x01C`: `nFileSizeLow` is at `+0x01C` and `nFileSizeHigh` is at `+0x020` (equivalent to an 8-byte `uint64_le` at `+0x01C`). This differs from the Win32 `WIN32_FIND_DATA` C header declaration where `nFileSizeHigh` precedes `nFileSizeLow`; treating `+0x01C` as `nFileSizeHigh` incorrectly shifts file sizes by $2^{32}$.

---

## 7. `<OTIMSG>` and `<ExtraXmlCommand>` XML Control Protocols

### 7.1 `<OTIMSG>` Dynamic Library Plugin RPC Protocol (Opcode `0x34`, `0x0043F13D` / `0x00420D71`)

> [!IMPORTANT]
> **No XML Handshake in Standard File Transfer:**
> Disassembly of `EasySuite.exe` reveals that standard file manager operations do **not** exchange an XML handshake upon link connection. As soon as both sides enter Transfer Line Mode (`0xD8 0x01` `op_type=5, param=0x007F`), both hosts immediately transition to the connected state (status bar `"Connected"`, ID `0xF026`) and issue Client Command **`0x01`** (**Query Peer Root / Drives**) using the 11-byte structure described in Section 4.2.1.
>
> Furthermore, Opcode **`0x16` is NOT an XML control channel**; it is the **Extended Batch Upload Pre-Check** command (`0x0043F5F3`, Section 4.2.4). Framing `<OTIMSG>` ASCII XML with opcode `0x16` causes `EasySuite.exe` to interpret the first 4 ASCII bytes `b"<OTI"` (`0x49544F3C` = ~1.23 billion) as a 32-bit count, resulting in an immediate crash in `ntdll!RtlFreeHeap`.

#### Architecture & RPC Dispatch
In `EasySuite.exe`, `<OTIMSG>` XML messages are used exclusively for inter-host Remote Procedure Calls (RPC) between dynamic library plugins (such as `ECN001.DLL` for Microsoft Outlook / PIM synchronization):
1. **Thread Origin:** The XML processing loop is executed exclusively by dedicated dynamic library plugin RPC threads spawned via `CreateThread` at `0x0040FA3C` (thread worker at `0x00434CDD`).
2. **Wire Framing:** RPC messages are tunneled across the bulk wire protocol using Client Command **`0x34`** (`0x0040EFDF` client sender, dispatched to `0x0043F13D` on the server).
3. **Parser (`0x00420D71`):** The XML parser searches for root tag `<OTIMSG>` and command tags `<DynamicLibCmd>` and `<TransferFile>`.

#### Recognized Tags & XML Schema

```xml
<OTIMSG>
    <DynamicLibCmd>COMMAND_NAME</DynamicLibCmd>
    <MultiID>...</MultiID>
    <SingleID>...</SingleID>
    <SrcW>SOURCE_PATH_OR_ITEM</SrcW>
    <TrgW>TARGET_PATH_OR_ITEM</TrgW>
    <DataCounter>0</DataCounter>
    <AllDataCounter>0</AllDataCounter>
</OTIMSG>
```

| Tag Name | Role in Plugin RPC (`0x00420D71` / `0x00434CDD`) |
| :--- | :--- |
| `<OTIMSG>` | Envelope root tag. |
| `<DynamicLibCmd>` | Add-on plugin command verb passed to `ECN001.DLL` / companion tools. |
| `<TransferFile>` | Internal file transfer directive dispatched during plugin data sync. |
| `<MultiID>` / `<SingleID>` | Item identifiers for batch item selection and record tracking. |
| `<SrcW>` / `<TrgW>` | Wide/Unicode source and target item paths or MAPI entry identifiers. |
| `<ExceptionCode>` / `<WhereError>` | Error propagation tags returned in failure replies. |
| `<DataCounter>` / `<AllDataCounter>` | Progress and record counters during synchronization. |
| `<DriveListData>` | Logical drive list payload tunneled to plugin components. |

### 7.2 `<ExtraXmlCommand>` Virtual NIC Mode-Switch & Control Protocol (Opcode `0x39`, `0x0040F635` / `0x00441DB1`)

When the user clicks the **Virtual Network** button or selects **Switch EasySync to Virtual Network** (`Menu ID 32789`), or when exchanging extended environment configurations (such as `LocalLibraryInformation`), `EasySuite.exe` sends Client Command **`0x39`** (`0x0040F635`) formatted with a 32-bit little-endian length prefix:

```text
Offset  Size        Type          Field
+0x00   1           uint8         opcode = 0x39
+0x01   4           uint32_le     xml_len (X)
+0x05   X           bytes         xml_bytes (ASCII/UTF-8 XML with root <ExtraXmlCommand>)
```

The receiving server handler at `0x00441DB1` reads `xml_len`, allocates `xml_len + 1` bytes for NUL termination, copies the payload, and dispatches the XML commands.

> [!NOTE]
> **Opcodes `0x32` and `0x33` are Keep-Alive / No-Op Frames:**
> Disassembly of the dispatcher at `0x0043D2C1` (for `0x32`) and `0x0043D374` (for `0x33`) proves that neither opcode processes an XML payload. Both opcodes immediately jump to exit `0x0043D42A` and serve as 1-byte keep-alive or ping/ack signals. All `<ExtraXmlCommand>` XML data is carried exclusively over opcode **`0x39`**.

| Command Token Inside `<ExtraXmlCommand>` | Direction | Meaning & Handler Action (`0x00441DB1`) |
| :--- | :---: | :--- |
| `PleaseSwitchAsVirtualNIC` | Initiator $\rightarrow$ Peer | Requests the remote host to switch from Transfer Line Mode (`0x007F`) to Virtual NIC Mode (`0x0044`). Displays confirmation prompt on peer (`0xF09D` `"Sure to switch as Virtual Network?"`). |
| `MeSwitchAsRouter` | Initiator $\rightarrow$ Peer | Informs the peer that the initiating host will act as the ICS/NAT Router (`nicinst.exe IS`, IP `192.168.0.1`), so the peer should configure itself as a DHCP/ICS Client (`nicinst.exe IC`). |
| `MeSwitchAsNoneRouter` | Initiator $\rightarrow$ Peer | Informs the peer that neither host requested router mode (or peer should run standard `nicinst.exe I`). |
| `RejectSwitchAsVirtualNIC` / `Busy` | Peer $\rightarrow$ Initiator | Sent if the peer is currently busy transferring files (`0xF0A0`) or declines the switch (`0xF09F`). |
| `LocalLibraryInformation` | Bidirectional | Sent during startup (`0x00422CBD`) with `<ExtraXmlParam>...</ExtraXmlParam>`; `0x00441E79` passes `<ExtraXmlParam>` to `0x00410058` on `0x45F8D8` and returns immediately (non-blocking). |
