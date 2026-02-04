import os
import io
import asyncio
from pathlib import Path
from typing import Optional, Dict, Any, Literal

from fastapi import HTTPException
from concurrent.futures import ThreadPoolExecutor
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload
from googleapiclient.errors import HttpError

from config.config import get_learnhouse_config


# If modifying these scopes, delete the file token.json
SCOPES = ["https://www.googleapis.com/auth/drive.file"]

# Chunk size for resumable uploads
CHUNK_SIZE = 10 * 1024 * 1024   # 10 MB
executor = ThreadPoolExecutor(max_workers=2)


class GoogleDriveUploader:
    """Handle Google Drive file uploads with authentication and progress tracking."""
    
    def __init__(self):
        """Initialize the uploader with credential files.
        
        Args:
            credentials_file: Path to OAuth2 credentials JSON file
            token_file: Path to store/load authentication token
        """
        learnhouse_config = get_learnhouse_config()

        self.credentials_file = learnhouse_config.video_storage_config.ggdrive.credentials_file
        self.token_file = learnhouse_config.video_storage_config.ggdrive.token_file
        self.base_folder = learnhouse_config.video_storage_config.ggdrive.base_folder
        self.service = None
    
    def authenticate(self) -> None:
        """Authenticate with Google Drive API and build service."""
        creds = None
        
        # Load existing token if available
        if os.path.exists(self.token_file):
            creds = Credentials.from_authorized_user_file(self.token_file, SCOPES)
        
        # Refresh or create new credentials
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                print("Refreshing expired token...")
                creds.refresh(Request())
            else:
                if not os.path.exists(self.credentials_file):
                    raise FileNotFoundError(
                        f"Credentials file not found: {self.credentials_file}\n"
                        "Please download it from Google Cloud Console."
                    )
                
                print("Opening browser for authentication...")
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_file, SCOPES
                )
                creds = flow.run_local_server(port=0)
            
            # Save credentials for future use
            with open(self.token_file, "w") as token:
                token.write(creds.to_json())
            print(f"Token saved to {self.token_file}")
        
        self.service = build("drive", "v3", credentials=creds)
        print("✓ Authentication successful")
    
    def find_folder(self, folder_name: str, parent_id: str = "root") -> Optional[str]:
        """Find folder by name in Google Drive.
        
        Args:
            folder_name: Name of folder to find
            parent_id: Parent folder ID (default: root)
        
        Returns:
            Folder ID if found, None otherwise
        """
        query = (
            f"name='{folder_name}' and "
            f"mimeType='application/vnd.google-apps.folder' and "
            f"'{parent_id}' in parents and "
            f"trashed=false"
        )
        
        try:
            results = self.service.files().list(
                q=query,
                spaces="drive",
                fields="files(id, name)"
            ).execute()
            
            items = results.get("files", [])
            if items:
                print(f"✓ Found folder '{folder_name}' (ID: {items[0]['id']})")
                return items[0]["id"]
            
            print(f"Folder '{folder_name}' not found")
            return None
            
        except HttpError as error:
            print(f"Error searching for folder: {error}")
            raise
    
    def create_folder(self, folder_name: str, parent_id: str = "root") -> str:
        """Create a new folder in Google Drive.
        
        Args:
            folder_name: Name of folder to create
            parent_id: Parent folder ID (default: root)
        
        Returns:
            Created folder ID
        """
        file_metadata = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id]
        }
        
        try:
            folder = self.service.files().create(
                body=file_metadata,
                fields="id, name"
            ).execute()
            
            folder_id = folder.get("id")
            print(f"✓ Created folder '{folder_name}' (ID: {folder_id})")
            return folder_id
            
        except HttpError as error:
            print(f"Error creating folder: {error}")
            raise
    
    def get_or_create_folder(self, folder_name: str, parent_id: str = "root") -> str:
        """Get existing folder or create if it doesn't exist.
        
        Args:
            folder_name: Name of folder
            parent_id: Parent folder ID (default: root)
        
        Returns:
            Folder ID
        """
        folder_id = self.find_folder(folder_name, parent_id)
        
        if folder_id is None:
            print(f"Creating folder '{folder_name}'...")
            folder_id = self.create_folder(folder_name, parent_id)
        
        return folder_id
    
    def make_file_public(self, file_id: str) -> Dict[str, str]:
        """Make file publicly accessible to anyone with the link.
        
        Args:
            file_id: ID of the file to make public
        
        Returns:
            Dictionary with permission details
        
        Raises:
            HttpError: If permission creation fails
        """
        try:
            permission = {
                'type': 'anyone',
                'role': 'reader',
                'allowFileDiscovery': False
            }
            
            result = self.service.permissions().create(
                fileId=file_id,
                body=permission,
                fields='id'
            ).execute()
            
            print("✓ File is now publicly accessible")
            return result
            
        except HttpError as error:
            print(f"Error setting public permission: {error}")
            raise
    
    def get_shareable_link(self, file_id: str) -> str:
        """Get the shareable public link for a file.
        
        Args:
            file_id: ID of the file
        
        Returns:
            Shareable link URL
        """
        return f"https://drive.google.com/file/d/{file_id}/preview"
    
    def upload_file(
        self, 
        file_path: str, 
        folder_id: Optional[str] = None,
        mime_type: Optional[str] = None,
        make_public: bool = False
    ) -> Dict[str, Any]:
        """Upload file to Google Drive with resumable upload and progress tracking.
        
        Args:
            file_path: Path to file to upload
            folder_id: Optional parent folder ID
            mime_type: Optional MIME type (auto-detected if None)
            make_public: Whether to make file publicly accessible (default: False)
        
        Returns:
            Dictionary with file information including shareable link if public
        
        Raises:
            FileNotFoundError: If file doesn't exist
            HttpError: If upload fails
        """
        # Validate file exists
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")
        
        file_path = Path(file_path)
        file_size_mb = file_path.stat().st_size / (1024 * 1024)
        
        print(f"\n{'='*60}")
        print(f"Uploading: {file_path.name}")
        print(f"Size: {file_size_mb:.2f} MB")
        print(f"{'='*60}")
        
        # Prepare metadata
        file_metadata = {"name": file_path.name}
        if folder_id:
            file_metadata["parents"] = [folder_id]
        
        # Create media upload with resumable support
        media = MediaFileUpload(
            str(file_path),
            mimetype=mime_type,
            resumable=True,
            chunksize=CHUNK_SIZE
        )
        
        try:
            # Create upload request (NOT execute it yet)
            request = self.service.files().create(
                body=file_metadata,
                media_body=media,
                fields="id, name, size, webViewLink, mimeType"
            )
            
            # Execute with progress tracking
            response = None
            last_progress = -1
            
            while response is None:
                status, response = request.next_chunk()
                
                if status:
                    progress = int(status.progress() * 100)
                    
                    # Only print when progress changes significantly
                    if progress != last_progress and progress % 5 == 0:
                        print(f"Progress: {progress}% [{self._progress_bar(progress)}]")
                        last_progress = progress
            
            print(f"Progress: 100% [{self._progress_bar(100)}]")
            
            file_id = response.get('id')
            
            # Make file public if requested
            shareable_link = None
            if make_public:
                print("\nSetting public permissions...")
                self.make_file_public(file_id)
                shareable_link = self.get_shareable_link(file_id)
            
            # Display results
            print(f"\n{'='*60}")
            print(f"✓ Upload complete!")
            print(f"File ID: {file_id}")
            print(f"File name: {response.get('name')}")
            print(f"File size: {int(response.get('size', 0)) / (1024*1024):.2f} MB")
            print(f"View link: {response.get('webViewLink', 'N/A')}")
            
            if shareable_link:
                print(f"\n🔗 Public Share Link:")
                print(f"   {shareable_link}")
                print(f"   (Anyone with this link can view the file)")
            
            print(f"{'='*60}\n")
            
            # Add shareable link to response
            response['shareableLink'] = shareable_link
            
            return response
            
        except HttpError as error:
            print(f"\n✗ Upload failed: {error}")
            raise
    
    def upload_stream(
        self,
        file_obj: object,
        file_name: str,
        folder_id: Optional[str] = None,
        mime_type: Optional[str] = None,
        make_public: bool = False,
        file_size: Optional[int] = None
    ) -> Dict[str, Any]:
        """Upload file from stream/file object to Google Drive with resumable upload.
        
        Args:
            file_obj: File-like object to upload (e.g., SpooledTemporaryFile, BytesIO)
            file_name: Name for the uploaded file
            folder_id: Optional parent folder ID
            mime_type: Optional MIME type (auto-detected if None)
            make_public: Whether to make file publicly accessible (default: False)
            file_size: Optional file size in bytes for progress tracking
        
        Returns:
            Dictionary with file information including shareable link if public
        
        Raises:
            HttpError: If upload fails
        """
        # Get file size if not provided
        if file_size is None:
            # Try to get size from file object
            try:
                current_pos = file_obj.tell()
                file_obj.seek(0, 2)  # Seek to end
                file_size = file_obj.tell()
                file_obj.seek(current_pos)  # Restore position
            except (AttributeError, OSError):
                file_size = 0
        
        file_size_mb = file_size / (1024 * 1024) if file_size > 0 else 0
        
        print(f"\n{'='*60}")
        print(f"Uploading (stream): {file_name}")
        if file_size > 0:
            print(f"Size: {file_size_mb:.2f} MB")
        print(f"{'='*60}")
        
        # Prepare metadata
        file_metadata = {"name": file_name}
        if folder_id:
            file_metadata["parents"] = [folder_id]
        
        # Wrap file object in BytesIO if needed to ensure seekability
        if not isinstance(file_obj, io.BytesIO):
            # Read entire content into BytesIO for seekability
            file_obj.seek(0)
            content = file_obj.read()
            file_obj = io.BytesIO(content)
        
        # Create media upload from stream with resumable support
        media = MediaIoBaseUpload(
            file_obj,
            mimetype=mime_type or 'application/octet-stream',
            resumable=True,
            chunksize=CHUNK_SIZE
        )
        
        try:
            # Create upload request
            request = self.service.files().create(
                body=file_metadata,
                media_body=media,
                fields="id, name, size, webViewLink, mimeType"
            )
            
            # Execute with progress tracking
            response = None
            last_progress = -1
            
            while response is None:
                status, response = request.next_chunk()
                
                if status:
                    progress = int(status.progress() * 100)
                    
                    # Only print when progress changes significantly
                    if progress != last_progress and progress % 5 == 0:
                        print(f"Progress: {progress}% [{self._progress_bar(progress)}]")
                        last_progress = progress
            
            print(f"Progress: 100% [{self._progress_bar(100)}]")
            
            file_id = response.get('id')
            
            # Make file public if requested
            shareable_link = None
            if make_public:
                print("\nSetting public permissions...")
                self.make_file_public(file_id)
                shareable_link = self.get_shareable_link(file_id)
            
            # Display results
            print(f"\n{'='*60}")
            print(f"✓ Upload complete!")
            print(f"File ID: {file_id}")
            print(f"File name: {response.get('name')}")
            print(f"File size: {int(response.get('size', 0)) / (1024*1024):.2f} MB")
            print(f"View link: {response.get('webViewLink', 'N/A')}")
            
            if shareable_link:
                print(f"\n🔗 Public Share Link:")
                print(f"   {shareable_link}")
                print(f"   (Anyone with this link can view the file)")
            
            print(f"{'='*60}\n")
            
            # Add shareable link to response
            response['shareableLink'] = shareable_link
            
            return response
            
        except HttpError as error:
            print(f"\n✗ Upload failed: {error}")
            raise
    
    @staticmethod
    def _progress_bar(progress: int, length: int = 40) -> str:
        """Generate a text progress bar.
        
        Args:
            progress: Progress percentage (0-100)
            length: Length of progress bar
        
        Returns:
            Progress bar string
        """
        filled = int(length * progress / 100)
        bar = "█" * filled + "░" * (length - filled)
        return bar


# Singleton instance
_uploader = None

def get_google_drive_uploader() -> GoogleDriveUploader:
    """Get or create GoogleDriveUploader instance"""
    global _uploader
    if _uploader is None:
        _uploader = GoogleDriveUploader()
    return _uploader


async def stream_upload_to_google_drive(
    course_uuid: str,
    activity_uuid: str,
    type_of_dir: Literal["orgs", "users"],
    uuid: str,  # org_uuid or user_uuid
    file_obj: object,
    file_and_format: str,
) -> str:
    """
    Upload video to Google Drive directly from stream
    
    Returns:
        Google Drive file preview link
    """
    def _upload():
        try:
            uploader = get_google_drive_uploader()

            # 1. Authenticate
            uploader.authenticate()
            
            # 2. Get or create folder structure
            folder_id = uploader.get_or_create_folder(uploader.base_folder)
            folder_id = uploader.get_or_create_folder("content", parent_id=folder_id)
            folder_id = uploader.get_or_create_folder(type_of_dir, parent_id=folder_id)
            folder_id = uploader.get_or_create_folder(uuid, parent_id=folder_id)
            folder_id = uploader.get_or_create_folder("courses", parent_id=folder_id)
            folder_id = uploader.get_or_create_folder(course_uuid, parent_id=folder_id)
            folder_id = uploader.get_or_create_folder("activities", parent_id=folder_id)
            folder_id = uploader.get_or_create_folder(activity_uuid, parent_id=folder_id)
            folder_id = uploader.get_or_create_folder("video", parent_id=folder_id)
            
            # 3. Get file size for progress tracking (if possible)
            file_size = None
            try:
                current_pos = file_obj.tell()
                file_obj.seek(0, 2)  # Seek to end
                file_size = file_obj.tell()
                file_obj.seek(current_pos)  # Restore position
            except (AttributeError, OSError):
                pass
            
            # 4. Upload file directly from stream (no temporary file)
            result = uploader.upload_stream(
                file_obj=file_obj,
                file_name=file_and_format,
                folder_id=folder_id,
                make_public=True,
                file_size=file_size
            )

            # 5. Return shareable link        
            shareable_link = result.get("shareableLink")
            return {
                "success": True,
                "shareable_link": shareable_link,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(executor, _upload)
    
    if not result["success"]:
        raise HTTPException(
            status_code=500,
            detail=f"Google Drive upload failed: {result['error']}"
        )
    
    print("Google Drive upload successful")
    return result["shareable_link"]