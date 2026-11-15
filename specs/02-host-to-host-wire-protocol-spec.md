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

## 3. Cross-Platform & Mac (`0x10`) Endianness Rules

`EasySuite.exe` supports connecting a Windows x86 host (little-endian) to a PowerPC Mac OS peer (big-endian) via a global boolean flag `bMac` (`[0x0045F980]`), set when Client Command `0x10` (`Set Peer Platform = Mac`) is received (`0x0043DC6A`).
- **Default (`bMac == 0`, Windows/Linux/x86/ARM little-endian peers):** All multi-byte integers (`uint16`, `uint32`, `int64`) inside wire command/reply payloads (and inside the 552-byte directory entry struct) are **little-endian**.
- **Mac Peer Mode (`bMac == 1`):** Multi-byte integers in command headers and directory entries are byte-swapped (`0x00442525`).

---

## 4. Complete Client Command Catalog (`0x01..0x39`)

Client Commands are sent by the initiating host (`0x0040D9C9..0x0040F800`) and handled by the receiving peer's Client Command Dispatcher (`0x0043D181..0x00441F02`).

### 4.1 Master Client Opcode Table

| Opcode | Name | Client Sender VA | Server Handler VA | Expected Reply Opcode(s) | Payload Format (after `opcode` byte at `+0x00`) |
| :---: | :--- | :--- | :--- | :---: | :--- |
| **`0x01`** | **Query Peer Root / Drives** | `0x0040D9C9` | `0x0043D479` | `0xA4` (and `0xA1`) | `[0x01][flags:u8][code_page:u16_le][name_len:u8][computer_name:bytes]` (11 bytes total for host query; Section 4.2.1) |
| **`0x02`** | **Notify / Set Active Remote Directory** | `0x0040DA90` | `0x0043D577` | `0xA1` (`status=0`) | `[0x02][path_len:u8][dir_path:bytes]` (folder-change notification; Section 4.2.2) |
| **`0x03`** | **Refresh Current Directory** | — | `0x0043D64D` | `0xA1` (`status=0`), then `0xA2` | `[0x03]` (1 byte; re-lists active directory on peer, returns 24B 0xA1 followed by 0xA2 listing) |
| **`0x04`** | **Change / List Remote Directory** | `0x0040DB44` | `0x0043D741` | `0xA1` (`status=0`), then `0xA2` | `[0x04][path_len:u16_le][dir_path:wchar_t]` (emits 24B 0xA1 status=0 followed by 552B `0xA2` listing; Section 4.2.3) |
| **`0x05`** | **Select Remote Drive (Legacy/No-op)** | `0x0040DC28` | `0x0043D394` | — | `[0x05]` (1 byte; legacy/no-op, jumps to exit `0x0043D42A`) |
| **`0x06`** | **Remove Remote Directory** | `0x0040EE3A` | `0x0043D90F` | `0xA1` (`status=0`) | `[0x06][req_id:u32_le][path_len:u16_le][dir_path:wchar_t]` (calls `RemoveDirectoryW`, replies 24B `0xA1`) |
| **`0x07`** | **Create Remote Directory** | `0x0040EF0A` | `0x0043DA61` | `0xA1` (`status=0`) | `[0x07][req_id:u32_le][path_len:u16_le][dir_path:wchar_t]` (calls `CreateDirectoryW`, replies 24B `0xA1`) |
| **`0x08`** | **Delete Remote File** | `0x0040EC14` | `0x0043DBD1` | `0xA1` (`status=0` or `1`) | `[0x08][req_id:u32_le][path_len:u16_le][file_path:wchar_t]` (calls `DeleteFileW`, replies 24B `0xA1`) |
| **`0x09`** | **Rename Remote File or Directory** | `0x0040ECE4` | `0x0043DD10` | `0xA1` (`status=0`) | `[0x09][req_id:u32_le][old_len:u16_le][new_len:u16_le][old_path:wchar_t][new_path:wchar_t]` (calls `MoveFileW`, replies 24B `0xA1`) |
| **`0x0A`** | **Single File Upload Request** | `0x0040E2A7` | `0x0043DF17` | `0xA1` (`status=0`) | `[0x0A][req_id:u32_le][dest_len:u16_le][file_size:u32_le][find_data:320B][dest_path:wchar_t]` (Section 4.2.4) |
| **`0x0B`** | **Single File Download Request** | `0x0040DC4B` | `0x0043E4DA` | `0xA3` (or `0xA1` error) | `[0x0B][req_id:u32_le][src_len:u16_le][src_path:wchar_t]` |
| **`0x0C`** | **Upload File Data Chunk** | `0x00443EC9` | `0x00440E41` | `0xA1` | `[0x0C][item_id:u32_le][chunk_index:u32_le][total_chunks:u32_le][data_len:u32_le][data:bytes]` (Section 4.2.6) |
| **`0x0D`** | **Abort Single File Upload (Delete Partial)** | `0x0040E9F8` | `0x0043E31E` | `0xA1` | `[0x0D]` (1 byte; closes handle and deletes partial destination file) |
| **`0x0E`** | **Acknowledge File Transfer Completion** | `0x0040CB5F` | `0x0043FFA6` | `0xA1` | `[0x0E]` (1 byte; sent after file streaming completes to finalize timestamps/attributes) |
| **`0x0F`** | **Cancel Single File Transfer (Keep Partial)** | `0x0040EB06` | `0x0043E3B4` | `0xA1` | `[0x0F]` (1 byte; closes handle without deleting destination file) |
| **`0x10`** | **Batch File Download Request (16-bit Count)** | `0x0040DD56` | `0x0043E781` | `0xA3`, `0xA7` | `[0x10][req_id:u32_le][item_count:u16_le][records: (item_id:u32_le, path_len:u16_le, path:wchar_t)...]` (replies with `0xA3` chunks, ended by `0xA7`) |
| **`0x11`** | **Abort Batch File Transfer** | `0x0040E19F` | `0x0043E45C` | `0xA1` | `[0x11]` (1 byte) |
| **`0x12`** | **Batch Prepare / Clear Read-Only Attributes** | `0x0040E628` | `0x0043F981` | `0xA1` | `[0x12][req_id:u32_le][item_count:u16_le][records: (item_id:u32_le, path_len:u16_le, path:wchar_t)...]` |
| **`0x13`** | **Batch Upload Pre-Check (Space & Conflicts)** | `0x0040E468` | `0x0043F269` | `0xA8` | `[0x13][req_id:u32_le][total_size:u64_le][flags:u16_le][item_count:u16_le][records: (item_id:u32_le, path_len:u16_le, path:wchar_t)...]` (replies 13B `0xA8`) |
| **`0x14`** | **Cancel Active Packet Splitter Job** | `0x0040E60A` | `0x0043FCF8` | `0xA1` | `[0x14]` (1 byte) |
| **`0x15`** | **Create Batch Zero-Length Files** | `0x0040E7B3` | `0x0043FB55` | `0xA1` | `[0x15]` (1 byte) |
| **`0x16`** | **Extended Batch Upload Pre-Check** | `0x0043F5F3` | `0x0043F5F3` | `0xAA` | `[0x16][req_id:u32_le][item_count:u32_le][records...]` (Section 4.2.7) |
| **`0x17`** | **UI Notification Command** | `0x0040F6B1` | `0x0043D2E8` | — | `[0x17][code:u8]` (posts Win32 message `0xFC0`) |
| **`0x32`** | **Keep-Alive / No-Op Ack 0x32** | — | `0x0043D2C1` | — | `[0x32]` (1 byte; immediate jump to exit `0x0043D42A`) |
| **`0x33`** | **Keep-Alive / No-Op Ack 0x33** | — | `0x0043D374` | — | `[0x33]` (1 byte; immediate jump to exit `0x0043D42A`) |
| **`0x34`** | **Dynamic Library Plugin RPC XML Tunnel (`<OTIMSG>`)** | `0x0040EFDF` | `0x0043F13D` | `0x34` | `[0x34][dll_name_len:u8][dll_name:bytes][xml_len:u32_le][xml_bytes]` (Section 7.1) |
| **`0x35`** | **Multi-ID Batch Download Request (16-bit Count)** | `0x0040DF00` | `0x0043EB3F` | `0xA3`, `0xA7` | `[0x35][item_count:u16_le][item_ids:u32_le[item_count]]` |
| **`0x36`** | **Multi-ID Batch Download Request (32-bit Count)** | `0x0040E05D` | `0x0043EE3E` | `0xA3`, `0xA7` | `[0x36][item_count:u32_le][item_ids:u32_le[item_count]]` |
| **`0x37`** | **Set Remote File Attributes and Timestamps** | `0x0040F511` | `0x00441A47` | `0xA1` | `[0x37][attributes:u32_le][ftCreationTime:8B][ftLastAccessTime:8B][ftLastWriteTime:8B][path_len:u32_le][path:wchar_t]` |
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

#### 4.2.3 `0x04` — Change / List Remote Directory (`0x0040DB44` / `0x0043D741`)
Requests the remote host to change to the target directory and return its full directory listing. Constructed at `0x0040DB44` and handled at `0x0043D741`:

```text
Offset  Size        Type          Field
+0x00   1           uint8         opcode = 0x04
+0x01   2           uint16_le     path_len (P, byte length of dir_path)
+0x03   P           bytes         dir_path (UTF-16LE wchar_t target directory path on peer, e.g. "C:\\")
```

- **Server Behavior (`0x0043D741`):**
  1. Reads `path_len` as a 16-bit little-endian integer (`dx = word ptr [ecx]`).
  2. Copies `path_len` bytes from offset `+0x03` as UTF-16LE wchar_t.
  3. Appends wildcard (`\*.*`) and calls directory enumeration routines (`FindFirstFileW` / `FindNextFileW` via `0x004401AD`).
  4. Transmits Server Reply **`0xA1`** (24-byte structured ACK, `req_opcode = 0x04`, `status = 0`, `req_id = 0`).
  5. Formats directory entries into fixed-size 552-byte structures (`0x00440C2D` / `0x00440C66`) and transmits Server Reply **`0xA2`** (**Directory Listing Reply**; Section 6).

#### 4.2.4 `0x0A` — Single File Upload Request (`0x0040E2A7` / `0x0043DF17`)
Initiates the upload of a single file along with its destination path:

```text
Offset  Size        Type          Field
+0x00   1           uint8         opcode = 0x0A
+0x01   4           uint32_le     req_id (Transaction sequence ID)
+0x05   2           uint16_le     dest_path_len (N, byte length of dest_path)
+0x07   4           uint32_le     file_size (M)
+0x0B   320 (0x140) struct        WIN32_FIND_DATAA (source timestamps and attributes)
+0x14B  N           bytes         dest_path (UTF-16LE wchar_t destination path on peer)
```

- **Server Behavior (`0x0043DF17`):**
  1. Creates or opens the destination file via `CreateFileW(dest_path, GENERIC_WRITE, ...)`.
  2. Awaits incoming streaming file chunks transmitted via opcode **`0x0C`** (`Upload File Data Chunk`).
  3. Once streaming completes, receives opcode **`0x0E`** (`Acknowledge File Transfer Completion`), applies `CreationTime`, `LastAccessTime`, and `LastWriteTime` via `SetFileTime` (`0x0043DE80`), and restores file attributes via `SetFileAttributesW` (`0x0043DEA5`).
  4. Responds with Server Reply `0xA1` (24-byte structured ACK with `status = 0` on success, or `status = 1` and `win32_error = GetLastError()` on failure).

#### 4.2.5 `0x0B` — Single File Download Request (`0x0040DC4B` / `0x0043E4DA`)
Requests the remote peer to transmit a single file back to the initiator:

```text
Offset  Size        Type          Field
+0x00   1           uint8         opcode = 0x0B
+0x01   4           uint32_le     req_id (Transaction sequence ID)
+0x05   2           uint16_le     src_path_len (N, byte length of src_path)
+0x07   N           bytes         src_path (UTF-16LE wchar_t source path on peer)
```

- **Server Behavior (`0x0043E4DA`):**
  1. Opens the requested source file via `CreateFileW(src_path, GENERIC_READ, ...)`.
  2. Queries file attributes and timestamps (`GetFileInformationByHandle` / `FindFirstFileW`).
  3. Streams file contents back to the client using Server Reply **`0xA3`** (**File Download Data Chunk**; Section 5.1), finalized with **`0xA7`**.

#### 4.2.6 `0x0C` — Upload File Data Chunk (`0x00443EC9` / `0x00440E41`)
Streams a chunk of binary data for an in-flight file upload:

```text
Offset  Size        Type          Field
+0x00   1           uint8         opcode = 0x0C
+0x01   4           uint32_le     item_id (Transfer item identifier)
+0x05   4           uint32_le     chunk_index (1-based chunk sequence index)
+0x09   4           uint32_le     total_chunks (Total chunks in file)
+0x0D   4           uint32_le     chunk_len (C, byte length of chunk payload)
+0x11   C           bytes         chunk_data (Binary file slice)
```

- **Server Behavior (`0x00440E41`):**
  1. Writes `chunk_len` bytes via `WriteFile` at sequential offset.
  2. Acknowledges with 24-byte Server Reply `0xA1` (`req_opcode = 0x0C`, `status = 0` on success, or `status = 1` with `win32_error = GetLastError()` on disk full/write failure).

#### 4.2.7 `0x16` — Extended Batch Upload Pre-Check (`0x0043F5F3`)
Performs a pre-transfer metadata collision check for a batch of files against the peer's existing files before upload:

```text
Offset  Size        Type          Field
+0x00   1           uint8         opcode = 0x16
+0x01   4           uint32_le     dest_dir_len (D)
+0x05   4           uint32_le     item_count (K)
+0x09   D           bytes         dest_dir (ASCII/MBCS target directory on peer)
+0x09+D ...         records[K]    Back-to-back file metadata pre-check records
```

Each of the `K` records has the layout:

```text
Record Offset  Size        Type          Field
+0x00          4           uint32_le     rel_path_len (R)
+0x04          320 (0x140) struct        WIN32_FIND_DATAA (source file attributes, size, and timestamps)
+0x144         R           bytes         rel_path (relative path under dest_dir)
```

- **Server Behavior (`0x0043F5F3`):**
  1. Dispatched from Client Command Dispatcher `0x0043D181` via `0x0043D2DE..0x0043D310`.
  2. Reads `dest_dir` and iterates through all `item_count` records.
  3. Tests whether each target file already exists on the peer via `FindFirstFileA`.
  4. For every colliding item, records both the peer's existing `WIN32_FIND_DATAA` and the source `WIN32_FIND_DATAA`.
  5. Generates Server Reply **`0xAA`** (**Extended Batch Pre-Check Reply** at `0x0044196E`) containing both sets of metadata so the client can display Dialog `133` (`Confirm Replace File`) with accurate date/size comparisons.
- **Critical Opcode 0x16 Crash Pitfall:**
  > [!CAUTION]
  > Opcode `0x16` is **strictly a binary batch pre-check command**; it is **NOT** an XML channel. If a client transmits `<OTIMSG>` ASCII XML preceded by opcode `0x16`, `EasySuite.exe` interprets the leading 4 ASCII bytes `b"<OTI"` (`0x49544F3C` = 1,230,327,612) as a 32-bit count, attempts to loop and allocate memory for 1.23 billion items, and crashes fatally in `ntdll!RtlFreeHeap`.

---

## 5. Complete Server Reply Catalog (`0xA1..0xAC`)

Server Replies are sent by `0x0043FD2D..0x00441DA8` in response to Client Commands and dispatched on the initiating host by `0x0040B93F` (which computes `idx = opcode - 0xA1` and uses the 11-entry jump table at `0x0040BA6A` plus `0x0040BA16` for `0xAC`).

### 5.1 Master Server Reply Opcode Table

| Opcode | Name | Server Sender VA | Client Handler VA | Payload Format (after `opcode` byte at `+0x00`) |
| :---: | :--- | :--- | :--- | :--- |
| **`0xA1`** | **Command Status / ACK** | `0x0043FD2D` | `0x0040BA96` | `[0xA1][req_opcode:u8][status:u8][req_id:u32_le][win32_error:u32_le][reserved:13B]` (24 bytes total):<br/>`req_opcode` = Command being acknowledged (`0x01`, `0x04`, `0x06`, `0x08`, `0x10`, etc.)<br/>`status` = `0` (Success/OK) or `1` (Error)<br/>`req_id` = Client transaction sequence ID echoed back<br/>`win32_error` = Win32 `GetLastError()` code (`0` on success, `0x20` on sharing violation, etc.) |
| **`0xA2`** | **Directory Listing Reply** | `0x00440C2D` | `0x0040C251` | `[0xA2][entry_count:u16_le][entries: Entry552[entry_count]]` (`3 + 552 * entry_count` bytes; see Section 6) |
| **`0xA3`** | **File Download Data Chunk** | `0x00441566` | `0x0040CF68` | `[0xA3][item_id:u32_le][chunk_index:u32_le][total_chunks:u32_le][data_len:u32_le][file_data:bytes]` (Streaming file content chunks for download requests via `0x10` / `0x0B`) |
| **`0xA4`** | **Remote Logical Drives List Reply** | `0x00440FDC` | `0x0040F093` | `[0xA4][drive_count:u16_le][records: (drive_letter:char, drive_type:u8)...]` (response to `0x01`; Section 4.2.1) |
| **`0xA5`** | **Batch File Download Stream Reply** | `0x00441566` | `0x0040C090` | `[0xA5][file_count:u32_le][records: (path_len:u32_le, data_len:u32_le, find_data:320B, path:bytes, data:bytes)...]` |
| **`0xA6`** | **Batch Zero-Length Files / Empty Dirs** | `0x00441602` | `0x0040CFEF` | `[0xA6][item_count:u32_le][records: (path_len:u32_le, find_data:320B, path:bytes)...]` (items with 0 data bytes during `0x0C`) |
| **`0xA7`** | **Batch Download Complete ACK** | `0x0044175A` | `0x0040D210` | `[0xA7][req_id:u32_le][status:u16_le]` (7 bytes total; signals end of `0x10` batch download) |
| **`0xA8`** | **Batch Upload Pre-Check Reply** | `0x004418C9` | `0x0040D290` | `[0xA8][subcode:u8][status:u8][flags:u16_le][req_id:u32_le][param:u32_le]` (13 bytes total; response to `0x13`) |
| **`0xA9`** | **Batch Upload Error Notification** | `0x004417A0` | `0x0040D420` | `[0xA9][win32_error:u32_le][failed_path_len:u32_le][failed_path:bytes]` |
| **`0xAA`** | **Extended Batch Pre-Check Reply** | `0x0044196E` | `0x0040D650` | `[0xAA][existing_count:u32_le][records: (path_len:u32_le, peer_find_data:320B, src_find_data:320B, path:bytes)...]` (response to `0x16` with both peer and source `WIN32_FIND_DATAA` for overwrite comparison dialog) |
| **`0xAB`** | **Batch Unreadable Files Notification** | `0x004416AE` | `0x0040D840` | `[0xAB][failed_count:u32_le][records: (path_len:u32_le, win32_err:u32_le, path:bytes)...]` (files that failed `FindFirstFile`/`CreateFile` during `0x0C`) |
| **`0xAC`** | **Formatted Win32 Error Strings Reply** | `0x00441CC4` | `0x0040BA16` | `[0xAC][count:u32_le][records: (err_code:u32_le, msg_len:u32_le, formatted_msg:bytes)...]` (response to `0x17` via `FormatMessageA`) |

> [!CAUTION]
> **Opcode `0xA2` Protocol Trap:**
> Do **not** send opcode `0xA2` as an error reply. The peer dispatcher at `0x0040C251` treats byte `+0x01` as a 16-bit `entry_count` (`word ptr [edi]`) and allocates heap to unpack fixed 552-byte structures. Emitting a short error reply with opcode `0xA2` (such as `0xA2 [win32_err:u32]`) causes the peer to read 552 bytes out-of-bounds, fatally corrupting memory and terminating communication.

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
+0x01C   4      uint32_le     nFileSizeHigh           High 32 bits of file size
+0x020   4      uint32_le     nFileSizeLow            Low 32 bits of file size
+0x024   4      uint32_le     cFileNameLength         Length of filename in bytes (2 * WCHAR count)
+0x028   520    wchar_t[260]  cFileName               UTF-16LE wide-character filename (zero-padded to 520 bytes / 260 WCHARs)
+0x228   ---    (End of 552-byte structure)
```

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
