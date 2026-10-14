"""Validation for narrative-style reports."""
from pathlib import Path
from pypdf import PdfReader
from .models import DataQualityError


def validate_narrative_pdf(path, ticker, events):
    """Validate narrative-style PDF structure."""
    try:
        reader = PdfReader(path)
        
        # Narrative reports can be 2-4 pages
        if len(reader.pages) < 2 or len(reader.pages) > 4:
            raise DataQualityError(f'PDF must have 2-4 pages; rendered {len(reader.pages)}')
        
        # Extract all text
        full_text = ' '.join(page.extract_text() or '' for page in reader.pages)
        
        # Check for ticker
        if ticker not in full_text:
            raise DataQualityError('PDF is missing ticker symbol')
        
        # Check for key narrative sections (at least "Sources" and "Valuation")
        if 'Sources' not in full_text and 'Event Index' not in full_text:
            raise DataQualityError('PDF is missing Sources section')
        
        if 'Valuation' not in full_text:
            raise DataQualityError('PDF is missing Valuation section')
        
        # Check for source links (extract URLs from all annotations)
        urls = set()
        for page in reader.pages:
            for annot in page.get('/Annots', []):
                try:
                    obj = annot.get_object()
                    uri = obj.get('/A', {}).get('/URI', '')
                    if uri:
                        urls.add(str(uri))
                except Exception:
                    pass
        
        # Check that required sources are present
        required = {s['url'] for e in events for s in e.get('sources', [])[:2]}
        if not required <= urls:
            missing = required - urls
            raise DataQualityError(f'PDF missing {len(missing)} source links')
    
    except DataQualityError:
        raise
    except Exception as exc:
        raise DataQualityError('Unreadable PDF: ' + type(exc).__name__) from None


def validate_narrative_artifacts(artifacts, ticker, events):
    """Validate narrative report artifacts."""
    if not artifacts:
        return False
    
    try:
        import hashlib
        
        def digest(path):
            return hashlib.sha256(Path(path).read_bytes()).hexdigest()
        
        # Check all artifacts have matching hashes
        for a in artifacts.values():
            if digest(a['path']) != a['sha256']:
                return False
        
        # Validate PDF structure
        validate_narrative_pdf(artifacts['pdf']['path'], ticker, events)
        return True
    
    except (OSError, ValueError, DataQualityError, KeyError):
        return False
