"""
In-App GitHub Auto-Updater Module for PyQt5 Applications
Handles version checking, chunked file downloading with progress tracking,
and automated executable replacement/restart via a self-deleting batch script.
"""

import os
import sys
import json
import time
import subprocess
import tempfile
import urllib.request
import urllib.error
from typing import Tuple, Dict, Any, Optional

from PyQt5.QtCore import QThread, pyqtSignal, QObject


def parse_version(v_str: str) -> Tuple[int, ...]:
    """
    Parse a semantic version string (e.g. '1.0.2', 'v1.0.2-beta') into a comparable tuple of integers.
    Strip leading 'v' or trailing release labels if present.
    """
    clean_v = str(v_str).strip().lstrip('vV')
    # Remove metadata like -beta or +build
    clean_v = clean_v.split('-')[0].split('+')[0]
    parts = []
    for part in clean_v.split('.'):
        try:
            parts.append(int(part))
        except ValueError:
            # Fallback if non-numeric component exists
            parts.append(0)
    return tuple(parts)


def is_version_newer(current_version: str, remote_version: str) -> bool:
    """
    Compare current version with remote version string.
    Returns True if remote_version is strictly newer than current_version.
    """
    return parse_version(remote_version) > parse_version(current_version)


class DownloadThread(QThread):
    """
    Asynchronous file downloader thread with chunked downloading,
    download speed, percentage calculation, and progress emission.
    """
    # Signals:
    # progress: (bytes_downloaded, total_bytes, percentage)
    progress = pyqtSignal(int, int, float)
    # finished: (saved_file_path)
    finished = pyqtSignal(str)
    # error: (error_message)
    error = pyqtSignal(str)

    def __init__(self, download_url: str, target_path: Optional[str] = None, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.download_url = download_url
        self.target_path = target_path
        self._is_cancelled = False

    def cancel(self):
        """Cancel the download operation."""
        self._is_cancelled = True

    def run(self):
        """Execute the chunked file download."""
        try:
            # Prepare destination path if not provided
            if not self.target_path:
                filename = os.path.basename(self.download_url.split('?')[0])
                if not filename or not filename.endswith('.exe'):
                    filename = 'update_new.exe'
                temp_dir = tempfile.gettempdir()
                self.target_path = os.path.join(temp_dir, filename)

            # Request header to mimic user-agent
            req = urllib.request.Request(
                self.download_url,
                headers={'User-Agent': 'PyQt5-GitHub-AutoUpdater/1.0'}
            )

            with urllib.request.urlopen(req, timeout=30) as response:
                content_length = response.getheader('Content-Length')
                total_size = int(content_length) if content_length else 0
                
                downloaded = 0
                chunk_size = 16384  # 16 KB chunks

                with open(self.target_path, 'wb') as out_file:
                    while True:
                        if self._is_cancelled:
                            out_file.close()
                            if os.path.exists(self.target_path):
                                os.remove(self.target_path)
                            self.error.emit("Download cancelled by user.")
                            return

                        chunk = response.read(chunk_size)
                        if not chunk:
                            break

                        out_file.write(chunk)
                        downloaded += len(chunk)

                        percentage = (downloaded / total_size * 100.0) if total_size > 0 else 0.0
                        self.progress.emit(downloaded, total_size, percentage)

            self.finished.emit(self.target_path)

        except urllib.error.URLError as e:
            self.error.emit(f"Network error during download: {e.reason}")
        except urllib.error.HTTPError as e:
            self.error.emit(f"HTTP Error {e.code}: {e.reason}")
        except Exception as e:
            self.error.emit(f"Unexpected error: {str(e)}")


class VersionCheckThread(QThread):
    """
    Asynchronous version checking thread that fetches version metadata from GitHub.
    """
    # Signals:
    # checked: (has_update: bool, version_info: dict)
    checked = pyqtSignal(bool, dict)
    # error: (error_message)
    error = pyqtSignal(str)

    def __init__(self, current_version: str, version_json_url: str, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.current_version = current_version
        self.version_json_url = version_json_url

    def run(self):
        try:
            req = urllib.request.Request(
                self.version_json_url,
                headers={
                    'User-Agent': 'PyQt5-GitHub-AutoUpdater/1.0',
                    'Cache-Control': 'no-cache'
                }
            )
            with urllib.request.urlopen(req, timeout=15) as response:
                data = response.read().decode('utf-8')
                version_info = json.loads(data)

            remote_ver = version_info.get("version", "0.0.0")
            has_update = is_version_newer(self.current_version, remote_ver)
            self.checked.emit(has_update, version_info)

        except urllib.error.URLError as e:
            self.error.emit(f"Could not connect to update server: {e.reason}")
        except json.JSONDecodeError:
            self.error.emit("Failed to parse version metadata JSON.")
        except Exception as e:
            self.error.emit(f"Failed to check for updates: {str(e)}")


def create_update_batch_script(downloaded_exe: str, current_exe: str, current_pid: int) -> str:
    """
    Generate a Windows batch script to:
    1. Wait for the application to close (2 seconds)
    2. Forcefully kill process if still running
    3. Replace current_exe with downloaded_exe
    4. Restart the updated executable
    5. Delete the batch script itself
    """
    bat_content = f"""@echo off
title Updating Application...
echo Waiting for application to exit (PID: {current_pid})...
timeout /t 2 /nobreak > NUL

:: Force process termination if still running
taskkill /f /pid {current_pid} > NUL 2>&1

echo Replacing old application binary...
copy /y "{downloaded_exe}" "{current_exe}"
if errorlevel 1 (
    echo Failed to replace binary directly. Retrying in 2 seconds...
    timeout /t 2 /nobreak > NUL
    copy /y "{downloaded_exe}" "{current_exe}"
)

echo Cleaning up downloaded file...
del /f /q "{downloaded_exe}" > NUL 2>&1

echo Starting updated application...
start "" "{current_exe}"

:: Self-destruct batch script
(goto) 2>nul & del "%~f0"
"""
    temp_dir = tempfile.gettempdir()
    script_path = os.path.join(temp_dir, f"update_and_restart_{int(time.time())}.bat")
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(bat_content)

    return script_path


def launch_update_script(batch_script_path: str):
    """
    Execute the Windows batch script as an independent detached process.
    """
    if sys.platform == "win32":
        # Launch batch script in new process group without blocking
        subprocess.Popen(
            ["cmd.exe", "/c", batch_script_path],
            creationflags=subprocess.CREATE_NEW_CONSOLE
        )
    else:
        # Fallback for POSIX system shell script
        subprocess.Popen(["sh", batch_script_path])
