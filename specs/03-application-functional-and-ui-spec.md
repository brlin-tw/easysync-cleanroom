# OTi-2108 Clean-Room Specification — Part 3: Application Functional & UI Specification

## 1. Main Window & Dual-Pane File Manager Layout

The companion application (`EasySuite.exe`, Main Dialog ID `128`) is a split-screen **Dual-Pane Host-to-Host File Manager** displaying both the **Local Host (`My Computer`)** and the **Remote Host (`Remote Computer`)** simultaneously.

```text
+-----------------------------------------------------------------------------------+
| [EasySync Icon] EasySync                                            [_] [□] [X]   |
+-----------------------------------------------------------------------------------+
| File(F)  Edit(E)  View(V)  Tool(T)  Help(H)                                       |
+-----------------------------------------------------------------------------------+
| [Up] [Refresh] [Copy ->] [<- Copy] [Delete] [New Folder] [View] | [Virtual NIC]   |
| [USB 1.1 L] [USB 2.0 L] [USB 1.1 R] [USB 2.0 R] [Read Only] [Tx/Rx Lamp]          |
+-----------------------------------------+-----------------------------------------+
| Local Host ("My Computer")              | Remote Host ("Remote Computer")         |
| +-------------+-----------------------+ | +-------------+-----------------------+ |
| | Folder Tree | File List (Detail/Ic) | | | Folder Tree | File List (Detail/Ic) | |
| | +-- C:\     | Name  Size  Type Date | | | +-- C:\     | Name  Size  Type Date | |
| | +-- D:\     | ...                   | | | +-- D:\     | ...                   | |
| +-------------+-----------------------+ | +-------------+-----------------------+ |
+-----------------------------------------+-----------------------------------------+
| Status Bar: [ Connection Status ] [ Local Free Space ] [ Remote Free Space ]      |
+-----------------------------------------------------------------------------------+
```

### 1.1 Hardware Status Indicator Lamps (Dialog `128` Controls `1014..1019`)
The main toolbar / header area includes six hardware status indicators updated by the transport poll loop (`0xD8 0x00` `op_type=2` and `op_type=3`):
- **Control `1014` (`USB 1.1 L`) / Control `1016` (`USB 2.0 L`):** Illuminates green based on local port USB speed (`resp[0] == 0x11` vs `0x20`).
- **Control `1015` (`USB 1.1 R`) / Control `1017` (`USB 2.0 R`):** Illuminates green based on remote peer USB speed (`resp[1] == 0x11` vs `0x20`).
- **Control `1018` (`Read Only`):** Illuminates when the local virtual disk or target volume is write-protected (Sense `07/27/00`).
- **Control `1019` (`Tx/Rx` Activity Lamp):** Blinks during active `0xD9 0x28` (Bulk Read) and `0xD9 0x2A` (Bulk Write) transfers.

### 1.2 File List Columns (String IDs `0xF02B..0xF032`)
1. **In Directory List Mode:**
   + `Name` (`0xF02B`), `Size` (`0xF02D`), `Type` (`0xF02E`), `Modified` (`0xF02F`).
2. **In Root / Drive List Mode (`entry_kind == 0` in `0xA2` or `0xA4`):**
   + `Name` (`0xF02B`), `Type` (`0xF02E`), `Total Size` (`0xF030`), `Free Space` (`0xF031`), `Comment` (`0xF032`).

---

## 2. Complete Menu Specification (8 Menus)

Extracted from `EasySuite.packed.exe` resource directory (Language `0x0409` English):

### 2.1 Main Menu Bar (Menu ID `128` & `484` / `485`)

| Top-Level Menu | Item Label | Command ID | Shortcut / Action |
| :--- | :--- | :---: | :--- |
| **`&File`** | `&Open` | `32779` | Open selected file (local) or folder (local/remote) |
| | `&Delete` | `32776` | Delete selected files/folders (`DeleteFile` / `0x08` for files, `RemoveDirectory` / `0x06` for folders) |
| | `&Rename` | `32778` | Rename selected file/folder (`MoveFile` / `0x09`) |
| | `&New Folder` | `32777` | Open Dialog `134` and create directory (`CreateDirectory` / `0x07`) |
| | `E&xit` | `32788` | Reset bridge to idle mode (`cmd=5, val=0`), stop worker threads, and quit |
| **`&Edit`** | `&Copy` | `57634` | Copy selected items in active pane to clipboard / transfer selection |
| | `&Paste` | `57637` | Paste / initiate transfer from opposite pane (`0x0A`/`0x0B`/`0x0C`/`0x0E`) |
| | `Select &All` | `32771` | Select all items in active file list |
| | `&Invert Selection` | `32772` | Invert selection in active file list |
| **`&View`** | `Lar&ge Icons` | `32780` | Switch list view to large icon mode |
| | `S&mall Icons` | `32781` | Switch list view to small icon mode |
| | `&List` | `32782` | Switch list view to compact list mode |
| | `&Details` | `32783` | Switch list view to report/details mode (default) |
| | `&Refresh` | `32775` | Re-query current local and remote directories (`0x01` / `0x04`) |
| **`&Tool`** | `&Option` | `32787` | Open `EasySync Setup` Dialog (`155`) |
| | `Switch EasySync to &Virtual Network` | `32789` | Open `Network Option` Dialog (`188`) and initiate Virtual NIC mode switch (`0x39`) |
| **`&Help`** | `&About EasySync...` | `57664` | Open `About EasySync` Dialog (`174`) |

### 2.2 Context & Auxiliary Menus (`129`, `136`, `156`, `157`, `483`)
- **Menu `129` (Right-Click Context Menu on File/Folder List):**
    + `&Copy` (`57634`), `&Paste` (`57637`), `---`, `&Open` (`32779`), `&Delete` (`32776`), `&Rename` (`32778`), `---`, `&New Folder` (`32777`).
- **Menu `136` (Toolbar View Dropdown):**
    + `Lar&ge Icons` (`32780`), `S&mall Icons` (`32781`), `&List` (`32782`), `&Details` (`32783`).
- **Menu `156` (System Tray Icon Menu):**
    + `&Open EasySync` (`32785`), `&Close EasySync` (`32786`).
- **Menu `157` (Optional Add-On Function Menu — `ECN001.DLL`):**
    + `Start &OTi AP` (`32790`), `Start &Mail AP` (`32791`), `Start &PIM AP` (`32792`), `Start &Security AP` (`32793`), `Start S&ync AP` (`32794`).
- **Menu `483` (Toolbar Virtual Network Dropdown):**
    + `Switch EasySync to &Virtual Network` (`32789`).

---

## 3. Complete Dialog Specification (7 Dialogs)

| Dialog ID | Title | Key Controls & IDs | Functional Behavior |
| :---: | :--- | :--- | :--- |
| **`128`** | *(Main View)* | `1005` (`Tree1`), `1006` (`List2`), `1014..1019` (USB/Status Lamps), `1047` (`Virtual Network` button) | Embedded form view inside main frame window. Supports drag-and-drop and toolbar-driven file transfers between local and remote panes. |
| **`131`** | `Progress Information` | `1007` (Source path label), `1008` (Dest path label), `1009` (`Progress1` bar), `2` (`Cancel` button) | Modal/modeless transfer dialog displayed during single or batch file transfers (`0x0A..0x0E`). Clicking `Cancel` (`2`) sends Client Command `0x0D` (`Abort Single File Upload`) or `0x11` (`Abort Batch File Transfer`) and deletes the incomplete target file. |
| **`133`** | `Confirm Replace File` | `1` (`&Yes`), `1010` (`Yes to &All`), `2` (`&No`), `1011` (`&Cancel`), `1012`/`1013` (Existing vs. New file size & modified date labels) | Shown when single pre-check (`0x0F` $\rightarrow$ `0xA1 status=1`) or batch pre-check (`0x13`/`0x16` $\rightarrow$ `0xA8`/`0xAA`) detects an existing destination file. Displays `"This folder already contains a file named '%s'"` (`0xF052`) and compares sizes/dates (`0xF053`). |
| **`134`** | `Input Name` / `Input Network Password` | `1000` (Edit box), `1` (`OK`), `2` (`Cancel`), `1027` (Prompt label) | Reused for creating a new folder (`32777`) and entering an optional password. |
| **`155`** | `EasySync Setup` | `1028` (`Enable Saving Function` checkbox), `1029` (Backing file path edit), `1030` (`Browse` button), `1` (`OK`), `2` (`Cancel`) | Configures the local backing file (`[this+0x654]`) used to service Virtual Disk Sector Read/Write requests (`action_code 2` and `3` via `0xDA 0x2A` / `0xDA 0x28`). |
| **`174`** | `About EasySync` | `1037` (`EasySync Version`), `1038` (`Driver Version`), `1` (`OK`) | Displays application version (`4.1.272.5` / `1.1.67.24`) and copyright notice. |
| **`188`** | `Network Option` | `1044` (Radio: `"Enable Internet Connection Sharing (Router)"`), `1045` (Radio: `"Standard Peer-to-Peer Virtual Network"`), `1` (`OK`), `2` (`Cancel`) | Prompts the user before sending `PleaseSwitchAsVirtualNIC` (`0x39`). Selecting Router mode sends `MeSwitchAsRouter` and launches `nicinst.exe IS`; selecting non-router sends `MeSwitchAsNoneRouter` and launches `nicinst.exe I`. |

---

## 4. End-to-End File Synchronization & Transfer Workflows

### 4.1 Startup & Single-Instance Handshake
1. Check named single-instance lock (`"Thunder Bird"` mutex on Windows). If another instance is running, bring its main window to the foreground and exit.
2. Start the hardware discovery and poll worker threads (`0x0041A480`).
3. Populate the **Local Host** pane immediately using native OS filesystem APIs (`GetLogicalDriveStrings`, `FindFirstFile`, `GetDiskFreeSpaceEx`).
4. While the remote peer is disconnected (`op_type=5` returns `0x0000`), display `"Disconnected"` (`0xF027`) in the status bar and disable remote file operations.
5. As soon as `op_type=5` returns `0x007F`, both hosts transition to Transfer Line Mode, flush their RX and TX hardware pipes (`cmd=8` / `cmd=7` at `0x0041D014`), update status bar to `"Connected"` (`0xF026`), and automatically issue Client Command `0x01` (**Query Peer Root / Drives**) using the 11-byte payload `[0x01][flags:u8][code_page:u16_le][name_len:u8][computer_name:bytes]` to exchange host identity and populate the **Remote Host** pane via the peer's `0xA4` reply. Initiating hosts must allow the link to settle to avoid pipe contention and buffer flushes.
6. When the user navigates directories or to synchronize active folder state with the peer, the host issues opcode `0x02` (**Notify / Set Active Remote Directory**) with `[0x02][path_len:u8][dir_path:bytes]`, to which the peer acknowledges with Server Reply `0xA1` (24-byte structured ACK, `status = 0`) and posts UI message `0xFBD`.

### 4.2 Single File Upload (`Local -> Remote`)
1. **Free Space Check:** Query cached volume free space (or send `0x0E` **Acknowledge File Transfer Completion** / cached drive space). If `file_size > remote_free_bytes`, abort and display `"Disk is full"` (`0xF038`).
2. **Overwrite Pre-Check:** Send `0x0F` (`[0x0F][dest_len][dest_path]`).
   + Wait for `0xA1`:
     - If `status == 0` (Does not exist): proceed.
     - If `status == 1` (Already exists): show Dialog `133` (`Confirm Replace File`). If user selects `No` or `Cancel`, abort.
3. **Upload Initiation & Streaming:**
   + Send `0x0A` (`[0x0A][req_id:u32_le][dest_len:u16_le][file_size:u32_le][WIN32_FIND_DATAA:320B][dest_path:wchar_t]`).
   + Stream file contents across subsequent 64 KB blocks using `0x0C` (`[0x0C][item_id:u32_le][chunk_index:u32_le][total_chunks:u32_le][chunk_len:u32_le][chunk_data]`).
   + Send `0x0E` (`[0x0E]`) to commit timestamps and file attributes via `0x37`.
   + Update Dialog `131` (`Progress Information`) after each 64 KB bulk block.
   + Wait for `0xA1` (`status == 0` on success, or `status == 1` failure status code with `win32_error`).
4. **Refresh:** Re-issue `0x04` (**Change / List Remote Directory**) on the target remote folder to update the UI.

### 4.3 Batch Upload (`Local -> Remote`, Multiple Files & Subdirectories)
1. Recursively enumerate all selected local files and directories to compute total byte count and relative paths.
2. **Batch Overwrite Pre-Check:** Send `0x16` (**Extended Batch Pre-Check**) or `0x13` (**Batch Pre-Check**) containing the target base directory and all relative paths.
3. Receive `0xAA` (or `0xA8`) listing only the conflicting files that already exist on the remote peer.
4. Iterate through conflicting items with Dialog `133` (`Yes`, `Yes to All`, `No`, `Cancel`) to filter the upload list.
5. Send file and directory records, create remote directories via `0x07` (`CreateDirectory`), and stream file chunks via `0x0C`.
6. Wait for `0xA1` (`status == 0`) or `0xA9` (Batch Error). If an unfamiliar Win32 error code is returned, optionally send `0x38` (**Format Win32 Error Codes**) and display the peer's `0xAC` formatted error message.

### 4.4 Single & Batch Download (`Remote -> Local`)
1. For a single file, check local overwrite (`Confirm Replace File` Dialog `133`), then send `0x0B` (**Single File Download Request**) and stream the incoming `0xA3` payload to the local file, applying the timestamps and attributes via `0x37` upon completion.
2. For a batch selection (multiple files or folders), send `0x10` (**Batch File Download Request**) or `0x35`/`0x36`. The remote peer expands subdirectories recursively and sends:
   + `0xAB` if any files could not be opened/statted,
   + `0xA6` for all zero-length files and empty directories (created immediately on the local disk),
   + `0xA3` streaming non-empty file data chunks (written sequentially to disk with progress updates), finalized by `0xA7` (Batch Download Complete ACK).

---

## 5. Localization & Multi-Language Support (11 Locales)

`EasySuite.exe` embeds complete localized menus, dialogs, and string tables for **11 locales**:

| Windows LCID | Language / Region | Code Page |
| :---: | :--- | :--- |
| `0x0404` | Chinese (Traditional — Taiwan) | CP950 (Big5) |
| `0x0804` | Chinese (Simplified — PRC) | CP936 (GBK) |
| `0x0407` | German (Germany) | CP1252 |
| `0x0409` | English (United States) | CP1252 |
| `0x040A` | Spanish (Traditional Sort) | CP1252 |
| `0x080A` | Spanish (Mexico) | CP1252 |
| `0x0C0A` | Spanish (Modern Sort) | CP1252 |
| `0x040C` | French (France) | CP1252 |
| `0x0410` | Italian (Italy) | CP1252 |
| `0x0411` | Japanese (Japan) | CP932 (Shift-JIS) |
| `0x0412` | Korean (Korea) | CP949 (EUC-KR) |

> [!TIP]
> **Cross-Platform Path Encoding Note:** Legacy `EasySuite.exe` transmits file paths in the active Windows ANSI/MBCS code page (`CP_ACP`) inside `WIN32_FIND_DATAA` and command payloads. A clean-room implementation should default to UTF-8 when talking to modern clean-room peers while supporting configurable MBCS encoding (e.g., CP950, CP936, CP932, CP1252) when interoperating with legacy Windows `EasySuite.exe`.
