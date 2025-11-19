from urllib.parse import urlparse, unquote
from typing import TypedDict, Optional, Dict, Any
import asyncio
import io
import magic
from PIL import Image
from .request_cli import make_request

# Type definition
class IconData(TypedDict):
    data: bytes
    size: int
    mimetype: str
    name: str

# MIME types mapping
MIME_TO_EXT: Dict[str, str] = {
    'image/jpeg': '.jpg',
    'image/png': '.png',
    'image/gif': '.gif',
    'image/webp': '.webp',
    'image/svg+xml': '.svg',
    'image/x-icon': '.ico',
    'image/vnd.microsoft.icon': '.ico',
}

async def download_icon(url: str, filename: Optional[str] = None) -> Optional[IconData]:
    """
    Download and validate an image from a URL.
    
    Args:
        url: Target image URL
        filename: Optional custom filename
        
    Returns:
        IconData object or None if download/validation fails
    """
    if not url:
        return None
        
    # Process filename
    if not filename:
        filename = _extract_filename(url)
        
    if any(term in filename.lower() for term in ['unknown', 'missing']):
        return None
        
    # Fetch image data
    response = await make_request(url, max_attempt=3, add_user_agent=True)
    if not response:
        return None
        
    file_buffer = response.content
    mime_type = _detect_mime_type(file_buffer)
    
    if not mime_type:
        return None
        
    if not verify_image(file_buffer, mime_type):
        return None
        
    # Generate filename with correct extension
    base_name = filename.rsplit('.', 1)[0]
    extension = MIME_TO_EXT.get(mime_type, '')
    final_filename = f"{base_name}{extension}"
    
    return {
        "data": file_buffer,
        "size": len(file_buffer),
        "mimetype": mime_type,
        "name": final_filename,
    }

def _extract_filename(url: str) -> str:
    """Extract filename from URL path."""
    parsed_url = urlparse(url)
    path_components = parsed_url.path.split('/')
    filename = next((comp for comp in reversed(path_components) if comp))
    return unquote(filename)

def _detect_mime_type(data: bytes) -> Optional[str]:
    """Detect MIME type from file content."""
    mime = magic.Magic(mime=True)
    return mime.from_buffer(data)

def verify_image(data: bytes, mime_type: str) -> bool:
    """Verify image data integrity."""
    if not mime_type.startswith('image/'):
        return False
        
    try:
        # SVG validation (basic XML check)
        if mime_type == 'image/svg+xml':
            return b'<svg' in data and b'</svg>' in data
            
        # Standard image validation through PIL
        with Image.open(io.BytesIO(data)) as img:
            img.verify()
        return True
    except Exception:
        return False
        
        
        
if __name__ == "__main__":
    url = "https://pbs.twimg.com/profile_images/1899026397915488256/mc-jPC-w.jpg"
    icon_data = asyncio.run(download_icon(url))
    if icon_data:
        print(f"Downloaded: {icon_data['name']}")
        print(f"MIME type: {icon_data['mimetype']}")
        print(f"Size: {icon_data['size']} bytes")
        
        # Display image if it's a standard format (not SVG)
        if icon_data['mimetype'] != 'image/svg+xml':
            try:
                img = Image.open(io.BytesIO(icon_data['data']))
                print(f"Image dimensions: {img.size}")
                img.show()
                print("Image displayed!")
            except Exception as e:
                print(f"Could not display image: {e}")
        else:
            print("SVG image downloaded (cannot display with PIL)")