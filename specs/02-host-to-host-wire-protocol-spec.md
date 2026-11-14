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
   - For **in-memory commands/replies** (e.g., large directory listings `0xA3`, batch pre-check lists `0x14`/`0xA8`/`0xAA`):
     + On `chunk_index == 0`, allocates an accumulation buffer of `total_chunks * 0xFFEC` bytes and records `expected_msg_seq_id = msg_seq_id`.
     + For each subsequent chunk (`chunk_index == 1 .. total_chunks - 1`), verifies `msg_seq_id == expected_msg_seq_id` and appends `block[0x14 : 0x14 + chunk_payload_len]`.
     + When `chunk_index == total_chunks - 1`, dispatches the complete reassembled buffer to `0x0040B93F` or `0x0043D181`.
   - For **streaming file data (`opcode == 0xA4` Single-File Data or `opcode == 0xA5` Batch File Stream):**
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
| **`0x01`** | **Query Peer Root / Drives** | `0x0040D9C9` | `0x0043D479` | `0xA4` (and `0xA1`) | `[0x01][flags:u8][param:u16_le][path_len:u8][path:bytes]` (5 bytes total for root query; Section 4.2.1) |
| **`0x02`** | **List Directory (`FindFirstFile`)** | `0x0040DCF0` | `0x0043D577` | `0xA3` (or `0xA2` on error) | `[0x02][path_len:u32_le][dir_path:bytes]` |
| **`0x03`** | **Delete File** | `0x0040DDD0` | `0x0043D64D` | `0xA1` (`status=1`) or `0xA2` | `[0x03][path_len:u32_le][file_path:bytes]` |
| **`0x04`** | **Delete Directory (Recursive)** | `0x0040DEB0` | `0x0043D741` | `0xA1` (`status=1`) or `0xA2` | `[0x04][path_len:u32_le][dir_path:bytes]` |
| **`0x05`** | *Reserved / Unused* | — | `0x0043D42A` | — | Jump table slot `0x05` jumps to default exit |
| **`0x06`** | **Create Directory** | `0x0040DF90` | `0x0043D90F` | `0xA1` (`status=1`) or `0xA2` | `[0x06][path_len:u32_le][dir_path:bytes]` |
| **`0x07`** | **Rename File** | `0x0040E070` | `0x0043DA61` | `0xA1` (`status=1`) or `0xA2` | `[0x07][old_len:u32_le][new_len:u32_le][old_path:bytes][new_path:bytes]` |
| **`0x08`** | **Rename Directory** | `0x0040E190` | `0x0043DBD1` | `0xA1` (`status=1`) or `0xA2` | `[0x08][old_len:u32_le][new_len:u32_le][old_path:bytes][new_path:bytes]` |
| **`0x09`** | **Upload Single File to Peer** | `0x0040E2B0` | `0x0043DD10` | `0xA1` (`status=9`) or `0xA2` | `[0x09][path_len:u32_le][data_len:u32_le][find_data:320B][dest_path:bytes][file_data:bytes]` |
| **`0x0A`** | **Download Single File from Peer** | `0x0040E4B0` | `0x0043DF17` | `0xA4` (or `0xA2` on error) | `[0x0A][path_len:u32_le][src_path:bytes]` |
| **`0x0B`** | **Batch Upload Files/Dirs to Peer** | `0x0040E590` | `0x0043E4DA` | `0xA1` (`status=0x0B`) or `0xA9` | `[0x0B][base_dir_len:u32_le][file_count:u32_le][base_dir:bytes][records...]` (Section 4.2.2) |
| **`0x0C`** | **Batch Download Files/Dirs from Peer** | `0x0040EAE0` | `0x00440E41` | `0xA5`, `0xA6`, `0xA7`, `0xAB` | `[0x0C][path_count:u32_le][records: (path_len:u32_le, path:bytes)...]` |
| **`0x0D`** | **Abort / Cancel Active Transfer** | `0x0040EE10` | `0x0043E31E` | `0xA1` (`status=1`) | `[0x0D]` (1 byte; sets cancel flag `[0x0045FA78] = 1`, deletes partial temp file) |
| **`0x0E`** | **Query Drive Free/Total Space** | `0x0040EE90` | `0x0043FFA6` | `0xA3` | `[0x0E][path_len:u32_le][root_path:bytes]` |
| **`0x0F`** | **Pre-Check Single File Overwrite** | `0x0040EF70` | `0x0043E3B4` | `0xA1` (`1`=New, `3`=Exists) | `[0x0F][path_len:u32_le][dest_path:bytes]` |
| **`0x10`** | **Set Peer Platform = Mac (`bMac=1`)** | — | `0x0043DC6A` | `0xA1` (`status=0x10`) | `[0x10]` (1 byte; sets `[0x0045F980] = 1`) |
| **`0x11`** | **Abort Batch File Transfer** | `0x0040E19F` | `0x0043E45C` | `0xA1` | `[0x11]` (1 byte) |
| **`0x12`** | **Batch Prepare / Clear Read-Only Attributes** | `0x0040E628` | `0x0043F981` | `0xA1` | `[0x12]` (1 byte) |
| **`0x13`** | **Batch Upload Pre-Check (Space & Conflicts)** | `0x0040E468` | `0x0043F269` | `0xA8` | `[0x13][dest_dir_len:u32_le][item_count:u32_le][dest_dir:bytes][items: (len:u32_le, rel_path:bytes)...]` |
| **`0x14`** | **Cancel Active Packet Splitter Job** | `0x0040E60A` | `0x0043FCF8` | `0xA1` | `[0x14]` (1 byte) |
| **`0x15`** | **Create Batch Zero-Length Files** | `0x0040E7B3` | `0x0043FB55` | `0xA1` | `[0x15]` (1 byte) |
| **`0x16`** | **Extended Batch Upload Pre-Check** | `0x0043F5F3` | `0x0043F5F3` | `0xAA` | `[0x16][dest_dir_len:u32_le][item_count:u32_le][dest_dir:bytes][records...]` (Section 4.2.4) |
| **`0x17`** | **Query Localized Win32 Error Strings** | `0x0040F6B1` | `0x00441AF0` | `0xAC` | `[0x17][err_count:u32_le][err_codes: u32_le[err_count]]` |
| **`0x32`** | **Virtual NIC Mode-Switch XML (`<ExtraXmlCommand>`)** | `0x0040F730` | `0x00441DB1` | `0x32` / Mode Switch | `[0x32][xml_ascii_nul_terminated]` (Section 7.2) |
| **`0x34`** | **Dynamic Library Plugin RPC XML Tunnel (`<OTIMSG>`)** | `0x0040EFDF` | `0x0043F13D` | `0x34` | `[0x34][dll_name_len:u8][dll_name:bytes][xml_len:u32_le][xml_bytes]` (Section 7.1) |
| **`0x33..0x39`** | **Extended XML Command Channel (`51..57`)** | `0x0040F730` | `0x00441DB1` | `0x33..0x39` | `[0x33..0x39][xml_ascii_nul_terminated]` (routed to `0x00441DB1` alongside `0x32`) |

---

### 4.2 Detailed Client Command Payload Specifications

#### 4.2.1 `0x01` — Query Peer Root / Drives (`0x0040D9C9` / `0x0043D479`)
Queries the remote peer's logical drives and volumes. Constructed by the client sender at `0x0040D9C9` (formatting at `0x0040D9F8`) and handled by `0x0043D479`:
```text
Offset  Size        Type          Field
+0x00   1           uint8         opcode = 0x01
+0x01   1           uint8         flags (Platform / Mac flags from [0x47FFFC]; bit 4: 0x10 = Mac mode, bit 5: 0x20)
+0x02   2           uint16_le     param (transaction sequence counter, typically 0x0000)
+0x04   1           uint8         path_len (0 for root drive query, or N for subpath)
+0x05   N           bytes         path (ASCII/MBCS path on peer if path_len > 0)
```
- **Root Query Framing:** For querying root drives/volumes, `path_len = 0` and the packet is exactly **5 bytes total**:
  `[0x01][flags:u8][param:u16_le][0x00]`
- **Server Behavior (`0x0043D479`):**
  1. Reads `flags` (`edi[0]`) and `param` (`*(u16*)&edi[1]`). If `flags & 0x10 != 0`, sets `bMac = 1`.
  2. Reads `path_len` (`edi[3]`).
  3. Sends Server Reply `0xA1` (`status = 1`, Generic OK) via `0x0043FFF9`.
  4. Calls `0x00440FDC` to enumerate logical drives (`GetLogicalDriveStringsA` / `GetDriveTypeA`), which formats and sends Server Reply **`0xA4`** (**Remote Logical Drive List**).
- **Critical Implementation Pitfall:**
  > [!WARNING]
  > Opcode `0x01` is **not a 1-byte command**. Sending only 1 byte (`b"\x01"`) results in an immediate buffer underrun: the handler reads `flags`, `param`, and `path_len` out-of-bounds from whatever uninitialized bytes follow the buffer in memory, corrupting state and causing communication desynchronization.

#### 4.2.2 `0x09` — Upload Single File to Peer (`0x0043DD10`)
Uploads a single file along with its original `WIN32_FIND_DATAA` attributes and timestamps:

```text
Offset  Size        Type          Field
+0x00   1           uint8         opcode = 0x09
+0x01   4           uint32_le     dest_path_len (N)
+0x05   4           uint32_le     file_data_len (M)
+0x09   320 (0x140) struct        WIN32_FIND_DATAA (attributes at +0, creation/access/write FILETIMEs at +4..+27)
+0x149  N           bytes         dest_path (ASCII/MBCS path on peer, without NUL)
+0x149+N M          bytes         raw file contents
```

- **Server Behavior (`0x0043DD10`):**
  1. Creates destination file via `CreateFileA(dest_path, GENERIC_WRITE, ...)`.
  2. Writes `M` bytes of file contents (streaming across subsequent chunks if `total_chunks > 1`).
  3. Restores `CreationTime`, `LastAccessTime`, and `LastWriteTime` via `SetFileTime` (`0x0043DE80`) and file attributes via `SetFileAttributesA` (`0x0043DEA5`).
  4. Sends `0xA1` reply with `status = 0x09` on success, or `0xA2` with `GetLastError()` on failure.

#### 4.2.3 `0x0B` — Batch Upload Files/Directories to Peer (`0x0043E4DA`)
Uploads a batch of files and/or empty directories into a target base directory:

```text
Offset  Size        Type          Field
+0x00   1           uint8         opcode = 0x0B
+0x01   4           uint32_le     base_dir_len (B)
+0x05   4           uint32_le     record_count (K)
+0x09   B           bytes         base_dir_path (ASCII/MBCS)
+0x09+B ...         records[K]    Back-to-back file/directory records
```

Each of the `K` records in `0x0B` has the layout:

```text
Record Offset  Size        Type          Field
+0x00          4           uint32_le     rel_path_len (R)
+0x04          4           uint32_le     file_data_len (M; 0 for directories or empty files)
+0x08          320 (0x140) struct        WIN32_FIND_DATAA (dwFileAttributes & 0x10 != 0 indicates directory)
+0x148         R           bytes         rel_path (appended to base_dir_path)
+0x148+R       M           bytes         file_data (present if M > 0)
```

- **Server Behavior (`0x0043E4DA`):**
    - Creates all intermediate parent directories (`0x00442610`), writes each file, restores `FILETIME` and `dwFileAttributes`, and replies with `0xA1` (`status = 0x0B`) or `0xA9` (Batch File Error Notification).

#### 4.2.4 `0x16` — Extended Batch Upload Pre-Check (`0x0043F5F3`)
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
| **`0xA1`** | **Command Status / ACK** | `0x0043FD2D` | `0x0040BA96` | `[0xA1][status_code:u8]` (2 bytes total):<br/>`1` = Generic OK / File Does Not Exist (`0x0F`)<br/>`3` = File Already Exists (`0x0F` overwrite prompt)<br/>`9` = Single File Upload (`0x09`) Complete<br/>`0x0B` = Batch Upload (`0x0B`) Complete<br/>`0x10` = Mac Mode (`0x10`) ACK |
| **`0xA2`** | **Command Error (Win32 Error Code)** | `0x0043FDC9` | `0x0040BB10` | `[0xA2][win32_error:u32_le]` (5 bytes total; e.g., `ERROR_DISK_FULL = 112`, `ERROR_ACCESS_DENIED = 5`) |
| **`0xA3`** | **Directory / Drive Listing Reply** | `0x004400B6` | `0x0040BC20` | `[0xA3][entry_count:u32_le][entries: Entry552[entry_count]]` (`5 + 552 * entry_count` bytes; see Section 6) |
| **`0xA4`** | **Single File Download Data Reply** | `0x00440C8B` | `0x0040BE10` | `[0xA4][file_size:u32_le][find_data:320B][file_data:bytes]` (`325 + file_size` bytes; response to `0x0A`) |
| **`0xA5`** | **Batch File Download Stream Reply** | `0x00441566` | `0x0040C090` | `[0xA5][file_count:u32_le][records: (path_len:u32_le, data_len:u32_le, find_data:320B, path:bytes, data:bytes)...]` |
| **`0xA6`** | **Batch Zero-Length Files / Empty Dirs** | `0x00441602` | `0x0040CFEF` | `[0xA6][item_count:u32_le][records: (path_len:u32_le, find_data:320B, path:bytes)...]` (items with 0 data bytes during `0x0C`) |
| **`0xA7`** | **Batch Download Complete / All-Empty** | `0x0044175A` | `0x0040D210` | `[0xA7]` (1 byte; signals end of `0x0C` batch download when no non-empty stream remains) |
| **`0xA8`** | **Batch Upload Pre-Check Reply** | `0x004418C9` | `0x0040D290` | `[0xA8][existing_count:u32_le][records: (path_len:u32_le, path:bytes)...]` (subset of `0x13` items that already exist on peer) |
| **`0xA9`** | **Batch Upload Error Notification** | `0x004417A0` | `0x0040D420` | `[0xA9][win32_error:u32_le][failed_path_len:u32_le][failed_path:bytes]` |
| **`0xAA`** | **Extended Batch Pre-Check Reply** | `0x0044196E` | `0x0040D650` | `[0xAA][existing_count:u32_le][records: (path_len:u32_le, peer_find_data:320B, src_find_data:320B, path:bytes)...]` (response to `0x16` with both peer and source `WIN32_FIND_DATAA` for overwrite comparison dialog) |
| **`0xAB`** | **Batch Unreadable Files Notification** | `0x004416AE` | `0x0040D840` | `[0xAB][failed_count:u32_le][records: (path_len:u32_le, win32_err:u32_le, path:bytes)...]` (files that failed `FindFirstFile`/`CreateFile` during `0x0C`) |
| **`0xAC`** | **Formatted Win32 Error Strings Reply** | `0x00441CC4` | `0x0040BA16` | `[0xAC][count:u32_le][records: (err_code:u32_le, msg_len:u32_le, formatted_msg:bytes)...]` (response to `0x17` via `FormatMessageA`) |

---

## 6. 552-Byte (`0x228`) Directory & Drive Entry Structure (`0xA3`)

Server Reply `0xA3` (`0x004400B6`, handled at `0x0040BC20`) returns an array of `entry_count` fixed-size **552-byte (`0x228`) structures**. This single structure represents both **logical drives/volumes** (in response to `0x01` or `0x0E`) and **files/subdirectories** (in response to `0x02`).

```text
Offset   Size   Type          Field Name              Description
+0x000   4      uint32_le     entry_kind              0 = Drive/Volume entry, 1 = File/Directory entry
+0x004   320    struct        win32_find_data         Standard 320-byte WIN32_FIND_DATAA:
                                                        +0x004 (4B): dwFileAttributes (0x10 = DIRECTORY)
                                                        +0x008 (8B): ftCreationTime (FILETIME)
                                                        +0x010 (8B): ftLastAccessTime (FILETIME)
                                                        +0x018 (8B): ftLastWriteTime (FILETIME)
                                                        +0x020 (4B): nFileSizeHigh
                                                        +0x024 (4B): nFileSizeLow
                                                        +0x028 (4B): dwReserved0
                                                        +0x02C (4B): dwReserved1
                                                        +0x030 (260B): cFileName (NUL-terminated ASCII/MBCS)
                                                        +0x134 (14B): cAlternateFileName
                                                        +0x142 (2B): padding
+0x144   4      uint32_le     drive_type              Win32 GetDriveTypeA result (2=Removable, 3=Fixed, 4=Remote, 5=CDROM)
+0x148   8      uint64_le     total_bytes             Total volume capacity in bytes (GetDiskFreeSpaceExA)
+0x150   8      uint64_le     total_free_bytes        Total free bytes on volume
+0x158   8      uint64_le     caller_free_bytes       Free bytes available to caller
+0x160   100    char[100]     volume_label            Volume name from GetVolumeInformationA (NUL-terminated)
+0x1C4   100    char[100]     filesystem_name         Filesystem type (e.g., "NTFS", "FAT32", NUL-terminated)
+0x228   ---    (End of 552-byte structure)
```

---

## 7. `<OTIMSG>` and `<ExtraXmlCommand>` XML Control Protocols

### 7.1 `<OTIMSG>` Dynamic Library Plugin RPC Protocol (Opcode `0x34`, `0x0043F13D` / `0x00420D71`)

> [!IMPORTANT]
> **No XML Handshake in Standard File Transfer:**
> Disassembly of `EasySuite.exe` reveals that standard file manager operations do **not** exchange an XML handshake upon link connection. As soon as both sides enter Transfer Line Mode (`0xD8 0x01` `op_type=5, param=0x007F`), both hosts immediately transition to the connected state (status bar `"Connected"`, ID `0xF026`) and issue Client Command **`0x01`** (**Query Peer Root / Drives**) using the 5-byte structure described in Section 4.2.1.
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

### 7.2 `<ExtraXmlCommand>` Virtual NIC Mode-Switch Protocol (Opcode `0x32`, `0x00441DB1`)
When the user clicks the **Virtual Network** button or selects **Switch EasySync to Virtual Network** (`Menu ID 32789`), `EasySuite.exe` sends opcode `0x32` containing a NUL-terminated XML string with root tag `<ExtraXmlCommand>...</ExtraXmlCommand>`:

| Command Token Inside `<ExtraXmlCommand>` | Direction | Meaning & Handler Action (`0x00441DB1`) |
| :--- | :---: | :--- |
| `PleaseSwitchAsVirtualNIC` | Initiator $\rightarrow$ Peer | Requests the remote host to switch from Transfer Line Mode (`0x007F`) to Virtual NIC Mode (`0x0044`). Displays confirmation prompt on peer (`0xF09D` `"Sure to switch as Virtual Network?"`). |
| `MeSwitchAsRouter` | Initiator $\rightarrow$ Peer | Informs the peer that the initiating host will act as the ICS/NAT Router (`nicinst.exe IS`, IP `192.168.0.1`), so the peer should configure itself as a DHCP/ICS Client (`nicinst.exe IC`). |
| `MeSwitchAsNoneRouter` | Initiator $\rightarrow$ Peer | Informs the peer that neither host requested router mode (or peer should run standard `nicinst.exe I`). |
| `RejectSwitchAsVirtualNIC` / `Busy` | Peer $\rightarrow$ Initiator | Sent if the peer is currently busy transferring files (`0xF0A0`) or declines the switch (`0xF09F`). |
